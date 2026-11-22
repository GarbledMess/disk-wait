"""Tests for the health checks (pure Python)."""

import importlib.util
import pathlib
import sys

_spec = importlib.util.spec_from_file_location(
    "dw_health",
    pathlib.Path(__file__).parents[1] / "custom_components/disk_wait/health.py",
)
health = importlib.util.module_from_spec(_spec)
sys.modules["dw_health"] = health
_spec.loader.exec_module(health)

H = 3600.0


def run(mon, system, disks=None, n=1, trends=False):
    out = {}
    for _ in range(n):
        out = mon.evaluate(system, disks or {}, trends)
    return out


def test_single_spike_is_smoothed():
    mon = health.HealthMonitor(interval=60)
    run(mon, {"loop_lag": 5}, n=10)
    assert run(mon, {"loop_lag": 400}) == {}  # one bad minute: EMA ~84 ms
    found = run(mon, {"loop_lag": 400}, n=5)
    assert found["loop_lag"].level == health.WARNING


def test_levels_and_hysteresis():
    mon = health.HealthMonitor(interval=300)  # alpha 1: no smoothing
    assert run(mon, {"disk_used": 96})["disk_used"].level == health.PROBLEM
    assert run(mon, {"disk_used": 90})["disk_used"].level == health.PROBLEM  # > 95*0.9
    assert run(mon, {"disk_used": 84})["disk_used"].level == health.WARNING  # warning still held
    assert run(mon, {"disk_used": 76}) == {}  # below 85*0.9
    assert run(mon, {"disk_used": 84}) == {}  # needs 85 to raise again


def test_disk_rules_keep_state_when_idle():
    mon = health.HealthMonitor(interval=300)
    f = run(mon, {}, {"mmcblk1": {"write_latency": 1500, "busy": 50}})
    assert f["mmcblk1_write_latency"].level == health.PROBLEM
    assert "Disk mmcblk1 write latency" in f["mmcblk1_write_latency"].summary
    # No writes completed this poll: latency is None, keep the finding.
    f = run(mon, {}, {"mmcblk1": {"write_latency": None, "busy": 1}})
    assert "mmcblk1_write_latency" in f


def test_disk_filling_forecast():
    mon = health.HealthMonitor(interval=60)
    t0 = 1_000_000.0
    # 1 % per day for 12 hours, now at 85.5 %: 95 % in ~9.5 days
    for i in range(145):
        t = t0 + i * 300
        mon.add_sample("disk_used", t, 85.0 + (t - t0) / health.DAY)
    f = run(mon, {"disk_used": 85.5 - 1}, trends=True)  # now-value below the now threshold
    trend = f["trend_disk_used"]
    assert trend.level == health.WARNING and trend.kind == "trend"
    assert "days" in trend.summary


def test_flat_or_noisy_history_is_ignored():
    mon = health.HealthMonitor(interval=60)
    t0 = 0.0
    for i in range(200):
        mon.add_sample("memory_used", t0 + i * 300, 60 + (5 if i % 2 else -5))
    assert "trend_memory_used" not in run(mon, {"memory_used": 60}, trends=True)


def test_needs_enough_history():
    mon = health.HealthMonitor(interval=60)
    for i in range(20):  # only ~1.5 h
        mon.add_sample("memory_used", i * 300.0, 50 + i)
    assert run(mon, {"memory_used": 70}, trends=True) == {}


def test_growth_rate_rule_and_seeding():
    mon = health.HealthMonitor(interval=60)
    # Seed 2 days of hourly HA memory growing 10 MiB/h (240/day)
    mon.seed("ha_memory", [(i * H, 500 + 10 * i) for i in range(48)])
    mon.add_sample("ha_memory", 48 * H, 980)
    f = run(mon, {"ha_memory": 980}, trends=True)
    assert f["trend_ha_memory"].level == health.WARNING
    assert "240 MiB a day" in f["trend_ha_memory"].summary
    # Between slow ticks the trend finding is carried, not recomputed.
    assert "trend_ha_memory" in run(mon, {"ha_memory": 980}, trends=False)


def test_overall():
    f = {
        "a": health.Finding("a", health.WARNING, "now", "", "", ""),
        "b": health.Finding("b", health.PROBLEM, "trend", "", "", ""),
    }
    assert health.HealthMonitor.overall(f) == health.PROBLEM
    assert health.HealthMonitor.overall({}) == health.OK


def test_fit():
    slope, intercept, r2, span = health.fit([(0, 1), (10, 2), (20, 3)])
    assert abs(slope - 0.1) < 1e-9 and abs(intercept - 1) < 1e-9 and r2 > 0.999 and span == 20
