"""Polling coordinator: one executor job per interval reads a few small /proc files."""

from __future__ import annotations

from datetime import timedelta
import asyncio
import logging
import os

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import proc
from .const import (
    CONF_DISKS,
    CONF_INTERFACES,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOOP_PROBE_INTERVAL,
    SLOW_INTERVAL,
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
        return metrics
