#!/usr/bin/env python3
"""Marlin triage index — compact one-line-per-signal view of marlin_state.json.

Default mode: read `marlin_state.json` and print one pipe-delimited line per
signal, sorted by `updated_seq` descending:

    seq:<N> | <id> | <handling> | <imp>/<nov> | <signal_type> | <channel> | <title> | <tags>

`--ids A,B,C` mode: dump full records (title, what_changed, why_it_matters,
scores, channel, tags) for the listed signal IDs. Used by the skill to back
up `urgent_signals` `why` lines without loading the whole state file.

Channel flags (for the multi-channel landscape — every signal has exactly one
channel, so the landscape is synthesized per channel):

    --channels        List the distinct channels in state, one per line, as
                      `<channel>\t<count>`, ordered by count desc then id asc.
                      Lets the skill enumerate channel keys deterministically.
                      Reflects the whole state, ignoring any --channel filter.
    --channel <id>    Restrict the triage index (or --by-channel grouping, and
                      the --entity-candidates / --urgent-top helpers) to one
                      channel. Unknown channel → empty output, exit 0.
    --by-channel      Group the triage index into `## <channel>` sections
                      (sections ordered by count desc then id asc; newest-first
                      within each).
    --since-seq <N>   Restrict the triage index to signals with
                      `updated_seq > N`. The diff-mode filter: pass the prior
                      landscape's `updated_through_seq` to see only what's new.
                      Composes with --channel / --by-channel.

Landscape-synthesis helpers (pre-compute the deterministic selection/sort the
skill's rules require, so the agent doesn't do it by hand):

    --entity-candidates   Per channel, list entities qualifying for
                          `entities_to_watch` under the count rule (appear in
                          ≥2 signals in that channel), names taken verbatim
                          from `entity_tags`, with supporting signal IDs. The
                          agent still applies the "not named in any theme
                          string" exclusion (which needs theme membership).
    --urgent-top [N]      Per channel, list `handling=urgent` signals with the
                          deterministic sort applied (importance desc, ties by
                          updated_seq desc), capped at N (default 5), noting any
                          dropped beyond N.
    --theme-key A,B,C     Given a theme's signal IDs, print its composite sort
                          key — `max_importance`, `count`, `max_updated_seq` —
                          so themes can be ordered without hand-computing.
    --now                 Print the current UTC time as ISO-8601 seconds with a
                          trailing Z (`YYYY-MM-DDTHH:MM:SSZ`), for the
                          landscape's `as_of`. Needs no state file.

Precedence: --now > --ids > --channels > --theme-key > --entity-candidates >
--urgent-top > --by-channel/default (the latter honoring --channel and
--since-seq).

Note on `seq` gaps: `updated_seq` is a single global monotonic counter on the
server (MAX+1, reassigned on every insert AND update), so numbers are routinely
absent from any one view — updates abandon the old number, and merged / deduped
/ cross-channel / trimmed signals consume numbers that don't appear here. Gaps
are expected; the `updated_seq > N` threshold is gap-safe.

Designed to be the agent's triage index: scan default output to decide which
signals to drill into, then re-invoke with `--ids` for the few that matter.

Stdlib only so it works anywhere Python 3 is available.

Output:
    stdout: triage lines (default), grouped lines (--by-channel), channel
            counts (--channels), full records (--ids), helper output
            (--entity-candidates / --urgent-top / --theme-key), or a UTC line
            (--now).
    stderr: errors (missing file, corrupt JSON, unknown ID). Exits non-zero.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path("marlin_state.json")
URGENT_TOP_DEFAULT = 5


def _iso_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def _format_signal(s: dict) -> str:
    seq = s.get("updated_seq", "?")
    sid = s.get("id", "?")
    handling = s.get("handling", "?")
    imp = s.get("importance")
    nov = s.get("novelty")
    scores = (
        f"{imp:.2f}/{nov:.2f}"
        if isinstance(imp, (int, float)) and isinstance(nov, (int, float))
        else "?/?"
    )
    signal_type = s.get("signal_type", "?")
    channel = s.get("channel", "?")
    title = (s.get("title") or "").replace("|", "/").strip()
    tags = ",".join(s.get("entity_tags") or [])
    return (
        f"seq:{seq} | {sid} | {handling} | {scores} | "
        f"{signal_type} | {channel} | {title} | {tags}"
    )


def _format_full(s: dict) -> str:
    """Pretty-text dump of a single signal for `--ids` mode."""
    imp = s.get("importance")
    nov = s.get("novelty")
    scores = (
        f"importance={imp:.2f} novelty={nov:.2f}"
        if isinstance(imp, (int, float)) and isinstance(nov, (int, float))
        else "importance=? novelty=?"
    )
    tags = ",".join(s.get("entity_tags") or []) or "<none>"
    lines = [
        f"=== {s.get('id', '?')} (seq:{s.get('updated_seq', '?')}) ===",
        f"handling:       {s.get('handling', '?')}",
        f"signal_type:    {s.get('signal_type', '?')}",
        f"channel:        {s.get('channel', '?')}",
        f"scores:         {scores}",
        f"entity_tags:    {tags}",
        f"title:          {(s.get('title') or '').strip()}",
        "what_changed:",
        f"  {(s.get('what_changed') or '').strip()}",
        "why_it_matters:",
        f"  {(s.get('why_it_matters') or '').strip()}",
    ]
    return "\n".join(lines)


def _channel_order(signals: list[dict]) -> list[str]:
    """Distinct channels ordered by signal count desc, then id asc."""
    counts: dict[str, int] = {}
    for s in signals:
        counts[s.get("channel", "?")] = counts.get(s.get("channel", "?"), 0) + 1
    return sorted(counts, key=lambda c: (-counts[c], c))


def _newest_first(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda s: s.get("updated_seq", 0), reverse=True)


def _as_float(v: object) -> float:
    return float(v) if isinstance(v, (int, float)) else 0.0


def _parse_argv(argv: list[str]) -> dict[str, object]:
    """Parse flags. Value flags: --ids, --channel, --since-seq, --theme-key
    (also `--flag=value`). Optional-value flag: --urgent-top (an int may
    follow; defaults otherwise). Boolean flags: --channels, --by-channel,
    --entity-candidates, --now. Unknown args ignored for forward compat."""
    out: dict[str, object] = {}
    value_flags = {
        "--ids": "ids",
        "--channel": "channel",
        "--since-seq": "since_seq",
        "--theme-key": "theme_key",
    }
    bool_flags = {
        "--channels": "channels",
        "--by-channel": "by_channel",
        "--entity-candidates": "entity_candidates",
        "--now": "now",
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in value_flags and i + 1 < len(argv):
            out[value_flags[arg]] = argv[i + 1]
            i += 2
        elif "=" in arg and arg.split("=", 1)[0] in value_flags:
            key, val = arg.split("=", 1)
            out[value_flags[key]] = val
            i += 1
        elif arg in bool_flags:
            out[bool_flags[arg]] = True
            i += 1
        elif arg == "--urgent-top":
            # Optional integer value; consume it only if it looks like one.
            if i + 1 < len(argv) and argv[i + 1].lstrip("-").isdigit():
                out["urgent_top"] = argv[i + 1]
                i += 2
            else:
                out["urgent_top"] = str(URGENT_TOP_DEFAULT)
                i += 1
        elif "=" in arg and arg.split("=", 1)[0] == "--urgent-top":
            out["urgent_top"] = arg.split("=", 1)[1]
            i += 1
        else:
            i += 1
    return out


def _load_state() -> dict:
    if not STATE_PATH.exists():
        _die(f"{STATE_PATH} not found; run sync.py first")
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        _die(f"could not read {STATE_PATH}: {e}")


def _print_ids(signals: list[dict], raw_ids: str) -> None:
    wanted = [sid.strip() for sid in raw_ids.split(",") if sid.strip()]
    if not wanted:
        _die("--ids requires at least one signal id")
    by_id = {s.get("id"): s for s in signals}
    missing = [sid for sid in wanted if sid not in by_id]
    if missing:
        _die(f"unknown signal id(s): {','.join(missing)}")
    for i, sid in enumerate(wanted):
        if i:
            print()
        print(_format_full(by_id[sid]))


def _print_channels(signals: list[dict]) -> None:
    counts: dict[str, int] = {}
    for s in signals:
        counts[s.get("channel", "?")] = counts.get(s.get("channel", "?"), 0) + 1
    for cid in sorted(counts, key=lambda c: (-counts[c], c)):
        print(f"{cid}\t{counts[cid]}")


def _print_theme_key(signals: list[dict], raw_ids: str) -> None:
    wanted = [sid.strip() for sid in raw_ids.split(",") if sid.strip()]
    if not wanted:
        _die("--theme-key requires at least one signal id")
    by_id = {s.get("id"): s for s in signals}
    missing = [sid for sid in wanted if sid not in by_id]
    if missing:
        _die(f"unknown signal id(s): {','.join(missing)}")
    members = [by_id[sid] for sid in wanted]
    max_imp = max(_as_float(s.get("importance")) for s in members)
    max_seq = max(s.get("updated_seq", 0) for s in members)
    print(
        f"max_importance={max_imp:.2f} count={len(members)} "
        f"max_updated_seq={max_seq}"
    )


def _print_entity_candidates(signals: list[dict]) -> None:
    """Per channel: entities in ≥2 of the channel's signals, verbatim from
    entity_tags, with supporting signal IDs. Sorted by count desc, name asc."""
    for i, cid in enumerate(_channel_order(signals)):
        if i:
            print()
        print(f"## {cid}")
        chan_signals = [s for s in signals if s.get("channel", "?") == cid]
        ids_by_entity: dict[str, list[str]] = {}
        for s in chan_signals:
            sid = s.get("id", "?")
            # de-dup entity per signal so one signal can't count twice
            for ent in dict.fromkeys(s.get("entity_tags") or []):
                ids_by_entity.setdefault(ent, []).append(sid)
        qualifying = {e: ids for e, ids in ids_by_entity.items() if len(ids) >= 2}
        for ent in sorted(qualifying, key=lambda e: (-len(qualifying[e]), e)):
            ids = qualifying[ent]
            print(f"{ent}\t{len(ids)}\t{','.join(ids)}")


def _print_urgent_top(signals: list[dict], n: int) -> None:
    """Per channel: handling=urgent signals, sorted importance desc then
    updated_seq desc, capped at n, noting any dropped beyond n."""
    for i, cid in enumerate(_channel_order(signals)):
        if i:
            print()
        print(f"## {cid}")
        urgent = [
            s
            for s in signals
            if s.get("channel", "?") == cid and s.get("handling") == "urgent"
        ]
        urgent.sort(
            key=lambda s: (_as_float(s.get("importance")), s.get("updated_seq", 0)),
            reverse=True,
        )
        for s in urgent[:n]:
            print(_format_signal(s))
        dropped = len(urgent) - n
        if dropped > 0:
            print(f"# dropped {dropped} beyond top {n}")


def main() -> None:
    args = _parse_argv(sys.argv[1:])

    # --now needs no state file.
    if args.get("now"):
        print(_iso_now())
        return

    state = _load_state()
    signals = state.get("signals", [])

    if "ids" in args:
        _print_ids(signals, str(args["ids"]))
        return

    # --channels: enumerate channels in the full state (ignores --channel filter).
    if args.get("channels"):
        _print_channels(signals)
        return

    # --theme-key: look up across the full state (themes are within a channel,
    # but the IDs are unambiguous, so no channel filter is needed).
    if "theme_key" in args:
        _print_theme_key(signals, str(args["theme_key"]))
        return

    # --channel: filter the working set to one channel (applies to the helpers
    # and the triage views below).
    if "channel" in args:
        signals = [s for s in signals if s.get("channel") == args["channel"]]

    # --since-seq: diff-mode filter to what's new since N.
    if "since_seq" in args:
        try:
            threshold = int(str(args["since_seq"]))
        except ValueError:
            _die(f"--since-seq requires an integer, got {args['since_seq']!r}")
        signals = [s for s in signals if s.get("updated_seq", 0) > threshold]

    if args.get("entity_candidates"):
        _print_entity_candidates(signals)
        return

    if "urgent_top" in args:
        _print_urgent_top(signals, int(str(args["urgent_top"])))
        return

    # --by-channel: grouped sections, ordered by count desc then id asc.
    if args.get("by_channel"):
        for i, cid in enumerate(_channel_order(signals)):
            if i:
                print()
            print(f"## {cid}")
            for s in _newest_first([s for s in signals if s.get("channel", "?") == cid]):
                print(_format_signal(s))
        return

    for s in _newest_first(signals):
        print(_format_signal(s))


if __name__ == "__main__":
    main()
