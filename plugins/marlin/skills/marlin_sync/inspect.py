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
    --retired-since <N>
                      List full-state signals retired after sequence N, with
                      their recorded successor where present. Composes with
                      --channel so the consumer can inspect a channel's
                      retirements and spot a wrong supersede.

Landscape-synthesis helpers (pre-compute the deterministic selection/sort the
skill's rules require, so the agent doesn't do it by hand):

    --entity-candidates   Per channel, list entities qualifying for
                          `entities_to_watch` under the count rule (appear in
                          ≥2 signals in that channel), names taken verbatim
                          from `entity_tags`, with supporting signal IDs.
                          With `--channel <id> --themes <blob>` also given,
                          instead emits the final paste-ready
                          `entities_to_watch` JSON array for that one channel
                          — the theme-subject exclusion applied, the set
                          complete (no top-N cap) — meant to be pasted
                          verbatim as the channel's `entities_to_watch`.
        --themes <blob>       The channel's draft theme names, joined into
                               one string (any separator; matched as a
                               lowercased substring check, same as
                               validate.py). Only meaningful with
                               --entity-candidates and --channel; without
                               --channel the "final set" is undefined
                               (entities_to_watch is per-channel).
    --urgent-top [N]      Per channel, list `handling=urgent` signals with the
                          deterministic sort applied (importance desc, ties by
                          updated_seq desc), capped at N (default 5), noting any
                          dropped beyond N.
    --theme-key A,B,C     Given a theme's signal IDs, print its composite sort
                          key — `max_importance`, `count`, `max_updated_seq` —
                          so themes can be ordered without hand-computing.
    --landscape-survivors Cross-reference the prior `marlin_landscape.json`
                          against state, reporting which referenced IDs
                          survived, were retired, or were trimmed from the
                          rolling window.
    --archive             Read mode over the local archive (everything
                          sync.py has ever evicted from the rolling window —
                          see marlin_archive/<YYYY-MM>.jsonl), instead of
                          marlin_state.json. Reads every monthly file,
                          de-duplicates by signal `id` (keeping the entry with
                          the newest `archived_at`, since a retried sync can
                          append a duplicate), and prints one
                          `archived:<YYYY-MM-DD> | ...` line per signal,
                          sorted by `archived_at` descending then
                          `updated_seq` descending. Malformed lines are
                          skipped and counted; if any were skipped, a warning
                          is printed to stderr after the output (exit 0
                          regardless). Missing/empty archive dir -> empty
                          output, exit 0. Composes with --channel, plus these
                          archive-only value flags:
                            --entity <text>       Case-insensitive substring
                                                   match against entity_tags.
                            --signal-type <t>      Exact match on signal_type.
                            --since <YYYY-MM-DD>   Keep archived_at[:10] >= this.
                            --until <YYYY-MM-DD>   Keep archived_at[:10] <= this.
                          --entity/--signal-type/--since/--until are ignored
                          outside --archive mode.
    --deadlines           The deadline radar (S8). Read mode over BOTH state and
                          the archive — the first inspect mode to read both —
                          keyed on the explicit `deadline_at` field (never
                          `event_time`). Lists every **active** signal whose
                          `deadline_at` parses to a date in `[now − 7d,
                          now + horizon]`, deduplicated by id across the two
                          sources (state wins), sorted by deadline date
                          ascending. Each line is
                          `<when> | <origin> | <standard index line>`, where
                          `<when>` is `due in <n>d` / `due today` /
                          `PASSED <n>d ago` and `<origin>` is `in-window`
                          (state) or `archived`. Superseded/moved/cancelled
                          deadlines never appear (status must be active in both
                          sources); a null `deadline_at` never qualifies. The
                          archive scan is bounded to the current + prior month's
                          files. Ack-aware (S13): a signal the user marked
                          `handled` is hidden, and an `expired` signal's
                          passed-deadline tail is printed on the first run after
                          it expired and suppressed on every later run — without
                          that, expiry itself re-nags for the whole tail window.
                          A signal with a non-`open` state is tagged
                          ` | ack:<state>` at the end of its line. Reading acks
                          never writes them: the ack store's own transitions are
                          persisted by assemble's write step, not here.
                          Composes with:
                            --horizon <days>      Forward window in days
                                                   (default 21). The just-passed
                                                   tail is fixed at 7 days.
                            --all                 Show every radar hit, including
                                                   handled ones and already-shown
                                                   expired tails.
    --now                 Print the current UTC time as ISO-8601 seconds with a
                          trailing Z (`YYYY-MM-DDTHH:MM:SSZ`), for the
                          landscape's `as_of`. Needs no state file.

