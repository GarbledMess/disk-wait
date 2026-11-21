"""Pure /proc readers and rate maths for Disk Wait.

No Home Assistant imports here, so this module can be unit tested on its own.
Everything that touches files is blocking and must run in an executor.

On HA OS the Home Assistant Core container shares the host kernel and the
host network namespace, so /proc/stat, /proc/diskstats, /proc/meminfo,
/proc/pressure/* and /proc/net/dev describe the whole machine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
import time

PROC_STAT = "/proc/stat"
PROC_DISKSTATS = "/proc/diskstats"
PROC_LOADAVG = "/proc/loadavg"
PROC_MEMINFO = "/proc/meminfo"
PROC_VMSTAT = "/proc/vmstat"
PROC_NET_DEV = "/proc/net/dev"
PROC_NET_ROUTE = "/proc/net/route"
PROC_PSI = {
    "io": "/proc/pressure/io",
    "memory": "/proc/pressure/memory",
    "cpu": "/proc/pressure/cpu",
}
PROC_SELF_STAT = "/proc/self/stat"
PROC_SELF_STATM = "/proc/self/statm"
PROC_MOUNTINFO = "/proc/self/mountinfo"
THERMAL_DIR = "/sys/class/thermal"

SECTOR_BYTES = 512  # /proc/diskstats always counts 512-byte sectors
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
NCPU = os.cpu_count() or 1
MIB = 1024 * 1024

_VMSTAT_KEYS = (b"pswpin", b"pswpout", b"pgmajfault")
_MEMINFO_KEYS = ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree", "Dirty", "Writeback")
_IGNORED_DISKS = ("loop", "ram", "zram", "nbd", "sr", "fd")
_IGNORED_NETS = ("lo", "veth", "ifb", "dummy")
_DATA_MOUNTS = ("/config", "/data", "/share", "/media", "/")


# ---------------------------------------------------------------- data types


@dataclass(slots=True)
class DiskCounters:
    reads: int
    sectors_read: int
    read_ms: int
    writes: int
    sectors_written: int
    write_ms: int
    in_flight: int
    io_ticks_ms: int


@dataclass(slots=True)
class Sources:
    """What to read. Decided once at setup so each poll does no discovery."""

    disks: frozenset[str] = frozenset()
    nets: frozenset[str] = frozenset()
    psi: tuple[str, ...] = ()
    thermal: str | None = None  # path to a millidegree temp file
    space_path: str | None = None  # statvfs target (the /config filesystem)
    db_paths: tuple[str, ...] = ()  # recorder SQLite file (+ -wal)


@dataclass(slots=True)
class Snapshot:
    """One reading of the counters. Gauges are stored already converted."""

    monotonic: float
    cpu_total: int | None = None
    cpu_idle: int | None = None
    cpu_iowait: int | None = None
    disks: dict[str, DiskCounters] = field(default_factory=dict)
    nets: dict[str, tuple[int, int]] = field(default_factory=dict)
    psi: dict[str, tuple[int | None, int | None]] = field(default_factory=dict)
    vm: dict[bytes, int] = field(default_factory=dict)
    self_ticks: int | None = None
    gauges: dict[str, float | None] = field(default_factory=dict)


@dataclass(slots=True)
class DiskRates:
    read_kbps: float
    write_kbps: float
    read_iops: float
    write_iops: float
    read_latency_ms: float | None
    write_latency_ms: float | None
    busy_pct: float
    in_flight: int


@dataclass(slots=True)
class NetRates:
    rx_kbps: float
    tx_kbps: float


@dataclass(slots=True)
class Metrics:
    """Everything the sensors show."""

    system: dict[str, float | None] = field(default_factory=dict)
    disks: dict[str, DiskRates] = field(default_factory=dict)
    nets: dict[str, NetRates] = field(default_factory=dict)


# ---------------------------------------------------------------- parsing


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _read_bytes(path: str) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def parse_cpu(text: str) -> tuple[int, int, int] | None:
    """(total, idle+iowait, iowait) jiffies from the aggregate cpu line."""
    line = text[: text.find("\n")] if "\n" in text else text
    if not line.startswith("cpu "):
        return None
    parts = [int(x) for x in line.split()[1:9]]
    # user nice system idle iowait irq softirq steal; guest is inside user.
    if len(parts) < 5:
        return None
    return sum(parts), parts[3] + parts[4], parts[4]


def parse_diskstats(text: str, wanted: frozenset[str] | set[str] | None = None) -> dict[str, DiskCounters]:
    out: dict[str, DiskCounters] = {}
    for line in text.splitlines():
        p = line.split()
        if len(p) < 14 or (wanted is not None and p[2] not in wanted):
            continue
        out[p[2]] = DiskCounters(
            reads=int(p[3]),
            sectors_read=int(p[5]),
            read_ms=int(p[6]),
            writes=int(p[7]),
            sectors_written=int(p[9]),
            write_ms=int(p[10]),
            in_flight=int(p[11]),
            io_ticks_ms=int(p[12]),
        )
    return out


def parse_psi(text: str) -> tuple[int | None, int | None]:
    """Cumulative stall microseconds (some, full)."""
    some = full = None
    for line in text.splitlines():
        m = re.search(r"total=(\d+)", line)
        if not m:
            continue
        if line.startswith("some"):
            some = int(m.group(1))
        elif line.startswith("full"):
            full = int(m.group(1))
    return some, full


def parse_meminfo(text: str) -> dict[str, int]:
    """Selected /proc/meminfo fields in kB."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key in _MEMINFO_KEYS:
            out[key] = int(rest.split()[0])
            if len(out) == len(_MEMINFO_KEYS):
                break
    return out


