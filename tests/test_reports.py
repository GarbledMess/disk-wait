"""Tests for the on-demand recorder and storage reports."""

from datetime import datetime, UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.disk_wait import reports
from custom_components.disk_wait.const import DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def test_recorder_markdown_exclude_candidates():
    r = {
        "hours": 24,
        "rows_in_window": 5000,
        "rows_per_day": 5000,
        "total_states": 90000,
        "stats_entities": 40,
        "query_seconds": 0.1,
        "top": [
            {"entity_id": "sensor.a", "rows": 4000, "rows_per_day": 4000, "attribute_changes": 2, "est_bytes": 400000, "integration": "x"},
            {"entity_id": "sensor.b", "rows": 500, "rows_per_day": 500, "attribute_changes": 1, "est_bytes": 50000, "integration": "y"},
        ],
        "integrations": [{"integration": "x", "rows": 4000}],
        "events": [],
    }
    md = reports.recorder_markdown(r)
    assert "      - sensor.a" in md
    assert "      - sensor.b" not in md


async def test_recorder_report_without_recorder(hass: HomeAssistant) -> None:
    with pytest.raises(HomeAssistantError):
        await reports.recorder_report(hass, 24, 10)


def _node(id_, used, children=None, total=None, label=None):
    return SimpleNamespace(id=id_, used_bytes=used, total_bytes=total, label=label, children=children)


async def test_storage_report(hass: HomeAssistant) -> None:
    hass.config.components.add("hassio")
    gib = 1024**3
    client = MagicMock()
    client.addons.list = AsyncMock(
        return_value=[
            SimpleNamespace(slug="a0d7b954_adguard", name="AdGuard Home", state="started"),
            SimpleNamespace(slug="core_esphome", name="ESPHome", state="stopped"),
        ]
    )
    usage = AsyncMock(
        return_value=_node(
            "root",
            44 * gib,
            total=200 * gib,
            children=[
                _node("system", 20 * gib, label="System"),
                _node(
                    "addons_data",
                    10 * gib,
                    label="App data",
                    children=[_node("a0d7b954_adguard", 9 * gib), _node("core_esphome", gib)],
                ),
                _node("backup", 13 * gib, label="Backups"),
            ],
        )
    )
    client.addons.addon_stats = AsyncMock(
        return_value=SimpleNamespace(memory_usage=200 * 1024**2, cpu_percent=1.23, blk_write=5 * gib)
    )
    loc = SimpleNamespace(size_bytes=2 * gib)
    client.backups.list = AsyncMock(
        return_value=[
            SimpleNamespace(name="Auto", date=datetime(2026, 11, 1, tzinfo=UTC), type="full", location_attributes={".local": loc}),
            SimpleNamespace(name="Cloud", date=datetime(2026, 11, 20, tzinfo=UTC), type="full", location_attributes={"cloud": loc}),
        ]
    )
    with (
        patch("homeassistant.components.hassio.get_supervisor_client", return_value=client),
        patch.object(reports, "_disk_usage", usage),
    ):
        r = await reports.storage_report(hass, 0)

    assert r["errors"] == []
    assert [c["label"] for c in r["disk"]["children"]] == ["System", "Backups", "App data"]
    app = r["disk"]["children"][2]
    assert app["children"][0]["label"] == "AdGuard Home"  # slug mapped to name
    adguard, esphome = r["addons"]
    assert adguard["data_bytes"] == 9 * gib and adguard["disk_written_bytes"] == 5 * gib
    assert esphome["running"] is False and esphome["memory_bytes"] is None
    client.addons.addon_stats.assert_awaited_once_with("a0d7b954_adguard")
    assert r["backups"]["count"] == 2 and r["backups"]["local_count"] == 1
    assert r["backups"]["local_bytes"] == 2 * gib

    md = reports.storage_markdown(r)
    assert "AdGuard Home" in md and "9.0 GiB" in md and "stopped" in md
    assert "Written since start" in md

    # cgroup v2: Supervisor reports 0 writes for everyone -> column hidden
    for a in r["addons"]:
        a["disk_written_bytes"] = 0 if a["running"] else None
    md = reports.storage_markdown(r)
    assert "Written since start" not in md and "reports 0" in md

    # Slow card: the disk walk times out -> clear error, data shown as n/a
    usage.side_effect = TimeoutError
    with (
        patch("homeassistant.components.hassio.get_supervisor_client", return_value=client),
        patch.object(reports, "_disk_usage", usage),
    ):
        r = await reports.storage_report(hass, 0)
    assert "disk" not in r and "took over" in r["errors"][0]
    md = reports.storage_markdown(r)
    assert md.startswith("**Some parts could not be read:**") and "| n/a |" in md


async def test_disk_usage_uses_long_timeout() -> None:
    raw = MagicMock()
    raw.get = AsyncMock(return_value=SimpleNamespace(data={"id": "root", "used_bytes": 1, "children": [
        {"id": "apps_data", "used_bytes": 1, "children": [{"id": "core_x", "used_bytes": 1}]}]}))
    client = SimpleNamespace(host=SimpleNamespace(_client=raw))
    usage = await reports._disk_usage(client)
    assert usage.children[0].children[0].id == "core_x"
    kwargs = raw.get.await_args.kwargs
    assert kwargs["params"] == {"max_depth": "2"}
    assert kwargs["timeout"].total == reports.DISK_USAGE_TIMEOUT
