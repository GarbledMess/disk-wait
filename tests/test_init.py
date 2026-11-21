"""Load the integration in a real Home Assistant test instance."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.disk_wait import proc
from custom_components.disk_wait.const import (
    CONF_DISKS,
    CONF_INTERFACES,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)

from .test_proc import (
    DISK_A,
    DISK_B,
    MEMINFO,
    NET_A,
    NET_B,
    PSI_A,
    PSI_B,
    SELF_A,
    SELF_B,
    STAT_A,
    STAT_B,
    VMSTAT_A,
    VMSTAT_B,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _fake_reads(seq):
    files = iter(seq)
    current = {}

    def fake_read(path):
        return current.get(path)

    def fake_read_bytes(path):
        text = current.get(path)
        return text if text is None or isinstance(text, bytes) else text.encode()

    def advance():
        current.clear()
        current.update(next(files))

    return fake_read, fake_read_bytes, advance


async def test_setup_and_rates(hass: HomeAssistant, freezer) -> None:
    fake_read, fake_read_bytes, advance = _fake_reads(
        [
            {
                proc.PROC_STAT: STAT_A,
                proc.PROC_DISKSTATS: DISK_A,
                proc.PROC_PSI["io"]: PSI_A,
                proc.PROC_NET_DEV: NET_A,
                proc.PROC_VMSTAT: VMSTAT_A,
                proc.PROC_SELF_STAT: SELF_A,
                proc.PROC_MEMINFO: MEMINFO,
                proc.PROC_LOADAVG: "0.50 0.40 0.30 1/100 200\n",
                proc.PROC_SELF_STATM: "1000 51200 0 0 0 0 0\n",
            },
            {
                proc.PROC_STAT: STAT_B,
                proc.PROC_DISKSTATS: DISK_B,
                proc.PROC_PSI["io"]: PSI_B,
                proc.PROC_NET_DEV: NET_B,
                proc.PROC_VMSTAT: VMSTAT_B,
                proc.PROC_SELF_STAT: SELF_B,
                proc.PROC_MEMINFO: MEMINFO,
                proc.PROC_LOADAVG: "0.75 0.40 0.30 1/100 200\n",
                proc.PROC_SELF_STATM: "1000 51200 0 0 0 0 0\n",
            },
        ]
    )
    advance()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_DISKS: ["mmcblk1"], CONF_INTERFACES: ["end0"], CONF_SCAN_INTERVAL: 30},
    )
    entry.add_to_hass(hass)
    with (
        patch.object(proc, "_read", side_effect=fake_read),
        patch.object(proc, "_read_bytes", side_effect=fake_read_bytes),
        patch.object(proc, "available_psi", return_value=("io",)),
        patch.object(proc, "find_thermal", return_value=None),
        patch.object(proc, "time", **{"monotonic.side_effect": [0.0, 10.0]}),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get("sensor.system_performance_cpu_iowait").state == "unknown"
        # Gauges are available from the first poll.
        assert hass.states.get("sensor.system_performance_memory_used").state == "75"

        advance()
        freezer.tick(timedelta(seconds=31))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)

    def state(entity_id):
        return hass.states.get(entity_id).state

    assert state("sensor.system_performance_cpu_iowait") == "40.0"
    assert state("sensor.system_performance_cpu_usage") == "30.0"
    assert state("sensor.system_performance_i_o_pressure_some") == "30.0"
    assert state("sensor.system_performance_load_1_min") == "0.75"
    assert state("sensor.system_performance_home_assistant_memory") == str(
        round(51200 * proc.PAGE_SIZE / proc.MIB)
    )
    assert state("sensor.system_performance_event_loop_lag").isdigit()
    assert state("sensor.system_performance_data_disk_used") not in ("unknown", "unavailable")
    assert state("sensor.network_end0_download") == "10.0"
    # Not created: no thermal zone, no PSI for memory/cpu, no SQLite DB in test config
    assert hass.states.get("sensor.system_performance_cpu_temperature") is None
    assert hass.states.get("sensor.system_performance_cpu_pressure") is None
    assert hass.states.get("sensor.disk_mmcblk1_busy").state == "75.0"
    assert hass.states.get("sensor.disk_mmcblk1_write_latency").state == "100.0"
    assert hass.states.get("sensor.disk_mmcblk1_write_throughput").state == "102.4"
    # Disabled by default
    assert hass.states.get("sensor.disk_mmcblk1_read_iops") is None
    assert hass.states.get("sensor.system_performance_load_5_min") is None

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_config_flow(hass: HomeAssistant) -> None:
    with (
        patch.object(proc, "list_disks", return_value=["mmcblk0", "mmcblk1"]),
        patch.object(proc, "find_data_disk", return_value="mmcblk1"),
        patch.object(proc, "list_nets", return_value=["end0"]),
        patch.object(proc, "default_net", return_value="end0"),
        patch("custom_components.disk_wait.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DISKS: ["mmcblk1"], CONF_SCAN_INTERVAL: 20}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_DISKS: ["mmcblk1"],
        CONF_INTERFACES: ["end0"],
        CONF_SCAN_INTERVAL: 20,
    }
