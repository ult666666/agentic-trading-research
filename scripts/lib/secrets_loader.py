#!/usr/bin/env python3
"""Secret loader — the ONLY sanctioned way scripts obtain API credentials.

Resolves a named credential from, in priority order:
  1. The process environment (e.g. ALPHAVANTAGE_API_KEY exported by a launchd wrapper).
  2. A gitignored dotenv file at secrets/trading.env (KEY=VALUE lines, '#'=comment).

STRICT VARIABLE ALLOWLIST (defense-in-depth):
  * Only names in ALLOWLIST may EVER be loaded from the dotenv or returned. A poisoned
    secrets/trading.env that tries to set PATH/HOME/SHELL/PYTHONPATH/DYLD_*/CLAUDE_BIN/etc. is
    refused — those names are never returned and never exported. This stops a tampered secrets
    file from hijacking the headless launchd run's execution environment.
  * Unrelated/arbitrary variable names are also ignored (not in the allowlist => not loaded).
  * Existing unrelated lines in the dotenv are never modified or destroyed by this reader (it is
    read-only); preservation on WRITE is the setup helper's job.

NEVER prints, logs, or returns secret VALUES through any reporting path. `has()`/`status()` reveal
only PRESENCE (bool) and the source, never the value. The raw value is returned ONLY by `get()`,
which callers must use solely to authenticate an outbound request and must never echo. The
secrets/ directory is additionally read-deny-listed in .claude/settings.json.

Std-lib only.
"""
from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SECRETS_ENV = ROOT / "secrets" / "trading.env"

# The ONLY credential names this project may load. Add new providers here explicitly.
#   ALPHAVANTAGE_API_KEY — Alpha Vantage free direct-HTTP (fallback + diagnostics).
#   DATABENTO_API_KEY    — Databento historical metadata/cost probe and shadow bake-off.
#   FMP_API_KEY          — Financial Modeling Prep direct-HTTP (primary deterministic provider).
#   TWELVE_DATA_API_KEY  — Twelve Data Basic zero-dollar shadow daily-bar feed.
#   SEC_USER_AGENT       — SEC fair-access identity/contact string; treated as private metadata.
#   SEC_USER_AGENT_APPROVED — explicit local consent marker for transmitting that identity.
# Arbitrary names and execution-environment hijack names (PATH/HOME/SHELL/CLAUDE_BIN/PYTHONPATH/
# DYLD_*/project paths/etc.) are NEVER added here and so are never loaded or exported.
ALLOWLIST = frozenset({
    "ALPHAVANTAGE_API_KEY",
    "DATABENTO_API_KEY",
    "FMP_API_KEY",
    "SEC_USER_AGENT",
    "SEC_USER_AGENT_APPROVED",
    "TWELVE_DATA_API_KEY",
})

# audited_claude.py sets this for every model child. In that environment the loader refuses both
# process-environment credentials and the dotenv fallback, so a model-launched Python subprocess
# cannot recover a key that the runner deliberately removed before generation.
MODEL_ACCESS_DENY_ENV = "TRADING_SECRET_FILE_ACCESS_DISABLED"

# Names that must NEVER be honored from a secrets file (execution-environment hijack surface).
# Loading is allowlist-based, so these are already refused; they are enumerated for explicit,
# auditable refusal logging and so a test can assert they can never leak through.
DANGEROUS = frozenset({
    "PATH", "HOME", "SHELL", "USER", "LOGNAME", "TMPDIR", "IFS",
    "CLAUDE_BIN", "CLAUDE_TIMEOUT_SECS", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "BASH_ENV", "ENV", "PROMPT_COMMAND",
})


def is_allowed(name: str) -> bool:
    return name in ALLOWLIST


def access_disabled() -> bool:
    return os.environ.get(MODEL_ACCESS_DENY_ENV) == "1"


def _parse_dotenv(path: Path) -> dict:
    """Parse KEY=VALUE lines, returning ONLY allowlisted keys. Dangerous/arbitrary keys are
    dropped (never returned). The file itself is never modified here."""
    if access_disabled():
        return {}
    out: dict[str, str] = {}
    try:
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            if key not in ALLOWLIST:
                continue  # refuse dangerous/unrelated names (PATH/HOME/DYLD_*/arbitrary)
            val = val.strip().strip('"').strip("'")
            if key:
                out[key] = val
    except Exception:
        return {}
    return out


def get(name: str) -> str | None:
    """Return the raw secret value for an ALLOWLISTED name, or None. DO NOT log the result.

    Non-allowlisted names (incl. PATH/HOME/DYLD_*/PYTHONPATH/etc.) always return None — even if
    present in the environment — so this loader can never be used to read the execution env."""
    if name not in ALLOWLIST or access_disabled():
        return None
    v = os.environ.get(name)
    if v:
        return v
    return _parse_dotenv(SECRETS_ENV).get(name) or None


def source_of(name: str) -> str | None:
    """Return WHERE an allowlisted credential was found ('env' | 'secrets_file') or None —
    never the value. Non-allowlisted names always return None."""
    if name not in ALLOWLIST or access_disabled():
        return None
    if os.environ.get(name):
        return "env"
    if _parse_dotenv(SECRETS_ENV).get(name):
        return "secrets_file"
    return None


def has(name: str) -> bool:
    return source_of(name) is not None


def refused_names_in_file() -> list[str]:
    """Names PRESENT in the dotenv that were REFUSED (not allowlisted) — for audit logging.
    Returns only the NAMES (never values), and only those that are recognized as dangerous or
    simply not allowlisted, so an operator can spot a tampered secrets file."""
    if access_disabled():
        return []
    refused = []
    try:
        for raw in SECRETS_ENV.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if key and key not in ALLOWLIST:
                refused.append(key)
    except Exception:
        pass
    return refused


def status(names: list[str] | None = None) -> dict:
    """Presence-only report. Never includes values. Defaults to the full allowlist."""
    names = names or sorted(ALLOWLIST)
    return {
        "secrets_file_present": SECRETS_ENV.exists(),
        "secrets_file_path": str(SECRETS_ENV),
        "allowlist": sorted(ALLOWLIST),
        "credentials": {n: {"present": has(n), "source": source_of(n),
                            "allowlisted": is_allowed(n)} for n in names},
        "refused_names_in_file": refused_names_in_file(),  # names only, never values
    }


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser(description="Presence-only secret status (never prints values).")
    ap.add_argument("names", nargs="*", help="credential names to check (default: full allowlist)")
    a = ap.parse_args()
    print(json.dumps(status(a.names or None), indent=2, sort_keys=True))
