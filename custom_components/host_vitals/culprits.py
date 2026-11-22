"""Work out what is likely behind a health finding.

Costs while everything is healthy: one dict increment per state change (to
know which entities are busiest) and nothing else. Add-on CPU and memory are
asked from the Supervisor only while a CPU, memory, I/O or lag finding is
active, and at most once every 5 minutes.

What Home Assistant Core can and can't see:
- its own disk reads and writes (/proc/self/io), so it can say how much of a
  disk's traffic is Home Assistant itself versus everything else;
- which entities change state most often (each change is a recorder write);
- add-on CPU and memory (Supervisor stats).
It can't see other processes or per-add-on disk I/O: the container has its
own process namespace, and the Supervisor's per-add-on disk counters read 0
on current HA OS.
"""

from __future__ import annotations

from collections import Counter, deque
import logging
import time
from typing import Any

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback

from . import proc
from .health import Finding

_LOGGER = logging.getLogger(__name__)

ADDON_REFRESH = 300.0  # seconds
TOP = 3
_NEEDS_ADDONS = {"io", "cpu", "memory", "lag"}


def _mib(n: float) -> str:
    return f"{n / 1024**2:.0f} MiB"


class CulpritTracker:
    """Collects the few facts needed to explain a finding."""

    def __init__(self, hass: HomeAssistant, window_polls: int, own_entities: Any) -> None:
        self.hass = hass
        self._own_entities = own_entities  # callable returning our own entity ids
        self.version = 0  # bumped when add-on stats change, so causes get rebuilt
        self._current: Counter[str] = Counter()
        self._windows: deque[Counter[str]] = deque(maxlen=max(1, window_polls))
        self._addons: list[dict[str, Any]] = []
        self._addons_at: float | None = None
        self._addons_task: Any = None
        self._unsub = hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_state)

    @callback
    def _on_state(self, event: Event) -> None:
        self._current[event.data["entity_id"]] += 1

    @callback
    def async_stop(self) -> None:
        self._unsub()

    @callback
    def rotate(self) -> None:
        """Called once per poll: close the current window of state-change counts."""
        self._windows.append(self._current)
        self._current = Counter()

    def busiest_entities(self, n: int = TOP) -> list[tuple[str, int]]:
        total: Counter[str] = Counter()
        for c in self._windows:
            total.update(c)
        for e in self._own_entities() & total.keys():
            del total[e]  # our own sensors change once a poll by design
        return total.most_common(n)

    # ------------------------------------------------------------ add-ons

    @callback
    def maybe_refresh_addons(self, findings: dict[str, Finding]) -> None:
        if "hassio" not in self.hass.config.components:
            return
        if not any(f.category in _NEEDS_ADDONS for f in findings.values()):
            return
        if self._addons_task or (
            self._addons_at is not None and time.monotonic() - self._addons_at < ADDON_REFRESH
        ):
            return
        self._addons_task = self.hass.async_create_background_task(
            self._refresh_addons(), "host_vitals add-on stats"
        )

    async def _refresh_addons(self) -> None:
        import asyncio  # noqa: PLC0415

        from aiohasupervisor import SupervisorError  # noqa: PLC0415

        from homeassistant.components.hassio import get_supervisor_client  # noqa: PLC0415

        try:
            client = get_supervisor_client(self.hass)
            running = [a for a in await client.addons.list() if a.state == "started"]

            async def one(a: Any) -> dict[str, Any] | None:
                try:
                    st = await client.addons.addon_stats(a.slug)
                except SupervisorError:
                    return None
                return {"name": a.name, "cpu": st.cpu_percent, "memory": st.memory_usage}

            self._addons = [r for r in await asyncio.gather(*map(one, running)) if r]
            self.version += 1
        except Exception:  # noqa: BLE001 - explanations are best effort
            _LOGGER.debug("Could not read add-on stats", exc_info=True)
        finally:
            self._addons_at = time.monotonic()
            self._addons_task = None

    def _top_addons(self, key: str) -> list[dict[str, Any]]:
        return sorted(self._addons, key=lambda a: -(a[key] or 0))[:TOP]

    # ------------------------------------------------------------ text

    def explain(self, f: Finding, metrics: proc.Metrics) -> str:
        s = metrics.system
        parts: list[str] = []
        if f.category == "io":
            disk_kbps = sum(d.read_kbps + d.write_kbps for d in metrics.disks.values())
            ha_kbps = (s.get("ha_disk_read") or 0) + (s.get("ha_disk_write") or 0)
            if disk_kbps > 0:
                share = min(100, round(100 * ha_kbps / disk_kbps))
                where = (
                    "it's mostly Home Assistant itself"
                    if share >= 50
                    else "most of it comes from outside Home Assistant: an add-on or the OS"
                )
                parts.append(
                    f"Home Assistant itself accounts for {ha_kbps:.0f} KiB/s of the "
                    f"{disk_kbps:.0f} KiB/s read and written ({share} %), so {where}."
                )
            parts.append(self._entities_text())
            if self._addons:
                parts.append(
                    "Busiest add-ons by CPU (per-add-on disk use isn't available): "
                    + self._addons_text("cpu")
                )
        elif f.category == "cpu":
            if (ha := s.get("ha_cpu")) is not None:
                parts.append(f"Home Assistant itself is using {ha:.0f} % of the CPU.")
            if self._addons:
                parts.append("Busiest add-ons by CPU: " + self._addons_text("cpu"))
        elif f.category == "memory":
            if (ha := s.get("ha_memory")) is not None:
                parts.append(f"Home Assistant itself is using {ha:.0f} MiB.")
            if self._addons:
                parts.append("Largest add-ons by memory: " + self._addons_text("memory"))
        elif f.category == "lag":
            if (ha := s.get("ha_cpu")) is not None:
                parts.append(f"Home Assistant is using {ha:.0f} % of the CPU.")
            parts.append(self._entities_text())
        elif f.category == "recorder":
            parts.append(self._entities_text())
        return " ".join(p for p in parts if p)

    def _entities_text(self) -> str:
        top = self.busiest_entities()
        if not top:
            return ""
        return "Busiest entities in the last few minutes (each change is a database write): " + ", ".join(
            f"{e} ({n})" for e, n in top
        ) + "."

    def _addons_text(self, key: str) -> str:
        items = self._top_addons(key)
        if key == "cpu":
            return ", ".join(f"{a['name']} {a['cpu']:.0f} %" for a in items) + "."
        return ", ".join(f"{a['name']} {_mib(a['memory'])}" for a in items) + "."

    def short(self, f: Finding, metrics: proc.Metrics) -> str:
        """A few words for the Health reason sensor."""
        s = metrics.system
        if f.category == "io":
            disk_kbps = sum(d.read_kbps + d.write_kbps for d in metrics.disks.values())
            ha_kbps = (s.get("ha_disk_read") or 0) + (s.get("ha_disk_write") or 0)
            if disk_kbps > 0 and ha_kbps / disk_kbps < 0.5:
                return "mostly outside HA"
            top = self.busiest_entities(1)
            return f"top: {top[0][0]}" if top else "mostly HA"
        if f.category in ("cpu", "memory") and self._addons:
            key = "cpu" if f.category == "cpu" else "memory"
            return f"top add-on: {self._top_addons(key)[0]['name']}"
        if f.category in ("lag", "recorder"):
            top = self.busiest_entities(1)
            return f"top: {top[0][0]}" if top else ""
        return ""
