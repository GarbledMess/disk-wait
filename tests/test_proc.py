"""Tests for the /proc parsing and rate maths (no Home Assistant needed)."""

import importlib.util
import pathlib
import sys

_spec = importlib.util.spec_from_file_location(
    "dw_proc",
    pathlib.Path(__file__).parents[1] / "custom_components/host_vitals/proc.py",
)
proc = importlib.util.module_from_spec(_spec)
sys.modules["dw_proc"] = proc
_spec.loader.exec_module(proc)

# user nice system idle iowait irq softirq steal guest guest_nice
STAT_A = "cpu  1000 0 500 8000 500 0 0 0 0 0\ncpu0 1 2 3 4 5 6 7 8 9 10\n"
STAT_B = "cpu  1100 0 550 8150 700 0 0 0 0 0\n"  # +500 total, +200 iowait, +150 idle

# major minor name reads rmerged rsect rms writes wmerged wsect wms inflight ticks tiq ...
DISK_A = (
    " 179 0 mmcblk0 100 0 2000 50 10 0 80 40 0 30 90 0 0 0 0 0 0\n"
    " 179 8 mmcblk0p8 90 0 1800 45 10 0 80 40 0 28 85\n"
    " 179 32 mmcblk1 1000 0 20000 4000 500 0 8000 30000 0 10000 34000 0 0 0 0\n"
    "   7 0 loop0 0 0 0 0 0 0 0 0 0 0 0\n"
)
DISK_B = (
    " 179 0 mmcblk0 100 0 2000 50 10 0 80 40 0 30 90 0 0 0 0 0 0\n"
    " 179 32 mmcblk1 1010 0 22048 4050 540 0 10048 34000 3 17500 41000 0 0 0 0\n"
)
PSI_A = "some avg10=1.00 avg60=0 avg300=0 total=1000000\nfull avg10=0 avg60=0 avg300=0 total=500000\n"
PSI_B = "some avg10=1.00 avg60=0 avg300=0 total=4000000\nfull avg10=0 avg60=0 avg300=0 total=2000000\n"

NET_HDR = (
    "Inter-|   Receive                                                |  Transmit\n"
    " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
)
NET_A = NET_HDR + "    lo: 5 0 0 0 0 0 0 0 5 0 0 0 0 0 0 0\n  end0: 1000 0 0 0 0 0 0 0 2000 0 0 0 0 0 0 0\n"
NET_B = NET_HDR + "    lo: 5 0 0 0 0 0 0 0 5 0 0 0 0 0 0 0\n  end0: 103400 0 0 0 0 0 0 0 53200 0 0 0 0 0 0 0\n"

MEMINFO = (
    "MemTotal:        4000000 kB\nMemFree:          100000 kB\nMemAvailable:    1000000 kB\n"
    "SwapCached:            0 kB\nSwapTotal:       2000000 kB\nSwapFree:        1500000 kB\n"
    "Dirty:              2048 kB\nWriteback:           1024 kB\n"
)
VMSTAT_A = b"nr_free_pages 1\npgmajfault 100\npswpin 10\npswpout 20\n"
VMSTAT_B = b"nr_free_pages 1\npgmajfault 150\npswpin 20\npswpout 30\n"
SELF_A = "1234 (python3 x) S 1 1 1 0 -1 4194560 0 0 0 0 100 50 0 0 20 0 30 0 1 1 1\n"
SELF_B = "1234 (python3 x) S 1 1 1 0 -1 4194560 0 0 0 0 300 250 0 0 20 0 30 0 1 1 1\n"


def snap(t, stat, disks, psi, nets=NET_A, vm=VMSTAT_A, self_stat=SELF_A):
    total, idle, iowait = proc.parse_cpu(stat)
    s = proc.Snapshot(t, total, idle, iowait)
    s.disks = proc.parse_diskstats(disks)
    s.nets = proc.parse_net_dev(nets)
    s.psi = {"io": proc.parse_psi(psi)}
    s.vm = proc.parse_vmstat(vm)
    s.self_ticks = proc.parse_self_ticks(self_stat)
    return s


