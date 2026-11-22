"""Tests for explaining findings."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.host_vitals import proc
from custom_components.host_vitals.culprits import CulpritTracker
from custom_components.host_vitals.health import Finding


def _finding(category):
    return Finding("x", "problem", "now", "X", "", "", category=category)


async def test_addon_and_io_explanations(hass: HomeAssistant) -> None:
    hass.config.components.add("hassio")
    t = CulpritTracker(hass, 5, lambda: {"sensor.mine"})
    for i in range(4):
        hass.states.async_set("sensor.mine", str(i))
        hass.states.async_set("sensor.weather_forecast", str(i))
    t.rotate()

    client = MagicMock()
    client.addons.list = AsyncMock(return_value=[
        SimpleNamespace(slug="a", name="AdGuard Home", state="started"),
        SimpleNamespace(slug="b", name="ESPHome", state="started"),
        SimpleNamespace(slug="c", name="Node-RED", state="stopped"),
    ])
    stats = {"a": SimpleNamespace(cpu_percent=3.0, memory_usage=120 * 2**20),
             "b": SimpleNamespace(cpu_percent=40.0, memory_usage=250 * 2**20)}
    client.addons.addon_stats = AsyncMock(side_effect=lambda slug: stats[slug])
    with patch("homeassistant.components.hassio.get_supervisor_client", return_value=client):
        t.maybe_refresh_addons({"x": _finding("cpu")})
        await hass.async_block_till_done(wait_background_tasks=True)
    assert t.version == 1

    m = proc.Metrics(system={"ha_cpu": 12.0, "ha_memory": 600, "ha_disk_read": 10, "ha_disk_write": 290})
    m.disks["mmcblk1"] = proc.DiskRates(0, 400, 0, 0, None, None, 90, 0)  # 400 KiB/s total
    cpu = t.explain(_finding("cpu"), m)
    assert "Home Assistant itself is using 12 % of the CPU." in cpu
    assert cpu.index("ESPHome 40 %") < cpu.index("AdGuard Home 3 %")
    assert "Node-RED" not in cpu
    mem = t.explain(_finding("memory"), m)
    assert "ESPHome 250 MiB" in mem
    io = t.explain(_finding("io"), m)
    assert "300 KiB/s of the 400 KiB/s" in io and "mostly Home Assistant itself" in io
    assert "sensor.weather_forecast (4)" in io and "sensor.mine" not in io
    assert t.short(_finding("io"), m) == "top: sensor.weather_forecast"
    assert t.short(_finding("cpu"), m) == "top add-on: ESPHome"

    # Healthy: no Supervisor calls at all.
    client.addons.list.reset_mock()
    t._addons_at = None
    t.maybe_refresh_addons({})
    await hass.async_block_till_done(wait_background_tasks=True)
    client.addons.list.assert_not_called()
    t.async_stop()