Precedence: --now > --archive --ids > --ids > --channels > --theme-key > --retired-since >
--landscape-survivors > --archive > --deadlines > --entity-candidates >
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
            (--retired-since / --landscape-survivors / --archive / --deadlines /
            --entity-candidates / --urgent-top / --theme-key), or a UTC line
            (--now).
    stderr: errors (missing file, corrupt JSON, unknown ID). Exits non-zero.
            --archive is the exception: a skipped-malformed-line count is a
            warning, not an error, and still exits 0.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from params import DEADLINE_TAIL_DAYS, RADAR_HORIZON_DAYS, URGENT_CAP

# Resolve the state directory the same way sync.py does (MARLIN_STATE_DIR, else
# ~/.marlin) so reads find what sync.py wrote regardless of cwd. `--state-dir`
# prints it so the skill can read/write the landscape in the same place.
STATE_DIR = Path(os.environ.get("MARLIN_STATE_DIR") or (Path.home() / ".marlin")).expanduser()
STATE_PATH = STATE_DIR / "marlin_state.json"
LANDSCAPE_PATH = STATE_DIR / "marlin_landscape.json"
ARCHIVE_DIR = STATE_DIR / "marlin_archive"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def _is_active(s: dict) -> bool:
    """A signal belongs in the triage index / landscape unless it's been retired.

    A8b: the server retires a signal (`status="superseded"`) when a later event
    supersedes it, but keeps *delivering* it (flagged, updated_seq bumped) so the
    skill can drop it — "deliver-then-drop". Here is where it's dropped. Missing
    status is treated as active (pre-A8b state, or an old server)."""
    return s.get("status", "active") == "active"


def _as_float(v: object) -> float:
    return float(v) if isinstance(v, (int, float)) else 0.0


# Corporate/legal suffix tokens trimmed when they appear as the trailing token of an
# entity name (after lowercasing). Closed, conservative set — see A4.3.
_ENTITY_SUFFIX_TOKENS = frozenset(
    {"inc", "llc", "ltd", "corp", "co", "gmbh", "plc", "ag", "sa"}
)


def normalize_entity_key(name: str) -> str:
    """Return the normalized GROUPING KEY for an entity-tag string (A4 scaffolding).

    A4 (entity normalization) collapses trivial string variants of the same
    real-world entity for *counting and grouping* in `entities_to_watch`, while the
    string shown to the consumer stays the first-seen verbatim `entity_tags` value.
    This function computes only the key; it never produces a display string.

    Spec (see docs/features/ingest_algo/implementation_plan.md §A4.3 — source of truth):
      1. strip + collapse internal whitespace
      2. lowercase (casefold)
      3. trim a leading article "the" and a single closed set of trailing
         corporate/legal suffix tokens (inc/llc/ltd/corp/co/gmbh/plc/ag/sa),
         comma- and period-insensitive
      4. trim trailing punctuation left behind

    Deliberately does NOT strip version/qualifier tokens or do prefix/alias/fuzzy
    collapse, so distinct entities are never merged:
      normalize_entity_key("GPT-5.4")        != normalize_entity_key("GPT-5")
      normalize_entity_key("GPT-5.4-Cyber")  != normalize_entity_key("GPT-5.4")
      normalize_entity_key("Mozilla Firefox")!= normalize_entity_key("Mozilla")
    while collapsing the genuine trivial variants:
      normalize_entity_key("Anthropic") == normalize_entity_key("Anthropic, Inc.")
                                        == normalize_entity_key("  anthropic ")

    NOTE: scaffolding only — not yet wired into --entity-candidates (A4 Phase 2).
    """
    key = " ".join(name.split()).casefold()

    # Leading article.
    if key.startswith("the "):
        key = key[4:]

    # Trailing corporate/legal suffix: strip the last token if (sans trailing
    # period/comma) it is in the closed set. Loop once — names with two stacked
    # suffixes are not observed and stacking risks over-collapse.
    tokens = key.split()
    if len(tokens) > 1:
        last = tokens[-1].rstrip(".,")
        if last in _ENTITY_SUFFIX_TOKENS:
            tokens = tokens[:-1]
            key = " ".join(tokens)

    return key.rstrip(" .,")


