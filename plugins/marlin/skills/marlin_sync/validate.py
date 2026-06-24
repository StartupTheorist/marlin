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
    and the referenced signal's `channel` matches the section key.
  * theme exclusivity — each signal_id appears in ≤1 active_themes entry per
    channel.
  * active_themes order — matches the composite sort key (max importance desc,
    then signal_ids count desc, then max updated_seq desc).
  * entities_to_watch — each entity appears in ≥2 of the channel's signals,
    verbatim from `entity_tags`, and is not named in any of the channel's
    theme strings; listed signal_ids actually carry the entity.
  * urgent_signals — ≤5 per channel; sorted importance desc / updated_seq desc;
    every referenced signal is handling=urgent in state.
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
from pathlib import Path

# Both files live in the shared state directory (MARLIN_STATE_DIR, else
# ~/.marlin), the same one sync.py/inspect.py resolve — so the validator lints
# the landscape the skill actually wrote, regardless of cwd.
STATE_DIR = Path(os.environ.get("MARLIN_STATE_DIR") or (Path.home() / ".marlin")).expanduser()
STATE_PATH = STATE_DIR / "marlin_state.json"
LANDSCAPE_PATH = STATE_DIR / "marlin_landscape.json"
URGENT_CAP = 5
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


def _theme_key(signal_ids: list[str], by_id: dict[str, dict]) -> tuple[float, int, int]:
    members = [by_id[sid] for sid in signal_ids if sid in by_id]
    max_imp = max((_as_float(s.get("importance")) for s in members), default=0.0)
    max_seq = max((s.get("updated_seq", 0) for s in members), default=0)
    return (max_imp, len(signal_ids), max_seq)


def validate(landscape: dict, state: dict) -> list[str]:
    """Return a list of violation strings (empty == clean)."""
    v: list[str] = []
    signals = state.get("signals", [])
    by_id = {s.get("id"): s for s in signals if s.get("id")}

    # --- top-level ---
    if landscape.get("version") != 2:
        v.append(f"version is {landscape.get('version')!r}, expected 2")

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
    if not isinstance(channels, dict) or not channels:
        v.append("channels is missing or empty")
        return v

    for cid, section in channels.items():
        v.extend(_validate_channel(cid, section, by_id))

    # --- cross_channel (optional) ---
    cc = landscape.get("cross_channel")
    if cc is not None:
        v.extend(_validate_cross_channel(cc, by_id))

    return v


def _validate_channel(cid: str, section: dict, by_id: dict[str, dict]) -> list[str]:
    v: list[str] = []
    if not isinstance(section, dict):
        return [f"[{cid}] section is not an object"]

    for key in ("summary", "urgent_signals", "active_themes", "entities_to_watch"):
        if key not in section:
            v.append(f"[{cid}] missing '{key}'")

    chan_signals = [s for s in by_id.values() if s.get("channel") == cid]

    def _ref_ok(sid: str, where: str) -> bool:
        s = by_id.get(sid)
        if s is None:
            v.append(f"[{cid}] {where} references unknown signal id {sid!r}")
            return False
        if s.get("channel") != cid:
            v.append(
                f"[{cid}] {where} references signal {sid!r} from another "
                f"channel ({s.get('channel')!r})"
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
    theme_blob = " ".join(theme_names).lower()
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
        count = sum(1 for s in chan_signals if ent in (s.get("entity_tags") or []))
        if count < 2:
            v.append(
                f"[{cid}] entity {ent!r} appears in {count} signal(s); rule "
                "requires ≥2 in the channel"
            )
        if ent and ent.lower() in theme_blob:
            v.append(
                f"[{cid}] entity {ent!r} is named in a theme string; "
                "entities_to_watch excludes theme subjects"
            )

    # --- urgent_signals: cap, sort, handling source ---
    urgent = section.get("urgent_signals") or []
    if len(urgent) > URGENT_CAP:
        v.append(f"[{cid}] urgent_signals has {len(urgent)} entries; cap is {URGENT_CAP}")
    sort_keys: list[tuple[float, int]] = []
    for u in urgent:
        sid = u.get("id", "?")
        if _ref_ok(sid, "urgent_signals"):
            s = by_id[sid]
            if s.get("handling") != "urgent":
                v.append(
                    f"[{cid}] urgent_signals includes {sid!r} whose handling is "
                    f"{s.get('handling')!r}, not 'urgent'"
                )
            sort_keys.append((_as_float(s.get("importance")), s.get("updated_seq", 0)))
    for i in range(len(sort_keys) - 1):
        if sort_keys[i] < sort_keys[i + 1]:
            v.append(
                f"[{cid}] urgent_signals out of order at position {i} "
                "(sort: importance desc, updated_seq desc)"
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