def parse_vmstat(data: bytes) -> dict[bytes, int]:
    out: dict[bytes, int] = {}
    for line in data.splitlines():
        key, _, val = line.partition(b" ")
        if key in _VMSTAT_KEYS:
            out[key] = int(val)
    return out


def parse_net_dev(text: str, wanted: frozenset[str] | set[str] | None = None) -> dict[str, tuple[int, int]]:
    """(rx_bytes, tx_bytes) per interface."""
    out: dict[str, tuple[int, int]] = {}
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        if wanted is not None and name not in wanted:
            continue
        p = rest.split()
        if len(p) >= 9:
            out[name] = (int(p[0]), int(p[8]))
    return out


def parse_self_ticks(text: str) -> int | None:
    """utime + stime of this process (Home Assistant) in clock ticks."""
    # comm (field 2) can contain spaces; split after its closing paren.
    rest = text[text.rfind(")") + 2 :].split()
    if len(rest) < 13:
        return None
    return int(rest[11]) + int(rest[12])


# ---------------------------------------------------------------- reading


def read_snapshot(src: Sources, slow: bool = False) -> Snapshot:
    """Read all sources once. Blocking: call from an executor.

    Gauges are rounded to the coarsest step that is still useful, because
    Home Assistant only writes a recorder row when the state string changes.

    ``slow`` also refreshes values that change slowly (disk space, DB size),
    which the caller asks for only every few minutes.
    """
    snap = Snapshot(monotonic=time.monotonic())
    g = snap.gauges

    if text := _read(PROC_STAT):
        if cpu := parse_cpu(text):
            snap.cpu_total, snap.cpu_idle, snap.cpu_iowait = cpu

    if src.disks and (text := _read(PROC_DISKSTATS)):
        snap.disks = parse_diskstats(text, src.disks)

    if src.nets and (text := _read(PROC_NET_DEV)):
        snap.nets = parse_net_dev(text, src.nets)

    for kind in src.psi:
        if text := _read(PROC_PSI[kind]):
            snap.psi[kind] = parse_psi(text)

    if data := _read_bytes(PROC_VMSTAT):
        snap.vm = parse_vmstat(data)

    if text := _read(PROC_LOADAVG):
        p = text.split()
        g["load_1"], g["load_5"], g["load_15"] = float(p[0]), float(p[1]), float(p[2])

    if text := _read(PROC_MEMINFO):
        mi = parse_meminfo(text)
        total = mi.get("MemTotal")
        avail = mi.get("MemAvailable")
        if total and avail is not None:
            g["memory_used"] = round(100.0 * (total - avail) / total)
            g["memory_available"] = round(avail / 1024)
        swap_total = mi.get("SwapTotal", 0)
        if swap_total:
            g["swap_used"] = round(100.0 * (swap_total - mi.get("SwapFree", 0)) / swap_total)
        else:
            g["swap_used"] = 0
        if "Dirty" in mi:
            g["dirty"] = round((mi["Dirty"] + mi.get("Writeback", 0)) / 1024)

    if text := _read(PROC_SELF_STAT):
        snap.self_ticks = parse_self_ticks(text)
    if text := _read(PROC_SELF_STATM):
        g["ha_memory"] = round(int(text.split()[1]) * PAGE_SIZE / MIB)

    if src.thermal and (text := _read(src.thermal)):
        try:
            # Half-degree steps: enough detail, far fewer state changes.
            g["cpu_temperature"] = round(int(text) / 500) / 2
        except ValueError:
            pass

    if slow:
        if src.space_path:
            try:
                st = os.statvfs(src.space_path)
                total = st.f_blocks * st.f_frsize
                free = st.f_bavail * st.f_frsize
                if total:
                    g["disk_used"] = round(100.0 * (total - free) / total, 1)
                    g["disk_free"] = round(free / 1024**3, 1)
            except OSError:
                pass
        if src.db_paths:
            size = 0
            for path in src.db_paths:
                try:
                    size += os.stat(path).st_size
                except OSError:
                    pass
            g["db_size"] = round(size / MIB)

    return snap


