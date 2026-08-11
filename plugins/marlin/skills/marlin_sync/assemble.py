#!/usr/bin/env python3
"""The assemble pipeline: mechanical landscape construction.

--gate prints a cheap JSON verdict on whether time alone has made the written
landscape wrong (pure, stdout, writes nothing) — run it on every poll, including
the quiet ones where sync reports no new signals;
--pre prints the LLM-facing working set (pure, stdout, writes nothing);
--finish <draft> validates the membership draft and emits the ordered v3
skeleton with named prose slots; --finish <draft> --prose <map> injects the
prose and writes marlin_landscape.json. The model's entire output is the
draft and the prose map — every structured write happens here, in code.
The pure computation functions at the top are individually unit-tested.

The finish step also applies the S13 ack rules, in a fixed order: suppress
`handled`/`expired` from the action surfaces, then sort, then cap. Theme
membership is never filtered by ack state.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ack import (
    annotate_ack_statuses,
    effective_statuses,
    expired_tail_already_shown,
    load_ack_store,
    pending_transitions,
    persist_effective_transitions,
)
from inspect import (
    _deadline_radar,
    _load_archive,
    _recent_archive_months,
    entities_to_watch,
    urgent_top,
)
from params import (
    ACK_FINAL_NAG_DAYS,
    ARCHIVE_LOOKBACK_DAYS,
    CLUSTER_ARCHIVED_SAMPLE,
    CLUSTER_ENTITY_OVERLAP_MIN,
    MANDATORY_URGENT_DAYS,
    NOTABLE_CAP,
    RESEMBLES_MIN_OVERLAP,
    RADAR_HORIZON_DAYS,
    RENAME_TURNOVER,
    THEME_BIRTH_MIN,
    THEME_FADING_DAYS,
    THEME_RETIRE_DAYS,
    TREND_EMERGING_DAYS,
)

_CROCKFORD32 = {char: index for index, char in enumerate("0123456789ABCDEFGHJKMNPQRSTVWXYZ")}


def ulid_created_at(signal_id: object) -> datetime | None:
    """Return a signal ULID's UTC creation time without a third-party package."""
    if not isinstance(signal_id, str):
        return None
    raw = signal_id.removeprefix("sig_").upper()
    if len(raw) != 26:
        return None
    try:
        millis = 0
        for char in raw[:10]:
            millis = (millis << 5) | _CROCKFORD32[char]
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
    except (KeyError, OSError, OverflowError, ValueError):
        return None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def signal_entry(signal: dict) -> dict:
    """Return the pinned triage-weight signal entry used by assemble data."""
    created = ulid_created_at(signal.get("id"))
    return {
        "id": signal.get("id"),
        "channel": signal.get("channel"),
        "handling": signal.get("handling"),
        "importance": signal.get("importance"),
        "signal_type": signal.get("signal_type"),
        "title": signal.get("title"),
        "entity_tags": list(signal.get("entity_tags") or []),
        "updated_seq": signal.get("updated_seq"),
        "deadline_at": signal.get("deadline_at"),
        # The lifecycle clock, exposed so the 14-day retire rule and trend are
        # predictable from the working set instead of only trippable later.
        "created_at": created.date().isoformat() if created else None,
        "annotations": dict(signal.get("annotations") or {}),
    }


def prior_since(prior_landscape: dict | None) -> str | int | None:
    """Use a prior snapshot's timestamp, falling back to its sequence watermark."""
    if not prior_landscape:
        return None
    return prior_landscape.get("as_of") or prior_landscape.get("updated_through_seq")


def _referenced_ids(section: dict) -> set[str]:
    ids: set[str] = set()
    for theme in section.get("active_themes") or []:
        ids.update(sid for sid in theme.get("signal_ids") or [] if isinstance(sid, str))
    for urgent in section.get("urgent_signals") or []:
        if isinstance(urgent.get("id"), str):
            ids.add(urgent["id"])
    for notable in section.get("notable_signals") or []:
        if isinstance(notable.get("id"), str):
            ids.add(notable["id"])
    for entity in section.get("entities_to_watch") or []:
        ids.update(sid for sid in entity.get("signal_ids") or [] if isinstance(sid, str))
    return ids


def build_carryover(prior_landscape: dict | None, state_signals: list[dict]) -> dict:
    """Map prior themes to active state members and record every carried-id drop."""
    prior_landscape = prior_landscape or {}
    by_id = {signal.get("id"): signal for signal in state_signals if signal.get("id")}
    carryover: dict[str, list[dict]] = {}
    drops: list[dict] = []
    drop_set: set[str] = set()
    for channel, section in (prior_landscape.get("channels") or {}).items():
        themes: list[dict] = []
        for theme in section.get("active_themes") or []:
            members: list[str] = []
            for signal_id in theme.get("signal_ids") or []:
                signal = by_id.get(signal_id)
                if signal is not None and signal.get("status", "active") == "active":
                    members.append(signal_id)
                    continue
                reason = "superseded" if signal is not None else "trimmed"
                drops.append({"id": signal_id, "theme": theme.get("theme"), "channel": channel, "reason": reason})
                drop_set.add(signal_id)
            migrated_v2 = "named_member_ids" not in theme
            themes.append(
                {
                    "theme": theme.get("theme"),
                    "signal_ids": members,
                    "named_member_ids": list(members) if migrated_v2 else list(theme.get("named_member_ids") or []),
                    "migrated_v2": migrated_v2,
                }
            )
        carryover[channel] = themes
    return {"carryover": carryover, "drops": drops, "drop_set": drop_set, "since": prior_since(prior_landscape)}


