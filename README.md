# Disk Wait

A tiny Home Assistant integration that records the performance of the machine Home Assistant runs on, with a focus on finding out whether a slow disk is what makes the UI sluggish. It reads a few small kernel files once a minute and records them in the background like any other sensor. No dashboard or browser needs to be open.

Built for HA OS on a Home Assistant Green, but it works on any Linux install.

## Sensors

**System performance** device

| Sensor | What it tells you |
|---|---|
| CPU usage | Share of all CPU time that was busy. |
| CPU iowait | Share of all CPU time spent idle *waiting on disk*. On a 4-core Green, 25 % means roughly one core stuck waiting. |
| Load (1 min) | Runnable plus disk-blocked tasks. Load (5 min) and (15 min) are off by default. |
| CPU temperature | SoC temperature, in half-degree steps. Only if the board exposes a thermal zone. |
| Memory used | `MemTotal − MemAvailable`, in whole percent. Memory available (MiB) is off by default. |
| Swap used / Swap activity | Swap fill, and KiB/s swapped in plus out. On HA OS swap is zram (compressed RAM), so steady activity means memory pressure rather than disk I/O. |
| Unwritten disk cache | MiB of dirty and writeback pages waiting to be flushed. On a slow card this piles up before stalls. |
| I/O pressure (some) | Share of wall-clock time at least one task was stalled on I/O. This is the best single "is the disk holding things up" number. The (full) variant is off by default. |
| Memory pressure (some) | The same, for waiting on memory reclaim. The (full) variant is off by default. |
| CPU pressure | Share of time runnable tasks waited for a CPU. |
| Home Assistant CPU | CPU used by the Home Assistant Core process, as a share of the whole machine. |
| Home Assistant memory | Resident memory of the Core process, in MiB. |
| Event loop lag | Worst delay Home Assistant's event loop showed during the interval, in ms. This is what you *feel* as UI sluggishness. A healthy loop stays in single digits. |
| Data disk used | Fill of the filesystem holding `/config`. Data disk free (GiB) is off by default. |
| Recorder database size | Size of `home-assistant_v2.db` plus its WAL, in MiB. Only present when using the default SQLite recorder. |
| Major page faults | Off by default. Page-ins from disk per second. |

**Disk ⟨name⟩** device, for each disk you pick

| Sensor | What it tells you |
|---|---|
| Read / Write throughput | KiB/s. |
| Busy | Percent of time the disk had a request in flight (like `iostat %util`). Near 100 % means saturated. |
| Read / Write latency | Average ms per completed request (like `iostat r_await` / `w_await`). microSD write latency in the hundreds of ms is the classic culprit. |
| Read / Write IOPS, Requests in flight | Off by default. |

**Network ⟨name⟩** device, for each interface you pick: Download and Upload in KiB/s.

Sensors that are off by default can be enabled from the entity's settings. Pressure sensors only appear if the kernel exposes PSI (HA OS does).

## Problem highlighting

Disk Wait flags values that are bad **now**, and values that are **heading somewhere bad**:

- a **Health** sensor (`OK`, `Warning` or `Problem`) on the *System performance* device, with an `issues` attribute naming what is firing. You can use it for a dashboard badge, a conditional card or an automation;
- an entry under **Settings → Repairs** for each problem, with the value, the threshold, and what to check next. Turn this off under **Configure** if you only want the sensor.

Both clear by themselves when the value recovers.

**Now** checks use the median of the last 5 minutes of readings, so a value has to be bad for most of that window, and one long stall can't raise anything. Once raised, a check only clears below 90 % of its threshold, so it doesn't flap. Checks that load naturally pushes up (loop lag, pressure, iowait, CPU, disk busy and latency) are paused for 10 minutes after Home Assistant starts, and while one of Disk Wait's own reports runs plus 2 minutes after. Anything already raised is held during the pause rather than cleared. The sensors themselves keep recording the real values.

| Check | Warning | Problem |
|---|--:|--:|
| Event loop lag | 100 ms | 500 ms |
| I/O pressure | 10 % | 25 % |
| Memory pressure | 5 % | 20 % |
| CPU pressure | 30 % | 60 % |
| CPU iowait | 20 % | 40 % |
| CPU usage | 85 % | 95 % |
| Memory used | 85 % | 95 % |
| Swap used | 50 % | 80 % |
| Data disk used | 85 % | 95 % |
| CPU temperature | 75 °C | 85 °C |
| Disk busy (per disk) | 80 % | 95 % |
| Disk write latency (per disk) | 200 ms | 1000 ms |
| Disk read latency (per disk) | 100 ms | 500 ms |

**Coming up** checks fit a straight line through up to 3 days of readings, once every 5 minutes. They only fire when there are at least 6 hours of history and the line fits well, so noise and one-off jumps are ignored. On start-up the history is reloaded from the recorder's hourly statistics, so a restart doesn't reset it.

| Forecast | Warning | Problem |
|---|---|---|
| Data disk reaches 95 % | within 14 days | within 3 days |
| Memory reaches 95 % | within 2 days | within 6 hours |
| Swap reaches 90 % | within 2 days | within 6 hours |
| Home Assistant memory keeps growing | over 150 MiB a day | |
| Recorder database keeps growing | over 100 MiB a day | |

Cost: about 20 µs per poll, plus under 1 ms for the trend fits every 5 minutes. The Health sensor and Repairs only change when a check starts, stops or changes level, so they add almost nothing to the recorder.

