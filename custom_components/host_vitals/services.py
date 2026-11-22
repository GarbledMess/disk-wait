"""Actions that run the on-demand reports and post them as notifications."""

from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.util.hass_dict import HassKey

from . import reports
from .const import DOMAIN

SERVICE_RECORDER = "recorder_report"
SERVICE_STORAGE = "storage_report"

_LOCK: HassKey[asyncio.Lock] = HassKey(f"{DOMAIN}_report_lock")

_RECORDER_SCHEMA = vol.Schema(
    {
        vol.Optional("hours", default=24): vol.All(vol.Coerce(int), vol.Range(min=1, max=24 * 30)),
        vol.Optional("limit", default=15): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
        vol.Optional("notify", default=True): cv.boolean,
    }
)
_STORAGE_SCHEMA = vol.Schema(
    {
        vol.Optional("limit", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Optional("notify", default=True): cv.boolean,
    }
)


async def _exclusive(hass: HomeAssistant, coro: Any) -> Any:
    """Run one report at a time; a second press while one runs is refused.

    While a report runs, and briefly after, load-sensitive health checks are
    paused so Host Vitals doesn't flag the load it is causing itself.
    """
    lock = hass.data.setdefault(_LOCK, asyncio.Lock())
    if lock.locked():
        coro.close()
        raise HomeAssistantError("A Host Vitals report is already running")
    coordinators = [e.runtime_data for e in hass.config_entries.async_loaded_entries(DOMAIN)]
    async with lock:
        for c in coordinators:
            c.async_report_started()
        try:
            return await coro
        finally:
            for c in coordinators:
                c.async_report_finished()


async def run_recorder_report(
    hass: HomeAssistant, hours: int = 24, limit: int = 15, notify: bool = True
) -> dict[str, Any]:
    result = await _exclusive(hass, reports.recorder_report(hass, hours, limit))
    if notify:
        persistent_notification.async_create(
            hass,
            reports.recorder_markdown(result),
            title="Host Vitals: recorder report",
            notification_id=f"{DOMAIN}_recorder_report",
        )
    return result


async def run_storage_report(
    hass: HomeAssistant, limit: int = 0, notify: bool = True
) -> dict[str, Any]:
    if notify:
        persistent_notification.async_create(
            hass,
            "Measuring the data disk. On a microSD card this can take a few minutes; "
            "this notification will be replaced with the report.",
            title="Host Vitals: storage report",
            notification_id=f"{DOMAIN}_storage_report",
        )
    result = await _exclusive(hass, reports.storage_report(hass, limit))
    if notify:
        persistent_notification.async_create(
            hass,
            reports.storage_markdown(result),
            title="Host Vitals: storage report",
            notification_id=f"{DOMAIN}_storage_report",
        )
    return result


def async_register(hass: HomeAssistant) -> None:
    async def _recorder(call: ServiceCall) -> ServiceResponse:
        return await run_recorder_report(hass, **call.data)

    async def _storage(call: ServiceCall) -> ServiceResponse:
        return await run_storage_report(hass, **call.data)

    hass.services.async_register(
        DOMAIN, SERVICE_RECORDER, _recorder, _RECORDER_SCHEMA, SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, SERVICE_STORAGE, _storage, _STORAGE_SCHEMA, SupportsResponse.OPTIONAL
    )
