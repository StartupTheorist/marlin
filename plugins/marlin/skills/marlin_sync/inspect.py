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
    --channel <id>    Restrict the triage index (or --by-channel grouping) to
                      one channel. Unknown channel → empty output, exit 0.
    --by-channel      Group the triage index into `## <channel>` sections
                      (sections ordered by count desc then id asc; newest-first
                      within each).

Precedence: --ids > --channels > --by-channel/default (the latter honoring
--channel).

Designed to be the agent's triage index: scan default output to decide which
signals to drill into, then re-invoke with `--ids` for the few that matter.

Stdlib only so it works anywhere Python 3 is available.

Output:
    stdout: triage lines (default), grouped lines (--by-channel), channel
            counts (--channels), or full records (--ids).
    stderr: errors (missing file, corrupt JSON, unknown ID). Exits non-zero.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

STATE_PATH = Path("marlin_state.json")


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


def _parse_argv(argv: list[str]) -> dict[str, object]:
    """Parse flags. Value flags: --ids, --channel (also `--flag=value`).
    Boolean flags: --channels, --by-channel. Unknown args ignored for
    forward compat."""
    out: dict[str, object] = {}
    value_flags = {"--ids": "ids", "--channel": "channel"}
    bool_flags = {"--channels": "channels", "--by-channel": "by_channel"}
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
        else:
            i += 1
    return out


def main() -> None:
    args = _parse_argv(sys.argv[1:])

    if not STATE_PATH.exists():
        _die(f"{STATE_PATH} not found; run sync.py first")
    try:
        state = json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        _die(f"could not read {STATE_PATH}: {e}")

    signals = state.get("signals", [])

    if "ids" in args:
        wanted = [sid.strip() for sid in str(args["ids"]).split(",") if sid.strip()]
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
        return

    # --channels: enumerate channels in the full state (ignores --channel filter).
    if args.get("channels"):
        counts: dict[str, int] = {}
        for s in signals:
            counts[s.get("channel", "?")] = counts.get(s.get("channel", "?"), 0) + 1
        for cid in sorted(counts, key=lambda c: (-counts[c], c)):
            print(f"{cid}\t{counts[cid]}")
        return

    # --channel: filter the working set to one channel.
    if "channel" in args:
        signals = [s for s in signals if s.get("channel") == args["channel"]]

    def _newest_first(items: list[dict]) -> list[dict]:
        return sorted(items, key=lambda s: s.get("updated_seq", 0), reverse=True)

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
