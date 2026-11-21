"""Recorder-backed tests (need the recorder set up before hass)."""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.disk_wait import reports
from custom_components.disk_wait.const import DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    yield


async def test_recorder_report(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    for i in range(30):
        hass.states.async_set("sensor.chatty", str(i), {"n": i})
    for i in range(3):
        hass.states.async_set("sensor.quiet", str(i))
    await async_wait_recording_done(hass)

    result = await hass.services.async_call(
        DOMAIN, "recorder_report", {"hours": 1, "limit": 5}, blocking=True, return_response=True
    )
    top = result["top"]
    assert top[0]["entity_id"] == "sensor.chatty"
    assert top[0]["rows"] == 30
    assert top[0]["attribute_changes"] == 30
    assert top[0]["est_bytes"] > 30 * 100
    assert top[1]["entity_id"] == "sensor.quiet"
    assert result["rows_in_window"] >= 33
    assert result["integrations"][0]["integration"].startswith("sensor")

    md = reports.recorder_markdown(result)
    assert "`sensor.chatty`" in md and "| Entity |" in md


async def test_buttons_created(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"disks": [], "scan_interval": 60})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("button.system_performance_analyze_recorder") is not None
    # No Supervisor in tests, so no storage button.
    assert hass.states.get("button.system_performance_analyze_storage") is None

    await hass.services.async_call(
        "button", "press", {"entity_id": "button.system_performance_analyze_recorder"}, blocking=True
    )
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