# ---------------------------------------------------------------- maths


def _delta(new: int, old: int) -> int | None:
    """Counter difference, or None if the counter went backwards (reset/wrap)."""
    d = new - old
    return d if d >= 0 else None


def _pct(part: float, whole: float) -> float:
    return round(min(100.0, max(0.0, 100.0 * part / whole)), 1) if whole > 0 else 0.0


def compute(old: Snapshot, new: Snapshot) -> Metrics:
    """Turn two snapshots into sensor values. Pure function."""
    m = Metrics()
    s = m.system
    s.update(new.gauges)
    elapsed = new.monotonic - old.monotonic
    if elapsed <= 0:
        return m
    elapsed_ms = elapsed * 1000.0
    elapsed_us = elapsed * 1_000_000.0

    if None not in (old.cpu_total, new.cpu_total):
        d_total = _delta(new.cpu_total, old.cpu_total)  # type: ignore[arg-type]
        d_idle = _delta(new.cpu_idle, old.cpu_idle)  # type: ignore[arg-type]
        d_wait = _delta(new.cpu_iowait, old.cpu_iowait)  # type: ignore[arg-type]
        if d_total and d_idle is not None and d_wait is not None:
            s["cpu"] = _pct(d_total - d_idle, d_total)
            s["iowait"] = _pct(d_wait, d_total)

    if old.self_ticks is not None and new.self_ticks is not None:
        d = _delta(new.self_ticks, old.self_ticks)
        if d is not None:
            # Share of the whole machine, like the CPU usage sensor.
            s["ha_cpu"] = _pct(d / CLK_TCK, elapsed * NCPU)

    for kind, (n_some, n_full) in new.psi.items():
        o_some, o_full = old.psi.get(kind, (None, None))
        if n_some is not None and o_some is not None:
            d = _delta(n_some, o_some)
            if d is not None:
                s[f"{kind}_pressure_some"] = _pct(d, elapsed_us)
        if n_full is not None and o_full is not None:
            d = _delta(n_full, o_full)
            if d is not None:
                s[f"{kind}_pressure_full"] = _pct(d, elapsed_us)

    vm_rates = {}
    for key in _VMSTAT_KEYS:
        if key in new.vm and key in old.vm:
            d = _delta(new.vm[key], old.vm[key])
            if d is not None:
                vm_rates[key] = d / elapsed
    if b"pswpin" in vm_rates and b"pswpout" in vm_rates:
        pages = vm_rates[b"pswpin"] + vm_rates[b"pswpout"]
        s["swap_activity"] = round(pages * PAGE_SIZE / 1024, 1)
    if b"pgmajfault" in vm_rates:
        s["major_faults"] = round(vm_rates[b"pgmajfault"], 1)

    for name, n in new.disks.items():
        o = old.disks.get(name)
        if o is None:
            continue
        deltas = [
            _delta(n.reads, o.reads),
            _delta(n.sectors_read, o.sectors_read),
            _delta(n.read_ms, o.read_ms),
            _delta(n.writes, o.writes),
            _delta(n.sectors_written, o.sectors_written),
            _delta(n.write_ms, o.write_ms),
            _delta(n.io_ticks_ms, o.io_ticks_ms),
        ]
        if any(d is None for d in deltas):
            continue  # counter reset (device re-added); skip one interval
        d_r, d_rs, d_rms, d_w, d_ws, d_wms, d_ticks = deltas  # type: ignore[misc]
        m.disks[name] = DiskRates(
            read_kbps=round(d_rs * SECTOR_BYTES / 1024 / elapsed, 1),
            write_kbps=round(d_ws * SECTOR_BYTES / 1024 / elapsed, 1),
            read_iops=round(d_r / elapsed, 1),
            write_iops=round(d_w / elapsed, 1),
            read_latency_ms=round(d_rms / d_r, 1) if d_r else None,
            write_latency_ms=round(d_wms / d_w, 1) if d_w else None,
            busy_pct=_pct(d_ticks, elapsed_ms),
            in_flight=n.in_flight,
        )

    for name, (n_rx, n_tx) in new.nets.items():
        if name not in old.nets:
            continue
        o_rx, o_tx = old.nets[name]
        d_rx, d_tx = _delta(n_rx, o_rx), _delta(n_tx, o_tx)
        if d_rx is None or d_tx is None:
            continue
        m.nets[name] = NetRates(
            rx_kbps=round(d_rx / 1024 / elapsed, 1),
            tx_kbps=round(d_tx / 1024 / elapsed, 1),
        )
    return m


