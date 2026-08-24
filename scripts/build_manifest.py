#!/usr/bin/env python3
"""Build a project integrity manifest.

Records path, size, mtime for every tracked file, plus a SHA-256 for critical governance,
risk, scheduler, and brokerage-permission files. Never reads anything under secrets/ and
never records secret values. Std-lib only. Writes data/MANIFEST.json and prints a summary.

`--check` is READ-ONLY: it recomputes the critical hashes and compares them against the
stored manifest WITHOUT rewriting it, exiting nonzero on drift/missing. (Previously any
argument was silently ignored and every invocation — including "checks" — rewrote the
manifest, so a check could never actually detect drift.)
"""
from __future__ import annotations
import argparse, hashlib, json, os, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCUMENTATION_TRUTH_TIMEOUT_SECONDS = 90

# Files whose integrity we hash (governance / risk / scheduler / permissions).
CRITICAL = [
    "AGENTS.md",
    "CLAUDE.md",
    "config/risk_limits.yaml",
    "config/strategy.yaml",
    # 2026-08-16 audit: the mandatory fail-closed gates named in CLAUDE.md sec.7/8/14 and the
    # protected options limits were unpinned while web/dist bundles were pinned. A silent edit
    # to the risk gate must fail the next run closed exactly like an edit to risk_limits.yaml.
    "config/options_risk_limits.yaml",
    "scripts/lib/risk_gate.py",
    "scripts/lib/options_risk_gate.py",
    ".claude/settings.json",
    # .claude/settings.local.json is intentionally ignored machine-local state. The strict broker
    # validator audits it when present, but a portable release manifest must never require or
    # enumerate ignored local files that release_snapshot.py deliberately excludes.
    ".claude/hooks/protect_risk_limits.py",
    ".codex/hooks/protect_risk_limits.py",
    ".codex/hooks/protect_paths_bash.py",
    ".codex/hooks.json",
    "scripts/test_protected_path_hooks.py",
    ".claude/hooks/validate_report.py",
    ".claude/hooks/session_start.sh",
    "scripts/run_morning_scan.sh",
    "scripts/test_morning_runner_bash32.py",
    "scripts/candidate_quote_barrier.py",
    "scripts/test_candidate_quote_barrier.py",
    "scripts/classify_claude_failure.py",
    "scripts/check_claude_auth.py",
    "scripts/auth_attention.py",
    "scripts/sentry_red_bridge.py",
    "scripts/run_premarket_refresh.sh",
    "scripts/run_post_close_review.sh",
    "scripts/run_kalshi_observation.sh",
    "scripts/install_launchd.sh",
    "scripts/lib/common.sh",
    "scripts/lib/run_lock.py",
    "scripts/test_run_lock.py",
    "scripts/check_power_state.py",
    "scripts/journal_disk_reconcile.py",
    "scripts/lib/audited_window.py",
    "scripts/test_audited_window_wiring.py",
    "scripts/validate_market_day.py",
    "scripts/score_candidate.py",
    "scripts/freeze_gate.py",
    "scripts/freeze_report.py",
    "scripts/legacy_morning_receipt.py",
    "scripts/test_legacy_morning_receipt.py",
    "scripts/test_legacy_v1_source_gate.py",
    "scripts/freeze_completion_probe.py",
    "scripts/lib/launchd_contract.py",
    "scripts/test_launchd_contract.py",
    "scripts/test_legacy_receipt_audit.py",
    "scripts/morning_authority.py",
    "scripts/test_morning_authority_gate.py",
    "scripts/rh_availability.py",
    # 2026-08-04. Both are control-plane authorities and must be signed:
    #  * mcp_registry_probe.py MEASURES the live MCP registry and is what
    #    validate_broker_permissions.py --require-live binds its PASS to. If it could be edited
    #    without a re-sign, the measured half of the declared-vs-measured check would be
    #    forgeable — reintroducing exactly the gap that hid the 2026-07-22..08-04 outage.
    #  * control_plane_reconcile.py asserts the declared-vs-measured invariants themselves.
    "scripts/mcp_registry_probe.py",
    "scripts/control_plane_reconcile.py",
    # 2026-08-04: proves the broker lane under the REAL unattended environment. If it
    # could drift unsigned, the only evidence that production works would be forgeable.
    "scripts/launchd_env_rehearsal.sh",
    "scripts/lib/system_state_publish.py",
    "scripts/test_p0_guard_observability.py",
    "scripts/test_morning_freeze_handoff.py",
    "scripts/terminal_reconcile.py",
    "scripts/test_terminal_reconcile.py",
    "scripts/timeout_checkpoint.py",
    "scripts/test_post_freeze_exit.py",
    "scripts/test_duplicate_freeze_guard.py",
    "scripts/freeze_candidates.py",
    "scripts/test_freeze_candidates.py",
    "scripts/candidate_artifact_health.py",
    "scripts/test_candidate_artifact_health.py",
    "scripts/test_candidate_artifact_health_rollup.py",
    "scripts/test_candidate_health_wiring.py",
    "scripts/test_candidate_health_rollup_surfacing.py",
    "scripts/test_hardening.py",
    "scripts/lib/tzutil.py",
    "scripts/lib/receipt_hardening.py",
    "scripts/lib/portfolio_concentration.py",
    "scripts/lib/run_context.py",
    "scripts/lib/report_source_adapter.py",
    "scripts/lib/morning_report_console.py",
    "config/market_schedule.yaml",
    "scripts/lib/fractional_policy.py",
    "scripts/build_universe.py",
    "scripts/source_router.py",
    "scripts/test_source_router_health.py",
    "prompts/run_official_morning.md",
    "docs/source_router_health.md",
    "scripts/candidate_pipeline.py",
    "scripts/score_audit.py",
    "scripts/replay_session.py",
    "scripts/ledger_validate.py",
    "scripts/version_stamp.py",
    "scripts/av_budget.py",
    "scripts/validate_trial_baseline.py",
    "scripts/improvement_review.py",
    "scripts/run_diagnostics_emit.py",
    "scripts/test_improvement_review.py",
    # Run-bound Morning finality diagnostics and the deterministic post-run engineering audit.
    "config/morning_engineering_audit.json",
    "schemas/morning_engineering_audit_policy.schema.json",
    "schemas/morning_stage_telemetry.schema.json",
    "schemas/morning_engineering_audit.schema.json",
    "schemas/morning_engineering_audit_receipt.schema.json",
    "scripts/morning_stage_telemetry.py",
    "scripts/morning_finality_guard.py",
    "scripts/morning_engineering_audit.py",
    "scripts/morning_audit_supervisor.py",
    "scripts/test_morning_engineering_audit.py",
    "scripts/test_morning_audit_supervisor.py",
    "docs/morning_engineering_audit.md",
    # The shared pre-runtime source binding keeps exact release forensics distinct from validated
    # generated-output drift; every lock-taking lane depends on it.
    "scripts/runtime_provenance.py",
    "scripts/test_runtime_provenance.py",
    "scripts/test_runtime_provenance_enforcement.py",
    "scripts/workflow_quality.py",
    "scripts/source_circuit.py",
    "scripts/rehearse_session.py",
    "scripts/test_phase4.py",
    "scripts/decision_path.py",
    "scripts/coverage_policy.py",
    "scripts/discover_universe.py",
    "scripts/test_phase42.py",
    "scripts/validate_broker_permissions.py",
    "scripts/test_broker_permissions.py",
    # Cross-broker aggregation. The generic aggregate remains shadow-only; the dedicated Webull
    # path is production tighten-only and can never grant sizing, Buy, or execution authority.
    "config/broker_aggregation.json",
    "config/webull_connector_policy.json",
    "config/asset_allocation_policy.json",
    "schemas/sanitized_broker_snapshot.schema.json",
    "schemas/broker_account_aggregate.schema.json",
    "schemas/webull_model_capture.schema.json",
    "schemas/asset_allocation_context.schema.json",
    "prompts/webull_readonly_collector.md",
    "scripts/broker_account_aggregate.py",
    "scripts/webull_connector.py",
    "scripts/run_webull_readonly_collector.sh",
    "scripts/asset_allocation_context.py",
    "scripts/portfolio_risk_artifact.py",
    "scripts/test_broker_account_aggregate.py",
    "scripts/test_webull_connector.py",
    "scripts/test_asset_allocation_context.py",
    "scripts/test_webull_morning_integration.py",
    "scripts/test_risk_artifact.py",
    "docs/broker_account_aggregation.md",
    "scripts/test_morning_schedule.py",
    "scripts/stage_decision.py",
    "scripts/test_headless_contract.py",
    "scripts/test_prompt_discipline.py",
    "scripts/data_preflight.py",
    "scripts/providers.py",
    "scripts/preliminary_label.py",
    "scripts/snapshot_gate.py",
    "scripts/limited_coverage.py",
    "scripts/premarket_simulation.py",
    "scripts/notify_summary.py",
    "scripts/test_limited_coverage.py",
    "scripts/test_notify_summary.py",
    "config/limited_coverage_basket.yaml",
    "scripts/validate_robinhood_snapshot.py",
    "scripts/compare_price_sources.py",
    "scripts/run_robinhood_readonly_collector.sh",
    "scripts/robinhood_unavailable_streak.py",
    "scripts/test_robinhood_unavailable_streak.py",
    "scripts/rh_integration_selftest.sh",
    "scripts/test_rh_collector_write_block.py",
    "scripts/test_robinhood_verifier.py",
    "config/robinhood_verifier.yaml",
    "prompts/robinhood_readonly_collector.md",
    "prompts/morning_scan.md",
    "prompts/premarket_refresh.md",
    # Post-discovery candidate-specific Robinhood quote hydration. Read-only, audit-receipt-bound,
    # and never market-anchor / second-Buy-source / execution authority.
    "config/candidate_quote_hydration.json",
    "schemas/candidate_quote_hydration_policy.schema.json",
    "schemas/candidate_quote_request.schema.json",
    "schemas/candidate_quote_bundle.schema.json",
    "schemas/candidate_quote_health.schema.json",
    "prompts/robinhood_candidate_quote_collector.md",
    "scripts/candidate_quote_hydrator.py",
    "scripts/run_candidate_quote_hydrator.sh",
    "scripts/test_candidate_quote_hydrator.py",
    "docs/candidate_quote_hydration.md",
    # Phase: primary-stale / secondary-live supplemental behavior + entitlement cache + web fallback.
    "scripts/entitlement_cache.py",
    "scripts/web_catalyst_fallback.py",
    "scripts/morning_health.py",
    "scripts/proof_primary_stale_secondary_live.py",
    "scripts/test_supplemental_verifier.py",
    "scripts/test_web_catalyst_fallback.py",
    "scripts/test_entitlement_cache.py",
    "scripts/test_morning_health.py",
    "scripts/test_freeze_snapshot_authority.py",
    "scripts/lib/secrets_loader.py",
    "scripts/setup_alpha_vantage_key.sh",
    "scripts/setup_fmp_key.sh",
    "scripts/fmp_connection_check.py",
    "config/fmp_data_rights.json",
    "scripts/fmp_dataset_entitlement.py",
    "scripts/test_fmp_dataset_entitlement.py",
    "config/fmp_paid_enrichment.json",
    "scripts/fmp_paid_enrichment.py",
    "scripts/test_fmp_paid_enrichment.py",
    "docs/fmp_paid_integration.md",
    "scripts/post_close_market_snapshot.py",
    "scripts/test_post_close_market_snapshot.py",
    "scripts/post_open_market_snapshot.py",
    "scripts/test_post_open_market_snapshot.py",
    "scripts/post_open_finality.py",
    "scripts/test_post_open_finality.py",
    "scripts/post_close_report_contract.py",
    "scripts/test_post_close_report_contract.py",
    "scripts/test_post_close_outcomes.py",
    "scripts/test_fmp.py",
    "scripts/test_fmp_connection_check.py",
    "scripts/preflight_selftest.sh",
    "scripts/integration_selftest.sh",
    "scripts/test_integration_selftest_contract.py",
    "scripts/web_tools_selftest.sh",
    "scripts/test_preflight.py",
    "data/broker_tool_namespace.json",
    "config/universe.yaml",
    "config/preflight.yaml",
    "config/source_priority.yaml",
    "config/versions.yaml",
    "config/trial_baseline.yaml",
    "launchd/com.charles.trading.morning.plist",
    "launchd/com.charles.trading.premarket.plist",
    "launchd/com.charles.trading.postclose.plist",
    # 2026-07-02 fresh-eyes hardening: official-slot protection + this manifest tool itself.
    "scripts/archive_report.py",
    "scripts/build_manifest.py",
    "scripts/test_fresh_eyes_hardening.py",
    # 2026-07-02 governance v2: free parallel research ensemble (three-authority separation).
    ".claude/hooks/protect_paths_bash.py",
    ".claude/hooks/protect_broker_tools.py",
    # 2026-07-27 single-writer guard: concurrent agent sessions in one worktree are what
    # re-broke the manifest after a human had already re-signed it.
    "scripts/worktree_owner.py",
    "scripts/test_worktree_owner.py",
    # Single authoritative brokerage namespace identity (2026-07-22 rename class of defect).
    "scripts/lib/broker_namespace_policy.py",
    "scripts/test_broker_namespace_policy.py",
    # Fail-closed hashing + the single authoritative external-source pin.
    "scripts/lib/secure_hash.py",
    "scripts/lib/kalshi_source_pin.py",
    "scripts/test_secure_hash.py",
    ".codex/hooks/protect_broker_tools.py",
    "scripts/test_protect_broker_tools.py",
    "scripts/source_matrix.py",
    "scripts/evidence_schema.py",
    "scripts/deterministic_adjudicator.py",
    "scripts/test_deterministic_adjudicator.py",
    "scripts/evidence_freshness.py",
    "scripts/research_authority.py",
    "scripts/research_gate.py",
    "scripts/research_freeze.py",
    "scripts/freeze_request.py",
    "scripts/model_integrity_binding.py",
    "scripts/research_metrics.py",
    "scripts/test_research_metrics.py",
    "scripts/outcome_tracker.py",
    "scripts/test_outcome_tracker.py",
    "scripts/test_research_attribution.py",
    "scripts/data_quality.py",
    "scripts/time_budget.py",
    "scripts/test_deadline_checkpoint.py",
    "scripts/test_research_authority.py",
    "scripts/test_evidence_schema.py",
    "scripts/test_evidence_freshness.py",
    "scripts/test_free_ensemble.py",
    "scripts/test_freeze_request.py",
    "scripts/test_model_integrity_binding.py",
    "scripts/test_outer_publisher_provenance.py",
    "scripts/test_research_freeze_prose.py",
    "scripts/test_agent_contract_parity.py",
    "scripts/proof_free_ensemble.py",
    "config/governance.yaml",
    "config/evidence_freshness.json",
    "config/source_authority.yaml",
    "config/research_budget.yaml",
    "config/model_policy.yaml",
    "scripts/select_main_model.py",
    "scripts/test_production_model_selection.py",
    ".claude/agents/market-state-agent.md",
    ".claude/agents/catalyst-discovery-agent.md",
    ".claude/agents/macro-event-agent.md",
    ".claude/agents/sec-ir-verification-agent.md",
    ".claude/agents/technical-historical-agent.md",
    ".claude/agents/portfolio-risk-agent.md",
    ".claude/agents/skeptic-agent.md",
    ".claude/agents/evidence-adjudicator.md",
    ".claude/agents/final-strategy-synthesizer.md",
    ".claude/agents/portfolio-synthesizer.md",
    ".codex/agents/market-state-agent.toml",
    ".codex/agents/catalyst-discovery-agent.toml",
    ".codex/agents/macro-event-agent.toml",
    ".codex/agents/sec-ir-verification-agent.toml",
    ".codex/agents/technical-historical-agent.toml",
    ".codex/agents/portfolio-risk-agent.toml",
    ".codex/agents/skeptic-agent.toml",
    ".codex/agents/evidence-adjudicator.toml",
    ".codex/agents/final-strategy-synthesizer.toml",
    ".codex/agents/portfolio-synthesizer.toml",
    "schemas/evidence_freshness_policy.schema.json",
    "docs/evidence_freshness.md",
    "DAY_ONE_RUNBOOK.md",
    # 2026-07-09 autonomous research control plane (strictly research-only, no execution).
    "config/autonomy_workflows.json",
    "config/data_sources.yaml",
    "scripts/autonomy_orchestrator.py",
    "scripts/test_autonomy_orchestrator.py",
    "scripts/launchd_scheduler_health.py",
    "scripts/test_launchd_scheduler_health.py",
    "scripts/autonomy_service_health.py",
    "scripts/test_autonomy_service_health.py",
    "config/natural_run_acceptance.json",
    "schemas/natural_run_acceptance_policy.schema.json",
    "schemas/natural_run_acceptance_receipt.schema.json",
    "schemas/natural_run_acceptance_rollup.schema.json",
    "scripts/natural_run_acceptance.py",
    "scripts/test_natural_run_acceptance.py",
    "docs/natural_run_acceptance.md",
    "config/morning_dag.json",
    "scripts/morning_dag.py",
    "scripts/test_morning_dag.py",
    "schemas/predecision_event.schema.json",
    "scripts/event_ledger.py",
    "scripts/test_event_ledger.py",
    "schemas/release_snapshot.schema.json",
    "scripts/release_snapshot.py",
    "scripts/test_release_snapshot.py",
    "config/claude_broker_audit_settings.json",
    "scripts/broker_runtime_audit.py",
    "scripts/audited_claude.py",
    "scripts/control_plane_integrity.py",
    "scripts/test_broker_runtime_audit.py",
    "scripts/test_control_plane_integrity.py",
    "scripts/test_envelope_digest_binding.py",
    "scripts/postclose_install_preflight.py",
    "scripts/test_postclose_install_preflight.py",
    "scripts/post_close_finality.py",
    "scripts/test_post_close_finality.py",
    # 2026-08-17 post-close maintenance mode: the receipt module IS the fourth fail-closed
    # layer (it is what turns an expected skip into a validated one), so it has to be under
    # the same integrity gate as the three layers around it.
    "scripts/post_close_maintenance_receipt.py",
    "scripts/test_post_close_maintenance_receipt.py",
    "schemas/post_close_maintenance_receipt.schema.json",
    # 2026-07-09 Phase 1A point-in-time data foundation + deterministic 05:00 intake.
    "config/data_lake.json",
    "launchd/com.charles.trading.intake.plist",
    "scripts/run_data_intake.sh",
    "scripts/provider_adapters/__init__.py",
    "scripts/provider_adapters/base.py",
    "scripts/provider_adapters/sec_edgar.py",
    "scripts/provider_adapters/bls_public.py",
    "scripts/provider_adapters/bls_release_calendar.py",
    "scripts/provider_adapters/local_bars.py",
    "scripts/data_lake_ingest.py",
    "scripts/data_lake_snapshot.py",
    "scripts/data_lake_quality.py",
    "scripts/data_lake_intake.py",
    "scripts/universe_master.py",
    "scripts/test_provider_adapters.py",
    "scripts/test_data_lake_foundation.py",
    "scripts/test_data_lake_coverage_acceptance.py",
    "scripts/test_data_intake_schedule.py",
    "scripts/test_bls_release_calendar.py",
    "docs/data_lake.md",
    # Exact-batch BLS actual refresh between intake and the morning decision path.
    "config/macro_actual_refresh.json",
    "scripts/macro_actual_refresh.py",
    "scripts/test_macro_actual_refresh.py",
    "scripts/run_macro_actual_refresh.sh",
    "launchd/com.charles.trading.macro-refresh.plist",
    "docs/macro_actual_refresh.md",
    # Disabled two-provider data bake-off: aggregate shadow diagnostics and license hard gate.
    "config/provider_bakeoff.json",
    "config/provider_license_matrix.template.json",
    "schemas/provider_bakeoff_observation.schema.json",
    "schemas/provider_quality_receipt.schema.json",
    "schemas/provider_bakeoff_scorecard.schema.json",
    "schemas/provider_license_matrix.schema.json",
    "scripts/provider_bakeoff.py",
    "scripts/test_provider_bakeoff.py",
    # Credential-safe Databento metadata/cost onboarding; still disabled and shadow-only.
    "scripts/setup_databento_key.sh",
    "scripts/databento_onboarding.py",
    "scripts/test_databento_onboarding.py",
    "docs/provider_onboarding.md",
    # Zero-dollar Twelve Data Basic shadow lane; no live-price or production authority.
    "scripts/setup_twelve_data_key.sh",
    "scripts/provider_adapters/twelve_data_free.py",
    "scripts/provider_adapters/fmp_corporate_actions.py",
    "scripts/collect_fmp_corporate_actions.py",
    "scripts/twelve_data_free_probe.py",
    "scripts/test_twelve_data_free.py",
    "docs/free_data_stack.md",
    "config/data_procurement_policy.json",
    "schemas/data_procurement_policy.schema.json",
    "scripts/data_procurement_policy.py",
    "scripts/test_data_procurement_policy.py",
    # Zero-dollar heterogeneous collection + pre-adjudication fusion control plane.
    "config/free_source_ensemble.json",
    "schemas/free_source_ensemble.schema.json",
    "schemas/free_source_ensemble_plan.schema.json",
    "schemas/free_source_evidence.schema.json",
    "schemas/free_source_fusion.schema.json",
    "scripts/provider_adapters/nasdaq_trader_public.py",
    "scripts/provider_adapters/nasdaq_listing_events.py",
    "scripts/provider_adapters/nasdaq_ex_date_notices.py",
    "scripts/provider_adapters/fed_h15_public.py",
    "scripts/provider_adapters/cftc_cot_public.py",
    "schemas/listing_event_payload.schema.json",
    "schemas/corporate_action_notice_payload.schema.json",
    "schemas/bls_release_calendar_payload.schema.json",
    "scripts/free_source_ensemble.py",
    "scripts/free_source_fusion.py",
    "scripts/test_free_source_adapters.py",
    "scripts/test_free_source_ensemble.py",
    "scripts/test_free_source_fusion.py",
    "scripts/test_nasdaq_event_adapters.py",
    "docs/free_source_ensemble.md",
    # Cross-session zero-dollar collection health; aggregate read-only observability.
    "config/free_source_health.json",
    "schemas/free_source_health.schema.json",
    "scripts/free_source_health.py",
    "scripts/test_free_source_health.py",
    "docs/free_source_health.md",
    # Deduplicated local source-health alert decisions; no sender/model/broker surface.
    "config/free_source_health_alerts.json",
    "schemas/free_source_health_alert_state.schema.json",
    "schemas/free_source_health_alert_receipt.schema.json",
    "scripts/free_source_health_alert.py",
    "scripts/test_free_source_health_alert.py",
    "docs/free_source_health_alerts.md",
    # Read-only Mac wake readiness for the deterministic 05:00 PT intake.
    "config/power_schedule.json",
    "schemas/power_schedule_health.schema.json",
    "scripts/power_schedule_health.py",
    "scripts/test_power_schedule_health.py",
    "docs/power_schedule_health.md",
    # Pre-session fail-closed gate + independent weekday wake-continuity service.
    "config/next_session_readiness.json",
    "schemas/next_session_readiness_policy.schema.json",
    "schemas/next_session_readiness_receipt.schema.json",
    "scripts/next_session_readiness.py",
    "scripts/test_next_session_readiness.py",
    "scripts/run_morning_continuity.sh",
    "scripts/install_morning_continuity.sh",
    "scripts/test_morning_continuity.py",
    "launchd/com.charles.trading.continuity.plist",
    "docs/next_session_readiness.md",
    # Versioned start/terminal service objectives for every supervised research workflow.
    "config/autonomy_service_slos.json",
    "schemas/autonomy_service_slo_policy.schema.json",
    "scripts/test_autonomy_service_slo.py",
    "docs/autonomy_service_slos.md",
    # Independent post-deadline watchdog for a workflow that never starts or self-refreshes.
    "config/autonomy_service_alerts.json",
    "schemas/autonomy_service_alert_policy.schema.json",
    "schemas/autonomy_service_alert_state.schema.json",
    "schemas/autonomy_service_alert_receipt.schema.json",
    "scripts/autonomy_service_alert.py",
    "scripts/test_autonomy_service_watchdog.py",
    "scripts/run_autonomy_watchdog.sh",
    "launchd/com.charles.trading.watchdog.plist",
    "docs/autonomy_service_watchdog.md",
    # Machine-derived static operator truth + tracked account-data absence.
    "config/documentation_truth.json",
    "schemas/documentation_truth_policy.schema.json",
    "schemas/documentation_truth_receipt.schema.json",
    "scripts/documentation_truth.py",
    "scripts/test_documentation_truth.py",
    "docs/documentation_truth.md",
    "README.md",
    "OPERATING_STATE.md",
    "MISSING_INPUTS.md",
    "config/portfolio.yaml",
    "data/fractional_capability.json",
    # Loopback-only aggregate console, fixed offline controls, and deterministic web build.
    "config/operator_console.json",
    "config/operator_control.json",
    "config/agent_context.json",
    "schemas/agent_context_policy.schema.json",
    "schemas/agent_context_index.schema.json",
    "schemas/agent_context_component.schema.json",
    "scripts/agent_context.py",
    "scripts/test_agent_context.py",
    "docs/agent_context.md",
    "schemas/operator_console_policy.schema.json",
    "schemas/operator_console_overview.schema.json",
    "schemas/operator_control_policy.schema.json",
    "schemas/operator_command_receipt.schema.json",
    "schemas/operator_instruction.schema.json",
    "schemas/operator_control_state.schema.json",
    "scripts/operator_console.py",
    "scripts/operator_control.py",
    "scripts/test_operator_console.py",
    "scripts/test_morning_report_console.py",
    "scripts/test_operator_control.py",
    "scripts/run_operator_console.sh",
    "scripts/install_operator_console.sh",
    "scripts/open_operator_console.sh",
    "launchd/com.charles.trading.console.plist",
    "Charles Trading Console.command",
    "app/Charles Trading Console.app/Contents/Info.plist",
    "app/Charles Trading Console.app/Contents/MacOS/Charles Trading Console",
    "app/Charles Pulse.app/Contents/Info.plist",
    "app/Charles Pulse.app/Contents/MacOS/Charles Pulse",
    "app/Charles Pulse.app/Contents/Resources/CharlesPulse.icns",
    "app/Charles Pulse.app/Contents/Resources/CharlesPulse.swift",
    "app/Charles Pulse.app/Contents/Resources/CharlesPulseIcon.svg",
    "scripts/install_charles_pulse_app.sh",
    "docs/operator_console.md",
    "docs/production_settlement.md",
    "web/package.json",
    "web/package-lock.json",
    "web/vite.config.js",
    "web/index.html",
    "web/public/favicon.svg",
    "web/src/main.js",
    "web/src/styles.css",
    "web/dist/index.html",
    "web/dist/favicon.svg",
    "web/dist/assets/app.js",
    "web/dist/assets/app.css",
    # Daily Nasdaq event accumulator; always shadow-only until a separately approved history seed.
    "config/security_master_rollforward.json",
    "schemas/security_master_rollforward.schema.json",
    "scripts/security_master_rollforward.py",
    "scripts/test_security_master_rollforward.py",
    "docs/security_master_rollforward.md",
    "docs/bls_release_calendar.md",
    # WP-09 point-in-time corporate-action and adjustment semantics.
    "config/adjustment_policy.json",
    "schemas/adjustment_policy.schema.json",
    "schemas/corporate_action_payload.schema.json",
    "schemas/adjusted_bar_view.schema.json",
    "scripts/adjustment_policy.py",
    "scripts/test_adjustment_policy.py",
    "scripts/quant/features.py",
    "scripts/test_quant_features.py",
    "config/quant_label_policy.json",
    "schemas/quant_label_policy.schema.json",
    "schemas/quant_forward_label_dataset.schema.json",
    "scripts/quant/labels.py",
    "scripts/test_quant_labels.py",
    "docs/quant_labels.md",
    # Deterministic zero-research-model terminal when both live-price paths are unavailable.
    "scripts/deterministic_no_live_terminal.py",
    "scripts/test_deterministic_no_live_terminal.py",
    # Phase 2 catalyst graph + exact event/claim joins (diagnostic-only).
    "config/catalyst_taxonomy.json",
    "scripts/catalyst_graph.py",
    "scripts/test_catalyst_graph.py",
    "docs/catalyst_graph.md",
    # SEC point-in-time metadata classification and receipt-backed document review boundary.
    "config/sec_event_parser_policy.json",
    "schemas/sec_event_parser_policy.schema.json",
    "schemas/sec_filing_event_bundle.schema.json",
    "scripts/sec_event_parser.py",
    "scripts/test_sec_event_parser.py",
    "config/sec_document_review_policy.json",
    "schemas/sec_document_review_policy.schema.json",
    "schemas/sec_document_review_queue.schema.json",
    "schemas/sec_document_review_result_draft.schema.json",
    "schemas/sec_document_review_result.schema.json",
    "scripts/sec_document_review.py",
    "scripts/test_sec_document_review.py",
    # Shadow-only exact-byte provenance foundation. It grants no production authority until a
    # separate trusted collector and evidence bridge are implemented and prospectively approved.
    "config/source_content_policy.json",
    "schemas/source_content_policy.schema.json",
    "schemas/source_content_receipt.schema.json",
    "scripts/source_content_receipt.py",
    "scripts/test_source_content_receipt.py",
    "docs/source_content_receipt.md",
    # Shadow-only complete SEC/news source-order denominator and receipt inventory. It cannot
    # grant source, catalyst, publication, Buy, metric, broker, or execution authority.
    "config/source_receipt_manifest_policy.json",
    "schemas/source_receipt_manifest.schema.json",
    "scripts/source_receipt_manifest.py",
    "scripts/test_source_receipt_manifest.py",
    "docs/source_receipt_manifest.md",
    "config/sec_form4_parser_policy.json",
    "schemas/sec_form4_parser_policy.schema.json",
    "schemas/sec_form4_parse_request.schema.json",
    "schemas/sec_form4_transaction_bundle.schema.json",
    "scripts/sec_form4_parser.py",
    "scripts/test_sec_form4_parser.py",
    "docs/sec_form4_parser.md",
    # WP-17 fail-soft SEC data-lake -> review-queue shadow coordination.
    "config/sec_review_shadow.json",
    "schemas/sec_review_shadow_config.schema.json",
    "schemas/sec_review_shadow_status.schema.json",
    "scripts/sec_review_shadow.py",
    "scripts/test_sec_review_shadow.py",
    "docs/sec_review_shadow.md",
    # WP-18 SEC shadow adoption + sealed-review quality observability.
    "config/sec_review_shadow_rollup.json",
    "schemas/sec_review_shadow_rollup_policy.schema.json",
    "schemas/sec_review_shadow_rollup.schema.json",
    "scripts/sec_review_shadow_rollup.py",
    "scripts/test_sec_review_shadow_rollup.py",
    "scripts/test_sec_review_shadow_rollup_surfacing.py",
    "prompts/weekly_review.md",
    # WP-19 SEC automated-access identity, consent, and bounded network boundary.
    "config/sec_access_policy.json",
    "schemas/sec_access_policy.schema.json",
    "schemas/sec_access_readiness.schema.json",
    "schemas/sec_access_probe.schema.json",
    "scripts/sec_identity.py",
    "scripts/sec_access.py",
    "scripts/setup_sec_user_agent.sh",
    "scripts/test_sec_access.py",
    "scripts/test_setup_sec_user_agent.py",
    "docs/sec_access.md",
    # Dynamic, bounded SEC submissions locator used only to seed primary-source review.
    "config/sec_candidate_seed.json",
    "scripts/sec_candidate_seed.py",
    "scripts/test_sec_candidate_seed.py",
    "scripts/sec_catalyst_receipt_collector.py",
    "scripts/sec_catalyst_receipt_validator.py",
    "scripts/test_sec_catalyst_receipt_collector.py",
    "scripts/test_sec_catalyst_receipt_validator.py",
    "scripts/news_corroboration_collector.py",
    "scripts/news_corroboration_validator.py",
    "scripts/test_news_corroboration.py",
    # 2026-07-09 unattended quant research factory + selection-bias governance.
    "config/research_factory.json",
    "schemas/quant_research_factory_config.schema.json",
    "schemas/quant_research_bundle.schema.json",
    "schemas/quant_grid_worker.schema.json",
    "scripts/quant/selection_bias.py",
    "scripts/quant/research_factory.py",
    "scripts/quant/session_capture.py",
    "scripts/quant/feature_corpus.py",
    "scripts/quant/grid_worker.py",
    "scripts/quant/backtest.py",
    "scripts/quant/model_diagnostics.py",
    "scripts/quant/walk_forward.py",
    "scripts/quant/experiment_manifest.py",
    "scripts/test_quant_selection_bias.py",
    "scripts/test_quant_research_factory.py",
    "scripts/test_quant_session_capture.py",
    "scripts/test_quant_feature_corpus.py",
    "scripts/test_quant_grid_worker.py",
    "scripts/test_quant_backtest.py",
    "docs/research_factory.md",
    # Offline predecision transaction-cost and overlapping portfolio diagnostics.
    "config/quant_cost_policy.json",
    "config/quant_portfolio_policy.json",
    "schemas/quant_cost_policy.schema.json",
    "schemas/quant_cost_estimate.schema.json",
    "schemas/quant_portfolio_policy.schema.json",
    "schemas/quant_portfolio_plan.schema.json",
    "schemas/quant_portfolio_backtest.schema.json",
    "scripts/quant/cost_model.py",
    "scripts/quant/portfolio_backtest.py",
    "scripts/test_quant_portfolio_backtest.py",
    # Dependence-aware inference and train/test leakage diagnostics (no gate authority).
    "config/quant_inference_policy.json",
    "schemas/quant_inference_policy.schema.json",
    "schemas/quant_interval_assignments.schema.json",
    "schemas/quant_inference_audit.schema.json",
    "schemas/quant_purge_embargo_audit.schema.json",
    "scripts/quant/inference_audit.py",
    "scripts/test_quant_inference_audit.py",
    # Hansen SPA complete-family multiple-comparison diagnostic (no gate authority).
    "config/quant_spa_policy.json",
    "schemas/quant_spa_policy.schema.json",
    "schemas/quant_spa_panel.schema.json",
    "schemas/quant_spa_audit.schema.json",
    "scripts/quant/spa_audit.py",
    "scripts/test_quant_spa_audit.py",
    "docs/quant_spa_audit.md",
    # Offline strategy uniqueness and incremental-alpha diagnostics (no gate authority).
    "config/quant_strategy_uniqueness_policy.json",
    "schemas/quant_strategy_uniqueness_policy.schema.json",
    "schemas/quant_strategy_uniqueness.schema.json",
    "scripts/quant/strategy_uniqueness.py",
    "scripts/test_quant_strategy_uniqueness.py",
    "docs/strategy_uniqueness.md",
    # Append-only strategy registry + candidate-sequence comparison cohort lock.
    "config/quant_strategy_registry_policy.json",
    "schemas/quant_strategy_registry_policy.schema.json",
    "schemas/quant_strategy_registry_event.schema.json",
    "schemas/quant_strategy_comparison_cohort.schema.json",
    "schemas/quant_strategy_cohort_audit.schema.json",
    "data/quant_strategy_registry.jsonl",
    "scripts/quant/strategy_registry.py",
    "scripts/quant/strategy_cohort.py",
    "scripts/test_quant_strategy_cohort.py",
    "docs/strategy_registry_and_cohorts.md",
    # Preregistered factory strategy-artifact bridge + cost-matched SPY reference.
    "config/quant_strategy_artifact_policy.json",
    "schemas/quant_strategy_artifact_policy.schema.json",
    "schemas/quant_benchmark_reference.schema.json",
    "schemas/quant_factory_portfolio_export_contract.schema.json",
    "schemas/quant_factory_strategy_export_receipt.schema.json",
    "scripts/quant/strategy_artifacts.py",
    "scripts/test_quant_strategy_artifacts.py",
    "docs/strategy_artifacts.md",
    # Current-run-bound write-once staging after exact factory candidate approval.
    "schemas/quant_strategy_artifact_stager.schema.json",
    "scripts/quant/strategy_artifact_stager.py",
    "scripts/test_quant_strategy_artifact_stager.py",
    "docs/strategy_artifact_staging.md",
    # Cross-diagnostic candidate evidence gate for human shadow-review eligibility only.
    "config/quant_strategy_evidence_gate.json",
    "schemas/quant_strategy_evidence_gate_policy.schema.json",
    "schemas/quant_strategy_evidence_gate.schema.json",
    "scripts/quant/strategy_evidence_gate.py",
    "scripts/test_quant_strategy_evidence_gate.py",
    "docs/strategy_evidence_gate.md",
    # Human-approved, future-only strategy shadow observation (no activation authority).
    "config/quant_strategy_shadow_policy.json",
    "schemas/quant_strategy_shadow_policy.schema.json",
    "schemas/quant_strategy_shadow_plan.schema.json",
    "schemas/quant_strategy_shadow_return_bundle.schema.json",
    "schemas/quant_strategy_shadow_observation_draft.schema.json",
    "schemas/quant_strategy_shadow_event.schema.json",
    "schemas/quant_strategy_shadow_summary.schema.json",
    "scripts/quant/strategy_shadow_monitor.py",
    "scripts/test_quant_strategy_shadow_monitor.py",
    "docs/strategy_shadow_observation.md",
    # Stateful future-session generator underpinning truthful shadow observation. Standalone and
    # unscheduled until an eligible dossier, frozen replay specs, data, and human approval exist.
    "config/quant_strategy_shadow_forward_policy.json",
    "schemas/quant_strategy_shadow_forward_policy.schema.json",
    "schemas/quant_strategy_shadow_replay_spec.schema.json",
    "schemas/quant_strategy_shadow_forward_result.schema.json",
    "schemas/quant_strategy_shadow_forward_run.schema.json",
    "scripts/quant/strategy_shadow_forward.py",
    "scripts/test_quant_strategy_shadow_forward.py",
    "docs/strategy_shadow_forward.md",
    # Phase 4 manual decision/entry-fill observation boundary (no broker or execution authority).
    "config/manual_execution_observation_policy.json",
    "schemas/manual_execution_observation_policy.schema.json",
    "schemas/manual_execution_case_registry.schema.json",
    "schemas/manual_execution_observation_event.schema.json",
    "schemas/manual_execution_source_receipt.schema.json",
    "schemas/manual_execution_observation_summary.schema.json",
    "scripts/manual_execution_observation.py",
    "scripts/test_manual_execution_observation.py",
    # WP-14 automatic manual-observation registry sync + post-close health surface.
    "scripts/manual_execution_observation_sync.py",
    "scripts/test_manual_execution_observation_sync.py",
    "prompts/post_close_review.md",
    "prompts/run_post_close.md",
    # Model-risk inventory includes the catalyst graph and other diagnostic components.
    "data/model_inventory.json",
    "scripts/model_inventory.py",
    "scripts/test_model_inventory.py",
    "docs/completion_roadmap.md",
    # Kalshi event-contract lane: ONE entry pins the package's own self-manifest,
    # which in turn pins every file in scripts/kalshi/. Keeps the global CRITICAL
    # footprint at one line per asset class regardless of package size.
    "scripts/kalshi/config/self_manifest.json",
]

