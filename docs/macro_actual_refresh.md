# BLS Macro Actual Refresh

Status: active, deterministic, research-only  
Schedule: 05:35 PT / 08:35 ET on trading days  
Execution authority: none

## Purpose

The 05:00 intake captures the official BLS release calendar and baseline observations before many
08:30 ET releases occur. `scripts/macro_actual_refresh.py` closes that timing gap without rerunning
the full data intake. It binds to the exact hash-validated 05:00 calendar and BLS batches, requests
only mapped series for events due by the refresh cutoff, ingests one immutable normalized BLS
batch, and writes `data/runtime/macro_actual_refresh/<target>.json` atomically and once.

## Semantics

A newer BLS reference period after the scheduled time is labeled
`official_observation_available_after_schedule`. It is not represented as an exact publication
timestamp because the BLS Public Data API observation does not supply one. An unchanged period is
`actual_unavailable`; a changed value without a period advance is
`revision_observed_without_period_advance`.

If any due mapped series is unresolved, the receipt remains valid but `buy_gate.blocked=true`.
Official Watch or No-Trade research may continue. `scripts/research_authority.py` independently
revalidates this gate, and `scripts/run_morning_scan.sh` fingerprints the receipt before the model
and rechecks the hash during both freeze reconciliation paths.

FMP, news, or a secondary economic calendar can never substitute for an unresolved official BLS
observation. A period advance also does not establish consensus surprise, market reaction, or
causal attribution.

## Operations

```bash
python3 scripts/macro_actual_refresh.py --check
python3 scripts/macro_actual_refresh.py --target YYYY-MM-DD
python3 scripts/macro_actual_refresh.py --target YYYY-MM-DD \
  --validate data/runtime/macro_actual_refresh/YYYY-MM-DD.json
scripts/install_launchd.sh --plan --only macro-refresh
```

The `com.charles.trading.macro-refresh` LaunchAgent runs at 05:35 PT under the autonomy supervisor.
At 05:46, the watchdog may recover a completely missing supervised attempt; if sleep/wake delivery
also misses that checkpoint, a delayed Morning supervisor has one final pre-child recovery window
from 05:49 through 05:52 PT. The normal Morning schedule remains 05:38 PT. Both recovery paths
require zero prior attempts under the autonomy-state lock, repeat all normal gates, apply a final
PT cutoff, and cap the child at 90 seconds. They never retry failed/running/terminal work. The
ordinary lane also rechecks PT immediately before dispatch. A delayed LaunchAgent seeing running
state returns contention; a coherent terminal is preserved; inconsistent state/attempt/latest-id
history fails structurally without mutation.

The output is idempotent under the same repository runner lock. A valid same-target receipt is
preserved byte-for-byte without another BLS request, including when it came from an allowed direct
runner. Existing invalid, target-mismatched, symlinked, or otherwise ambiguous bytes are never
overwritten and fail closed. Non-blocked receipts must have a target-date generation vintage in
the fixed 05:30-05:54 PT startup envelope; an out-of-window direct invocation is blocked before a
provider call.

The sole exception is a no-dispatch handoff, not a child retry. If the watchdog reserved its first
attempt, passed the valid regular/early-close trading-day, integrity, strict-read-only, readiness,
and initial phase gates, but terminally skipped because its final dispatch check crossed 05:48 PT,
Morning may recover only when the exact history proves no child PID, runner event, or postcondition
ever existed. The attempt, daily-state, and current validated configuration hashes must also match.
It appends a second attempt and preserves the skipped reservation. Any malformed,
extra, ambiguous, dry-run, blocked, failed, running, child-bearing, or differently skipped history
remains non-retryable.

Recovery remains late for the 05:45 service objective: it can improve Morning's research input but
cannot repair the SLO or natural-run metrics. After 05:52, continuity is a write-free no-op. The
Morning child retains its deterministic receipt fallback when no valid artifact exists, and never
reruns a valid target-bound receipt.

## Safety

The refresh uses only read-only official BLS HTTPS requests and local immutable writes. It reads no
credential, broker, account, order, or model surface. It cannot grant research eligibility, source
quorum, score, risk approval, freeze, Buy, or execution authority; it can only add a Buy blocker.