## On-demand reports

Two buttons on the *System performance* device, also available as actions. They're the heavy checks, so they never run on a timer, only when you press one, call the action, or write your own automation. Results appear as a notification, and the actions also return them as response data.

**Analyze recorder** (`disk_wait.recorder_report`, options `hours` and `limit`) runs one read-only query against the recorder database and shows:
- the entities writing the most state rows, with rows per day, share of the total, how often their attributes changed, and a rough size;
- the same totals grouped by integration, and the busiest event types;
- a ready-to-paste `recorder: exclude:` snippet for entities above 1,000 rows a day.

On a large SQLite database on a slow card the query can take tens of seconds. It runs in the recorder's own thread, so the UI stays responsive.

**Analyze storage** (`disk_wait.storage_report`, HA OS / Supervised only) asks the Supervisor and shows:
- the data disk breakdown you see under *Settings → System → Storage* (System, App data, Media, Share, Backups, Home Assistant), with the biggest folders in each;
- each add-on's data size, memory and CPU. Bytes written since start are shown only where the Supervisor reports them: it reads Docker's older cgroup v1 counters, which are always 0 on current HA OS;
- backups: how many there are, how much space they use on this device, the oldest and newest, and the largest.

The Supervisor measures the disk by walking the whole data partition. On a microSD card that can take minutes, so Disk Wait waits up to 10 minutes and shows a "measuring" notification in the meantime.

### What's in "System"?

*System* is whatever the Supervisor doesn't attribute: container images (including old add-on versions left behind), container logs and the systemd journal. Home Assistant Core can't see those, and Disk Wait deliberately doesn't ask for more access. To break it down, use the *Advanced SSH & Web Terminal* add-on with protection mode off:

```sh
docker system df -v | head -40      # images, containers, volumes
docker image prune -a               # remove images not used by any container
journalctl --disk-usage             # systemd journal size
du -sh /var/lib/docker/containers/*/*-json.log 2>/dev/null | sort -h | tail
ha supervisor repair                # Supervisor's own cleanup
```

Run `docker image prune -a` only if you're happy to re-download any add-on you've stopped but not uninstalled.

## Efficiency

This is designed to run forever in the background and cost close to nothing.

- **One executor job per poll.** Each poll reads about ten small files from `/proc` (kernel memory, not the disk) and does a little arithmetic. On a desktop CPU that takes about 0.3 ms. On the Green's Cortex-A55 expect a few ms once a minute, which works out to well under 0.01 % of one core. The event loop is never blocked.
- **No discovery at runtime.** Which disks, interfaces, thermal zone and pressure files exist is worked out once at setup.
- **Slow things are polled slowly.** Disk space and DB size are refreshed every 5 minutes, whatever the poll interval.
- **The lag probe is a single timer** that fires every 5 s and compares when it ran with when it was due. There's no thread and no busy-waiting.
- **Low recorder churn.** Home Assistant only writes a state row when the displayed value changes. Values are rounded to the coarsest useful step (whole percent for memory and swap, whole MiB, half degrees), there are no state attributes, and the extras are off by default. Every rate is averaged over the full interval from cumulative kernel counters, so the default 60 s interval loses resolution but never misses activity. As a rough guide, the default sensors on one disk and one interface produce at most about 33,000 small rows a day, and fewer when the system is idle. Raise the interval to cut that further.
- **No dependencies.** It's pure Python standard library, and `requirements` is empty.

## Security

- No web UI, no listening sockets, no outbound connections, no cloud. `iot_class: local_polling`.
- Read-only. It opens files under `/proc` and `/sys/class/thermal`, and calls `statvfs`/`stat` on `/config` and the recorder DB. The reports make read-only `SELECT`s and read-only Supervisor API calls using the access Home Assistant Core already has. Disk Wait never deletes, prunes or changes anything.
- It needs no add-on, no privileged access and no protection-mode changes.

## Does a custom component see the right data?

Yes. On HA OS, Home Assistant Core runs in a container on the same kernel and in the host network namespace. `/proc/stat`, `/proc/diskstats`, `/proc/meminfo`, `/proc/pressure/*` and `/proc/net/dev` are not namespaced, so they show the whole host, including the eMMC and the microSD card.

## Install

**HACS:** HACS → ⋮ → Custom repositories → add `https://github.com/GarbledMess/disk-wait` as type *Integration*. Then install *Disk Wait* and restart Home Assistant.

**Manual:** copy `custom_components/disk_wait` into `/config/custom_components/` and restart Home Assistant.

Then go to **Settings → Devices & services → Add integration → Disk Wait**. The form pre-selects the disk that holds `/config` and the interface with the default route. On a Home Assistant Green the eMMC is usually `mmcblk0` and a microSD card `mmcblk1`. Pick both to compare. You can change any of this later with **Configure**.

## Reading the numbers

Rate sensors show *unknown* for the first interval after start-up, while a baseline is taken.

A sluggish moment caused by the disk usually looks like this, all at the same time:

- Event loop lag jumps.
- I/O pressure (some) rises well above a few percent.
- Disk busy goes near 100 %.
- Write latency spikes to hundreds of ms.

If the loop lags while I/O stays quiet, compare Home Assistant CPU and CPU pressure, which point at a busy integration. Memory pressure and swap activity point at running out of RAM.

## Development

```sh
pip install pytest-homeassistant-custom-component
pytest
```
