#!/usr/bin/env python3
"""Marlin landscape validator — lint a written marlin_landscape.json against
the skill's determinism rules.

Reads the written artifact (`marlin_landscape.json`) AND ground truth
(`marlin_state.json`) from the shared state directory (MARLIN_STATE_DIR,
default ~/.marlin/), then checks every
rule from SKILL.md step 8/9 mechanically — so the agent doesn't rely on
remembering to apply them by hand. The v2 landscape is channel-keyed; every
per-channel rule runs within each `channels.<id>` section.

Checks:
  * shape — version == 2; `channels` is a non-empty object; each channel has
    summary / urgent_signals / active_themes / entities_to_watch.
  * referential integrity — every id / signal_id referenced exists in state,
    the referenced signal's `channel` matches the section key, and it is not a
    retired (`status="superseded"`) signal (A8b: retired signals are dropped, not
    referenced).
  * theme exclusivity — each signal_id appears in ≤1 active_themes entry per
    channel.
  * active_themes order — matches the composite sort key (max importance desc,
    then signal_ids count desc, then max updated_seq desc).
  * entities_to_watch — each entity appears in ≥2 of the channel's signals,
    verbatim from `entity_tags`, and is not a theme subject (by title or
    member-tag dominance); listed signal_ids actually carry the entity; and
    the set is complete: every qualifying entity is listed (S12).
  * urgent_signals — ≤5 per channel; sorted importance desc / updated_seq desc;
    every referenced signal is handling=urgent in state.
  * ack state (v3) — a `handled` or `expired` signal must not appear in
    urgent_signals / notable_signals; the optional `ack` value is allowlisted
    per surface, and the `via` deadline-lane marker only on urgent_signals.
  * top-level — `as_of` is ISO-8601 `YYYY-MM-DDTHH:MM:SSZ`; `updated_through_seq`
    equals max(updated_seq) over all signals in state.
  * cross_channel (if present) — linked_events reference real IDs spanning ≥2
    distinct channels.

Stdlib only.

Output:
    stdout: `OK` when the landscape is clean, otherwise one `- <violation>`
            line per problem.
    stderr: hard errors (missing/corrupt files). Exits non-zero on any
            violation or hard error; exit 0 only on `OK`.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from ack import effective_statuses, load_ack_store
from inspect import _load_archive as _load_archive_records, _parse_deadline_date, _recent_archive_months, entities_to_watch
from params import MANDATORY_URGENT_DAYS, URGENT_CAP

# Per-surface allowlists for the v3 optional annotation fields. `acknowledged`
# is the only ack value that ever reaches a written surface: `open` carries no
# marker, and `handled`/`expired` are suppressed out of these lists entirely.
ALLOWED_ACK = {"urgent_signals": {"acknowledged"}, "notable_signals": {"acknowledged"}}
# I21's deadline-lane marker. Only the urgent surface carries it, and only the
# mandatory-deadline rule sets it.
ALLOWED_VIA = {"deadline"}
SUPPRESSED_ACK = {"handled", "expired"}

# Both files live in the shared state directory (MARLIN_STATE_DIR, else
# ~/.marlin), the same one sync.py/inspect.py resolve — so the validator lints
# the landscape the skill actually wrote, regardless of cwd.
STATE_DIR = Path(os.environ.get("MARLIN_STATE_DIR") or (Path.home() / ".marlin")).expanduser()
STATE_PATH = STATE_DIR / "marlin_state.json"
LANDSCAPE_PATH = STATE_DIR / "marlin_landscape.json"
ARCHIVE_DIR = STATE_DIR / "marlin_archive"
AS_OF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def _load(path: Path) -> dict:
    if not path.exists():
        _die(f"{path} not found")
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        _die(f"could not read {path}: {e}")


def _as_float(v: object) -> float:
    return float(v) if isinstance(v, (int, float)) else 0.0


def _is_active(s: dict) -> bool:
    """A8b: a retired (superseded) signal must not appear in the landscape."""
    return s.get("status", "active") == "active"


def _parse_as_of(as_of: object) -> datetime:
    """Return the landscape's own synthesis time, falling back to the wall clock."""
    if isinstance(as_of, str) and as_of.strip():
        try:
            parsed = datetime.fromisoformat(as_of.strip().replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _theme_key(signal_ids: list[str], by_id: dict[str, dict]) -> tuple[float, int, int]:
    members = [by_id[sid] for sid in signal_ids if sid in by_id]
    max_imp = max((_as_float(s.get("importance")) for s in members), default=0.0)
    max_seq = max((s.get("updated_seq", 0) for s in members), default=0)
    return (max_imp, len(signal_ids), max_seq)


def _load_archive() -> dict[str, dict]:
    """Load all archived records for the v3 mandatory-deadline exception."""
    if not ARCHIVE_DIR.is_dir():
        return {}
    found: dict[str, dict] = {}
    for path in ARCHIVE_DIR.glob("*.jsonl"):
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = item.get("id")
            if sid and (sid not in found or item.get("archived_at", "") > found[sid].get("archived_at", "")):
                found[sid] = item
    return found


def validate(landscape: dict, state: dict) -> list[str]:
    """Return a list of violation strings (empty == clean)."""
    v: list[str] = []
    signals = state.get("signals", [])
    by_id = {s.get("id"): s for s in signals if s.get("id")}

    # --- top-level ---
    version = landscape.get("version")
    if version not in (2, 3):
        v.append(f"version is {version!r}, expected 2 or 3")

    as_of = landscape.get("as_of")
    if not isinstance(as_of, str) or not AS_OF_RE.match(as_of):
        v.append(f"as_of {as_of!r} is not ISO-8601 YYYY-MM-DDTHH:MM:SSZ")

    state_max_seq = max((s.get("updated_seq", 0) for s in signals), default=0)
    uts = landscape.get("updated_through_seq")
    if uts != state_max_seq:
        v.append(
            f"updated_through_seq is {uts!r}, expected {state_max_seq} "
            "(max updated_seq in state)"
        )

    channels = landscape.get("channels")
    if not isinstance(channels, dict):
        v.append("channels is missing or not an object")
        return v
    # An EMPTY channels object is legal, not a violation: once the emptiness
    # filter stopped resurrecting dead channels, "nothing to say" became a
    # reachable end state (every channel emptied, or a cold start with no
    # signals). Rejecting it left the pipeline unable to complete that
    # transition at all.

    # Every time-dependent check below is anchored to the landscape's own
    # `as_of`, not to the wall clock. Finish evaluates the deadline band once,
    # writes the file, and validate runs seconds later — across UTC midnight
    # those are different days, and the validator would reject a file that was
    # correct when it was written.
    anchor = _parse_as_of(as_of)
    archive_by_id = _load_archive() if version == 3 else {}
    # The written surfaces must already reflect suppression, so the validator
    # resolves ack state the same way the finish step did — one shared helper,
    # read-only. A missing or unreadable store simply means "no acks".
    ack_statuses: dict[str, str] = {}
    if version == 3:
        try:
            # The same bounded archive coverage the finish step resolved
            # against — current + prior month, never the whole archive — so
            # producer and validator cannot disagree about what has expired.
            radar_archive, _skipped = _load_archive_records(only_months=_recent_archive_months(anchor))
            ack_statuses = effective_statuses(load_ack_store(STATE_DIR), signals, anchor, radar_archive)
        except ValueError:
            ack_statuses = {}
    for cid, section in channels.items():
        v.extend(_validate_channel(cid, section, by_id, archive_by_id, version == 3, ack_statuses, anchor))

    if version == 3 and "delta" in landscape:
        delta = landscape["delta"]
        if not isinstance(delta, dict) or "since" not in delta or not isinstance(delta.get("channels"), dict):
            v.append("delta must be an object with since and channels")

    # --- cross_channel (optional) ---
    cc = landscape.get("cross_channel")
    if cc is not None:
        v.extend(_validate_cross_channel(cc, by_id))

    return v


def _validate_channel(
    cid: str,
    section: dict,
    by_id: dict[str, dict],
    archive_by_id: dict[str, dict],
    v3: bool,
    ack_statuses: dict[str, str] | None = None,
    anchor: datetime | None = None,
) -> list[str]:
    v: list[str] = []
    ack_statuses = ack_statuses or {}
    anchor_date = (anchor or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    if not isinstance(section, dict):
        return [f"[{cid}] section is not an object"]

    required = ("summary", "urgent_signals", "active_themes", "entities_to_watch")
    if v3:
        required += ("notable_signals",)
    for key in required:
        if key not in section:
            v.append(f"[{cid}] missing '{key}'")

    chan_signals = [s for s in by_id.values() if s.get("channel") == cid and _is_active(s)]

    def _ref_ok(sid: str, where: str, *, allow_archived_deadline: bool = False) -> bool:
        s = by_id.get(sid)
        if s is None and allow_archived_deadline:
            s = archive_by_id.get(sid)
        if s is None:
            v.append(f"[{cid}] {where} references unknown signal id {sid!r}")
            return False
        if s.get("channel") != cid:
            v.append(
                f"[{cid}] {where} references signal {sid!r} from another "
                f"channel ({s.get('channel')!r})"
            )
            return False
        if not _is_active(s):
            v.append(
                f"[{cid}] {where} references {s.get('status')!r} signal {sid!r} — "
                "retired (superseded) signals must be dropped from the landscape (A8b)"
            )
            return False
        return True

    # --- active_themes: exclusivity + order ---
    themes = section.get("active_themes") or []
    seen_in_theme: dict[str, str] = {}
    keys: list[tuple[float, int, int]] = []
    theme_names: list[str] = []
    for t in themes:
        name = t.get("theme", "?")
        theme_names.append(name)
        sids = t.get("signal_ids") or []
        if v3 and "named_member_ids" not in t:
            v.append(f"[{cid}] theme {name!r} missing 'named_member_ids'")
        for sid in sids:
            _ref_ok(sid, f"theme {name!r}")
            if sid in seen_in_theme:
                v.append(
                    f"[{cid}] signal {sid!r} in two themes "
                    f"({seen_in_theme[sid]!r} and {name!r}) — themes must be exclusive"
                )
            else:
                seen_in_theme[sid] = name
        keys.append(_theme_key(sids, by_id))
    for i in range(len(keys) - 1):
        if keys[i] < keys[i + 1]:
            v.append(
                f"[{cid}] active_themes out of order: {theme_names[i]!r} "
                f"{keys[i]} should not precede {theme_names[i + 1]!r} {keys[i + 1]} "
                "(sort: max importance desc, count desc, max updated_seq desc)"
            )

    # --- entities_to_watch ---
    # This shared helper owns both title and member-tag subject exclusions.
    # Calling it here keeps validation identical to assemble and inspect.
    expected_entities = {
        entry["entity"]: entry for entry in entities_to_watch(chan_signals, themes)
    }
    for e in section.get("entities_to_watch") or []:
        ent = e.get("entity", "")
        sids = e.get("signal_ids") or []
        for sid in sids:
            if _ref_ok(sid, f"entity {ent!r}"):
                tags = by_id[sid].get("entity_tags") or []
                if ent not in tags:
                    v.append(
                        f"[{cid}] entity {ent!r} not in entity_tags of its "
                        f"listed signal {sid!r}"
                    )
        if ent not in expected_entities:
            count = sum(1 for s in chan_signals if ent in (s.get("entity_tags") or []))
            if count < 2:
                v.append(
                    f"[{cid}] entity {ent!r} appears in {count} signal(s); rule "
                    "requires ≥2 in the channel"
                )
            else:
                v.append(
                    f"[{cid}] entity {ent!r} is a theme subject; "
                    "entities_to_watch excludes theme subjects"
                )

    # --- entities_to_watch: completeness (S12) — use inspect.py's shared
    # helper so title and member-tag subject exclusions cannot drift.
    listed_entities = {e.get("entity", "") for e in section.get("entities_to_watch") or []}
    for ent, expected in expected_entities.items():
        if ent not in listed_entities:
            v.append(
                f"[{cid}] entity {ent!r} qualifies for entities_to_watch "
                f"({len(expected['signal_ids'])} signals, not a theme subject) but is missing "
                "— the set must be complete (S12)"
            )

    # --- urgent_signals: cap, sort, handling source ---
    urgent = section.get("urgent_signals") or []
    sort_keys: list[tuple[float, int]] = []
    ordinary = 0
    urgent_ids: set[str] = set()
    for u in urgent:
        sid = u.get("id", "?")
        urgent_ids.add(sid)
        archived = sid not in by_id and sid in archive_by_id
        if _ref_ok(sid, "urgent_signals", allow_archived_deadline=v3):
            s = by_id.get(sid) or archive_by_id.get(sid)
            deadline = _parse_deadline_date(s.get("deadline_at"))
            # A brief signal may appear here only when its deadline is inside the
            # mandatory band.  This is the v3 exception to the v2 handling rule.
            mandatory = v3 and deadline is not None and 0 <= (deadline - anchor_date).days <= MANDATORY_URGENT_DAYS
            if s.get("handling") != "urgent" and not mandatory:
                v.append(
                    f"[{cid}] urgent_signals includes {sid!r} whose handling is "
                    f"{s.get('handling')!r}, not 'urgent'"
                )
            if not mandatory:
                ordinary += 1
            sort_keys.append((_as_float(s.get("importance")), s.get("updated_seq", 0)))
    if ordinary > URGENT_CAP:
        v.append(f"[{cid}] urgent_signals has {ordinary} ordinary entries; cap is {URGENT_CAP}")
    for i in range(len(sort_keys) - 1):
        if sort_keys[i] < sort_keys[i + 1]:
            v.append(
                f"[{cid}] urgent_signals out of order at position {i} "
                "(sort: importance desc, updated_seq desc)"
            )

    # --- notable_signals (v3): grounded, brief-only, and distinct from urgent ---
    if v3:
        for notable in section.get("notable_signals") or []:
            sid = notable.get("id", "?")
            if sid in urgent_ids:
                v.append(f"[{cid}] signal {sid!r} appears in both urgent_signals and notable_signals")
            if _ref_ok(sid, "notable_signals"):
                if by_id[sid].get("handling") != "brief":
                    v.append(f"[{cid}] notable_signals includes {sid!r} whose handling is not 'brief'")
        v.extend(_validate_ack_fields(cid, "notable_signals", section.get("notable_signals") or [], ack_statuses))
        v.extend(_validate_ack_fields(cid, "urgent_signals", urgent, ack_statuses))

    return v


def _validate_ack_fields(cid: str, surface: str, entries: list[dict], ack_statuses: dict[str, str]) -> list[str]:
    """Check the v3 ack/via annotations and the suppression rule on one surface."""
    v: list[str] = []
    allowed = ALLOWED_ACK.get(surface, set())
    for entry in entries:
        sid = entry.get("id", "?")
        ack = entry.get("ack")
        if ack is not None and ack not in allowed:
            v.append(
                f"[{cid}] {surface} entry {sid!r} has ack {ack!r}; "
                f"allowed here: {sorted(allowed) or 'none'}"
            )
        via = entry.get("via")
        if via is not None and (surface != "urgent_signals" or via not in ALLOWED_VIA):
            v.append(
                f"[{cid}] {surface} entry {sid!r} has via {via!r}; "
                f"allowed: {sorted(ALLOWED_VIA)} on urgent_signals only"
            )
        effective = ack_statuses.get(sid)
        if effective in SUPPRESSED_ACK:
            v.append(
                f"[{cid}] {surface} includes {sid!r}, whose ack state is "
                f"{effective!r} — handled and expired signals must be suppressed "
                "from the action surfaces (S13)"
            )
    return v


def _validate_cross_channel(cc: dict, by_id: dict[str, dict]) -> list[str]:
    v: list[str] = []
    for ev in cc.get("linked_events") or []:
        sids = ev.get("signal_ids") or []
        chans = set()
        for sid in sids:
            s = by_id.get(sid)
            if s is None:
                v.append(f"[cross_channel] unknown signal id {sid!r}")
            elif not _is_active(s):
                v.append(
                    f"[cross_channel] links {s.get('status')!r} signal {sid!r} — "
                    "retired (superseded) signals must be dropped (A8b)"
                )
            else:
                chans.add(s.get("channel"))
        if len(chans) < 2:
            v.append(
                f"[cross_channel] linked_event spans {len(chans)} distinct "
                "channel(s); a link needs ≥2"
            )
    return v


def main() -> None:
    landscape = _load(LANDSCAPE_PATH)
    state = _load(STATE_PATH)
    violations = validate(landscape, state)
    if not violations:
        print("OK")
        return
    for msg in violations:
        print(f"- {msg}")
    sys.exit(1)


if __name__ == "__main__":
    main()
