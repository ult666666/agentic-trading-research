#!/usr/bin/env python3
"""Claude CLI auth liveness probe — the source-side detector for the 2026-07-22 morning failure.

WHY
---
On 2026-07-22 the unattended morning run died at 05:40 because the Claude CLI could not
authenticate ("Not logged in · Please run /login"). Root cause (confirmed): the macOS login
keychain was locked / out of sync in the launchd context, so the CLI could not read its stored
OAuth token. Both the haiku Robinhood collector and the sonnet morning run failed rc=1 for this one
reason. Nobody was told for ~5 hours.

This probe answers ONE question cheaply and read-only: *can the Claude CLI authenticate right now,
in this exact process/launchd context?* Wired into the evening (post_close 13:15) and early-morning
(continuity 04:56) jobs, it lets the system warn a human LONG before 05:38 instead of failing
silently at 05:40.

DESIGN
------
- FREE + FAST: prefers `claude auth status`, which reports login state WITHOUT a paid model call.
  Falls back to no other method — if `auth status` is unavailable, it reports `unknown` (fail-open
  to "unknown", never a false "healthy"). No model invocation, no tokens spent, no network of ours.
- BOUNDED: a hard subprocess timeout; single attempt; never loops or retries. It can never hang the
  pipeline or burn quota.
- SAME CONTEXT: run BY the launchd job it guards, so it sees the same keychain/env state that will
  bite the real `claude -p` call. A probe in a different context would give a false green.
- READ-ONLY, NO SECRETS: it parses only presence/status text; it never prints, logs, or returns a
  token value. Its receipt carries booleans and a reason string only.

Reference implementation for adoption into scripts/ under the operator applier in this directory.
Unit-tested by test_check_claude_auth.py (parsing/classification is pure and fully covered; the
subprocess call is not exercised in tests).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

SCHEMA = "claude_auth_probe/1.1"
DEFAULT_TIMEOUT_SECONDS = 20
# v1.1 (2026-07-22 evening): `claude auth status` reported logged_in:true while the durable
# token was SERVER-invalid — a real call returned '401 ... OAuth access token is invalid'.
# Local state is therefore a FALSE GREEN for the exact class that killed the 2026-07-22
# morning run. The OPT-IN --server-check (budget-gated, single attempt, hard-capped timeout)
# sends one minimal `claude -p` ping — a real authenticated round-trip — and classifies a
# 401/invalid-token response as not logged in. The receipt stores booleans + reason tokens
# only: NEVER raw ping output, NEVER a token value.
DEFAULT_SERVER_TIMEOUT_SECONDS = 60
SERVER_PING_PROMPT = "auth probe: reply with exactly: ok"
_SERVER_CHECK_ENV = "CLAUDE_AUTH_SERVER_CHECK"

# Case-insensitive signatures of an UNAUTHENTICATED CLI. Kept in sync with the failure classifier.
_NOT_LOGGED_IN = re.compile(
    r"not\s+logged\s+in"
    r"|please\s+run\s+/login"
    r"|run\s+/login"
    r"|\bloggedin\b[\"\s]*[:=][\"\s]*false"
    r"|\blogged_in\b[\"\s]*[:=][\"\s]*false"
    r"|invalid\s+api\s+key"
    r"|authentication[_\s-]*error"
    r"|unauthorized"
    r"|oauth\s+(access\s+)?token\s+(is\s+)?(expired|invalid|revoked)"
    r"|session\s+expired",
    re.IGNORECASE,
)
# Signatures that positively confirm an authenticated CLI.
_LOGGED_IN = re.compile(
    r"\bloggedin\b[\"\s]*[:=][\"\s]*true"
    r"|\blogged_in\b[\"\s]*[:=][\"\s]*true"
    r"|logged\s+in\s+as"
    r"|login[:\s].*\bactive\b",
    re.IGNORECASE,
)


def classify_auth_text(text: str) -> tuple[bool | None, str]:
    """Return (logged_in, reason). logged_in is True / False / None(unknown). Pure function."""
    if not text:
        return (None, "empty_probe_output")
    if _NOT_LOGGED_IN.search(text):
        return (False, "not_logged_in")
    if _LOGGED_IN.search(text):
        return (True, "logged_in")
    return (None, "indeterminate_probe_output")


# --- v1.1 server-side validity ---------------------------------------------------------------
# `claude auth status` reads LOCAL state only. These are decisive signatures of a SERVER-
# rejected credential in a minimal ping. Observed 2026-07-22 evening: 'API Error: 401 ...
# "OAuth access token is invalid"' while `auth status` still said logged in.
_SERVER_AUTH_REJECTED = re.compile(
    r"\b401\b"
    r"|authentication[_\s-]*error"
    r"|\bunauthorized\b"
    r"|oauth\s+(?:access\s+)?token\s+(?:is\s+)?(?:expired|invalid|revoked)"
    r"|invalid\s+(?:bearer\s+)?token"
    r"|invalid\s+api\s+key"
    r"|not\s+logged\s+in"
    r"|please\s+run\s+/login",
    re.IGNORECASE,
)
# Known NON-auth ping failures (usage/limits/model/network/server-side). NEVER classified as
# a logout: the credential may be fine — fail OPEN to the local result so a quota dip or a
# transient outage can never fake a logout (false red) the way local state faked health.
_SERVER_NONAUTH_ERROR = re.compile(
    r"usage\s+limit"
    r"|rate[\s_-]*limit"
    r"|credit"
    r"|quota"
    r"|overloaded"
    r"|\b429\b"
    r"|\b5\d\d\b"
    r"|model\b.{0,60}?\b(not\s+found|not\s+available|unavailable|does\s+not\s+exist)"
    r"|econnrefused|enotfound|etimedout|network|fetch\s+failed|connection",
    re.IGNORECASE,
)


def classify_server_ping(returncode: int, text: str) -> tuple[bool | None, str]:
    """Classify ONE bounded `claude -p` ping. Pure function (unit-tested offline).

    Auth-rejection signatures are checked FIRST and regardless of exit code (a CLI that
    prints a 401 but exits 0 must still kill the false green); success requires rc==0 AND
    non-empty output.
    """
    stripped = (text or "").strip()
    if stripped and _SERVER_AUTH_REJECTED.search(stripped):
        return (False, "server_token_invalid")
    if returncode == 0:
        return (True, "server_validated") if stripped else (None, "server_ping_empty_output")
    if stripped and _SERVER_NONAUTH_ERROR.search(stripped):
        return (None, "server_ping_nonauth_error")
    return (None, "server_ping_unclassified_failure")


def _server_check_enabled(cli_flag: bool) -> bool:
    """Budget gate. env CLAUDE_AUTH_SERVER_CHECK: 0/false/no/off force-disables (global kill
    switch), 1/true/yes/on force-enables; otherwise the CLI flag decides."""
    env = os.environ.get(_SERVER_CHECK_ENV, "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    return cli_flag


def _run_server_ping(resolved: str, timeout_seconds: int) -> tuple[int | None, str, str | None]:
    """ONE minimal print-mode ping: single attempt, hard-capped timeout, never retries.
    cwd=$HOME so project hooks/settings never inflate the call. Output text is used for
    classification ONLY and is never stored, logged, or returned in the receipt."""
    cmd = [resolved, "-p", SERVER_PING_PROMPT]
    model = os.environ.get("CLAUDE_AUTH_PROBE_MODEL", "").strip()
    if model:
        cmd += ["--model", model]
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=min(180, max(5, int(timeout_seconds))),
            stdin=subprocess.DEVNULL, check=False, cwd=os.path.expanduser("~"))
        return (completed.returncode,
                (completed.stdout or "") + "\n" + (completed.stderr or ""), None)
    except subprocess.TimeoutExpired:
        return (None, "", "server_ping_timeout")
    except OSError as exc:
        return (None, "", f"server_ping_exec_error:{type(exc).__name__}")


def _server_check_result(resolved: str, timeout_seconds: int) -> tuple[bool | None, str]:
    rc, text, fail_reason = _run_server_ping(resolved, timeout_seconds)
    if rc is None:
        return (None, fail_reason or "server_ping_unavailable")
    return classify_server_ping(rc, text)


def _resolve_claude(explicit: str | None) -> str | None:
    for candidate in (explicit, os.environ.get("CLAUDE_BIN"),
                      os.path.expanduser("~/.local/bin/claude"), shutil.which("claude")):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def probe(claude: str | None = None, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
          now_utc: datetime | None = None, server_check: bool = False,
          server_timeout_seconds: int = DEFAULT_SERVER_TIMEOUT_SECONDS) -> dict:
    now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    resolved = _resolve_claude(claude)
    base = {
        "schema": SCHEMA,
        "checked_utc": now_utc.isoformat(),
        "read_only": True,
        "model_invoked": False,
        "execution_enabled": False,
    }
    if resolved is None:
        return {**base, "ok": False, "logged_in": None, "reason": "claude_binary_not_found",
                "actionable": "install/point CLAUDE_BIN at the claude CLI"}
    try:
        completed = subprocess.run(
            [resolved, "auth", "status"],
            capture_output=True, text=True, timeout=max(1, int(timeout_seconds)),
            stdin=subprocess.DEVNULL, check=False)
        combined = (completed.stdout or "") + "\n" + (completed.stderr or "")
    except subprocess.TimeoutExpired:
        return {**base, "ok": False, "logged_in": None, "reason": "probe_timeout",
                "actionable": "claude auth status hung; investigate the CLI"}
    except OSError as exc:
        return {**base, "ok": False, "logged_in": None,
                "reason": f"probe_exec_error:{type(exc).__name__}"}
    logged_in, reason = classify_auth_text(combined)
    if logged_in is False:
        return {**base, "ok": False, "logged_in": False, "reason": reason,
                "actionable": "re-authenticate: unlock login keychain, then `claude auth login` "
                              "(or set a CLAUDE_CODE_OAUTH_TOKEN via `claude setup-token`)"}
    if server_check:
        # v1.1: local status is necessary but NOT sufficient — on 2026-07-22 `auth status`
        # said logged in while the server rejected the token (401). ONE bounded ping settles
        # it. A confirmed-local logout above never reaches here (no spend on a red probe).
        base["model_invoked"] = True
        server_state, server_reason = _server_check_result(resolved, server_timeout_seconds)
        base["server_check"] = {"enabled": True, "ran": True, "reason": server_reason}
        if server_state is False:
            return {**base, "ok": False, "logged_in": False, "reason": "server_token_invalid",
                    "local_status_reason": reason,
                    "actionable": "stored credential is SERVER-invalid despite local "
                                  "logged-in state — run `claude setup-token` again and "
                                  "replace the git-ignored durable token file (or `claude "
                                  "auth login`), then re-run with --server-check to confirm"}
        if server_state is True:
            return {**base, "ok": True, "logged_in": True, "reason": "server_validated",
                    "local_status_reason": reason}
        # Indeterminate ping (usage/network/timeout): NOT an auth verdict — fall through to
        # the local result so a quota dip or transient outage can never fake a logout.
    if logged_in is True:
        return {**base, "ok": True, "logged_in": True, "reason": reason}
    # Indeterminate: fail-open to "unknown" (never assert healthy on ambiguous output).
    return {**base, "ok": False, "logged_in": None, "reason": reason,
            "actionable": "probe output was indeterminate; verify `claude auth status` manually"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Probe whether the Claude CLI can authenticate.")
    ap.add_argument("--claude", default=None, help="explicit path to the claude binary")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    ap.add_argument("--server-check", action="store_true",
                    help="also send ONE bounded minimal `claude -p` ping to validate the "
                         "stored credential SERVER-side (spends one tiny model call; global "
                         "kill switch: env CLAUDE_AUTH_SERVER_CHECK=0)")
    ap.add_argument("--server-timeout", type=int, default=DEFAULT_SERVER_TIMEOUT_SECONDS)
    args = ap.parse_args(argv)
    result = probe(claude=args.claude, timeout_seconds=args.timeout,
                   server_check=_server_check_enabled(bool(args.server_check)),
                   server_timeout_seconds=args.server_timeout)
    print(json.dumps(result, indent=2, sort_keys=True))
    # exit 0 = logged in; 1 = not logged in; 2 = unknown/unavailable. Callers branch on this.
    return 0 if result.get("logged_in") is True else (1 if result.get("logged_in") is False else 2)


if __name__ == "__main__":
    raise SystemExit(main())
