"""Buttons that run the on-demand reports (results appear as notifications)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DiskWaitConfigEntry
from .const import DOMAIN
from .services import run_recorder_report, run_storage_report

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class ReportButton(ButtonEntityDescription):
    run: Callable[[HomeAssistant], Awaitable[Any]]
    needs: str


BUTTONS = (
    ReportButton(
        key="recorder_report",
        translation_key="recorder_report",
        entity_category=EntityCategory.DIAGNOSTIC,
        run=run_recorder_report,
        needs="recorder",
    ),
    ReportButton(
        key="storage_report",
        translation_key="storage_report",
        entity_category=EntityCategory.DIAGNOSTIC,
        run=run_storage_report,
        needs="hassio",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DiskWaitConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        ReportEntity(entry, desc) for desc in BUTTONS if desc.needs in hass.config.components
    )


class ReportEntity(ButtonEntity):
    _attr_has_entity_name = True
    entity_description: ReportButton

    def __init__(self, entry: DiskWaitConfigEntry, desc: ReportButton) -> None:
        self.entity_description = desc
        self._attr_unique_id = f"{entry.entry_id}_{desc.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_system")},
            name="System performance",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_press(self) -> None:
        await self.entity_description.run(self.hass)