def tag_new_vs_updated(
    delivered_signals: list[dict], known_ids: set[str], baseline: str | datetime | None
) -> dict[str, list[dict]]:
    """Split delivered records into first-seen and re-delivered records.

    A known ID is re-delivered.  When local known-ID state was trimmed, a ULID
    created before the prior snapshot's time is also necessarily a re-delivery.
    """
    baseline_time = baseline if isinstance(baseline, datetime) else _parse_time(baseline)
    result = {"new_signals": [], "updated_signals": []}
    for signal in delivered_signals:
        signal_id = signal.get("id")
        created = ulid_created_at(signal_id)
        is_updated = signal_id in known_ids or (
            baseline_time is not None and created is not None and created < baseline_time
        )
        result["updated_signals" if is_updated else "new_signals"].append(signal)
    return result


def _months_between(start: datetime, end: datetime) -> set[str]:
    months: set[str] = set()
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.add(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def read_archive_slice(
    archive_dir: Path, now: datetime, lookback_days: int = ARCHIVE_LOOKBACK_DAYS
) -> tuple[list[dict], int]:
    """Read, de-duplicate, and date-bound monthly JSONL archive records."""
    now = now.astimezone(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    if not archive_dir.is_dir():
        return [], 0
    by_id: dict[str, dict] = {}
    skipped = 0
    for path in sorted(archive_dir.glob("*.jsonl")):
        if path.stem not in _months_between(cutoff, now):
            continue
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            signal_id = entry.get("id")
            archived_at = _parse_time(entry.get("archived_at"))
            if not isinstance(signal_id, str) or archived_at is None:
                skipped += 1
                continue
            # The lookback bounds signal *creation* time (ULID), not eviction
            # time: a post-gap resync archives hundreds of old signals "today",
            # and none of them are current-awareness material. archived_at is
            # only the fallback clock when the id isn't a parseable ULID.
            created = ulid_created_at(signal_id) or archived_at
            if created < cutoff or created > now:
                continue
            existing = by_id.get(signal_id)
            if existing is None or entry.get("archived_at", "") > existing.get("archived_at", ""):
                by_id[signal_id] = entry
    return list(by_id.values()), skipped


def unthemed_pool(
    state_signals: list[dict], archive_signals: list[dict], assigned_ids: set[str]
) -> tuple[list[dict], list[dict]]:
    """Return unassigned active window and archive records, with state winning on ID."""
    state_ids = {signal.get("id") for signal in state_signals if signal.get("id")}
    window = [
        signal for signal in state_signals
        if signal.get("id") not in assigned_ids and signal.get("status", "active") == "active"
    ]
    archived = [
        signal for signal in archive_signals
        if signal.get("id") not in assigned_ids
        and signal.get("id") not in state_ids
        and signal.get("status", "active") == "active"
    ]
    return window, archived


def _comparison_keys(signal: dict) -> set[str]:
    family = (signal.get("annotations") or {}).get("family")
    if isinstance(family, str) and family.strip():
        return {f"family:{family.strip()}"}
    if isinstance(family, list) and family:
        return {f"family:{item}" for item in family if isinstance(item, str) and item}
    return {f"tag:{tag}" for tag in signal.get("entity_tags") or [] if isinstance(tag, str) and tag}


def _display_keys(keys: set[str]) -> list[str]:
    return sorted(key.split(":", 1)[1] for key in keys)


def compute_prior_support(archived_members: list[dict]) -> dict | None:
    """Describe archived support precisely enough for later local recovery."""
    if not archived_members:
        return None
    ordered = sorted(
        archived_members,
        key=lambda signal: (ulid_created_at(signal.get("id")) or datetime.max.replace(tzinfo=timezone.utc), signal.get("id", "")),
    )
    created = [ulid_created_at(signal.get("id")) for signal in ordered]
    fallback = min((signal.get("archived_at") for signal in ordered if signal.get("archived_at")), default=None)
    return {
        "count": len(ordered),
        "since": _iso(next((value for value in created if value is not None), None)) or fallback,
        "ids": [signal.get("id") for signal in ordered],
    }


def cluster_candidates(
    window_signals: list[dict],
    archive_signals: list[dict],
    existing_themes: dict[str, list[dict]] | None = None,
    min_overlap: int = CLUSTER_ENTITY_OVERLAP_MIN,
    birth_min: int = THEME_BIRTH_MIN,
) -> list[dict]:
    """Find same-channel/type connected components over family-or-tag overlap."""
    existing_themes = existing_themes or {}
    combined = [(signal, "in-window") for signal in window_signals] + [(signal, "archived") for signal in archive_signals]
    grouped: dict[tuple[str, str], list[tuple[dict, str]]] = {}
    for item in combined:
        signal = item[0]
        grouped.setdefault((str(signal.get("channel", "")), str(signal.get("signal_type", ""))), []).append(item)
    candidates: list[dict] = []
    for (channel, signal_type), group in grouped.items():
        parent = list(range(len(group)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def join(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        keys = [_comparison_keys(signal) for signal, _origin in group]
        for left in range(len(group)):
            for right in range(left + 1, len(group)):
                if len(keys[left] & keys[right]) >= min_overlap:
                    join(left, right)
        components: dict[int, list[int]] = {}
        for index in range(len(group)):
            components.setdefault(find(index), []).append(index)
        for indexes in components.values():
            if len(indexes) < birth_min:
                continue
            members = [group[index] for index in indexes]
            archived_members = [signal for signal, origin in members if origin == "archived"]
            window_member_ids = [signal.get("id") for signal, origin in members if origin == "in-window"]
            # A candidate with no in-window member is un-promotable by
            # construction (drafts may only reference in-window ids) — don't
            # emit noise the agent cannot act on.
            if not window_member_ids:
                continue
            counts: dict[str, int] = {}
            for index in indexes:
                for key in keys[index]:
                    counts[key] = counts.get(key, 0) + 1
            shared = {key for key, count in counts.items() if count >= 2}
            # Members are slim on purpose ({id, title, origin}): in-window
            # entries already appear in full in the unthemed/new sections.
            # Archived members are SAMPLED — they aren't draftable, so a few
            # titles convey the storyline's history; prior_support carries the
            # complete count and ids. (A steady-state archive holds many
            # recent-created evictions; listing them all bloated --pre.)
            window_entries = [(signal, origin) for signal, origin in members if origin == "in-window"]
            archived_entries = [(signal, origin) for signal, origin in members if origin == "archived"]
            candidate = {
                "channel": channel,
                "signal_type": signal_type,
                "members": [
                    {"id": signal.get("id"), "title": signal.get("title"), "origin": origin}
                    for signal, origin in window_entries + archived_entries[:CLUSTER_ARCHIVED_SAMPLE]
                ],
                "in_window_member_ids": window_member_ids,
                "shared_entities": _display_keys(shared),
            }
            support = compute_prior_support(archived_members)
            if support is not None:
                candidate["prior_support"] = support
            candidate_keys = set().union(*(keys[index] for index in indexes))
            resembles: list[tuple[int, str]] = []
            for theme in existing_themes.get(channel, []):
                theme_keys: set[str] = set()
                for signal in theme.get("members") or []:
                    theme_keys.update(_comparison_keys(signal))
                overlap = len(candidate_keys & theme_keys)
                # A stricter bar than cluster edges: one shared entity is how a
                # single over-tagged signal poisons every hint in a channel
                # (observed live) — resemblance needs corroboration.
                if overlap >= RESEMBLES_MIN_OVERLAP:
                    resembles.append((overlap, str(theme.get("theme", ""))))
            if resembles:
                candidate["resembles"] = max(resembles, key=lambda item: (item[0], item[1]))[1]
            candidates.append(candidate)
    return sorted(candidates, key=lambda item: (item["channel"], item["signal_type"], item["in_window_member_ids"]))


def compute_lifecycle(
    themes_by_channel: dict[str, list[dict]], all_signals: list[dict], now: datetime
) -> dict:
    """Compute trend, retirement, and set-based rename eligibility for themes."""
    now = now.astimezone(timezone.utc)
    by_id = {signal.get("id"): signal for signal in all_signals if signal.get("id")}
    lifecycle: dict[str, dict[str, dict]] = {}
    retirements: list[dict] = []
    for channel, themes in themes_by_channel.items():
        channel_result: dict[str, dict] = {}
        for theme in themes:
            current = set(theme.get("signal_ids") or [])
            named = set(theme.get("named_member_ids") or [])
            historical_ids = current | set((theme.get("prior_support") or {}).get("ids") or [])
            times = [ulid_created_at(signal_id) for signal_id in historical_ids if signal_id in by_id]
            times = [value for value in times if value is not None]
            oldest, newest = (min(times), max(times)) if times else (None, None)
            age_since_newest = (now - newest).days if newest is not None else None
            if oldest is not None and (now - oldest).days <= TREND_EMERGING_DAYS:
                trend = "emerging"
            elif age_since_newest is not None and age_since_newest > THEME_FADING_DAYS:
                trend = "fading"
            else:
                trend = "stable"
            added, departed = current - named, named - current
            denominator = named | current
            turnover = len(added | departed) / len(denominator) if denominator else 0.0
            migrated = bool(theme.get("migrated_v2"))
            item = {
                "trend": trend,
                "retired": age_since_newest is not None and age_since_newest > THEME_RETIRE_DAYS,
                # An empty naming baseline means turnover is unmeasurable (it
                # reads 1.0 vacuously) — never flag those; the write step
                # re-stamps them so the next run measures for real.
                "rename_eligible": False if (migrated or not named) else turnover > RENAME_TURNOVER,
                "turnover": turnover,
                "added_member_ids": sorted(added),
                "departed_member_ids": sorted(departed),
            }
            channel_result[str(theme.get("theme"))] = item
            if item["retired"]:
                retirements.append({"channel": channel, "theme": theme.get("theme")})
        lifecycle[channel] = channel_result
    return {"channels": lifecycle, "retirements": retirements}


def select_notables(unthemed_signals: list[dict], urgent_ids: set[str], cap: int = NOTABLE_CAP) -> list[dict]:
    """Select brief-only, non-urgent unthemed records in deterministic order."""
    candidates = [
        signal for signal in unthemed_signals
        if signal.get("id") not in urgent_ids
        and signal.get("handling") == "brief"
        and signal.get("status", "active") == "active"
    ]
    return sorted(
        candidates,
        key=lambda signal: (float(signal.get("importance") or 0), signal.get("updated_seq", 0)),
        reverse=True,
    )[:cap]


def delta_precompute(
    prior_landscape: dict | None,
    state_signals: list[dict],
    current_themes: dict[str, list[dict]] | None = None,
) -> dict:
    """Precompute per-channel signal additions, drops, and theme rank movement."""
    prior_landscape = prior_landscape or {}
    current_themes = current_themes or {}
    prior_channels = prior_landscape.get("channels") or {}
    active_by_channel: dict[str, set[str]] = {}
    for signal in state_signals:
        if signal.get("id") and signal.get("status", "active") == "active":
            active_by_channel.setdefault(str(signal.get("channel")), set()).add(signal["id"])
    channels = set(prior_channels) | set(active_by_channel) | set(current_themes)
    output: dict[str, dict] = {}
    for channel in sorted(channels):
        prior_section = prior_channels.get(channel) or {}
        prior_ids = _referenced_ids(prior_section)
        current_ids = active_by_channel.get(channel, set())
        old_ranks = {theme.get("theme"): index + 1 for index, theme in enumerate(prior_section.get("active_themes") or [])}
        new_ranks = {theme.get("theme"): index + 1 for index, theme in enumerate(current_themes.get(channel, []))}
        rank_changes = [
            {"theme": name, "from_rank": old_ranks[name], "to_rank": new_ranks[name]}
            for name in sorted(set(old_ranks) & set(new_ranks))
            if old_ranks[name] != new_ranks[name]
        ]
        output[channel] = {
            "added_signal_ids": sorted(current_ids - prior_ids),
            "dropped_signal_ids": sorted(prior_ids - current_ids),
            "theme_rank_changes": rank_changes,
        }
    return {"since": prior_since(prior_landscape), "channels": output}


def urgent_candidates(state_signals: list[dict]) -> dict[str, list[dict]]:
    """Reuse inspect.py's active urgent selection for assemble's working set."""
    return urgent_top(state_signals)


def deadline_hits(state_signals: list[dict], archive_signals: list[dict], now: datetime) -> list[dict]:
    """Reuse inspect.py's deadline radar, including state-over-archive de-duplication."""
    return _deadline_radar(state_signals, archive_signals, now, RADAR_HORIZON_DAYS)


def deadline_archive(now: datetime) -> list[dict]:
    """Read the archive coverage the DEADLINE radar contract requires.

    Deliberately not `read_archive_slice`: that slice bounds by ULID *creation*
    time (ARCHIVE_LOOKBACK_DAYS), which is right for birth clustering and wrong
    for deadlines. A signal created 30 days ago, evicted today, and due in two
    days is a live obligation, and bounding by creation age hid it from the
    mandatory-urgent rule and from its own once-only expiry tail — while
    `inspect.py --deadlines` showed it. This restores the radar's own bound
    (current + prior month's files), so the two agree.
    """
    archived, _skipped = _load_archive(only_months=_recent_archive_months(now))
    return archived


def mandatory_deadline_records(radar: list[dict], ack_statuses: dict[str, str]) -> list[dict]:
    """Return the radar hits the finish step forces into `urgent_signals`.

    One rule, one implementation — `--finish` applies it and `--gate` asks
    whether applying it would change anything.
    """
    selected: list[dict] = []
    for record in radar:
        status = ack_statuses.get(record["signal"].get("id"), "open")
        # Forward band only: a just-passed deadline (the radar's tail) is
        # awareness, not a mandatory urgent — the design's rule is "upcoming
        # within the band", so bound below at 0.
        if not (0 <= record["days_until"] <= MANDATORY_URGENT_DAYS):
            continue
        if status in ("handled", "expired"):
            continue
        # Final nag: an acknowledged obligation stops being forced urgent until
        # it is imminent, then returns in every run's urgent list until it is
        # handled or expires.
        if status == "acknowledged" and record["days_until"] > ACK_FINAL_NAG_DAYS:
            continue
        selected.append(record)
    return selected


STATE_DIR = Path(os.environ.get("MARLIN_STATE_DIR") or (Path.home() / ".marlin")).expanduser()
STATE_PATH = STATE_DIR / "marlin_state.json"
LANDSCAPE_PATH = STATE_DIR / "marlin_landscape.json"
# The true prior for this window, preserved across the validate-fix loop: a
# re-run of the write step must not treat this run's own output as the prior
# (that collapses the delta baseline onto itself).
PRIOR_BACKUP_PATH = STATE_DIR / "marlin_landscape.prior.json"
ARCHIVE_DIR = STATE_DIR / "marlin_archive"


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON via a same-directory temp file + os.replace (crash-safe)."""
    temp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        with temp.open("w") as handle:
            handle.write(json.dumps(payload, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise ValueError(f"could not write {path}: {exc}") from exc


def _load_json(path: Path, *, required: bool) -> dict | None:
    if not path.exists():
        if required:
            raise ValueError(f"{path} not found")
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _active(signals: list[dict]) -> list[dict]:
    return [signal for signal in signals if signal.get("status", "active") == "active"]


def _channels(*groups: object) -> list[str]:
    values: set[str] = set()
    for group in groups:
        if isinstance(group, dict):
            values.update(str(key) for key in group)
        elif isinstance(group, list):
            values.update(str(item.get("channel")) for item in group if item.get("channel") is not None)
    return sorted(values)


def _grounded_entry(signal: dict) -> dict:
    entry = signal_entry(signal)
    entry["what_changed"] = signal.get("what_changed")
    entry["why_it_matters"] = signal.get("why_it_matters")
    return entry


def _carried_with_titles(themes: list[dict], by_id: dict[str, dict]) -> list[dict]:
    """Attach `{id, title}` members to carried themes (I22b).

    Cluster candidates already ship titles, so judging a proposed birth is a
    single read while judging a thin carried theme required a per-id drill.
    `signal_ids` stays as the draftable list; `members` is the readable one.
    """
    return [
        dict(
            theme,
            members=[
                {"id": sid, "title": (by_id.get(sid) or {}).get("title")}
                for sid in theme.get("signal_ids") or []
            ],
        )
        for theme in themes
    ]


def build_pre(state: dict, prior: dict | None, now: datetime) -> dict:
    """Build the read-only, LLM-facing working set for the membership turn."""
    # Acks are annotations on the working set, never a filter in this stage.
    # This read-only path deliberately does not persist a stale/expired result.
    raw_state = list(state.get("signals") or [])
    ack_store = load_ack_store()
    radar_archive = deadline_archive(now)
    ack_statuses = effective_statuses(ack_store, raw_state, now, radar_archive)
    all_state = annotate_ack_statuses(raw_state, ack_statuses)
    active = _active(all_state)
    archive, skipped = read_archive_slice(ARCHIVE_DIR, now)
    archive = annotate_ack_statuses(archive, ack_statuses)
    radar_archive = annotate_ack_statuses(radar_archive, ack_statuses)
    carry = build_carryover(prior, all_state)
    prior_themes = carry["carryover"]
    lifecycle = compute_lifecycle(prior_themes, all_state + archive, now)
    assigned = {
        sid for themes in prior_themes.values() for theme in themes
        for sid in theme.get("signal_ids") or []
    }
    window_unthemed, archive_unthemed = unthemed_pool(active, archive, assigned)
    existing = {
        channel: [
            {"theme": theme.get("theme"), "members": [
                next((signal for signal in active if signal.get("id") == sid), {})
                for sid in theme.get("signal_ids") or []
            ]}
            for theme in themes
        ]
        for channel, themes in prior_themes.items()
    }
    clusters = cluster_candidates(window_unthemed, archive_unthemed, existing)
    tagged = tag_new_vs_updated(active, _referenced_ids_all(prior), prior_since(prior))
    urgents = urgent_candidates(active)
    # The expired passed-tail is a once-only notice: after the write step has
    # logged the expiry, repeating it for the rest of DEADLINE_TAIL_DAYS would
    # make expiry itself re-nag for a week.
    tail_shown = expired_tail_already_shown(ack_store, ack_statuses, STATE_DIR)
    radar = [
        record for record in deadline_hits(all_state, radar_archive, now)
        if not (record["days_until"] < 0 and record["signal"].get("id") in tail_shown)
    ]
    delta = delta_precompute(prior, active, prior_themes)
    state_by_id = {signal.get("id"): signal for signal in all_state if signal.get("id")}
    prior_channels = (prior or {}).get("channels") or {}
    channels = _channels(active, prior_channels, prior_themes)
    result_channels: dict[str, dict] = {}
    for channel in channels:
        drops = [drop for drop in carry["drops"] if drop.get("channel") == channel]
        channel_life = lifecycle["channels"].get(channel, {})
        result_channels[channel] = {
            "carryover": {"themes": _carried_with_titles(prior_themes.get(channel, []), state_by_id), "drops": drops},
            "retirements_to_review": [
                item for item in lifecycle["retirements"] if item.get("channel") == channel
            ],
            "new_signals": [signal_entry(signal) for signal in tagged["new_signals"] if signal.get("channel") == channel],
            "updated_signals": [signal_entry(signal) for signal in tagged["updated_signals"] if signal.get("channel") == channel],
            "unthemed": [signal_entry(signal) for signal in window_unthemed if signal.get("channel") == channel],
            "cluster_candidates": [candidate for candidate in clusters if candidate.get("channel") == channel],
            "urgent_candidates": [_grounded_entry(signal) for signal in urgents.get(channel, [])],
            "deadline_hits": [
                dict(_grounded_entry(record["signal"]), date=record["deadline_date"].isoformat(), origin=record["origin"])
                for record in radar if record["signal"].get("channel") == channel
            ],
            "lifecycle": {
                "themes": channel_life,
                "fading": [name for name, item in channel_life.items() if item.get("trend") == "fading"],
                "retire": [name for name, item in channel_life.items() if item.get("retired")],
                "rename_eligible": [name for name, item in channel_life.items() if item.get("rename_eligible")],
            },
            "delta_precompute": delta["channels"].get(channel, {"added_signal_ids": [], "dropped_signal_ids": [], "theme_rank_changes": []}),
        }
    result = {
        "as_of": _iso(now), "cursor": state.get("cursor"),
        "updated_through_seq": max((signal.get("updated_seq", 0) for signal in all_state), default=0),
        "channels": result_channels,
    }
    if skipped:
        result["archive_skipped_lines"] = skipped
    return result


def _referenced_ids_all(landscape: dict | None) -> set[str]:
    ids: set[str] = set()
    for section in ((landscape or {}).get("channels") or {}).values():
        ids.update(_referenced_ids(section))
    return ids


def _theme_key_for_ids(ids: list[str], by_id: dict[str, dict]) -> tuple[float, int, int]:
    members = [by_id[sid] for sid in ids if sid in by_id]
    return (
        max((float(item.get("importance") or 0) for item in members), default=0),
        len(ids), max((item.get("updated_seq", 0) for item in members), default=0),
    )


def _computed_trend(ids: list[str], support_ids: list[str], now: datetime) -> str:
    """Apply the lifecycle age rule to the final, not merely carried, members."""
    times = [ulid_created_at(sid) for sid in list(ids) + list(support_ids)]
    times = [value for value in times if value is not None]
    if not times:
        return "stable"
    if (now - min(times)).days <= TREND_EMERGING_DAYS:
        return "emerging"
    if (now - max(times)).days > THEME_FADING_DAYS:
        return "fading"
    return "stable"


def _draft_themes(draft: dict, active_by_id: dict[str, dict], errors: list[str]) -> dict[str, list[dict]]:
    """Parse the draft, collecting every violation so one round-trip fixes all."""
    if not isinstance(draft, dict):
        raise ValueError("membership draft must be a JSON object")
    result: dict[str, list[dict]] = {}
    for channel, raw_themes in draft.items():
        if not isinstance(raw_themes, list):
            raise ValueError(f"draft channel {channel!r} must be an array")
        seen: set[str] = set()
        themes: list[dict] = []
        for raw in raw_themes:
            if not isinstance(raw, dict) or not isinstance(raw.get("theme"), str) or not raw["theme"].strip():
                errors.append(f"draft channel {channel!r} has a theme without a non-empty name")
                continue
            ids = raw.get("signal_ids")
            if not isinstance(ids, list) or not all(isinstance(sid, str) for sid in ids):
                errors.append(f"theme {raw['theme']!r} must have a signal_ids array")
                continue
            if not ids:
                errors.append(f"theme {raw['theme']!r} has no members — omit empty themes from the draft")
                continue
            for sid in ids:
                signal = active_by_id.get(sid)
                if signal is None:
                    errors.append(f"theme {raw['theme']!r} references unknown or retired signal id {sid!r}")
                elif signal.get("channel") != channel:
                    errors.append(f"theme {raw['theme']!r} places {sid!r} in {channel!r}, but it belongs to {signal.get('channel')!r}")
                elif sid in seen:
                    errors.append(f"theme exclusivity violation in {channel!r}: {sid!r} appears twice")
                seen.add(sid)
            themes.append({"theme": raw["theme"], "signal_ids": ids})
        result[str(channel)] = themes
    return result


def _prior_theme_for_overlap(theme: dict, prior_themes: list[dict]) -> dict | None:
    current = set(theme.get("signal_ids") or [])
    matches = []
    for prior in prior_themes:
        old = set(prior.get("signal_ids") or [])
        # A proposed name is a rename when a majority of *its new membership*
        # carries forward from one prior theme.  The eligibility flag separately
        # measures turnover against that prior theme's naming baseline.
        if old and current and len(current & old) / len(current) > 0.5:
            matches.append(prior)
    return max(matches, key=lambda item: len(current & set(item.get("signal_ids") or [])), default=None)


def _final_delta(prior: dict, channels: dict[str, dict]) -> dict:
    prior_channels = prior.get("channels") or {}
    output: dict[str, dict] = {}
    for channel in sorted(set(prior_channels) | set(channels)):
        old = prior_channels.get(channel) or {}
        new = channels.get(channel) or {}
        old_ids, new_ids = _referenced_ids(old), _referenced_ids(new)
        old_ranks = {theme.get("theme"): index + 1 for index, theme in enumerate(old.get("active_themes") or [])}
        new_ranks = {theme.get("theme"): index + 1 for index, theme in enumerate(new.get("active_themes") or [])}
        output[channel] = {
            "added_signal_ids": sorted(new_ids - old_ids),
            "dropped_signal_ids": sorted(old_ids - new_ids),
            "theme_rank_changes": [
                {"theme": name, "from_rank": old_ranks[name], "to_rank": new_ranks[name]}
                for name in sorted(set(old_ranks) & set(new_ranks)) if old_ranks[name] != new_ranks[name]
            ],
        }
    return {"since": prior_since(prior), "channels": output}


def _theme_set_turned_over(prior_themes: list[dict], final_themes: list[dict]) -> bool:
    """Did this channel's theme set change materially since the prior landscape?

    The predicate is deliberately blunt and mechanical (I22a): it is true when a
    theme name appears or disappears — a birth, a death, or a rename — or when a
    surviving theme LOST a member. A theme that only gained members is ordinary
    accretion, and a summary written last run usually still reads true, so pure
    additions do not raise the flag.
    """
    prior_members = {str(theme.get("theme")): set(theme.get("signal_ids") or []) for theme in prior_themes}
    final_members = {str(theme.get("theme")): set(theme.get("signal_ids") or []) for theme in final_themes}
    if set(prior_members) != set(final_members):
        return True
    return any(prior_members[name] - final_members[name] for name in prior_members)


def build_finish(state: dict, prior: dict | None, draft: dict, now: datetime) -> tuple[dict, list[dict]]:
    """Return the v3 skeleton and named prose slots, without writing a file."""
    all_state, active = list(state.get("signals") or []), _active(list(state.get("signals") or []))
    active_by_id = {signal.get("id"): signal for signal in active if signal.get("id")}
    errors: list[str] = []
    drafted = _draft_themes(draft, active_by_id, errors)
    archive, _skipped = read_archive_slice(ARCHIVE_DIR, now)
    carry = build_carryover(prior, all_state)
    lifecycle = compute_lifecycle(carry["carryover"], all_state + archive, now)["channels"]
    carried_ids = {
        sid for themes in carry["carryover"].values() for theme in themes
        for sid in theme.get("signal_ids") or []
    }
    cluster_window, cluster_archive = unthemed_pool(active, archive, carried_ids)
    cluster_support = cluster_candidates(cluster_window, cluster_archive)
    prior_channels = (prior or {}).get("channels") or {}
    radar_archive = deadline_archive(now)
    radar = deadline_hits(all_state, radar_archive, now)

    # --- ack suppression, applied before sorting and before any cap ---
    # `handled` and `expired` leave the action surfaces entirely; `acknowledged`
    # stays, keeps its marker, and still occupies a slot under the urgent cap.
    # Theme membership is never touched by ack state — themes record the state
    # of the world, acks record the user's relationship to it.
    ack_statuses = effective_statuses(load_ack_store(), all_state, now, radar_archive)
    suppressed = {sid for sid, status in ack_statuses.items() if status in ("handled", "expired")}
    surfaced = [signal for signal in active if signal.get("id") not in suppressed]
    ordinary_urgent = urgent_candidates(surfaced)

    mandatory_by_channel: dict[str, list[dict]] = {}
    for record in mandatory_deadline_records(radar, ack_statuses):
        mandatory_by_channel.setdefault(str(record["signal"].get("channel")), []).append(record)

    # I17: the finish key set is "channels with something to say" — active
    # signals, drafted themes, or a forced deadline whose signal already left
    # the window. Unioning in the PRIOR landscape's keys (as this did) made an
    # emptied channel self-perpetuate with model-written padding forever.
    # `--pre` still unions them: it needs prior keys to compute carryover/drops.
    drafted_with_themes = {channel: themes for channel, themes in drafted.items() if themes}
    channels = _channels(
        active,
        drafted_with_themes,
        [record["signal"] for records in mandatory_by_channel.values() for record in records],
    )
    sections: dict[str, dict] = {}
    slots: list[dict] = []
    for channel in channels:
        prior_section = prior_channels.get(channel) or {}
        prior_themes = prior_section.get("active_themes") or []
        carried_themes = carry["carryover"].get(channel, [])
        finalized_themes = []
        for theme in drafted.get(channel, []):
            old_same = next((old for old in prior_themes if old.get("theme") == theme["theme"]), None)
            carried_same = next((old for old in carried_themes if old.get("theme") == theme["theme"]), None)
            overlap = None if old_same else _prior_theme_for_overlap(theme, prior_themes)
            life = lifecycle.get(channel, {})
            if overlap is not None:
                eligible = life.get(str(overlap.get("theme")), {}).get("rename_eligible", False)
                if not eligible:
                    errors.append(f"theme {theme['theme']!r} renames prior theme {overlap.get('theme')!r}, which is not rename_eligible")
                    continue
            source = old_same or overlap
            baseline = carried_same if old_same is not None else source
            item = {
                "theme": theme["theme"],
                "trend": "stable",
                "signal_ids": list(theme["signal_ids"]),
                "named_member_ids": list(theme["signal_ids"]) if (overlap is not None or source is None or not (baseline or {}).get("named_member_ids")) else list(baseline["named_member_ids"]),
            }
            if source and source.get("prior_support"):
                item["prior_support"] = source["prior_support"]
            if "prior_support" not in item:
                supporting = [
                    candidate for candidate in cluster_support
                    if candidate.get("channel") == channel
                    and candidate.get("prior_support")
                    and set(candidate.get("in_window_member_ids") or []) <= set(theme["signal_ids"])
                ]
                if supporting:
                    item["prior_support"] = max(
                        supporting, key=lambda candidate: len(candidate.get("in_window_member_ids") or [])
                    )["prior_support"]
            item["trend"] = _computed_trend(
                item["signal_ids"], (item.get("prior_support") or {}).get("ids") or [], now
            )
            # Inactivity death is enforced here, not by prose: a drafted theme
            # whose newest member is older than the retire window is dead —
            # placing a new signal into it is what legitimately revives it.
            member_times = [t for t in (ulid_created_at(sid) for sid in item["signal_ids"]) if t is not None]
            if member_times and (now - max(member_times)).days > THEME_RETIRE_DAYS:
                errors.append(
                    f"theme {theme['theme']!r} is retired (no member newer than "
                    f"{THEME_RETIRE_DAYS} days; newest is {max(member_times).date().isoformat()}) "
                    f"— remove it from the draft"
                )
                continue
            if overlap is not None:
                item["formerly"] = list(overlap.get("formerly") or []) + [overlap.get("theme")]
            finalized_themes.append(item)
        finalized_themes.sort(key=lambda item: _theme_key_for_ids(item["signal_ids"], active_by_id), reverse=True)
        mandatory = mandatory_by_channel.get(channel, [])
        ordinary_by_id = {signal["id"]: signal for signal in ordinary_urgent.get(channel, [])}
        urgent_by_id = dict(ordinary_by_id)
        mandatory_meta: dict[str, dict] = {}
        for record in mandatory:
            signal = record["signal"]
            urgent_by_id[signal["id"]] = signal
            mandatory_meta[signal["id"]] = record
        urgent_records = sorted(urgent_by_id.values(), key=lambda item: (float(item.get("importance") or 0), item.get("updated_seq", 0)), reverse=True)
        prior_urgent = {item.get("id"): item for item in prior_section.get("urgent_signals") or []}
        urgent = []
        for signal in urgent_records:
            sid = signal["id"]
            entry = {"id": sid, "why": prior_urgent.get(sid, {}).get("why", "")}
            if ack_statuses.get(sid) == "acknowledged":
                entry["ack"] = "acknowledged"
            # I21, mark don't gate: only an entry the deadline rule FORCED in
            # gets the lane marker. A signal that is urgent on its own merits
            # and merely carries a date is an incident, not a dated obligation.
            if sid in mandatory_meta and sid not in ordinary_by_id:
                entry["via"] = "deadline"
            urgent.append(entry)
        urgent_ids = {item["id"] for item in urgent}
        assigned = {sid for theme in finalized_themes for sid in theme["signal_ids"]}
        notables = []
        for signal in select_notables([signal for signal in surfaced if signal.get("channel") == channel and signal.get("id") not in assigned], urgent_ids):
            entry = {"id": signal["id"], "title": signal.get("title")}
            if ack_statuses.get(signal["id"]) == "acknowledged":
                entry["ack"] = "acknowledged"
            notables.append(entry)
        section = {
            "summary": prior_section.get("summary", ""), "urgent_signals": urgent,
            "active_themes": finalized_themes, "notable_signals": notables,
            "entities_to_watch": entities_to_watch([signal for signal in active if signal.get("channel") == channel], " ".join(item["theme"] for item in finalized_themes)),
        }
        sections[channel] = section
        summary_slot = {"slot": f"channels.{channel}.summary"}
        if "summary" in prior_section:
            summary_slot["value"] = prior_section.get("summary", "")
            # I22a: a pre-filled summary is only safe to reuse while the channel's
            # storyline set held still. The flag is advisory — it tells the prose
            # turn which reused paragraphs to re-read, not which to rewrite.
            if _theme_set_turned_over(prior_themes, finalized_themes):
                summary_slot["stale_risk"] = True
        slots.append(summary_slot)
        for entry in urgent:
            sid = entry["id"]
            slot = {"slot": f"channels.{channel}.urgent.{sid}.why", "context": _grounded_entry(active_by_id.get(sid) or next((record["signal"] for record in mandatory if record["signal"].get("id") == sid), {}))}
            if sid in mandatory_meta:
                slot["context"].update({"date": mandatory_meta[sid]["deadline_date"].isoformat(), "origin": mandatory_meta[sid]["origin"]})
            if sid in prior_urgent and "why" in prior_urgent[sid]:
                slot["value"] = prior_urgent[sid]["why"]
            slots.append(slot)
    if errors:
        raise ValueError("draft rejected (" + str(len(errors)) + " violation(s), all listed):\n  " + "\n  ".join(errors))
    skeleton = {"version": 3, "as_of": _iso(now), "updated_through_seq": max((signal.get("updated_seq", 0) for signal in all_state), default=0), "channels": sections}
    if prior is not None:
        skeleton["delta"] = _final_delta(prior, sections)
    return skeleton, slots


def _apply_prose(skeleton: dict, slots: list[dict], prose: dict) -> dict:
    if not isinstance(prose, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in prose.items()):
        raise ValueError("prose map must be an object of string slot keys and string values")
    known = {slot["slot"]: slot for slot in slots}
    unknown = sorted(set(prose) - set(known))
    if unknown:
        raise ValueError("unknown prose slot(s): " + ", ".join(unknown))
    missing = sorted(slot["slot"] for slot in slots if "value" not in slot and slot["slot"] not in prose)
    if missing:
        raise ValueError("missing prose slot(s): " + ", ".join(missing))
    for name, slot in known.items():
        value = prose.get(name, slot.get("value"))
        parts = name.split(".")
        channel = parts[1]
        if parts[2] == "summary":
            skeleton["channels"][channel]["summary"] = value
        else:
            sid = parts[3]
            for urgent in skeleton["channels"][channel]["urgent_signals"]:
                if urgent["id"] == sid:
                    urgent["why"] = value
                    break
    return skeleton


def build_gate(state: dict, landscape: dict | None, now: datetime) -> dict:
    """Report whether the passage of TIME alone has made the landscape wrong.

    The pipeline is driven by sync's new/updated counts, so a quiet poll stops
    before the finish step — and every ack rule that turns on a clock rather than
    on an arriving signal silently stalls with it. An acknowledged deadline
    synthesized at four days out would never re-enter the urgent list when it
    reached three; a passed deadline would never expire; a once-only tail would
    never be shown or retired. This is the cheap mechanical check for exactly
    those four cases. It reads state, the landscape, the ack store and the
    radar's archive coverage, and writes nothing.
    """
    state_signals = list(state.get("signals") or [])
    store = load_ack_store()
    radar_archive = deadline_archive(now)
    statuses = effective_statuses(store, state_signals, now, radar_archive)
    radar = deadline_hits(state_signals, radar_archive, now)

    channels = (landscape or {}).get("channels") or {}
    listed_urgent = {
        str(item.get("id"))
        for section in channels.values()
        if isinstance(section, dict)
        for item in section.get("urgent_signals") or []
    }

    newly_mandatory: list[str] = []
    final_nag_due: list[str] = []
    for record in mandatory_deadline_records(radar, statuses):
        sid = str(record["signal"].get("id"))
        if sid in listed_urgent:
            continue
        newly_mandatory.append(sid)
        if statuses.get(sid) == "acknowledged":
            final_nag_due.append(sid)

    shown = expired_tail_already_shown(store, statuses, STATE_DIR)
    unshown_tail = sorted({
        str(record["signal"].get("id")) for record in radar
        if record["days_until"] < 0
        and statuses.get(record["signal"].get("id")) == "expired"
        and record["signal"].get("id") not in shown
    })

    pending = [
        {"id": item["id"], "from": item["from"], "to": item["to"]}
        for item in pending_transitions(store, state_signals, now, radar_archive)
    ]
    result = {
        "checked_at": _iso(now),
        "landscape_as_of": (landscape or {}).get("as_of"),
        "pending_ack_transitions": pending,
        "newly_mandatory_urgents": sorted(set(newly_mandatory)),
        # A subset of the line above, named separately because it is the rule
        # most likely to be wrong-looking in the field: "you noted this, and it
        # is now imminent".
        "final_nag_due": sorted(set(final_nag_due)),
        "unshown_expired_tail": unshown_tail,
    }
    result["stale"] = bool(
        pending or result["newly_mandatory_urgents"] or result["final_nag_due"] or unshown_tail
    )
    return result


def _parse_args(argv: list[str]) -> tuple[str, Path | None, Path | None]:
    mode = ""
    draft = prose = None
    index = 0
    while index < len(argv):
        if argv[index] == "--pre":
            mode = "pre"; index += 1
        elif argv[index] == "--gate":
            mode = "gate"; index += 1
        elif argv[index] == "--finish" and index + 1 < len(argv):
            mode = "finish"; draft = Path(argv[index + 1]); index += 2
        elif argv[index] == "--prose" and index + 1 < len(argv):
            prose = Path(argv[index + 1]); index += 2
        else:
            raise ValueError(f"unknown or incomplete argument {argv[index]!r}")
    if mode in ("pre", "gate") and (draft or prose):
        raise ValueError(f"--{mode} takes no input files")
    if mode not in ("pre", "gate") and (mode != "finish" or draft is None):
        raise ValueError("use --pre, --gate, or --finish <draft.json>")
    return mode, draft, prose


def main() -> None:
    try:
        mode, draft_path, prose_path = _parse_args(sys.argv[1:])
        state = _load_json(STATE_PATH, required=True) or {}
        prior = _load_json(LANDSCAPE_PATH, required=False)
        # Mid-run re-run detection: a landscape whose seq watermark equals the
        # current window's is this run's own output — the real prior is the
        # backup the write step preserved before the first overwrite.
        current_seq = max((s.get("updated_seq", 0) for s in state.get("signals") or []), default=0)
        if prior is not None and prior.get("updated_through_seq") == current_seq:
            backup = _load_json(PRIOR_BACKUP_PATH, required=False)
            if backup is not None:
                prior = backup
        now = datetime.now(timezone.utc)
        if mode == "gate":
            # The gate asks about the landscape as WRITTEN, so it deliberately
            # skips the mid-run prior-backup swap above.
            print(json.dumps(build_gate(state, _load_json(LANDSCAPE_PATH, required=False), now), indent=2))
            return
        if mode == "pre":
            print(json.dumps(build_pre(state, prior, now), indent=2))
            return
        draft = _load_json(draft_path, required=True) if draft_path else None
        skeleton, slots = build_finish(state, prior, draft or {}, now)
        if prose_path is None:
            print(json.dumps({"landscape": skeleton, "slots": slots}, indent=2))
            return
        prose = _load_json(prose_path, required=True) or {}
        LANDSCAPE_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing = _load_json(LANDSCAPE_PATH, required=False)
        if existing is not None and existing.get("updated_through_seq") != skeleton["updated_through_seq"]:
            _atomic_write_json(PRIOR_BACKUP_PATH, existing)
        # Same crash-safety contract as the ack store (F3): a partial landscape
        # write would hard-fail every later _load_json of the prior.
        _atomic_write_json(LANDSCAPE_PATH, _apply_prose(skeleton, slots, prose))
        # `--pre` is intentionally pure. The finish/prose path is the one
        # mechanical write step, so it is also where automatic lifecycle facts
        # become durable. The helper is idempotent across validate-fix reruns.
        persist_effective_transitions(
            list(state.get("signals") or []), STATE_DIR, now, deadline_archive(now)
        )
        print(json.dumps({"written": str(LANDSCAPE_PATH)}, indent=2))
    except ValueError as exc:
        print(f"assemble: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
