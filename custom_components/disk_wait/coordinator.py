"""Polling coordinator: one executor job per interval reads a few small /proc files."""

from __future__ import annotations

from datetime import timedelta
import asyncio
import logging
import os
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import proc
from .health import LOAD_SENSITIVE, PROBLEM, TREND_RULES, Finding, HealthMonitor
from .const import (
    CONF_DISKS,
    CONF_INTERFACES,
    CONF_REPAIRS,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOOP_PROBE_INTERVAL,
    REPORT_GRACE,
    SLOW_INTERVAL,
    STARTUP_GRACE,
)

_LOGGER = logging.getLogger(__name__)


class DiskWaitCoordinator(DataUpdateCoordinator[proc.Metrics]):
    """Keeps the previous snapshot and turns counter deltas into rates."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        opts = {**entry.data, **entry.options}
        interval = int(opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
            always_update=False,
        )
        self.disks: list[str] = list(opts.get(CONF_DISKS, []))
        self.nets: list[str] = list(opts.get(CONF_INTERFACES, []))
        self.sources = proc.Sources()
        self._slow_every = max(1, round(SLOW_INTERVAL / interval))
        self._polls = 0
        self._last: proc.Snapshot | None = None
        self._slow_gauges: dict[str, float | None] = {}
        self._probe: asyncio.TimerHandle | None = None
        self._probe_due = 0.0
        self._max_lag = 0.0
        self.health = HealthMonitor(interval)
        self.findings: dict[str, Finding] = {}
        self._repairs = bool(opts.get(CONF_REPAIRS, True))
        self._raised: dict[str, str] = {}  # issue id -> level currently in Repairs
        # Load-sensitive checks are paused until this monotonic time.
        self._quiet_until = time.monotonic() + STARTUP_GRACE
        self._reports_running = 0

    @callback
    def async_hass_started(self, _hass: HomeAssistant | None = None) -> None:
        """Start-up is heavy on a slow card; give it a grace period once it finishes."""
        self._quiet_until = max(self._quiet_until, time.monotonic() + STARTUP_GRACE)

    @callback
    def async_report_started(self) -> None:
        self._reports_running += 1

    @callback
    def async_report_finished(self) -> None:
        """Our own reports load the disk; keep checks paused a little after."""
        self._reports_running = max(0, self._reports_running - 1)
        self._quiet_until = max(self._quiet_until, time.monotonic() + REPORT_GRACE)

    @property
    def quiet(self) -> bool:
        return self._reports_running > 0 or time.monotonic() < self._quiet_until

    async def async_prime(self) -> None:
        """Work out once what to read, so polls do no discovery."""
        db = self.hass.config.path("home-assistant_v2.db")

        def _discover() -> proc.Sources:
            return proc.Sources(
                disks=frozenset(self.disks),
                nets=frozenset(self.nets),
                psi=proc.available_psi(),
                thermal=proc.find_thermal(),
                space_path=self.hass.config.config_dir,
                # Only the default SQLite recorder; external DBs are skipped.
                db_paths=(db, db + "-wal") if os.path.exists(db) else (),
            )

        self.sources = await self.hass.async_add_executor_job(_discover)
        self._schedule_probe()

    # -------------------------------------------------- event loop lag probe

    @callback
    def _schedule_probe(self) -> None:
        loop = self.hass.loop
        self._probe_due = loop.time() + LOOP_PROBE_INTERVAL
        self._probe = loop.call_at(self._probe_due, self._on_probe)

    @callback
    def _on_probe(self) -> None:
        lag = self.hass.loop.time() - self._probe_due
        if lag > self._max_lag:
            self._max_lag = lag
        self._schedule_probe()

    @callback
    def async_stop_probe(self) -> None:
        if self._probe:
            self._probe.cancel()
            self._probe = None

    # -------------------------------------------------- polling

    async def _async_update_data(self) -> proc.Metrics:
        slow = self._polls % self._slow_every == 0
        self._polls += 1
        snap = await self.hass.async_add_executor_job(
            proc.read_snapshot, self.sources, slow
        )
        if snap.cpu_total is None:
            raise UpdateFailed("Could not read /proc/stat")
        if slow:
            self._slow_gauges = {
                k: snap.gauges.get(k) for k in ("disk_used", "disk_free", "db_size")
            }
        snap.gauges.update(self._slow_gauges)

        old, self._last = self._last, snap
        if old is None:
            # First poll only sets the baseline; rate sensors fill in next poll.
            metrics = proc.Metrics(system=dict(snap.gauges))
        else:
            metrics = proc.compute(old, snap)
        metrics.system["loop_lag"] = round(self._max_lag * 1000)
        self._max_lag = 0.0
        self._check_health(metrics, slow)
        return metrics

    # -------------------------------------------------- health

    def _check_health(self, metrics: proc.Metrics, slow: bool) -> None:
        """A few dozen comparisons per poll; trend fits only on slow ticks."""
        if slow:
            now = time.time()
            for rule in TREND_RULES:
                self.health.add_sample(rule.key, now, metrics.system.get(rule.key))
        disks: dict[str, dict[str, float | None]] = {
            name: {
                "busy": d.busy_pct,
                "write_latency": d.write_latency_ms,
                "read_latency": d.read_latency_ms,
            }
            for name, d in metrics.disks.items()
        }
        system = metrics.system
        if self.quiet:
            # Feeding nothing for these keeps their current state unchanged:
            # nothing new is raised and nothing already raised is cleared.
            system = {k: v for k, v in system.items() if k not in LOAD_SENSITIVE}
            disks = {name: {} for name in disks}
        self.findings = self.health.evaluate(system, disks, trends=slow)
        if self._repairs:
            self._sync_repairs()

    @callback
    def _sync_repairs(self) -> None:
        """Create, update or delete Repairs issues only when a finding changes level."""
        wanted = {f"health_{f.id}": f for f in self.findings.values()}
        for issue_id in list(self._raised):
            if issue_id not in wanted:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                del self._raised[issue_id]
        for issue_id, f in wanted.items():
            if self._raised.get(issue_id) == f.level:
                continue  # unchanged: don't touch the registry (it saves to disk)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR if f.level == PROBLEM else ir.IssueSeverity.WARNING,
                translation_key="trend" if f.kind == "trend" else "now",
                translation_placeholders={"name": f.name, "summary": f.summary, "hint": f.hint},
            )
            self._raised[issue_id] = f.level

    @callback
    def async_clear_repairs(self) -> None:
        """Remove every issue this integration raised (on unload)."""
        registry = ir.async_get(self.hass)
        for (domain, issue_id) in list(registry.issues):
            if domain == DOMAIN and issue_id.startswith("health_"):
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        self._raised.clear()

    async def async_seed_trends(self) -> None:
        """Load up to 3 days of hourly means from the recorder, so trends survive restarts.

        One read-only statistics query, once at start-up.
        """
        if "recorder" not in self.hass.config.components:
            return
        from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
            statistics_during_period,
        )
        from homeassistant.helpers.recorder import get_instance  # noqa: PLC0415

        registry = er.async_get(self.hass)
        entry_id = self.config_entry.entry_id
        ids: dict[str, str] = {}
        for rule in TREND_RULES:
            if entity_id := registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry_id}_{rule.key}"
            ):
                ids[entity_id] = rule.key
        if not ids:
            return
        start = dt_util.utcnow() - timedelta(days=3)
        try:
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start,
                None,
                set(ids),
                "hour",
                None,
                {"mean"},
            )
        except Exception:  # noqa: BLE001 - history is optional
            _LOGGER.debug("Could not load statistics for trend seeding", exc_info=True)
            return
        for entity_id, rows in stats.items():
            self.health.seed(
                ids[entity_id],
                # Hourly means are stamped at the hour's start; use its middle.
                [(row["start"] + 1800, row["mean"]) for row in rows if row.get("mean") is not None],
            )
