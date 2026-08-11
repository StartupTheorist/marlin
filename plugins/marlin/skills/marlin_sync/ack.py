#!/usr/bin/env python3
"""Per-signal acknowledgement store and lifecycle resolution helpers.

The command writes a user's acknowledged/handled disposition to the stable
state directory.  The importable helpers are also used by assemble.py to
resolve stale dispositions and persist automatic lifecycle transitions during
its mechanical write step.  Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from inspect import _load_archive, _parse_deadline_date
from params import ACK_EXPIRED_PRUNE_DAYS


# Keep this expression exactly aligned with sync.py / inspect.py / assemble.py.
STATE_DIR = Path(os.environ.get("MARLIN_STATE_DIR") or (Path.home() / ".marlin")).expanduser()
STATE_PATH = STATE_DIR / "marlin_state.json"
ACKS_PATH = STATE_DIR / "marlin_acks.json"
ACKS_LOG_PATH = STATE_DIR / "marlin_acks_log.jsonl"
_ACK_STATUSES = {"acknowledged", "handled", "expired"}


def _iso_now(now: datetime | None = None) -> str:
    value = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_retired(signal: dict) -> bool:
    return signal.get("status", "active") != "active"


def _deadline_changed(record: dict, signal: dict) -> bool:
    """Compare the captured deadline snapshot exactly, including null."""
    return record.get("deadline_at") != signal.get("deadline_at")


def resolve_effective_status(record: dict, signal: dict | None, now: datetime | None = None) -> str:
    """Return the effective lifecycle state for one stored disposition.

    `open` is intentionally represented by no store record.  Callers remove a
    stale record after persisting its automatic open transition to the ledger.
    A missing signal cannot establish a new lifecycle fact, so its stored state
    remains effective until the state/archive resolution corpus finds it.
    """
    status = record.get("status")
    if status not in _ACK_STATUSES:
        return "open"
    if signal is None:
        return status

    # Expiry is terminal, except a changed deadline is an announced extension.
    if status == "expired":
        return "open" if _deadline_changed(record, signal) else "expired"

    # A handled item remains handled through ordinary corroborating updates.
    if status == "handled":
        if _deadline_changed(record, signal) or _is_retired(signal):
            return "open"
        return "handled"

    # Retirement is a material change, not an expiry: being superseded re-opens
    # an acknowledged item exactly as it re-opens a handled one, so the fresh
    # situation gets looked at again.  `expired` is reserved for one thing only,
    # a deadline that passed.  Otherwise a sequence bump makes "seen" stale.
    if _is_retired(signal):
        return "open"
    if deadline_has_passed(signal, now):
        return "expired"
    if isinstance(signal.get("updated_seq"), int) and isinstance(record.get("ack_seq"), int) and signal["updated_seq"] > record["ack_seq"]:
        return "open"
    return "acknowledged"


def deadline_has_passed(signal: dict, now: datetime | None = None) -> bool:
    """Return whether an active signal's deadline is behind the current UTC date.

    The one shared UTC-date helper (I19): an offset-carrying deadline is
    converted to UTC before its date is compared against today's UTC date.
    """
    if _is_retired(signal):
        return False
    deadline = _parse_deadline_date(signal.get("deadline_at"))
    now_date = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    return deadline is not None and deadline < now_date


def load_ack_store(state_dir: Path = STATE_DIR) -> dict:
    """Load the latest-state ack store, treating a missing file as empty."""
    path = state_dir / "marlin_acks.json"
    if not path.exists():
        return {"version": 1, "acks": {}}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("acks"), dict):
        raise ValueError(f"{path} must contain an object with an 'acks' object")
    return {"version": 1, "acks": dict(data["acks"])}


def _save_ack_store(store: dict, state_dir: Path = STATE_DIR) -> None:
    """Replace the store atomically.

    A partial write would leave unparseable JSON, and `load_ack_store` hard-fails
    on a corrupt store — which would take every ack-aware path down with it. The
    temp file is created in the SAME directory so `os.replace` stays a rename
    within one filesystem, which is the part POSIX guarantees is atomic.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "marlin_acks.json"
    temp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        with temp.open("w") as handle:
            handle.write(json.dumps(store, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise ValueError(f"could not write {path}: {exc}") from exc


def _append_ledger(signal_id: str, record: dict, by: str, state_dir: Path = STATE_DIR) -> None:
    """Append one transition to the write-ahead ledger, durably.

    The ledger is the record the later server phase uploads, so it must never
    lag the store: callers append here BEFORE saving the store, and the fsync is
    what makes that ordering survive a crash rather than just an exception.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    entry = {"id": signal_id, **record, "by": by}
    with (state_dir / "marlin_acks_log.jsonl").open("a") as ledger:
        ledger.write(json.dumps(entry) + "\n")
        ledger.flush()
        os.fsync(ledger.fileno())


def _logged_system_transitions(state_dir: Path) -> set[tuple]:
    """Return every `(id, ack_seq, status)` key the system has already logged.

    One pass over the append-only ledger, so callers that ask about many ids
    (the radar's expired-tail check) don't re-read the file per id.
    """
    path = state_dir / "marlin_acks_log.jsonl"
    if not path.exists():
        return set()
    keys: set[tuple] = set()
    try:
        for line in path.read_text().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            seq = entry.get("ack_seq")
            if entry.get("by") != "system" or not isinstance(seq, (int, float, str, type(None))):
                continue
            keys.add((entry.get("id"), seq, entry.get("status")))
    except OSError as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc
    return keys


def _system_transition_logged(signal_id: str, ack_seq: object, status: str, state_dir: Path) -> bool:
    """Return whether the append-only ledger already has this transition key."""
    return (signal_id, ack_seq, status) in _logged_system_transitions(state_dir)


def expired_tail_already_shown(store: dict, statuses: dict[str, str], state_dir: Path = STATE_DIR) -> set[str]:
    """Return the expired ids whose passed-deadline tail an earlier run showed.

    The radar's passed tail normally repeats for `DEADLINE_TAIL_DAYS`, which
    would turn expiry itself into a week of re-nagging.  A signal is "already
    shown" once the ledger carries its system expiry line — written by the
    finish/write step at the end of the run that first observed the expiry, so
    the run that discovers it still prints the tail exactly once.  The key
    includes `ack_seq`, so a deadline extension that re-opens and later re-expires
    a signal gets its own single showing rather than being silenced by the old
    record.
    """
    keys = _logged_system_transitions(state_dir)
    acks = store.get("acks") or {}
    shown: set[str] = set()
    for sid, status in statuses.items():
        if status != "expired":
            continue
        record = acks.get(sid)
        seq = record.get("ack_seq") if isinstance(record, dict) else None
        if (sid, seq, "expired") in keys:
            shown.add(sid)
    return shown


def prune_expired_records(store: dict, now: datetime | None = None) -> bool:
    """Prune only long-expired latest-state records; the ledger is untouched."""
    cutoff = (now or datetime.now(timezone.utc)).astimezone(timezone.utc) - timedelta(days=ACK_EXPIRED_PRUNE_DAYS)
    acks = store.setdefault("acks", {})
    stale = [
        sid for sid, record in acks.items()
        if isinstance(record, dict)
        and record.get("status") == "expired"
        and (recorded := _parse_time(record.get("at"))) is not None
        and recorded < cutoff
    ]
    for sid in stale:
        del acks[sid]
    return bool(stale)


def load_resolution_corpus(state_signals: list[dict], store: dict) -> dict[str, dict]:
    """Build state plus archive coverage for every non-expired ack-store id.

    `_load_archive` is inspect.py's established archive-by-id machinery.  It
    walks the monthly archive as needed instead of applying the radar's fixed
    two-month slice; state wins over an archived snapshot for duplicate IDs.
    """
    by_id = {signal.get("id"): signal for signal in state_signals if isinstance(signal.get("id"), str)}
    wanted = {
        sid for sid, record in (store.get("acks") or {}).items()
        if isinstance(record, dict) and record.get("status") != "expired" and sid not in by_id
    }
    if wanted:
        archived, _skipped = _load_archive()
        for signal in archived:
            sid = signal.get("id")
            if sid in wanted and sid not in by_id:
                by_id[sid] = signal
    return by_id


def bounded_corpus(state_signals: list[dict], archive_signals: list[dict] | None = None) -> dict[str, dict]:
    """Merge state over archive coverage, keyed by id — state always wins.

    The archive half is whatever the caller already read for the deadline radar
    (current + prior month). Passing it keeps the `open`-expiry sweep bounded to
    signals a run legitimately looks at, instead of backfilling the whole
    archive's history of long-dead deadlines.
    """
    by_id: dict[str, dict] = {}
    for signal in list(archive_signals or []) + list(state_signals):
        sid = signal.get("id")
        if isinstance(sid, str):
            by_id[sid] = signal
    return by_id


def effective_statuses(
    store: dict,
    state_signals: list[dict],
    now: datetime | None = None,
    archive_signals: list[dict] | None = None,
) -> dict[str, str]:
    """Resolve every stored record, plus the expiries `open` cannot record itself.

    `open` is the absence of a record, so a user who never acks anything would
    never see a deadline expire — its signal would sit in the action surfaces
    forever and its radar tail would repeat for the full tail window. The second
    pass derives `expired` for record-less signals whose deadline has passed, so
    the memo's "expiry goes quiet by itself" holds without a prior ack.
    """
    corpus = load_resolution_corpus(state_signals, store)
    statuses = {
        sid: resolve_effective_status(record, corpus.get(sid), now)
        for sid, record in (store.get("acks") or {}).items()
        if isinstance(record, dict)
    }
    for sid, signal in bounded_corpus(state_signals, archive_signals).items():
        if sid not in statuses and deadline_has_passed(signal, now):
            statuses[sid] = "expired"
    return statuses


def annotate_ack_statuses(signals: list[dict], statuses: dict[str, str]) -> list[dict]:
    """Return copied records with non-open effective ack values annotated."""
    result: list[dict] = []
    for signal in signals:
        copied = dict(signal)
        annotations = dict(signal.get("annotations") or {})
        status = statuses.get(signal.get("id"))
        if status and status != "open":
            annotations["ack"] = status
        else:
            annotations.pop("ack", None)
        copied["annotations"] = annotations
        result.append(copied)
    return result


def pending_transitions(
    store: dict,
    state_signals: list[dict],
    now: datetime | None = None,
    archive_signals: list[dict] | None = None,
) -> list[dict]:
    """Return the automatic status changes a write step would persist, read-only.

    Shared by `persist_effective_transitions` (which applies them) and
    `assemble.py --gate` (which only reports that they are waiting).
    """
    corpus = bounded_corpus(state_signals, archive_signals)
    statuses = effective_statuses(store, state_signals, now, archive_signals)
    acks = store.get("acks") or {}
    pending: list[dict] = []
    for sid, effective in statuses.items():
        record = acks.get(sid)
        stored = record.get("status") if isinstance(record, dict) else "open"
        if stored == effective:
            continue
        if isinstance(record, dict):
            transition = dict(record)
        elif effective == "expired":
            # An expiry nobody acked: the signal itself carries the facts the
            # record needs, so a later extension can still re-open it.
            signal = corpus.get(sid) or {}
            transition = {"ack_seq": signal.get("updated_seq"), "deadline_at": signal.get("deadline_at")}
        else:
            continue
        transition["status"] = effective
        pending.append({"id": sid, "from": stored, "to": effective, "record": transition})
    return sorted(pending, key=lambda item: item["id"])


def persist_effective_transitions(
    state_signals: list[dict],
    state_dir: Path = STATE_DIR,
    now: datetime | None = None,
    archive_signals: list[dict] | None = None,
) -> bool:
    """Persist automatic status changes once, during assemble's write path.

    The ledger's `(id, ack_seq, new status)` tuple is the idempotency key. An
    open transition removes the latest-state record (open has no store entry),
    while its ledger line remains as the durable history.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    store = load_ack_store(state_dir)
    changed = False
    for item in pending_transitions(store, state_signals, now, archive_signals):
        sid, effective = item["id"], item["to"]
        transition = dict(item["record"])
        transition["at"] = _iso_now(now)
        if effective == "open":
            store["acks"].pop(sid, None)
        else:
            store["acks"][sid] = transition
        # Ledger before store, and idempotent across the validate-fix loop.
        if not _system_transition_logged(sid, transition.get("ack_seq"), effective, state_dir):
            _append_ledger(sid, transition, "system", state_dir)
        changed = True
    if prune_expired_records(store, now):
        changed = True
    if changed:
        _save_ack_store(store, state_dir)
    return changed


def _load_state_signals() -> list[dict]:
    if not STATE_PATH.exists():
        raise ValueError(f"{STATE_PATH} not found; run sync.py first")
    try:
        state = json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read {STATE_PATH}: {exc}") from exc
    if not isinstance(state, dict) or not isinstance(state.get("signals"), list):
        raise ValueError(f"{STATE_PATH} must contain a signals array")
    return state["signals"]


def _find_signal(signal_id: str, state_signals: list[dict]) -> dict | None:
    found = next((signal for signal in state_signals if signal.get("id") == signal_id), None)
    if found is not None:
        return found
    archived, _skipped = _load_archive()
    return next((signal for signal in archived if signal.get("id") == signal_id), None)


def set_ack(signal_id: str, status: str, note: str | None = None, now: datetime | None = None) -> dict:
    """Record one user disposition, resolving an archived signal when needed."""
    if status not in {"acknowledged", "handled"}:
        raise ValueError("status must be acknowledged or handled (expired is system-only)")
    signal = _find_signal(signal_id, _load_state_signals())
    if signal is None:
        raise ValueError(f"unknown signal id: {signal_id}")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    store = load_ack_store()
    record = {
        "status": status,
        "at": _iso_now(now),
        "ack_seq": signal.get("updated_seq"),
        "deadline_at": signal.get("deadline_at"),
    }
    if note is not None:
        record["note"] = note
    store["acks"][signal_id] = record
    prune_expired_records(store, now)
    # Ledger first, store second. The ledger is the write-ahead record, so a
    # crash between the two must leave it AHEAD of the store (a transition
    # recorded but not yet effective), never behind (an effective ack with no
    # history). The user path re-records deliberately rather than de-duplicating:
    # two identical ledger lines are a faithful account of two invocations.
    _append_ledger(signal_id, record, "user")
    _save_ack_store(store)
    return record


def _list_records(status: str | None) -> list[tuple[str, dict]]:
    store = load_ack_store()
    # Listing is read-only. The CLI write path and assemble's mechanical write
    # path both apply retention.
    records = [
        (sid, record) for sid, record in store["acks"].items()
        if isinstance(record, dict) and (status is None or record.get("status") == status)
    ]
    return sorted(records, key=lambda item: (item[1].get("at", ""), item[0]), reverse=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Record a Marlin signal acknowledgement.")
    parser.add_argument("signal_id", nargs="?")
    parser.add_argument("status", nargs="?", choices=("acknowledged", "handled"))
    parser.add_argument("--note")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--status", choices=("acknowledged", "handled", "expired"))
    args = parser.parse_args()
    try:
        if args.list:
            if args.signal_id:
                parser.error("--list takes no signal id or disposition")
            for signal_id, record in _list_records(args.status):
                print(json.dumps({"id": signal_id, **record}, sort_keys=True))
            return
        if not args.signal_id or not args.status:
            parser.error("provide <signal_id> acknowledged|handled, or use --list")
        record = set_ack(args.signal_id, args.status, args.note)
        print(json.dumps({"id": args.signal_id, **record}, sort_keys=True))
    except ValueError as exc:
        print(f"ack: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
