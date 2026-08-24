#!/usr/bin/env python3
"""Classify a failed Claude CLI invocation from its captured output.

The unattended morning runner captures the combined stdout/stderr of the main Claude call. When the
call exits non-zero, this classifier reads that text and returns a coarse reason token so the runner
can (a) record an accurate failure reason and (b) decide whether a stable-model fallback attempt is
warranted (only for a pre-generation usage/credit/model-unavailability failure).

Output (stdout, one token):
  claude_usage_or_model_unavailable  -> out of usage credits / usage or rate limit / quota /
                                        requested model not available. A retry on a DIFFERENT stable
                                        model may succeed. This is a startup-time rejection.
  claude_invocation_failed           -> any other non-zero exit (generic). No model fallback.

Exit code is always 0 (the token is the signal). Reads from --from-file PATH or stdin.
"""
from __future__ import annotations

import argparse
import re
import sys

USAGE_OR_MODEL_UNAVAILABLE = "claude_usage_or_model_unavailable"
GENERIC_FAILURE = "claude_invocation_failed"
NOT_LOGGED_IN = "claude_not_logged_in"

# Signatures that indicate a usage/credit exhaustion or an unavailable/unknown model — i.e. a
# startup-time rejection where retrying on a different stable model is reasonable. Matched
# case-insensitively against the captured invocation output.
_PATTERNS = [
    r"out of usage credits",
    r"out of credits",
    r"usage[\s_-]*credits?",
    r"/usage-credits",
    r"run\s+/usage",
    r"usage limit",
    r"rate[\s_-]*limit",
    r"credit balance",
    r"insufficient\s+(usage\s+)?credits?",
    r"quota\s+(exceeded|exhausted|reached)",
    r"upgrade to keep using",
    r"switch models",
    r"model\b.{0,60}?\b(not\s+found|not\s+available|unavailable|does\s+not\s+exist|no\s+longer\s+available)",
    r"no access to (the )?model",
    r"unknown model",
]
_REGEX = re.compile("|".join(_PATTERNS), re.IGNORECASE)
# A CLI-session logout (keychain locked / OAuth expired / api key invalid) is human-only to
# fix: no model fallback can help. Kept tight to avoid matching a bare "login" substring.
_NOT_LOGGED_IN = re.compile(
    r"not\s+logged\s+in"
    r"|please\s+run\s+/login"
    r"|\brun\s+/login\b"
    r"|\blogin\s+required\b"
    r"|authentication[\s_-]*(?:required|failed|expired|error)"
    r"|\bsession\s+(?:has\s+)?expired\b"
    r"|oauth\s+(?:access\s+)?token\s+(?:is\s+)?(?:expired|invalid|revoked)"
    r"|invalid\s+api\s+key",
    re.IGNORECASE,
)


def classify(text: str) -> str:
    if text and _NOT_LOGGED_IN.search(text):
        return NOT_LOGGED_IN
    if text and _REGEX.search(text):
        return USAGE_OR_MODEL_UNAVAILABLE
    return GENERIC_FAILURE


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Classify a failed Claude CLI invocation.")
    ap.add_argument("--from-file", help="Read captured output from this file (default: stdin).")
    ap.add_argument("--text", help="Classify this literal string instead of a file/stdin.")
    args = ap.parse_args(argv)

    if args.text is not None:
        text = args.text
    elif args.from_file:
        try:
            with open(args.from_file, "r", errors="replace") as fh:
                text = fh.read()
        except Exception:
            text = ""
    else:
        text = sys.stdin.read() if not sys.stdin.isatty() else ""

    print(classify(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