# ---------------------------------------------------------------- discovery


def available_psi() -> tuple[str, ...]:
    return tuple(k for k, path in PROC_PSI.items() if os.path.exists(path))


def find_thermal() -> str | None:
    """Pick the CPU/SoC thermal zone, if the board exposes one."""
    try:
        zones = sorted(z for z in os.listdir(THERMAL_DIR) if z.startswith("thermal_zone"))
    except OSError:
        return None
    first = None
    for zone in zones:
        path = os.path.join(THERMAL_DIR, zone, "temp")
        if not os.path.exists(path):
            continue
        first = first or path
        kind = (_read(os.path.join(THERMAL_DIR, zone, "type")) or "").lower()
        if "cpu" in kind or "soc" in kind:
            return path
    return first


def list_disks() -> list[str]:
    """Whole block devices worth offering in the config flow."""
    text = _read(PROC_DISKSTATS) or ""
    names = [line.split()[2] for line in text.splitlines() if len(line.split()) >= 14]
    try:
        whole: set[str] | None = set(os.listdir("/sys/block"))
    except OSError:
        whole = None
    out = []
    for name in names:
        if name.startswith(_IGNORED_DISKS):
            continue
        if whole is not None and name not in whole:
            continue  # a partition; the parent disk is what matters
        if "boot" in name or "rpmb" in name:
            continue  # eMMC boot/RPMB hardware partitions
        out.append(name)
    return out


def list_nets() -> list[str]:
    text = _read(PROC_NET_DEV) or ""
    return [n for n in parse_net_dev(text) if not n.startswith(_IGNORED_NETS)]


def default_net(route: str | None = None) -> str | None:
    """Interface carrying the default route."""
    route = route if route is not None else _read(PROC_NET_ROUTE)
    for line in (route or "").splitlines()[1:]:
        p = line.split()
        if len(p) > 1 and p[1] == "00000000":
            return p[0]
    return None


def parent_disk(part: str) -> str:
    """Map a partition name to its whole-disk name (mmcblk1p8 -> mmcblk1)."""
    try:
        real = os.path.realpath(f"/sys/class/block/{part}")
        if os.path.exists(os.path.join(real, "partition")):
            return os.path.basename(os.path.dirname(real))
    except OSError:
        pass
    m = re.fullmatch(r"((?:mmcblk|nvme\d+n)\d+)p\d+", part)
    if m:
        return m.group(1)
    m = re.fullmatch(r"([a-z]+)\d+", part)
    if m and part.startswith(("sd", "vd", "hd", "xvd")):
        return m.group(1)
    return part


def find_data_disk(mountinfo: str | None = None, diskstats: str | None = None) -> str | None:
    """Best-effort guess of the disk holding HA's data (/config on HA OS)."""
    mountinfo = mountinfo if mountinfo is not None else _read(PROC_MOUNTINFO)
    diskstats = diskstats if diskstats is not None else _read(PROC_DISKSTATS)
    if not mountinfo or not diskstats:
        return None
    by_devno = {}
    for line in diskstats.splitlines():
        p = line.split()
        if len(p) >= 3:
            by_devno[f"{p[0]}:{p[1]}"] = p[2]
    mounts: dict[str, str] = {}
    for line in mountinfo.splitlines():
        p = line.split()
        if len(p) >= 5:
            mounts.setdefault(p[4], p[2])  # mount point -> major:minor
    for mp in _DATA_MOUNTS:
        devno = mounts.get(mp)
        if devno and devno in by_devno:
            return parent_disk(by_devno[devno])
    return None