def _parse_argv(argv: list[str]) -> dict[str, object]:
    """Parse flags. Value flags: --ids, --channel, --since-seq, --theme-key,
    --retired-since, --entity, --signal-type, --since, --until, --themes,
    --horizon (also `--flag=value`). Optional-value flag: --urgent-top (an int
    may follow; defaults otherwise). Boolean flags: --channels, --by-channel,
    --entity-candidates, --landscape-survivors, --archive, --deadlines, --all,
    --now. Unknown args ignored for forward compat."""
    out: dict[str, object] = {}
    value_flags = {
        "--ids": "ids",
        "--channel": "channel",
        "--since-seq": "since_seq",
        "--theme-key": "theme_key",
        "--retired-since": "retired_since",
        "--entity": "entity",
        "--signal-type": "signal_type",
        "--since": "since",
        "--until": "until",
        "--themes": "themes",
        "--horizon": "horizon",
    }
    bool_flags = {
        "--channels": "channels",
        "--by-channel": "by_channel",
        "--entity-candidates": "entity_candidates",
        "--landscape-survivors": "landscape_survivors",
        "--archive": "archive",
        "--deadlines": "deadlines",
        "--all": "all",
        "--now": "now",
        "--state-dir": "state_dir",
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
                out["urgent_top"] = str(URGENT_CAP)
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


def _load_landscape() -> dict:
    if not LANDSCAPE_PATH.exists():
        _die(
            f"{LANDSCAPE_PATH} not found; run the landscape-synthesis step first"
        )
    try:
        return json.loads(LANDSCAPE_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        _die(
            f"could not read {LANDSCAPE_PATH}; run the landscape-synthesis "
            f"step first: {e}"
        )


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


def _print_retired_since(all_signals: list[dict], threshold: int) -> None:
    """List retired signals after threshold with their recorded successor."""
    by_id = {s.get("id"): s for s in all_signals}
    retired = [
        s
        for s in all_signals
        if s.get("status", "active") != "active"
        and s.get("updated_seq", 0) > threshold
    ]
    for s in _newest_first(retired):
        sid = s.get("id", "?")
        status = s.get("status", "?")
        channel = s.get("channel", "?")
        title = (s.get("title") or "").strip()
        print(f"seq:{s.get('updated_seq', '?')} | {sid} | retired:{status} | {channel} | {title}")
        successor_id = s.get("superseded_by")
        if not successor_id:
            print("  superseded_by: <none recorded>")
        elif successor_id in by_id:
            successor_title = (by_id[successor_id].get("title") or "").strip()
            print(f"  superseded_by: {successor_id} | {successor_title}")
        else:
            print(f"  superseded_by: {successor_id} (not in local window)")


def _print_landscape_survivors(all_signals: list[dict]) -> None:
    """Classify every landscape-referenced signal against the full state."""
    landscape = _load_landscape()
    referenced_ids: set[str] = set()
    channels = landscape.get("channels") or {}
    for channel_data in channels.values():
        for theme in channel_data.get("active_themes") or []:
            referenced_ids.update(theme.get("signal_ids") or [])
        for urgent in channel_data.get("urgent_signals") or []:
            signal_id = urgent.get("id")
            if signal_id:
                referenced_ids.add(signal_id)
        for entity in channel_data.get("entities_to_watch") or []:
            referenced_ids.update(entity.get("signal_ids") or [])
    cross_channel = landscape.get("cross_channel") or {}
    for event in cross_channel.get("linked_events") or []:
        referenced_ids.update(event.get("signal_ids") or [])

    by_id = {s.get("id"): s for s in all_signals}
    survived = [by_id[sid] for sid in referenced_ids if sid in by_id and _is_active(by_id[sid])]
    retired = [by_id[sid] for sid in referenced_ids if sid in by_id and not _is_active(by_id[sid])]
    trimmed = sorted(sid for sid in referenced_ids if sid not in by_id)

    sections = [("survived", survived), ("retired", retired), ("trimmed", trimmed)]
    for i, (name, items) in enumerate(sections):
        if i:
            print()
        print(f"## {name} ({len(items)})")
        if name == "survived":
            for s in _newest_first(items):
                print(_format_signal(s))
        elif name == "retired":
            for s in _newest_first(items):
                successor_id = s.get("superseded_by") or "-"
                title = (s.get("title") or "").strip()
                print(
                    f"{s.get('id', '?')} | retired:{s.get('status', '?')} | "
                    f"superseded_by:{successor_id} | {title}"
                )
        else:
            for signal_id in items:
                print(signal_id)


def _validate_date(flag: str, value: str) -> str:
    if not _DATE_RE.match(value):
        _die(f"{flag} requires YYYY-MM-DD, got {value!r}")
    return value


def _load_archive(only_months: set[str] | None = None) -> tuple[list[dict], int]:
    """Read marlin_archive/*.jsonl files, de-duplicated by id.

    Returns (signals, skipped_count). Keeps, per id, the entry with the
    newest `archived_at` — a retried sync can append the same signal twice.
    Missing/empty archive dir returns ([], 0), not an error.

    `only_months` (a set of `YYYY-MM` file stems) bounds the scan to those
    monthly files — the deadline radar passes the recent months so it never
    walks a multi-year archive (scoping decision 3). None reads every file
    (the default `--archive` behavior).
    """
    if not ARCHIVE_DIR.is_dir():
        return [], 0
    by_id: dict[str, dict] = {}
    skipped = 0
    for path in sorted(ARCHIVE_DIR.glob("*.jsonl")):
        if only_months is not None and path.stem not in only_months:
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
            sid = entry.get("id")
            if not sid:
                skipped += 1
                continue
            existing = by_id.get(sid)
            if existing is None or entry.get("archived_at", "") > existing.get("archived_at", ""):
                by_id[sid] = entry
    return list(by_id.values()), skipped


def _print_archive(signals: list[dict]) -> None:
    """archived:<YYYY-MM-DD> | <triage line>, sorted archived_at desc then
    updated_seq desc."""
    ordered = sorted(
        signals,
        key=lambda s: (s.get("archived_at", ""), s.get("updated_seq", 0)),
        reverse=True,
    )
    for s in ordered:
        date = (s.get("archived_at") or "")[:10]
        print(f"archived:{date} | {_format_signal(s)}")


def _parse_deadline_date(value: object) -> date | None:
    """Parse a `deadline_at` value to its UTC calendar date, or None.

    Tolerant of the two shapes the extractor emits: a bare `YYYY-MM-DD` or a
    full ISO-8601 datetime (with a trailing `Z` or an explicit offset). Anything
    unparseable — including null, empty, or a non-string — returns None, so a
    signal with no usable deadline simply never reaches the radar. Comparison is
    on the date component only, since the radar's horizon is measured in days.

    I19: an offset-carrying timestamp is converted to UTC **before** its date is
    taken, because every caller compares the result against a UTC calendar date.
    Reading `2026-08-07T23:59:00-07:00` as Aug 7 dropped a still-future deadline
    out of the mandatory-urgent band up to ~11 hours early. A naive timestamp is
    assumed to be UTC already. This is the one shared date helper — `ack.py`
    imports it rather than keeping a second copy of the same arithmetic.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if _DATE_RE.match(text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.date()
    except ValueError:
        head = text[:10]
        if _DATE_RE.match(head):
            try:
                return date.fromisoformat(head)
            except ValueError:
                return None
        return None


def _recent_archive_months(now: datetime) -> set[str]:
    """The current and prior month as `YYYY-MM` file stems.

    Bounds the radar's archive scan (scoping decision 3): a near-future deadline
    was, in the common case, evicted recently, so scanning the current + prior
    month's archive files covers it without walking the whole archive. The
    horizon filter in `_deadline_radar` is the real bound; this only limits I/O.
    """
    cur = now.strftime("%Y-%m")
    if now.month == 1:
        prior = f"{now.year - 1:04d}-12"
    else:
        prior = f"{now.year:04d}-{now.month - 1:02d}"
    return {cur, prior}


def _deadline_radar(
    state_signals: list[dict],
    archive_signals: list[dict],
    now: datetime,
    horizon: int,
) -> list[dict]:
    """The deadline radar: active signals whose `deadline_at` lands in
    `[now − 7d, now + horizon]`, keyed on the explicit `deadline_at` field
    (never `event_time`).

    Reads both the rolling window (`state_signals`, the full state including
    retired records) and the archive (`archive_signals`), so a deadline-bearing
    signal already evicted from the window still surfaces. Rules:

    - **Active only** (design decision 6): a signal counts only when its
      `status` is `"active"`, applied to both sources — a superseded, moved, or
      cancelled deadline is never resurfaced.
    - **State wins** on id: if an id is present in state at all, its archive
      copy is ignored entirely. State carries the current status and deadline;
      the archive holds a snapshot from eviction time. This is also what stops a
      signal superseded *in state* from being revived by a stale still-active
      archive snapshot.
    - **Null / unparseable `deadline_at` never qualifies.**

    Returns annotated records `{signal, origin, days_until, deadline_date}`,
    sorted by deadline date ascending (ties: newest `updated_seq` first, then
    id), where `origin` is `"in-window"` or `"archived"`.
    """
    now_date = now.date()
    state_ids = {s.get("id") for s in state_signals}
    records: list[dict] = []

    def consider(s: dict, origin: str) -> None:
        if s.get("status", "active") != "active":
            return
        deadline_date = _parse_deadline_date(s.get("deadline_at"))
        if deadline_date is None:
            return
        days_until = (deadline_date - now_date).days
        if not (-DEADLINE_TAIL_DAYS <= days_until <= horizon):
            return
        records.append(
            {
                "signal": s,
                "origin": origin,
                "days_until": days_until,
                "deadline_date": deadline_date,
            }
        )

    for s in state_signals:
        consider(s, "in-window")
    for s in archive_signals:
        if s.get("id") in state_ids:
            continue  # state wins — the archive snapshot is never consulted
        consider(s, "archived")

    records.sort(
        key=lambda r: (
            r["deadline_date"],
            -r["signal"].get("updated_seq", 0),
            str(r["signal"].get("id", "")),
        )
    )
    return records


def _format_deadline_line(record: dict, ack_status: str = "open") -> str:
    """`<when> | <origin> | <standard index line>[ | ack:<state>]` for one hit."""
    days = record["days_until"]
    if days > 0:
        when = f"due in {days}d"
    elif days == 0:
        when = "due today"
    else:
        when = f"PASSED {-days}d ago"
    line = f"{when} | {record['origin']} | {_format_signal(record['signal'])}"
    return f"{line} | ack:{ack_status}" if ack_status != "open" else line


def _radar_ack_view(state_signals: list[dict]) -> tuple[dict[str, str], set[str]]:
    """Resolve ack state for the radar, read-only.

    Imported lazily, and only here: `ack.py` imports this module for its archive
    and date helpers, so a module-level import in the other direction would be a
    cycle. Returns the effective status per signal id plus the ids whose expired
    passed-tail an earlier run already showed. A missing or unreadable ack store
    must never break the radar, so any store error degrades to "no acks".
    """
    try:
        from ack import effective_statuses, expired_tail_already_shown, load_ack_store

        store = load_ack_store(STATE_DIR)
        statuses = effective_statuses(store, state_signals)
        return statuses, expired_tail_already_shown(store, statuses, STATE_DIR)
    except ValueError:
        return {}, set()


def _qualifying_entities(chan_signals: list[dict]) -> dict[str, list[str]]:
    """Entities appearing in ≥2 of chan_signals, verbatim from entity_tags,
    mapped to their supporting signal IDs. chan_signals is expected to
    already be scoped to one channel."""
    ids_by_entity: dict[str, list[str]] = {}
    for s in chan_signals:
        sid = s.get("id", "?")
        # de-dup entity per signal so one signal can't count twice
        for ent in dict.fromkeys(s.get("entity_tags") or []):
            ids_by_entity.setdefault(ent, []).append(sid)
    return {e: ids for e, ids in ids_by_entity.items() if len(ids) >= 2}


def _print_entity_candidates(signals: list[dict]) -> None:
    """Per channel: entities in ≥2 of the channel's signals, verbatim from
    entity_tags, with supporting signal IDs. Sorted by count desc, name asc."""
    for i, cid in enumerate(_channel_order(signals)):
        if i:
            print()
        print(f"## {cid}")
        chan_signals = [s for s in signals if s.get("channel", "?") == cid]
        qualifying = _qualifying_entities(chan_signals)
        for ent in sorted(qualifying, key=lambda e: (-len(qualifying[e]), e)):
            ids = qualifying[ent]
            print(f"{ent}\t{len(ids)}\t{','.join(ids)}")


def entities_to_watch(chan_signals: list[dict], themes_blob: str) -> list[dict]:
    """Return the final deterministic entities_to_watch value for one channel."""
    qualifying = _qualifying_entities(chan_signals)
    blob = themes_blob.lower()
    by_id = {s.get("id"): s for s in chan_signals}
    result = []
    for ent in sorted(qualifying, key=lambda e: (-len(qualifying[e]), e)):
        if ent.lower() in blob:
            continue
        ids = sorted(
            qualifying[ent], key=lambda sid: by_id[sid].get("updated_seq", 0), reverse=True
        )
        result.append({"entity": ent, "signal_ids": ids})
    return result


def _print_entity_candidates_final(chan_signals: list[dict], themes_blob: str) -> None:
    """The final paste-ready entities_to_watch JSON array for ONE channel
    (S12): qualifying entities (≥2 signals, same rule as
    _qualifying_entities) minus those named in the channel's theme blob —
    `ent.lower()` as a substring of `themes_blob.lower()`, matching
    validate.py's theme_blob check exactly. Sorted count desc then entity
    name asc; each entry's signal_ids sorted by updated_seq descending."""
    print(json.dumps(entities_to_watch(chan_signals, themes_blob), indent=2))


def _print_urgent_top(signals: list[dict], n: int) -> None:
    """Per channel: handling=urgent signals, sorted importance desc then
    updated_seq desc, capped at n, noting any dropped beyond n."""
    for i, cid in enumerate(_channel_order(signals)):
        if i:
            print()
        print(f"## {cid}")
        all_urgent = [
            signal
            for signal in signals
            if signal.get("channel", "?") == cid
            and signal.get("handling") == "urgent"
            and _is_active(signal)
        ]
        urgent = urgent_top(signals, n).get(cid, [])
        for s in urgent[:n]:
            print(_format_signal(s))
        dropped = len(all_urgent) - n
        if dropped > 0:
            print(f"# dropped {dropped} beyond top {n}")


def urgent_top(signals: list[dict], cap: int = URGENT_CAP) -> dict[str, list[dict]]:
    """Return active urgent signals per channel using --urgent-top's ordering."""
    result: dict[str, list[dict]] = {}
    for cid in _channel_order(signals):
        urgent = [
            signal
            for signal in signals
            if signal.get("channel", "?") == cid
            and signal.get("handling") == "urgent"
            and _is_active(signal)
        ]
        urgent.sort(
            key=lambda signal: (
                _as_float(signal.get("importance")),
                signal.get("updated_seq", 0),
            ),
            reverse=True,
        )
        result[cid] = urgent[:cap]
    return result


def main() -> None:
    args = _parse_argv(sys.argv[1:])

    # --now / --state-dir need no state file.
    if args.get("now"):
        print(_iso_now())
        return
    if args.get("state_dir"):
        print(str(STATE_DIR))
        return

    state = _load_state()
    all_signals = state.get("signals", [])

    # --ids and --theme-key take explicit ids and are drill/lookup tools, so they
    # operate on the FULL state — you can still inspect a retired (superseded)
    # signal by id. Every aggregate/triage view below operates on active-only, so
    # retired signals never enter the landscape (A8b deliver-then-drop).
    if "ids" in args and args.get("archive"):
        archived, skipped = _load_archive()
        _print_ids(archived, str(args["ids"]))
        if skipped:
            print(f"warning: skipped {skipped} malformed archive line(s)", file=sys.stderr)
        return

    if "ids" in args:
        # State first, archive as automatic fallback: a superseded signal is
        # still in state; a trimmed one lives only in the archive. One flag
        # resolves either, so callers never guess which corpus an id is in.
        wanted = {sid.strip() for sid in str(args["ids"]).split(",") if sid.strip()}
        known = {s.get("id") for s in all_signals}
        corpus = all_signals
        if wanted - known:
            archived, skipped = _load_archive()
            corpus = all_signals + [r for r in archived if r.get("id") not in known]
            if skipped:
                print(f"warning: skipped {skipped} malformed archive line(s)", file=sys.stderr)
        _print_ids(corpus, str(args["ids"]))
        return

    # --theme-key: look up across the full state (themes are within a channel,
    # but the IDs are unambiguous, so no channel filter is needed).
    if "theme_key" in args:
        _print_theme_key(all_signals, str(args["theme_key"]))
        return

    # --retired-since intentionally operates on the full state: retired
    # signals are excluded from every ordinary triage/landscape view below.
    if "retired_since" in args:
        try:
            threshold = int(str(args["retired_since"]))
        except ValueError:
            _die(f"--retired-since requires an integer, got {args['retired_since']!r}")
        candidates = all_signals
        if "channel" in args:
            candidates = [s for s in candidates if s.get("channel") == args["channel"]]
        _print_retired_since(candidates, threshold)
        return

    # The landscape itself is channel-keyed, so this reports whole-window
    # survivorship rather than applying --channel / --since-seq filters.
    if args.get("landscape_survivors"):
        _print_landscape_survivors(all_signals)
        return

    # --archive reads a separate on-disk log (everything ever evicted from the
    # rolling window), not marlin_state.json — it doesn't touch all_signals.
    if args.get("archive"):
        archived, skipped = _load_archive()
        if "channel" in args:
            archived = [s for s in archived if s.get("channel") == args["channel"]]
        if "entity" in args:
            needle = str(args["entity"]).lower()
            archived = [
                s
                for s in archived
                if any(needle in (e or "").lower() for e in (s.get("entity_tags") or []))
            ]
        if "signal_type" in args:
            archived = [s for s in archived if s.get("signal_type") == args["signal_type"]]
        if "since" in args:
            since = _validate_date("--since", str(args["since"]))
            archived = [s for s in archived if (s.get("archived_at") or "")[:10] >= since]
        if "until" in args:
            until = _validate_date("--until", str(args["until"]))
            archived = [s for s in archived if (s.get("archived_at") or "")[:10] <= until]
        _print_archive(archived)
        if skipped:
            print(f"warning: skipped {skipped} malformed archive line(s)", file=sys.stderr)
        return

    # --deadlines reads state AND archive (the first inspect mode to read both),
    # keyed on the explicit deadline_at field. all_signals is the full state
    # (incl. retired) on purpose: the radar filters active internally, and a
    # retired-in-state id must still block its stale archive snapshot.
    if args.get("deadlines"):
        horizon = RADAR_HORIZON_DAYS
        if "horizon" in args:
            try:
                horizon = int(str(args["horizon"]))
            except ValueError:
                _die(f"--horizon requires an integer, got {args['horizon']!r}")
        now = datetime.now(timezone.utc)
        archived, skipped = _load_archive(only_months=_recent_archive_months(now))
        show_all = bool(args.get("all"))
        statuses, tail_shown = _radar_ack_view(all_signals)
        for record in _deadline_radar(all_signals, archived, now, horizon):
            status = statuses.get(record["signal"].get("id"), "open")
            if not show_all:
                if status == "handled":
                    continue
                # Expiry announces itself once. After the ledger has recorded the
                # transition, the remaining tail days would be pure re-nagging.
                if status == "expired" and record["days_until"] < 0 and record["signal"].get("id") in tail_shown:
                    continue
            print(_format_deadline_line(record, status))
        if skipped:
            print(f"warning: skipped {skipped} malformed archive line(s)", file=sys.stderr)
        return

    signals = [s for s in all_signals if _is_active(s)]

    # --channels: enumerate channels in the active state (ignores --channel filter).
    if args.get("channels"):
        _print_channels(signals)
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
        if "themes" in args:
            if "channel" not in args:
                _die(
                    "--entity-candidates --themes emits the final "
                    "entities_to_watch set, which is per-channel; pass "
                    "--channel <id>"
                )
            _print_entity_candidates_final(signals, str(args["themes"]))
        else:
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
