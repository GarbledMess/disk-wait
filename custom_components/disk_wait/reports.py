"""On-demand reports: what fills the recorder, and what fills the disk.

These are the heavy checks, so nothing here runs on a timer. They run only
when someone presses a button or calls an action. Everything is read-only.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
import time
from typing import Any

from sqlalchemy import bindparam, text

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

# Rough fixed cost of one states row (columns + index entries) in SQLite.
_ROW_OVERHEAD_BYTES = 100

_Q_TOP_ENTITIES = text(
    """
    SELECT s.metadata_id, sm.entity_id, COUNT(*) AS n,
           COUNT(DISTINCT s.attributes_id) AS attr_sets
    FROM states s JOIN states_meta sm ON sm.metadata_id = s.metadata_id
    WHERE s.last_updated_ts > :since
    GROUP BY s.metadata_id, sm.entity_id
    ORDER BY n DESC
    """
)
_Q_ATTR_BYTES = text(
    """
    SELECT d.metadata_id, SUM(LENGTH(sa.shared_attrs)) AS bytes
    FROM (SELECT DISTINCT metadata_id, attributes_id FROM states
          WHERE last_updated_ts > :since AND metadata_id IN :ids) d
    JOIN state_attributes sa ON sa.attributes_id = d.attributes_id
    GROUP BY d.metadata_id
    """
).bindparams(bindparam("ids", expanding=True))
_Q_TOP_EVENTS = text(
    """
    SELECT et.event_type, COUNT(*) AS n
    FROM events e JOIN event_types et ON et.event_type_id = e.event_type_id
    WHERE e.time_fired_ts > :since
    GROUP BY et.event_type
    ORDER BY n DESC
    LIMIT :limit
    """
)
_Q_TOTAL_STATES = text("SELECT COUNT(*) FROM states")
_Q_TOTAL_STATS_META = text("SELECT COUNT(*) FROM statistics_meta")


def _mib(n: float | None) -> str:
    if n is None:
        return "?"
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GiB"
    return f"{n / 1024**2:.0f} MiB" if n >= 1024**2 else f"{n / 1024:.0f} KiB"


# ---------------------------------------------------------------- recorder


async def recorder_report(hass: HomeAssistant, hours: int, limit: int) -> dict[str, Any]:
    """Entities and event types that wrote the most rows in the last ``hours``."""
    if "recorder" not in hass.config.components:
        raise HomeAssistantError("The recorder is not running")
    from homeassistant.helpers.recorder import get_instance, session_scope  # noqa: PLC0415

    instance = get_instance(hass)
    since = time.time() - hours * 3600

    def _run() -> dict[str, Any]:
        started = time.monotonic()
        with session_scope(session=instance.get_session(), read_only=True) as session:
            entities = session.execute(_Q_TOP_ENTITIES, {"since": since}).all()
            top = entities[:limit]
            attr_bytes = {}
            if top:
                attr_bytes = dict(
                    session.execute(
                        _Q_ATTR_BYTES, {"since": since, "ids": [r.metadata_id for r in top]}
                    ).all()
                )
            events = session.execute(_Q_TOP_EVENTS, {"since": since, "limit": limit}).all()
            total_states = session.execute(_Q_TOTAL_STATES).scalar() or 0
            stats_entities = session.execute(_Q_TOTAL_STATS_META).scalar() or 0
        return {
            "all": [(r.entity_id, r.n) for r in entities],
            "top": [
                {
                    "entity_id": r.entity_id,
                    "rows": r.n,
                    "attribute_changes": r.attr_sets,
                    "est_bytes": r.n * _ROW_OVERHEAD_BYTES
                    + int(attr_bytes.get(r.metadata_id) or 0),
                }
                for r in top
            ],
            "events": [{"event_type": r.event_type, "rows": r.n} for r in events],
            "total_states": total_states,
            "stats_entities": stats_entities,
            "query_seconds": round(time.monotonic() - started, 2),
        }

    raw = await instance.async_add_executor_job(_run)

    registry = er.async_get(hass)

    def integration(entity_id: str) -> str:
        entry = registry.async_get(entity_id)
        return entry.platform if entry else f"{entity_id.split('.')[0]} (no registry entry)"

    by_integration: dict[str, int] = {}
    window_rows = 0
    for entity_id, rows in raw.pop("all"):
        window_rows += rows
        key = integration(entity_id)
        by_integration[key] = by_integration.get(key, 0) + rows
    for row in raw["top"]:
        row["integration"] = integration(row["entity_id"])
        row["rows_per_day"] = round(row["rows"] * 24 / hours)

    return {
        "hours": hours,
        "rows_in_window": window_rows,
        "rows_per_day": round(window_rows * 24 / hours),
        **raw,
        "integrations": [
            {"integration": k, "rows": v}
            for k, v in sorted(by_integration.items(), key=lambda kv: -kv[1])[:limit]
        ],
    }


def recorder_markdown(r: dict[str, Any]) -> str:
    total = r["rows_in_window"] or 1
    lines = [
        f"**{r['rows_in_window']:,} state rows in the last {r['hours']} h** "
        f"(about {r['rows_per_day']:,} a day). The database holds "
        f"{r['total_states']:,} state rows and long-term statistics for "
        f"{r['stats_entities']:,} entities. Query took {r['query_seconds']} s.",
        "",
        "### Entities writing the most",
        "| Entity | Rows/day | Share | Attr changes | Est. size |",
        "|---|--:|--:|--:|--:|",
    ]
    for t in r["top"]:
        lines.append(
            f"| `{t['entity_id']}` | {t['rows_per_day']:,} | "
            f"{100 * t['rows'] / total:.0f} % | {t['attribute_changes']:,} | {_mib(t['est_bytes'])} |"
        )
    lines += ["", "### By integration", "| Integration | Rows | Share |", "|---|--:|--:|"]
    for i in r["integrations"]:
        lines.append(f"| {i['integration']} | {i['rows']:,} | {100 * i['rows'] / total:.0f} % |")
    if r["events"]:
        lines += ["", "### Event types", "| Event | Rows |", "|---|--:|"]
        lines += [f"| `{e['event_type']}` | {e['rows']:,} |" for e in r["events"]]
    candidates = [t["entity_id"] for t in r["top"] if t["rows_per_day"] >= 1000][:10]
    if candidates:
        lines += [
            "",
            "### Exclude candidates",
            "Entities above 1,000 rows a day. Exclude only those you don't need "
            "history or graphs for. Long-term statistics stop too.",
            "```yaml",
            "recorder:",
            "  exclude:",
            "    entities:",
            *[f"      - {e}" for e in candidates],
            "```",
        ]
    lines += [
        "",
        "*Attr changes* counts distinct attribute sets, each a new row in "
        "`state_attributes`. *Est. size* is a rough guide.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- storage


_AREA_LABELS = {
    "system": "System",
    "addons_data": "App data",
    "apps_data": "App data",
    "addons_config": "App config",
    "apps_config": "App config",
    "media": "Media",
    "share": "Share",
    "backup": "Backups",
    "ssl": "SSL",
    "homeassistant": "Home Assistant config (incl. recorder DB)",
}


def _usage_tree(node: Any, names: dict[str, str]) -> dict[str, Any]:
    children = sorted(node.children or [], key=lambda c: -c.used_bytes)
    return {
        "id": node.id,
        "label": names.get(node.id) or _AREA_LABELS.get(node.id) or node.label or node.id,
        "used_bytes": node.used_bytes,
        "total_bytes": node.total_bytes,
        "children": [_usage_tree(c, names) for c in children],
    }


DISK_USAGE_TIMEOUT = 600  # seconds


async def _disk_usage(client: Any) -> Any:
    """GET host/disks/default/usage at depth 2 (per-add-on), with a long timeout."""
    from aiohttp import ClientTimeout  # noqa: PLC0415

    try:
        from aiohasupervisor.models.host import DiskUsage  # noqa: PLC0415

        raw = client.host._client  # noqa: SLF001 - public method has no timeout argument
        result = await raw.get(
            "host/disks/default/usage",
            params={"max_depth": "2"},
            timeout=ClientTimeout(total=DISK_USAGE_TIMEOUT),
        )
        return DiskUsage.from_dict(result.data)
    except (AttributeError, ImportError, TypeError):
        # Library internals changed; fall back to the public call.
        return await client.host.get_disk_usage(max_depth=2)


async def storage_report(hass: HomeAssistant, limit: int) -> dict[str, Any]:
    """Data disk breakdown, add-on data/memory/disk writes, and backups."""
    if "hassio" not in hass.config.components:
        raise HomeAssistantError("Storage report needs Home Assistant OS or Supervised")
    from aiohasupervisor import SupervisorError  # noqa: PLC0415

    from homeassistant.components.hassio import get_supervisor_client  # noqa: PLC0415

    client = get_supervisor_client(hass)
    report: dict[str, Any] = {"errors": []}
    # The Supervisor walks the whole data partition for this, which on a
    # microSD card takes far longer than the client's 10 s default timeout.
    # Start it first so the other calls overlap with it.
    disk_task = asyncio.create_task(_disk_usage(client))

    addons = []
    try:
        addons = await client.addons.list()
    except SupervisorError as err:
        report["errors"].append(f"add-on list: {err}")
    names = {a.slug: a.name for a in addons}

    try:
        usage = await disk_task
        report["disk"] = _usage_tree(usage, names)
    except TimeoutError:
        report["errors"].append(
            f"disk breakdown: the Supervisor took over {DISK_USAGE_TIMEOUT // 60} min "
            "to measure the data disk"
        )
    except SupervisorError as err:
        report["errors"].append(f"disk breakdown: {err}")
    except AttributeError:
        report["errors"].append("disk breakdown: this Home Assistant version is too old")

    data_sizes: dict[str, int] = {}
    for child in report.get("disk", {}).get("children", []):
        if child["id"] in ("addons_data", "apps_data"):
            data_sizes = {c["id"]: c["used_bytes"] for c in child["children"]}

    async def _stats(slug: str) -> Any:
        try:
            return await client.addons.addon_stats(slug)
        except SupervisorError:
            return None

    running = [a for a in addons if a.state == "started"]
    stats = dict(zip(
        [a.slug for a in running],
        await asyncio.gather(*(_stats(a.slug) for a in running)),
        strict=True,
    ))
    rows = []
    for a in addons:
        st = stats.get(a.slug)
        rows.append(
            {
                "slug": a.slug,
                "name": a.name,
                "running": st is not None,
                "data_bytes": data_sizes.get(a.slug),
                "memory_bytes": st.memory_usage if st else None,
                "cpu_percent": round(st.cpu_percent, 1) if st else None,
                "disk_written_bytes": st.blk_write if st else None,
            }
        )
    rows.sort(key=lambda r: -(r["data_bytes"] or 0))
    report["addons"] = rows[:limit] if limit else rows

    try:
        backups = await client.backups.list()
        items = []
        for b in backups:
            sizes = {loc or ".local": attrs.size_bytes for loc, attrs in b.location_attributes.items()}
            items.append(
                {
                    "name": b.name,
                    "date": b.date.isoformat() if isinstance(b.date, datetime) else str(b.date),
                    "type": str(b.type),
                    "locations": sizes,
                    "size_bytes": max(sizes.values(), default=0),
                }
            )
        items.sort(key=lambda b: b["date"])
        local = [
            b for b in items if any(loc in ("local", ".local") for loc in b["locations"])
        ]
        report["backups"] = {
            "count": len(items),
            "local_count": len(local),
            "local_bytes": sum(b["size_bytes"] for b in local),
            "oldest": items[0]["date"] if items else None,
            "newest": items[-1]["date"] if items else None,
            "largest": sorted(items, key=lambda b: -b["size_bytes"])[:5],
        }
    except SupervisorError as err:
        report["errors"].append(f"backups: {err}")

    return report


def storage_markdown(r: dict[str, Any]) -> str:
    lines: list[str] = []
    if r.get("errors"):
        lines += ["**Some parts could not be read:** " + "; ".join(r["errors"]) + ".", ""]
    disk = r.get("disk")
    if disk:
        total = disk.get("total_bytes")
        lines.append(
            f"**Data disk: {_mib(disk['used_bytes'])} used"
            + (f" of {_mib(total)}**" if total else "**")
        )
        lines += ["", "| Area | Size |", "|---|--:|"]
        for c in disk["children"]:
            lines.append(f"| {c['label']} | {_mib(c['used_bytes'])} |")
            for g in c["children"][:5]:
                lines.append(f"| &nbsp;&nbsp;↳ {g['label']} | {_mib(g['used_bytes'])} |")
    if r.get("addons"):
        no_data = "disk" not in r
        # Supervisor reads writes from Docker's cgroup v1 blkio fields; on
        # cgroup v2 hosts (current HA OS) they always come back as 0.
        writes = any(a["disk_written_bytes"] for a in r["addons"])
        lines += [
            "",
            "### Add-ons",
            "| Add-on | Data | Memory | CPU |" + (" Written since start |" if writes else ""),
            "|---|--:|--:|--:|" + ("--:|" if writes else ""),
        ]
        for a in r["addons"]:
            data = "n/a" if no_data else _mib(a["data_bytes"] or 0)
            row = (
                f"| {a['name']} | {data} | "
                f"{_mib(a['memory_bytes']) if a['running'] else 'stopped'} | "
                f"{'' if a['cpu_percent'] is None else str(a['cpu_percent']) + ' %'} |"
            )
            if writes:
                row += f" {_mib(a['disk_written_bytes']) if a['running'] else ''} |"
            lines.append(row)
        if not writes:
            lines += [
                "",
                "Per-add-on disk writes aren't shown because the Supervisor reports 0 "
                "for every add-on on this system.",
            ]
    b = r.get("backups")
    if b:
        lines += [
            "",
            f"### Backups: {b['count']} total, {b['local_count']} on this device "
            f"using {_mib(b['local_bytes'])}",
        ]
        if b["oldest"]:
            lines.append(f"Oldest {b['oldest'][:10]}, newest {b['newest'][:10]}.")
        if b["largest"]:
            lines += ["", "| Largest | Date | Size | Where |", "|---|---|--:|---|"]
            for x in b["largest"]:
                lines.append(
                    f"| {x['name']} | {x['date'][:10]} | {_mib(x['size_bytes'])} | "
                    f"{', '.join(x['locations'])} |"
                )
    lines += [
        "",
        "**System** is everything the Supervisor doesn't attribute: container "
        "images (including old add-on versions), container logs and the systemd "
        "journal. Home Assistant can't look inside it. See the Disk Wait README "
        "for the SSH commands that break it down.",
    ]
    return "\n".join(lines)
