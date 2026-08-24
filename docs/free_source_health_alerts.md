# Local Free-Source Health Alerts

## Purpose

scripts/free_source_health_alert.py converts the validated aggregate collection-health receipt into
deduplicated local alert transitions. It closes the operational gap between recording a failed
free source and making that failure visible without requiring the user to inspect logs every day.

The Python evaluator never sends a message. It writes:

    data/runtime/free_source_health/alert_state.json
    data/runtime/free_source_health/current_alert.json

The intake and post-close shell wrappers translate only exit code 10 (new/changed alert) and exit
code 11 (recovery) into fixed local macOS notification text. No model output, provider payload,
symbol, price, account value, credential, or arbitrary artifact text can become notification
content.

## Alert Conditions

The versioned policy is config/free_source_health_alerts.json.

An alert is active for:

- an invalid aggregate health receipt;
- aggregate health status blocked;
- one or more invalid receipt/plan session chains;
- a public zero-key source with at least two consecutive collection failures;
- a public source whose rolling health remains degraded after the latest successful collection;
- a partial public collection;
- missing approved SEC identity.

The following are intentionally not alerts:

- fewer than five sessions and status collecting;
- one isolated public-source failure;
- local point-in-time history still accumulating;
- an unchanged active incident;
- a missing health artifact before the first valid intake.

## State Transitions

- new_alert: no prior incident fingerprint existed.
- changed_alert: the active condition-code set changed.
- unchanged: the same fingerprint remains active; no repeat notification.
- resolved: a prior active fingerprint cleared; one recovery notification.
- clear: no issue is active and no prior issue needs resolution.
- unavailable: health is missing; prior state is preserved and never falsely resolved.
- state_invalid: the prior alert state failed hash/semantic validation and is not overwritten.

The incident fingerprint contains only sorted condition codes and severity. It deliberately omits
record counts and failure-streak values so a persistent incident cannot generate a new
notification every day merely because its count increased.

## Integrity and Concurrency

Both state and transition receipts are self-hashed and schema-valid. The evaluator recomputes the
free-source health receipt semantics before making a decision. A cross-process file lock serializes
intake, post-close, and attended evaluations; state and receipts use atomic replace.

A tampered state is retained for inspection, produces a blocked transition receipt, and returns a
structural error. Missing health does not clear an existing incident.

## Exit Codes

- 0: clear, unchanged, unavailable, or no local notification required;
- 10: fixed local alert notification required;
- 11: fixed local recovery notification required;
- 2: policy/state/output/self-validation failure.

The exit codes are local observability decisions, never research or trading decisions.

## Commands

    # Validate policy and its threshold/path agreement with free-source health
    python3 scripts/free_source_health_alert.py --check

    # Evaluate production state
    python3 scripts/free_source_health_alert.py

    # Isolated evaluation
    python3 scripts/free_source_health_alert.py \
      --health /tmp/health.json \
      --state /tmp/alert-state.json \
      --out /tmp/alert-receipt.json

    # Adversarial tests
    python3 scripts/test_free_source_health_alert.py

## Authority Boundary

The evaluator has source-data read access and local state-write access only. It has no network,
model, broker, account, evidence, live-price, Buy, score, risk, freeze, metric, strategy,
profitability, order, or execution authority. It does not confirm that a local notification was
delivered; autonomy health therefore always exposes notification_delivery_confirmed=false.
