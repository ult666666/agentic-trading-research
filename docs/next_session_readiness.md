# Next-Session Readiness and Morning Continuity

> **Public snapshot:** this document comes from the private repository and describes the
> private system as of the dates it gives. Many files it names, such as `config/`, `data/` and
> `prompts/` files, `AGENTS.md` and many scripts, are private and not included here. Links point
> only to files in this repository.

## Purpose

`scripts/next_session_readiness.py` provides one fail-closed answer before an unattended research
day. It does not claim perfect execution. It distinguishes:

- `official_research_path_ready`: the system can attempt a governed Official Research result;
- `actionable_buy_path_ready`: the stricter direct FMP-dependent Buy-proposal data path is present;
- `perfect_execution_guaranteed=false`: external services and machine state can still change.

Missing optional SEC identity, direct FMP credentials, a fresh FMP transport receipt, or AC power
produces advisories, not fabricated authority. A failed required model, connector, scheduler,
wake, integrity, broker-deny, timing, or clean-slot check blocks readiness.

## Live Check

```bash
python3 scripts/next_session_readiness.py --target YYYY-MM-DD
bash scripts/install_morning_continuity.sh --status
```

The atomic receipt is written to
`data/runtime/readiness/next_session_<target>.json` with mode 0600. It contains normalized booleans,
safe status tokens, model durations, and connector names only. It excludes auth output, email,
organization identifiers, credentials, model responses, session identifiers, account data,
prices, symbols, and report content.

## Required Checks

- valid covered US trading day;
- manifest and documentation truth; exact release-snapshot forensics plus a passing runtime-source
  binding. Validator-approved generated-output drift is an explicit advisory, while unsafe drift
  remains a required blocker;
- all six core LaunchAgents loaded and byte-exact, including the 05:35 macro refresh;
- accepted weekday wake at 04:55 PT and continuity LaunchAgent loaded/exact;
- untouched target autonomy, intake, and official-report slots plus no global run lock;
- writable runtime storage and at least 5 GiB free;
- AC power for the next unattended window; battery or unavailable power inspection is an explicit
  optional advisory and does not grant or withhold research authority;
- strict read-only broker permission contract;
- headless Claude auth, Sonnet 5 primary, and exact Haiku worker probes;
- connected Alpha, FMP, and Robinhood MCP services;
- a target-matched FMP transport receipt no older than 30 minutes, with authenticated regular
  quotes, positive volume context, and extended-hours transport. Failure closes only the stricter
  Buy data path; Official Watch/No-Trade research remains independently evaluated;
- 05:38 scheduled start, 05:43 start SLO, 06:10 planned completion/internal stop, 06:12 outer model
  stop, 06:15 service terminal, 06:20 legal freeze, and 1,920-second stage-budget cross-lock;
- dry-run target injection isolated from production date resolution.

The MCP status command performs serial remote health checks and has its own bounded 90-second
per-attempt budget. If any required connector is unavailable, the gate performs at most one
immediate bounded recheck. A repeated timeout or failure still fails closed; it is not treated as
proof that any connector is available.

## Continuity Service

The `com.charles.trading.continuity` user LaunchAgent runs Monday-Friday at 04:56 PT. It validates
the real PT target against the exchange calendar, starts `/usr/bin/caffeinate -i -s -t 8040`, runs
the direct read-only FMP transport probe, clears credentials, runs the readiness gate, and keeps
the hold alive until 07:10. It is skipped on non-trading days.

The service does not call a broker, request account data, alter a strategy, or grant Buy or
execution authority. Its only market request is the bounded, secret-safe FMP transport/volume
probe. Even if readiness is blocked, it preserves the wake window so downstream
fail-closed diagnostics can run. Rollback is:

```bash
bash scripts/install_morning_continuity.sh --uninstall
```

The wake/continuity pair cannot overcome a closed laptop lid, explicit shutdown, depleted battery,
power loss, network loss, provider outage, or model outage. Those conditions remain reasons for a
safe incomplete/Watch/No-Trade terminal, never guessed data.
