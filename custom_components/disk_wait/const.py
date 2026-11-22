"""Constants for Disk Wait."""

DOMAIN = "disk_wait"

CONF_DISKS = "disks"
CONF_INTERFACES = "interfaces"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_REPAIRS = "repairs"

# Rates are averaged over the whole interval from cumulative kernel counters,
# so a longer interval loses resolution but never misses activity.
DEFAULT_SCAN_INTERVAL = 60  # seconds
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 300

# Disk space and recorder DB size change slowly; refresh them this often.
SLOW_INTERVAL = 300  # seconds

# How often the event-loop lag probe wakes. Each wake is one timer callback.
LOOP_PROBE_INTERVAL = 5.0  # seconds

# Load-sensitive health checks (loop lag, pressure, disk busy/latency) are
# paused for this long after Home Assistant finishes starting, and after one
# of Disk Wait's own reports finishes, so they don't flag our own load.
STARTUP_GRACE = 600  # seconds
REPORT_GRACE = 120  # seconds
