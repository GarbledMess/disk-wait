"""Sensors for Host Vitals.

No sensor has extra state attributes, so each update is at most one small
recorder row, and only when the rounded value actually changes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfDataRate,
    UnitOfInformation,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HostVitalsConfigEntry
from .const import DOMAIN
from .coordinator import HostVitalsCoordinator
from .health import OK, PROBLEM, WARNING, HealthMonitor

PARALLEL_UPDATES = 0

M = SensorStateClass.MEASUREMENT


@dataclass(frozen=True, kw_only=True)
class SystemSensor(SensorEntityDescription):
    """A machine-wide value, looked up by ``key`` in Metrics.system."""

    available_if: Callable[[HostVitalsCoordinator], bool] = lambda c: True


@dataclass(frozen=True, kw_only=True)
class ItemSensor(SensorEntityDescription):
    """A per-disk or per-interface value."""

    value: Callable[[Any], float | int | None]


def _pct(key: str, enabled: bool = True, precision: int = 1, **kw: Any) -> SystemSensor:
    return SystemSensor(
        key=key,
        translation_key=key,
        native_unit_of_measurement=PERCENTAGE,
        state_class=M,
        suggested_display_precision=precision,
        entity_registry_enabled_default=enabled,
        **kw,
    )


def _psi(kind: str, level: str, enabled: bool) -> SystemSensor:
    return _pct(
        f"{kind}_pressure_{level}",
        enabled,
        available_if=lambda c: kind in c.sources.psi,
    )


SYSTEM_SENSORS: tuple[SystemSensor, ...] = (
    # CPU
    _pct("cpu"),
    _pct("iowait"),
    SystemSensor(key="load_1", translation_key="load_1", state_class=M, suggested_display_precision=2),
    SystemSensor(key="load_5", translation_key="load_5", state_class=M, suggested_display_precision=2, entity_registry_enabled_default=False),
    SystemSensor(key="load_15", translation_key="load_15", state_class=M, suggested_display_precision=2, entity_registry_enabled_default=False),
    SystemSensor(
        key="cpu_temperature",
        translation_key="cpu_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=M,
        suggested_display_precision=1,
        available_if=lambda c: c.sources.thermal is not None,
    ),
    # Memory
    _pct("memory_used", precision=0),
    SystemSensor(
        key="memory_available",
        translation_key="memory_available",
        native_unit_of_measurement=UnitOfInformation.MEBIBYTES,
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=M,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
    ),
    _pct("swap_used", precision=0),
    SystemSensor(
        key="swap_activity",
        translation_key="swap_activity",
        native_unit_of_measurement=UnitOfDataRate.KIBIBYTES_PER_SECOND,
        device_class=SensorDeviceClass.DATA_RATE,
        state_class=M,
        suggested_display_precision=0,
    ),
    SystemSensor(
        key="major_faults",
        translation_key="major_faults",
        native_unit_of_measurement="faults/s",
        state_class=M,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
    ),
    SystemSensor(
        key="dirty",
        translation_key="dirty",
        native_unit_of_measurement=UnitOfInformation.MEBIBYTES,
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=M,
        suggested_display_precision=0,
    ),
    # Pressure stall information
    _psi("io", "some", True),
    _psi("io", "full", False),
    _psi("memory", "some", True),
    _psi("memory", "full", False),
    _psi("cpu", "some", True),
    # Home Assistant itself
    _pct("ha_cpu"),
    SystemSensor(
        key="ha_memory",
        translation_key="ha_memory",
        native_unit_of_measurement=UnitOfInformation.MEBIBYTES,
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=M,
        suggested_display_precision=0,
    ),
    SystemSensor(
        key="ha_disk_write",
        translation_key="ha_disk_write",
        native_unit_of_measurement=UnitOfDataRate.KIBIBYTES_PER_SECOND,
        device_class=SensorDeviceClass.DATA_RATE,
        state_class=M,
        suggested_display_precision=0,
    ),
    SystemSensor(
        key="ha_disk_read",
        translation_key="ha_disk_read",
        native_unit_of_measurement=UnitOfDataRate.KIBIBYTES_PER_SECOND,
        device_class=SensorDeviceClass.DATA_RATE,
        state_class=M,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
    ),
    SystemSensor(
        key="loop_lag",
        translation_key="loop_lag",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=M,
        suggested_display_precision=0,
    ),
    # Storage (refreshed every few minutes)
    _pct("disk_used"),
    SystemSensor(
        key="disk_free",
        translation_key="disk_free",
        native_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=M,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
    ),
    SystemSensor(
        key="db_size",
        translation_key="db_size",
        native_unit_of_measurement=UnitOfInformation.MEBIBYTES,
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=M,
        suggested_display_precision=0,
        available_if=lambda c: bool(c.sources.db_paths),
    ),
)

_KBPS: dict[str, Any] = {
    "native_unit_of_measurement": UnitOfDataRate.KIBIBYTES_PER_SECOND,
    "device_class": SensorDeviceClass.DATA_RATE,
    "state_class": M,
    "suggested_display_precision": 0,
}
_MS: dict[str, Any] = {
    "native_unit_of_measurement": UnitOfTime.MILLISECONDS,
    "device_class": SensorDeviceClass.DURATION,
    "state_class": M,
    "suggested_display_precision": 1,
}
_IOPS: dict[str, Any] = {
    "native_unit_of_measurement": "IOPS",
    "state_class": M,
    "suggested_display_precision": 1,
    "entity_registry_enabled_default": False,
}

DISK_SENSORS = (
    ItemSensor(key="read", translation_key="read", value=lambda d: d.read_kbps, **_KBPS),
    ItemSensor(key="write", translation_key="write", value=lambda d: d.write_kbps, **_KBPS),
    ItemSensor(
        key="busy",
        translation_key="busy",
        value=lambda d: d.busy_pct,
        native_unit_of_measurement=PERCENTAGE,
        state_class=M,
        suggested_display_precision=1,
    ),
    ItemSensor(key="read_latency", translation_key="read_latency", value=lambda d: d.read_latency_ms, **_MS),
    ItemSensor(key="write_latency", translation_key="write_latency", value=lambda d: d.write_latency_ms, **_MS),
    ItemSensor(key="read_iops", translation_key="read_iops", value=lambda d: d.read_iops, **_IOPS),
    ItemSensor(key="write_iops", translation_key="write_iops", value=lambda d: d.write_iops, **_IOPS),
    ItemSensor(
        key="in_flight",
        translation_key="in_flight",
        value=lambda d: d.in_flight,
        state_class=M,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)

NET_SENSORS = (
    ItemSensor(key="rx", translation_key="rx", value=lambda n: n.rx_kbps, **_KBPS),
    ItemSensor(key="tx", translation_key="tx", value=lambda n: n.tx_kbps, **_KBPS),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HostVitalsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    c = entry.runtime_data
    entities: list[SensorEntity] = [HealthEntity(c), HealthReasonEntity(c)]
    entities += [
        SystemEntity(c, desc) for desc in SYSTEM_SENSORS if desc.available_if(c)
    ]
    entities += [
        ItemEntity(c, desc, "disks", disk, f"Disk {disk}")
        for disk in c.disks
        for desc in DISK_SENSORS
    ]
    entities += [
        ItemEntity(c, desc, "nets", net, f"Network {net}")
        for net in c.nets
        for desc in NET_SENSORS
    ]
    async_add_entities(entities)


class _Base(CoordinatorEntity[HostVitalsCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def _device(self, ident: str, name: str) -> DeviceInfo:
        entry_id = self.coordinator.config_entry.entry_id
        return DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{ident}")},
            name=name,
            entry_type=DeviceEntryType.SERVICE,
        )


class SystemEntity(_Base):
    entity_description: SystemSensor

    def __init__(self, coordinator: HostVitalsCoordinator, desc: SystemSensor) -> None:
        super().__init__(coordinator)
        self.entity_description = desc
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{desc.key}"
        self._attr_device_info = self._device("system", "System performance")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.system.get(self.entity_description.key)


class ItemEntity(_Base):
    entity_description: ItemSensor

    def __init__(
        self,
        coordinator: HostVitalsCoordinator,
        desc: ItemSensor,
        group: str,
        item: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = desc
        self._group = group
        self._item = item
        entry_id = coordinator.config_entry.entry_id
        # Disk unique ids keep the 0.1.0 format so existing history carries over.
        prefix = "" if group == "disks" else "net_"
        self._attr_unique_id = f"{entry_id}_{prefix}{item}_{desc.key}"
        self._attr_device_info = self._device(f"{prefix}{item}", device_name)

    @property
    def native_value(self) -> float | int | None:
        rates = getattr(self.coordinator.data, self._group).get(self._item)
        return None if rates is None else self.entity_description.value(rates)


class HealthEntity(_Base):
    """Overall health: ok, warning or problem, plus which checks are firing.

    The attribute lists only names and levels, not values, so it changes only
    when a check starts or stops firing, and it is excluded from the recorder.
    """

    _attr_translation_key = "health"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [OK, WARNING, PROBLEM]
    _unrecorded_attributes = frozenset({"issues"})

    def __init__(self, coordinator: HostVitalsCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_health"
        self._attr_device_info = self._device("system", "System performance")

    @property
    def native_value(self) -> str:
        return HealthMonitor.overall(self.coordinator.findings)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "issues": [
                f"{f.name} ({'coming up' if f.kind == 'trend' else 'now'}, {f.level})"
                for f in sorted(self.coordinator.findings.values(), key=lambda f: f.id)
            ]
        }


def health_reason(findings: dict[str, Any], short: dict[str, str] | None = None) -> str:
    """One line saying what is wrong, worst first, e.g.
    "Problem: CPU iowait high · Warning: Data disk rising".

    Names only, no numbers, so the state changes only when the set of
    problems changes (the numbers are in the Repairs entry).
    """
    if not findings:
        return "All clear"
    parts = []
    for level, title in ((PROBLEM, "Problem"), (WARNING, "Warning")):
        names = []
        for f in sorted(findings.values(), key=lambda f: f.id):
            if f.level != level:
                continue
            name = f"{f.name} {'rising' if f.kind == 'trend' else 'high'}"
            if why := (short or {}).get(f.id):
                name += f" ({why})"
            names.append(name)
        if names:
            parts.append(f"{title}: {', '.join(names)}")
    text = " · ".join(parts)
    return text if len(text) <= 255 else text[:254] + "…"


class HealthReasonEntity(_Base):
    """Why Health isn't OK, as plain text right in the state."""

    _attr_translation_key = "health_reason"

    def __init__(self, coordinator: HostVitalsCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_health_reason"
        self._attr_device_info = self._device("system", "System performance")

    @property
    def native_value(self) -> str:
        return health_reason(self.coordinator.findings, self.coordinator.culprit_short)

    @property
    def icon(self) -> str:
        level = HealthMonitor.overall(self.coordinator.findings)
        return {OK: "mdi:check-circle-outline", WARNING: "mdi:alert-outline"}.get(
            level, "mdi:alert-octagon-outline"
        )
