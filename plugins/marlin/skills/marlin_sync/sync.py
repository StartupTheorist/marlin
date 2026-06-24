#!/usr/bin/env python3
"""Marlin sync — mechanical ingestion, stdlib only.

Fetches new signals from the Marlin REST API, merges into marlin_state.json
in a stable state directory (MARLIN_STATE_DIR, default ~/.marlin/ — NOT the
cwd, so diff-mode survives scheduled runs launched from anywhere), trims to a
rolling window, and prints a one-line summary. Designed to be invoked by the
marlin-sync skill so that
the mechanical steps (fetch, page, merge, trim, write) run outside the LLM
loop and don't pay token cost proportional to payload size.

Credentials (primary path, set by the skill in the child process env):
    MARLIN_URL           Base URL of the Marlin server.
    MARLIN_SYNC_GRANT    Short-lived sync grant (sfsg_...) from create_sync_grant.

Credentials (escape hatch, standalone invocation with a static token):
    MARLIN_URL           Base URL of the Marlin server.
    MARLIN_TOKEN         Long-lived static bearer token.

Manual/debug flags (last resort — argv can leak via ps/history):
    --url, --grant, --token

When a grant is supplied the script hits /sync/signals; when a static token
is supplied it hits /signals. If both are supplied, grant mode wins and a
warning goes to stderr.

Output:
    stdout: one line, e.g.
        synced 12 new signals, cursor=seq:64, last_new_signal_at=2026-04-17T19:22:05Z
    stderr: errors (auth, unreachable, corrupt state). Exits non-zero.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# State lives in a stable directory, not the cwd, so a scheduled run launched
# from an arbitrary/ephemeral working dir still finds prior state (otherwise it
# cold-starts every run — re-syncing the whole window and defeating diff mode).
# inspect.py and validate.py resolve the same directory the same way.
STATE_DIR = Path(os.environ.get("MARLIN_STATE_DIR") or (Path.home() / ".marlin")).expanduser()
STATE_PATH = STATE_DIR / "marlin_state.json"
BACKUP_PATH = STATE_DIR / "marlin_state.json.bak"
WINDOW = 100
PAGE_LIMIT = 100
MAX_PAGES = 50
TIMEOUT_SECONDS = 30


def _iso_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def _is_egress_block(text: str) -> bool:
    """A sandbox egress proxy denying a non-allowlisted host, not a real outage.

    Sandboxed runtimes (e.g. Cowork) route subprocess traffic through an
    allowlisting proxy that refuses the HTTPS CONNECT tunnel for unlisted hosts.
    The signatures below distinguish that from the server actually being down.
    """
    t = text.lower()
    return (
        "tunnel connection failed" in t
        or "blocked-by-allowlist" in t
        or ("403" in t and "forbidden" in t)
    )


def _die_network(detail: str) -> None:
    if _is_egress_block(detail):
        _die(
            "network egress blocked by the sandbox, not a server outage "
            f"({detail}). The server is reachable; this runtime restricts which "
            "hosts code may contact. Fix: allowlist the server host in your "
            "client's network settings (e.g. Cowork -> Settings -> Capabilities "
            "-> Code execution -> allowed domains), or run where outbound network "
            "is permitted."
        )
    _die(f"marlin unreachable: {detail}")


def _fetch_page(
    base_url: str, path: str, credential: str, since: str | None
) -> dict:
    params = {"limit": str(PAGE_LIMIT)}
    if since:
        params["since"] = since
    url = f"{base_url}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {credential}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code == 401:
            _die(f"marlin auth failed: {body}")
        _die(f"marlin http {e.code}: {body}")
    except urllib.error.URLError as e:
        _die_network(str(e.reason))
    except (TimeoutError, OSError) as e:
        _die_network(str(e))


def _parse_argv(argv: list[str]) -> dict[str, str]:
    """Pull --url / --grant / --token out of argv (manual/debug path only)."""
    out: dict[str, str] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--url", "--grant", "--token") and i + 1 < len(argv):
            out[arg[2:]] = argv[i + 1]
            i += 2
        else:
            i += 1
    return out


def _load_state() -> tuple[dict, bool]:
    """Return (state, corrupt_backed_up)."""
    if not STATE_PATH.exists():
        return {"version": 1, "signals": []}, False
    try:
        return json.loads(STATE_PATH.read_text()), False
    except (json.JSONDecodeError, OSError):
        STATE_PATH.rename(BACKUP_PATH)
        return {"version": 1, "signals": []}, True


def main() -> None:
    argv = _parse_argv(sys.argv[1:])

    url = (argv.get("url") or os.environ.get("MARLIN_URL") or "").strip()
    grant = (argv.get("grant") or os.environ.get("MARLIN_SYNC_GRANT") or "").strip()
    token = (argv.get("token") or os.environ.get("MARLIN_TOKEN") or "").strip()

    if not url:
        _die("MARLIN_URL must be set (or passed via --url)")
    if not grant and not token:
        _die(
            "No credential. Set MARLIN_SYNC_GRANT (primary) or MARLIN_TOKEN "
            "(escape hatch), or pass --grant / --token for manual runs."
        )
    if grant and token:
        print(
            "warning: both MARLIN_SYNC_GRANT and MARLIN_TOKEN supplied; using grant.",
            file=sys.stderr,
        )

    if grant:
        credential = grant
        path = "/sync/signals"
    else:
        credential = token
        path = "/signals"

    base_url = url.rstrip("/")
    if base_url.endswith("/mcp"):
        base_url = base_url[:-4].rstrip("/")

    state, corrupt = _load_state()
    signals_by_id: dict[str, dict] = {
        s["id"]: s for s in state.get("signals", []) if "id" in s
    }
    cursor = state.get("cursor")
    prior_last_new = state.get("last_new_signal_at")

    new_count = 0
    next_cursor = cursor
    pages = 0
    while True:
        page = _fetch_page(base_url, path, credential, next_cursor)
        for s in page.get("signals", []):
            sid = s.get("id")
            if not sid:
                continue
            existing = signals_by_id.get(sid)
            if existing is None:
                signals_by_id[sid] = s
                new_count += 1
            elif s.get("updated_seq", 0) > existing.get("updated_seq", 0):
                signals_by_id[sid] = s
        next_cursor = page.get("next_cursor") or next_cursor
        pages += 1
        if not page.get("has_more"):
            break
        if pages >= MAX_PAGES:
            _die(f"aborted after {MAX_PAGES} pages; cursor may be wrong")

    now = _iso_now()
    prefix = "restored from backup; " if corrupt else ""

    STATE_DIR.mkdir(parents=True, exist_ok=True)  # ensure the state dir exists before writing

    if new_count == 0:
        state["version"] = 1
        state["last_sync"] = now
        if next_cursor:
            state["cursor"] = next_cursor
        # preserve existing last_new_signal_at untouched
        STATE_PATH.write_text(json.dumps(state, indent=2))
        last_new = prior_last_new or "never"
        print(
            f"{prefix}synced 0 new signals, "
            f"cursor={state.get('cursor')}, last_new_signal_at={last_new}"
        )
        return

    trimmed = sorted(
        signals_by_id.values(),
        key=lambda s: s.get("updated_seq", 0),
        reverse=True,
    )[:WINDOW]

    new_state = {
        "version": 1,
        "last_sync": now,
        "last_new_signal_at": now,
        "cursor": next_cursor,
        "signals": trimmed,
    }
    STATE_PATH.write_text(json.dumps(new_state, indent=2))
    print(
        f"{prefix}synced {new_count} new signals, "
        f"cursor={next_cursor}, last_new_signal_at={now}"
    )


if __name__ == "__main__":
    main()
