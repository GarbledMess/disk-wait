"""Health checks: flag values that are bad now, and ones heading somewhere bad.

Pure Python, no Home Assistant imports. Every check is a handful of float
operations per poll; trend fits run once per slow tick (every 5 minutes).

*Now* checks compare the median of the last 5 minutes of readings against
warning and problem thresholds, so a value has to be bad for most of that
window to raise anything: a single spike, such as one long stall while
Home Assistant starts, can't do it on its own. They clear with hysteresis, at 90 % of the
threshold, so a value hovering at the line doesn't flap.

*Trend* checks fit a straight line through the recent history of slow-moving
values (disk fill, memory, swap, Home Assistant's own memory, recorder DB
size) and estimate when they will cross a limit.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
import math

WARNING = "warning"
PROBLEM = "problem"
OK = "ok"
_RANK = {OK: 0, WARNING: 1, PROBLEM: 2}

SMOOTHING_SECONDS = 300.0
CLEAR_RATIO = 0.9

# Trend settings
HISTORY_SECONDS = 3 * 86400
MIN_SPAN_SECONDS = 6 * 3600  # need this much history before predicting
MIN_POINTS = 12
MIN_R2 = 0.6  # how well a line must fit before we believe it
DAY = 86400.0


# Checks that Host Vitals's own heavy work (a storage or recorder report) and
# Home Assistant's start-up legitimately push up; paused at those times.
LOAD_SENSITIVE = frozenset(
    {
        "loop_lag",
        "io_pressure_some",
        "memory_pressure_some",
        "cpu_pressure_some",
        "iowait",
        "cpu",
        "busy",
        "write_latency",
        "read_latency",
    }
)


@dataclass(frozen=True, slots=True)
class NowRule:
    key: str  # metric key in Metrics.system, or the per-disk field name
    name: str
    unit: str
    warning: float
    problem: float
    hint: str


@dataclass(frozen=True, slots=True)
class TrendRule:
    key: str
    name: str
    unit: str
    limit: float | None  # predict time to reach this (None: growth-rate rule)
    warn_within: float  # seconds
    problem_within: float  # seconds
    hint: str
    max_growth_per_day: float | None = None  # growth-rate rule threshold


@dataclass(frozen=True, slots=True)
class Finding:
    id: str
    level: str
    kind: str  # "now" or "trend"
    name: str
    summary: str
    hint: str
    category: str = "other"  # what kind of culprit to look for (see CATEGORY)
    cause: str = ""  # filled in by the coordinator: what is likely causing it


# Which culprits explain each check.
CATEGORY = {
    "loop_lag": "lag",
    "io_pressure_some": "io",
    "iowait": "io",
    "busy": "io",
    "write_latency": "io",
    "read_latency": "io",
    "memory_pressure_some": "memory",
    "memory_used": "memory",
    "swap_used": "memory",
    "cpu_pressure_some": "cpu",
    "cpu": "cpu",
    "cpu_temperature": "cpu",
    "ha_memory": "ha_memory",
    "db_size": "recorder",
    "disk_used": "storage",
}


SYSTEM_RULES = (
    NowRule("loop_lag", "Event loop lag", "ms", 100, 500,
            "Home Assistant's event loop is being held up, which is what makes the UI feel slow. "
            "Compare I/O pressure and Home Assistant CPU to see whether the disk or an integration is to blame."),
    NowRule("io_pressure_some", "I/O pressure", "%", 10, 25,
            "Tasks are stalling on disk I/O. Run Analyze recorder to find what is writing the most."),
    NowRule("memory_pressure_some", "Memory pressure", "%", 5, 20,
            "The system is short of RAM and reclaiming memory. Check add-on memory in Analyze storage."),
    NowRule("cpu_pressure_some", "CPU pressure", "%", 30, 60,
            "Tasks are queuing for the CPU."),
    NowRule("iowait", "CPU iowait", "%", 20, 40,
            "CPUs are idle waiting on the disk."),
    NowRule("cpu", "CPU usage", "%", 85, 95,
            "The CPU is nearly saturated."),
    NowRule("memory_used", "Memory used", "%", 85, 95,
            "Memory is nearly full. Check add-on memory in Analyze storage."),
    NowRule("swap_used", "Swap used", "%", 50, 80,
            "Memory has spilled into swap."),
    NowRule("disk_used", "Data disk used", "%", 85, 95,
            "The data disk is filling up. Run Analyze storage to see what is using it."),
    NowRule("cpu_temperature", "CPU temperature", "°C", 75, 85,
            "The CPU is hot and may throttle. Check ventilation."),
)

DISK_RULES = (
    NowRule("busy", "busy", "%", 80, 95,
            "The disk is saturated. Requests are queuing behind each other."),
    NowRule("write_latency", "write latency", "ms", 200, 1000,
            "Writes are slow. On a microSD card this is the usual cause of a sluggish UI."),
    NowRule("read_latency", "read latency", "ms", 100, 500,
            "Reads are slow."),
)

TREND_RULES = (
    TrendRule("disk_used", "Data disk", "%", 95, 14 * DAY, 3 * DAY,
              "At the current rate the data disk will be nearly full. Run Analyze storage to see what is growing."),
    TrendRule("memory_used", "Memory", "%", 95, 2 * DAY, 6 * 3600,
              "Memory use is climbing steadily, which often means a leak in an add-on or integration."),
    TrendRule("swap_used", "Swap", "%", 90, 2 * DAY, 6 * 3600,
              "Swap use is climbing steadily."),
    TrendRule("ha_memory", "Home Assistant memory", "MiB", None, 0, 0,
              "Home Assistant's own memory keeps growing, which often means a leak in an integration. "
              "A restart frees it for now.", max_growth_per_day=150),
    TrendRule("db_size", "Recorder database", "MiB", None, 0, 0,
              "The recorder database keeps growing, so purging isn't keeping up. "
              "Run Analyze recorder and exclude the noisiest entities.", max_growth_per_day=100),
)


def _fmt(v: float) -> str:
    return f"{v:.0f}" if abs(v) >= 10 else f"{v:.1f}"


def _fmt_duration(seconds: float) -> str:
    if seconds < 2 * 3600:
        return f"{seconds / 60:.0f} minutes"
    if seconds < 2 * DAY:
        return f"{seconds / 3600:.0f} hours"
    return f"{seconds / DAY:.0f} days"


def fit(points: Iterable[tuple[float, float]]) -> tuple[float, float, float, float] | None:
    """Least-squares line through (t, v). Returns (slope/s, intercept, r², span)."""
    n = sx = sy = sxx = sxy = syy = 0.0
    t0 = t_last = None
    for t, v in points:
        if t0 is None:
            t0 = t
        x = t - t0
        n += 1
        sx += x
        sy += v
        sxx += x * x
        sxy += x * v
        syy += v * v
        t_last = t
    if n < 2 or t0 is None or t_last is None:
        return None
    vx = n * sxx - sx * sx
    vy = n * syy - sy * sy
    if vx <= 0:
        return None
    slope = (n * sxy - sx * sy) / vx
    intercept = (sy - slope * sx) / n
    r2 = 1.0 if vy <= 0 else ((n * sxy - sx * sy) ** 2) / (vx * vy)
    return slope, intercept, r2, t_last - t0


class HealthMonitor:
    """Keeps smoothed values, trend history and the current findings."""

    def __init__(self, interval: float) -> None:
        self._window = max(1, math.ceil(SMOOTHING_SECONDS / interval))
        self._recent: dict[str, deque[float]] = {}
        self._active: dict[str, Finding] = {}
        self._history: dict[str, deque[tuple[float, float]]] = {
            r.key: deque() for r in TREND_RULES
        }

    # ------------------------------------------------------------ inputs

    def seed(self, key: str, points: Iterable[tuple[float, float]]) -> None:
        """Prepend older history (e.g. hourly recorder statistics)."""
        hist = self._history.get(key)
        if hist is None:
            return
        first = hist[0][0] if hist else math.inf
        older = [(t, v) for t, v in points if t < first and v is not None]
        hist.extendleft(reversed(older))

    def add_sample(self, key: str, t: float, value: float | None) -> None:
        hist = self._history.get(key)
        if hist is None or value is None:
            return
        hist.append((t, value))
        while hist and hist[0][0] < t - HISTORY_SECONDS:
            hist.popleft()

    # ------------------------------------------------------------ checks

    def _smooth(self, key: str, value: float) -> float | None:
        """Median of the last window of readings, or None until the window is full."""
        recent = self._recent.get(key)
        if recent is None:
            recent = self._recent[key] = deque(maxlen=self._window)
        recent.append(value)
        if len(recent) < self._window:
            return None
        return sorted(recent)[len(recent) // 2]

    def _now(self, fid: str, rule: NowRule, label: str, value: float | None) -> Finding | None:
        if value is None:
            # Missing this poll (e.g. no disk requests completed); keep the state.
            return self._active.get(fid)
        v = self._smooth(fid, value)
        was = self._active.get(fid)
        if v is None:
            return was
        # Hysteresis: once raised, a level only clears below 90 % of its threshold.
        hi = rule.problem * (CLEAR_RATIO if was and was.level == PROBLEM else 1.0)
        lo = rule.warning * (CLEAR_RATIO if was else 1.0)
        if v >= hi:
            level = PROBLEM
        elif v >= lo:
            level = WARNING
        else:
            return None
        threshold = rule.problem if level == PROBLEM else rule.warning
        return Finding(
            id=fid,
            level=level,
            kind="now",
            name=label,
            summary=f"{label} is {_fmt(v)} {rule.unit} (threshold {_fmt(threshold)} {rule.unit}).",
            hint=rule.hint,
            category=CATEGORY.get(rule.key, "other"),
            cause=was.cause if was else "",
        )

    def _trend(self, rule: TrendRule, now_value: float | None) -> Finding | None:
        hist = self._history[rule.key]
        if len(hist) < MIN_POINTS:
            return self._active.get(f"trend_{rule.key}")
        result = fit(hist)
        if result is None:
            return None
        slope, _, r2, span = result
        if span < MIN_SPAN_SECONDS or r2 < MIN_R2 or slope <= 0:
            return None
        fid = f"trend_{rule.key}"
        prev = self._active.get(fid)
        current = now_value if now_value is not None else hist[-1][1]
        if rule.limit is None:
            per_day = slope * DAY
            limit = rule.max_growth_per_day or math.inf
            was = fid in self._active
            if per_day < limit * (CLEAR_RATIO if was else 1.0):
                return None
            return Finding(
                id=fid,
                level=WARNING,
                kind="trend",
                name=rule.name,
                summary=(
                    f"{rule.name} is growing about {_fmt(per_day)} {rule.unit} a day "
                    f"over the last {_fmt_duration(span)} (now {_fmt(current)} {rule.unit})."
                ),
                hint=rule.hint,
                category=CATEGORY.get(rule.key, "other"),
                cause=prev.cause if prev else "",
            )
        if current >= rule.limit:
            return None  # the "now" rule covers it
        eta = (rule.limit - current) / slope
        was = self._active.get(fid)
        grace = 1.0 / CLEAR_RATIO if was else 1.0
        if eta <= rule.problem_within * grace:
            level = PROBLEM
        elif eta <= rule.warn_within * grace:
            level = WARNING
        else:
            return None
        return Finding(
            id=fid,
            level=level,
            kind="trend",
            name=rule.name,
            summary=(
                f"{rule.name} is at {_fmt(current)} {rule.unit} and rising; at this rate it "
                f"reaches {_fmt(rule.limit)} {rule.unit} in about {_fmt_duration(eta)}."
            ),
            hint=rule.hint,
            category=CATEGORY.get(rule.key, "other"),
            cause=was.cause if was else "",
        )

    def evaluate(
        self,
        system: dict[str, float | None],
        disks: dict[str, dict[str, float | None]],
        trends: bool,
    ) -> dict[str, Finding]:
        """Run the checks. ``trends`` is true on slow ticks only."""
        found: dict[str, Finding] = {}
        for rule in SYSTEM_RULES:
            if rule.key in system or rule.key in self._recent:
                if f := self._now(rule.key, rule, rule.name, system.get(rule.key)):
                    found[f.id] = f
        for disk, values in disks.items():
            for rule in DISK_RULES:
                fid = f"{disk}_{rule.key}"
                label = f"Disk {disk} {rule.name}"
                if f := self._now(fid, rule, label, values.get(rule.key)):
                    found[f.id] = f
        for rule in TREND_RULES:
            fid = f"trend_{rule.key}"
            if trends:
                f = self._trend(rule, system.get(rule.key))
            else:
                f = self._active.get(fid)
            if f:
                found[fid] = f
        self._active = found
        return found

    def set_active(self, findings: dict[str, Finding]) -> None:
        """Store findings after the caller has attached causes to them."""
        self._active = findings

    @staticmethod
    def overall(findings: dict[str, Finding]) -> str:
        return max((f.level for f in findings.values()), key=_RANK.__getitem__, default=OK)
