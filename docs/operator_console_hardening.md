# Operator console — hardening proposal (backend)

> **STATUS 2026-07-17 (second pass): IMPLEMENTED AND VERIFIED — ready to apply.**
> Every item below except #5 (deferred, latency-only) is now implemented in
> `docs/patches/operator-console-fortify-v2.patch` (4 files, 13 hunks, +81/−14), together with
> the v11 root-cause fix: the 2026-07-17 11:54 timing-contract migration (morning 05:32,
> macro postcondition binding) updated code + config + launchd but left the natural-run test
> fixtures on the old cadence — the patch completes that migration (fixture timestamps,
> outcome vocabulary, chronological event sort) and unifies the producer/validator
> observation predicate. Verified in an isolated shadow copy: **73/73 tests green**
> (test_natural_run_acceptance 37, test_operator_console + test_operator_control 36),
> plus a real `operator_console.py snapshot` self-validation pass and an empirical
> sensitive-filter behavior check (secrets/PII rejected; CIK/volume/market-cap accepted).
> Apply: `git apply --check docs/patches/operator-console-fortify-v2.patch && git apply docs/patches/operator-console-fortify-v2.patch`,
> re-run the three suites, then `launchctl kickstart -k gui/$(id -u)/com.charles.trading.console`.

Audited 2026-07-17 via a multi-agent review with adversarial verification of every finding.
**Verdict: the backend is already well-hardened — no critical or high issues.** All items below
are **low** severity after verification, appropriate for a loopback-only, single-user,
execution-disabled research tool.

These files are deterministic control-plane code protected by `.claude/hooks/protect_risk_limits.py`,
so a model Write/Edit cannot apply them. Apply via the sanctioned native-patch review path, then
re-run `scripts/test_operator_console.py` and `scripts/test_operator_control.py`.

The frontend counterparts of these findings (CSP-blocked bars, dead-icon regression, honest
surfacing of a dark source feed, a render-level error guard) were already fixed in `web/` this session.

---

## Correctness bug — `evaluate()` writes a receipt its own validator rejects (found via the v11 test)

**This is the sharpest finding — higher impact than the low items below. Fix first.**

`scripts/natural_run_acceptance.py` computes "observation eligibility" two different ways, and they
disagree:

- **Producer** — `evaluate()` (~line 774): `observation_eligible = natural_ok and health_ok`
- **Validator** — `validate_receipt()` (~line 1211): `observation_expected = check_pass["natural_attempts"]
  and check_pass["service_health"] and natural_projection_valid and state_health_consistent`