# Never traverse these (secrets / caches / vcs / transient).
SKIP_DIRS = {".git", "secrets", "__pycache__", ".run.lock"}
SKIP_SUFFIXES = {".pyc"}


class ManifestError(RuntimeError):
    """Repository inventory could not be derived safely."""


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def repository_files(root: Path = ROOT) -> list[Path]:
    """Return tracked plus untracked/nonignored files using Git's NUL-safe authority."""
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
         "--exclude-standard"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if completed.returncode != 0:
        raise ManifestError("git_repository_inventory_failed")
    paths: list[Path] = []
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        if relative.is_absolute() or ".." in relative.parts:
            raise ManifestError("repository_path_invalid")
        path = root / relative
        if path.is_file():
            paths.append(path)
    return sorted(paths, key=lambda path: os.fsencode(str(path.relative_to(root))))


def _critical_state() -> tuple[dict, list]:
    critical = {}
    missing = []
    for rel in CRITICAL:
        fp = ROOT / rel
        if fp.exists():
            critical[rel] = sha256(fp)
        else:
            missing.append(rel)
    return critical, missing


def _documentation_truth_state() -> dict:
    """Run the bounded local semantic check; never permit a manifest to bless stale docs."""
    checker = ROOT / "scripts" / "documentation_truth.py"
    if not checker.is_file():
        return {"ok": False, "status": "unavailable", "errors": ["checker_missing"]}
    try:
        completed = subprocess.run(
            [sys.executable, str(checker), "check", "--root", str(ROOT)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=DOCUMENTATION_TRUTH_TIMEOUT_SECONDS,
            env={
                "HOME": os.environ.get("HOME", ""),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
            },
        )
        receipt = json.loads(completed.stdout.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, subprocess.SubprocessError):
        return {"ok": False, "status": "unavailable", "errors": ["checker_failed"]}
    safety = receipt.get("safety") or {}
    safe = (
        safety.get("diagnostic_only") is True
        and safety.get("read_only") is True
        and safety.get("execution_enabled") is False
        and safety.get("broker_invoked") is False
        and safety.get("network_invoked") is False
        and safety.get("model_invoked") is False
        and safety.get("contains_account_data") is False
        and safety.get("contains_credentials") is False
        and safety.get("grants_authority") is False
    )
    ok = completed.returncode == 0 and receipt.get("status") == "healthy" \
        and receipt.get("errors") == [] and safe
    return {
        "ok": ok,
        "status": receipt.get("status") if isinstance(receipt.get("status"), str)
        else "invalid",
        "errors": receipt.get("errors") if isinstance(receipt.get("errors"), list)
        else ["receipt_invalid"],
        "artifact_sha256": receipt.get("artifact_sha256"),
        "tracked_text_files_scanned": receipt.get("tracked_text_files_scanned"),
    }


def check() -> int:
    """READ-ONLY drift check: recompute critical hashes, compare to the stored manifest."""
    out = ROOT / "data" / "MANIFEST.json"
    try:
        stored = json.loads(out.read_text())
    except Exception as exc:
        print(json.dumps({"check": "FAIL", "error": f"manifest unreadable: {exc}"}, indent=2))
        return 1
    critical, missing = _critical_state()
    documentation_truth = _documentation_truth_state()
    stored_crit = stored.get("critical_sha256", {}) or {}
    drift = sorted(rel for rel in critical
                   if stored_crit.get(rel) and stored_crit[rel] != critical[rel])
    unstored = sorted(rel for rel in critical if rel not in stored_crit)
    ok = not drift and not missing and not unstored and documentation_truth["ok"]
    print(json.dumps({"check": "PASS" if ok else "FAIL", "critical_checked": len(critical),
                      "drift": drift, "missing": missing, "not_in_stored_manifest": unstored,
                      "documentation_truth": documentation_truth,
                      "note": "read-only; rebuild with build_manifest.py (no flag) after an "
                              "approved change"}, indent=2, sort_keys=True))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Build (default) or verify (--check) the integrity manifest.")
    ap.add_argument("--check", action="store_true",
                    help="READ-ONLY: compare current critical hashes to the stored manifest; never writes.")
    args = ap.parse_args()
    if args.check:
        return check()

    # Audited-window fence (2026-07-22): a re-sign while an audited model window is open
    # is the rc=88 collision class (control code changing under a live lane, quarantining
    # a completed run). Advisory-at-the-source and FAIL-OPEN (primitive absent/unreadable
    # -> allow); --check above is read-only and always allowed.
    try:
        sys.path.insert(0, str(ROOT / "scripts" / "lib"))
        import audited_window as _audited_window
        _now = datetime.now(timezone.utc)
        _active, _ = _audited_window.is_window_active(ROOT, _now)
        if _active:
            print(json.dumps({"build": "REFUSED", "error": "audited_window_active",
                              "detail": _audited_window.describe_active(ROOT, _now)},
                             indent=2, sort_keys=True))
            return 3
    except ImportError:
        pass

    documentation_truth = _documentation_truth_state()
    if not documentation_truth["ok"]:
        print(json.dumps({
            "build": "FAIL",
            "error": "documentation_truth_failed",
            "documentation_truth": documentation_truth,
        }, indent=2, sort_keys=True))
        return 1

    files = []
    try:
        candidates = repository_files()
    except ManifestError as exc:
        print(json.dumps({"build": "FAIL", "error": str(exc)}, indent=2))
        return 1
    for p in candidates:
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.suffix in SKIP_SUFFIXES:
            continue
        st = p.stat()
        files.append({
            "path": str(rel),
            "size": st.st_size,
            "mtime_utc": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        })

    critical, missing = _critical_state()

    manifest = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(ROOT),
        "file_count": len(files),
        "critical_sha256": critical,
        "critical_missing": missing,
        "files": files,
    }
    out = ROOT / "data" / "MANIFEST.json"
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(f"manifest: {out}")
    print(f"files inventoried (tracked + untracked/nonignored): {len(files)}")
    print(f"critical hashed: {len(critical)}  missing: {missing}")
    for rel in sorted(critical):
        print(f"  {critical[rel][:16]}…  {rel}")
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