def test_rates():
    m = proc.compute(
        snap(0.0, STAT_A, DISK_A, PSI_A),
        snap(10.0, STAT_B, DISK_B, PSI_B, NET_B, VMSTAT_B, SELF_B),
    )
    s = m.system
    assert s["iowait"] == 40.0  # 200 / 500
    assert s["cpu"] == 30.0  # (500 - 350 idle+iowait) / 500
    assert s["io_pressure_some"] == 30.0  # 3 s stalled in 10 s
    assert s["io_pressure_full"] == 15.0
    assert s["major_faults"] == 5.0
    assert s["swap_activity"] == round(2 * proc.PAGE_SIZE / 1024, 1)  # 20 pages / 10 s
    # 400 ticks over 10 s, as a share of the whole machine
    assert s["ha_cpu"] == round(min(100, 100 * 400 / proc.CLK_TCK / (10 * proc.NCPU)), 1)
    d = m.disks["mmcblk1"]
    assert d.read_kbps == 102.4  # 2048 sectors * 512 B / 1024 / 10 s
    assert d.write_kbps == 102.4
    assert d.read_iops == 1.0
    assert d.write_iops == 4.0
    assert d.read_latency_ms == 5.0  # 50 ms / 10 reads
    assert d.write_latency_ms == 100.0  # 4000 ms / 40 writes
    assert d.busy_pct == 75.0  # 7500 ms busy in 10 s
    assert d.in_flight == 3
    idle = m.disks["mmcblk0"]
    assert idle.busy_pct == 0.0 and idle.read_latency_ms is None
    n = m.nets["end0"]
    assert n.rx_kbps == 10.0 and n.tx_kbps == 5.0


def test_counter_reset_skips():
    m = proc.compute(
        snap(0.0, STAT_B, DISK_B, PSI_B, NET_B, VMSTAT_B, SELF_B),
        snap(10.0, STAT_A, DISK_A, PSI_A),
    )
    assert "mmcblk1" not in m.disks
    assert "end0" not in m.nets
    for key in ("iowait", "cpu", "io_pressure_some", "major_faults", "ha_cpu"):
        assert key not in m.system


def test_meminfo_gauges(tmp_path, monkeypatch):
    files = {proc.PROC_MEMINFO: MEMINFO, proc.PROC_STAT: STAT_A}
    monkeypatch.setattr(proc, "_read", lambda p: files.get(p))
    monkeypatch.setattr(proc, "_read_bytes", lambda p: None)
    g = proc.read_snapshot(proc.Sources(space_path=str(tmp_path)), slow=True).gauges
    assert g["memory_used"] == 75
    assert g["memory_available"] == 977  # MiB
    assert g["swap_used"] == 25.0
    assert g["dirty"] == 3
    assert 0 <= g["disk_used"] <= 100
    assert "db_size" not in g  # no DB path configured


def test_filter_and_short_lines():
    assert list(proc.parse_diskstats(DISK_A + "garbage\n", {"mmcblk1"})) == ["mmcblk1"]
    assert list(proc.parse_net_dev(NET_A, {"end0"})) == ["end0"]


def test_self_ticks_with_spaces_in_comm():
    assert proc.parse_self_ticks(SELF_A) == 150


def test_find_data_disk():
    mountinfo = (
        "1 0 0:31 / / rw - overlay overlay rw\n"
        "50 1 179:8 /supervisor/homeassistant /config rw,relatime - ext4 /dev/mmcblk0p8 rw\n"
    )
    assert proc.find_data_disk(mountinfo, DISK_A) == "mmcblk0"
    assert proc.find_data_disk("1 0 0:31 / / rw - overlay overlay rw\n", DISK_A) is None


def test_default_net():
    route = "Iface\tDestination\tGateway\nend0\t000200C0\t00000000\nend0\t00000000\t010200C0\n"
    assert proc.default_net(route) == "end0"
    assert proc.default_net("Iface\tDestination\n") is None


def test_parent_disk_fallback():
    assert proc.parent_disk("mmcblk1p8") == "mmcblk1"
    assert proc.parent_disk("sda8") == "sda"
    assert proc.parent_disk("nvme0n1p2") == "nvme0n1"
    assert proc.parent_disk("mmcblk1") == "mmcblk1"


def test_live_proc_reads():
    src = proc.Sources(
        disks=frozenset(proc.list_disks()),
        nets=frozenset(proc.list_nets()),
        psi=proc.available_psi(),
        thermal=proc.find_thermal(),
        space_path="/",
    )
    a = proc.read_snapshot(src, slow=True)
    b = proc.read_snapshot(src)
    assert a.cpu_total and a.self_ticks is not None
    assert "memory_used" in a.gauges and "disk_used" in a.gauges
    proc.compute(a, b)