The producer omits `natural_projection_valid` and `state_health_consistent`. When a session's
**state/health projection is inconsistent** — e.g. a workflow is `at_issue` or its SLO was missed
(exactly today's real `morning:missed` state) — `state_health_consistent` is `False`, so `evaluate()`
emits `observation_eligible=True` while the validator recomputes it should be `False`. The freshly
produced receipt then fails its own `validate_receipt()` with `receipt_observation_semantics_invalid`.

**Live impact (not just a test):** on any real session with a missed SLO / at-issue workflow,
`_validated_natural_run_receipt()` re-validates the receipt `evaluate()` just wrote, gets the error,
and returns `None` — so the Overview shows the natural-run lane as *unavailable / waiting* instead of
the true `observed_not_accepted`. The console **under-reports the natural-run state on exactly the days
it matters.** It also fails `test_operator_console.py::...test_console_revalidates_v11_receipt…`
deterministically (verified by tracing: `state_health_consistent=False`, all other terms `True`).

Confirmed **unrelated to the `web/` frontend** — different module, no `web/` code path.

**Fix — single source of truth.** Make the producer's `observation_eligible` use the same terms the
validator requires, or factor the shared predicate into one helper both call. Minimal change in
`evaluate()`:

```python
# before
    observation_eligible = natural_ok and health_ok
# after — mirror validate_receipt()'s observation_expected
    observation_eligible = (
        natural_ok and health_ok
        and natural_projection_valid and state_health_consistent)
```
(`natural_projection_valid` / `state_health_consistent` must be computed in `evaluate()` from the same
inputs the validator uses; the clean refactor is to extract one `_observation_eligible(...)` predicate
that both `evaluate()` and `validate_receipt()` call, so they can never drift again.)

---

## Recommended

### 1. A dark source feed must not read as "info" (`scripts/operator_console.py` ~846)
`_source_projection(None)` returns `status="unavailable"` when `free_source_health.py` fails, but
that collapses into the same **info** code as the normal `collecting` warm-up, so `overall_status`
can still compute to `healthy` while source monitoring is actually dark.

```python
# before
    if sources["status"] in {"collecting", "unavailable"}:
        incidents.append({"code": "source_history_incomplete", "severity": "info", "scope": "source"})
    elif sources["status"] in {"degraded", "at_issue"}:
        incidents.append({"code": "source_health_at_issue", "severity": "warning", "scope": "source"})
# after
    if sources["status"] == "collecting":
        incidents.append({"code": "source_history_incomplete", "severity": "info", "scope": "source"})
    elif sources["status"] == "unavailable":
        incidents.append({"code": "source_health_unavailable", "severity": "warning", "scope": "source"})
    elif sources["status"] in {"degraded", "at_issue"}:
        incidents.append({"code": "source_health_at_issue", "severity": "warning", "scope": "source"})
```
The frontend already recognizes `source_health_unavailable` and renders the "Data sources" vital as
*monitoring dark → At issue*, so this backend change composes cleanly.

### 2. Guard the static-file stat against `OSError` (`scripts/operator_console.py` ~1230–1237)
`candidate.resolve()/relative_to()/is_file()/is_symlink()` are outside `try/except`. A request such
as `GET /assets/<~2000 chars>` makes `stat()` raise `OSError(ENAMETOOLONG)` (the 2048-byte cap is on
the whole path, not per component), which is uncaught — the client gets an abrupt reset instead of a
clean 404. Server stays up; only the requester is affected.

```python
# after
            try:
                candidate = (static_root / relative).resolve()
                candidate.relative_to(static_root)
                is_regular = candidate.is_file() and not candidate.is_symlink()
            except (OSError, ValueError):
                self._json(404, {"ok": False, "error": "not_found"}, head=head)
                return
            if not is_regular:
                self._json(404, {"ok": False, "error": "not_found"}, head=head)
                return
```

### 3. Fix the instruction sensitive-shape filter (`scripts/operator_control.py` 35–40)
Two verified defects in one place:
- **Under-inclusive:** the `:`/`=` anchor lets bare API keys/tokens/JWTs and natural-language
  passwords through, and separator-formatted SSN/card numbers evade `\b\d{9,}\b`. The module
  docstring's "never accepts … credentials" guarantee is therefore currently false.
- **Over-inclusive:** the blanket `\b\d{9,}\b` rejects legitimate instructions containing a 10-digit
  SEC CIK, share volume, market cap, or epoch timestamp.

```python
SENSITIVE_PATTERNS = (
    re.compile(r"(?i)\b(?:api[_ -]?key|secret|password|passwd|bearer|access[_ -]?token|"
               r"refresh[_ -]?token|private[_ -]?key|session[_ -]?token)\b"),
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),   # GitHub tokens
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),                          # AWS access key id
    re.compile(r"\bsk-[A-Za-z0-9-]{16,}"),                       # provider secret keys
    re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"),  # JWT
    re.compile(r"\b\d{3}[-. ]\d{2}[-. ]\d{4}\b"),                # SSN
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),                       # card-shaped runs
)
```
Also soften the module docstring line 6 from "It never accepts … credentials …" to
"It **best-effort screens** for common credential/PII shapes …", since a local screen is not a
guarantee. Dropping `\b\d{9,}\b` removes the CIK/volume false-reject; the new PII rules stay
separator-tolerant. Over-rejection remains fail-closed and safe (the instruction is inert, never
executed).

---

## Optional (defense-in-depth / near-theoretical)

### 4. Broaden the overview leak backstop (`scripts/operator_console.py` 67, 1031–1033)
The final PII/secret grep is the stated backstop but is narrow: `MASKED_ACCOUNT_RE` only matches
exactly four mask chars + four digits, and the secret term-list omits `secret`, `token`,
`access_token`, `bearer`, `private_key`, `session`, `passwd`. No live leak path exists today (the
overview is a whitelisted field set), so this is purely hardening the guard. Broaden the regex to
`(?:[*•]{2,}[- ]?)+\d{2,4}` plus SSN/long-run detection, and extend the term list.

### 5. Serve stale overview while rebuilding (`scripts/operator_console.py` ~1048)
> **STATUS 2026-07-17 (third pass): IMPLEMENTED —
> `docs/patches/operator-console-snapshotcache-v3.patch` (1 hunk, +38/−6).**
> State lock guards fields only; a separate build lock serializes builders. Polls arriving
> during an in-flight rebuild are served the last-good snapshot immediately; forced refreshes
> still wait for a fresh build; the first-ever build blocks all callers; builder exceptions
> propagate unchanged (503). Exactly one `time.monotonic()` call per `get()` and original
> force/rate-limit semantics preserved (the pinned cache test passes unmodified). Verified:
> operator suites 36/36 green + a dedicated concurrency smoke (stale served in <0.5 s during a
> slow rebuild; fresh value after completion).

`SnapshotCache.get()` previously ran up to four ~20 s subprocesses **inside** `self._lock`, so a
hung diagnostic blocked every concurrent `/api/v1/overview` caller for the whole rebuild. Latency
only; loopback single-user limits impact.

### 6. Don't wedge control on a failed thread start (`scripts/operator_control.py` ~565–580)
`start()` sets `self._active` before `thread.start()`; if thread creation raises (OS thread
exhaustion), `_active` is never cleared and every later run reports `command_already_running` until
restart. Wrap `thread.start()` in `try/except` and reset `self._active = None` on failure.

---

## Not a bug (verified, refuted)
- **Manifest `len()` "crash":** `build_manifest.py` structurally coerces `errors` to a list, so
  `len(x or [])` cannot raise — no reachable 503.
- **Frontend partial-payload white-screen:** the server fail-closes (`validate_overview` → 503) before
  a partial-but-schema-valid body can reach the client; the client's `!response.ok` path renders the
  error state. (A render-level `try/catch` guard was still added to `web/src/main.js` as cheap insurance.)
