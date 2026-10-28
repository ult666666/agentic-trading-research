#!/usr/bin/env python3
"""Hermetic runtime broker-audit hook and audited-Claude wrapper tests."""
from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from public_snapshot import skip_private_module  # noqa: E402

try:  # the public snapshot leaves some imported modules out
    import audited_claude as wrapper  # noqa: E402
    import broker_runtime_audit as audit  # noqa: E402
except ModuleNotFoundError as exc:
    raise skip_private_module(exc) from exc

TARGET = "2026-07-10"


def context(root: Path, invocation: str = "fixture-one") -> dict:
    return audit.start_invocation(
        audit_root=root, target=TARGET, workflow="morning",
        run_id="morning-fixture", invocation_id=invocation)


def env_for(root: Path, ctx: dict) -> dict[str, str]:
    return {
        audit.ENV_KEYS["root"]: str(root),
        audit.ENV_KEYS["target"]: TARGET,
        audit.ENV_KEYS["workflow"]: "morning",
        audit.ENV_KEYS["run_id"]: "morning-fixture",
        audit.ENV_KEYS["invocation_id"]: ctx["invocation_id"],
        audit.ENV_KEYS["event_log"]: str(ctx["event_log"]),
    }


def write_fake_claude(path: Path) -> None:
    script = f"""#!{sys.executable}
import json, os, pathlib, signal, subprocess, sys, time
audit_script = os.environ['TEST_AUDIT_SCRIPT']
mode = os.environ.get('TEST_FAKE_MODE', 'read')
if '--settings' not in sys.argv:
    raise SystemExit(9)
def call(command, payload):
    completed = subprocess.run(
        [sys.executable, audit_script, command], input=json.dumps(payload), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
    if completed.returncode:
        sys.stderr.write(completed.stderr)
        raise SystemExit(completed.returncode)
    return completed.stdout
if mode != 'no-heartbeat':
    call('hook-session', {{'hook_event_name':'SessionStart','session_id':'session-secret'}})
if mode == 'read':
    call('hook-tool', {{'hook_event_name':'PreToolUse','session_id':'session-secret',
        'tool_name':'mcp__robinhood-trading__get_equity_quotes',
        'tool_input':{{'symbol':'SPY','account_number':'must-not-persist'}}}})
elif mode == 'mutation':
    decision = call('hook-tool', {{'hook_event_name':'PreToolUse','session_id':'session-secret',
        'tool_name':'mcp__robinhood-trading__place_equity_order',
        'tool_input':{{'symbol':'SPY','quantity':999,'token':'must-not-persist'}}}})
    if 'permissionDecision' not in decision or 'deny' not in decision:
        raise SystemExit(10)
elif mode == 'env-probe':
    marker = os.environ['TEST_ENV_MARKER']
    credential_names = {sorted(wrapper.secrets_loader.ALLOWLIST)!r}
    with open(marker, 'w') as fh:
        json.dump({{'credential_names_present': sorted(
            name for name in credential_names if os.environ.get(name)),
            'model_access_disabled': os.environ.get('TRADING_SECRET_FILE_ACCESS_DISABLED')}}, fh)
elif mode == 'sleep-tree':
    marker = os.environ['TEST_ORPHAN_MARKER']
    child = "import pathlib,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(1.2); pathlib.Path(" + repr(marker) + ").write_text('bad')"
    subprocess.Popen([sys.executable, '-c', child])
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(20)
elif mode == 'exit-zero-tree':
    marker = os.environ['TEST_ORPHAN_MARKER']
    child = "import pathlib,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(1.2); pathlib.Path(" + repr(marker) + ").write_text('bad')"
    subprocess.Popen([sys.executable, '-c', child])
elif mode == 'audit-finalization-failure':
    root = pathlib.Path.cwd()
    target = os.environ[{audit.ENV_KEYS['target']!r}]
    report = root / 'reports' / 'post_close' / f'{{target}}.md'
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('must be quarantined after audit failure\\n')
    pathlib.Path(os.environ[{audit.ENV_KEYS['event_log']!r}]).unlink()
"""
    path.write_text(script)
    path.chmod(0o755)


class TestSettingsAndClassification(unittest.TestCase):
    def test_additional_settings_has_only_audit_hooks(self):
        document = json.loads(audit.SETTINGS_PATH.read_text())
        self.assertEqual(set(document), {"hooks"})
        self.assertNotIn("permissions", document)
        session = document["hooks"]["SessionStart"][0]["hooks"][0]
        tool = document["hooks"]["PreToolUse"][0]
        self.assertIn("broker_runtime_audit.py", session["command"])
        self.assertEqual(tool["matcher"], "mcp__.*")
        self.assertIn("hook-tool", tool["hooks"][0]["command"])

    def test_exact_and_unknown_broker_classification(self):
        self.assertEqual(audit.NAMESPACE_REGISTRY_STATE,
                         audit.NAMESPACE_REGISTRY_HEALTHY)
        self.assertEqual(audit.classify_tool(
            "mcp__robinhood-trading__get_equity_quotes"), "broker_read_only")
        self.assertEqual(audit.classify_tool(
            "mcp__robinhood-trading__review_equity_order"),
            "broker_preview_or_write")
        self.assertEqual(audit.classify_tool(
            "mcp__future_broker__get_new_thing"), "unknown_broker_tool")
        self.assertEqual(audit.classify_tool(
            "mcp__unrelated__create_order"), "unknown_broker_tool")
        self.assertEqual(audit.classify_tool(
            "mcp__github__search_repositories"), "non_broker_mcp")
        self.assertEqual(audit.classify_tool(
            "mcp__renamed_connector__neutral_new_capability"),
            "unknown_broker_tool")
        with mock.patch.object(audit, "_kalshi_source_current", return_value=True):
            self.assertEqual(audit.classify_tool(
                "mcp__kalshi__get_market"), "broker_read_only")
        self.assertEqual(audit.classify_tool(
            "mcp__kalshi__get_balance"), "unknown_broker_tool")
        self.assertEqual(audit.classify_tool(
            "mcp__kalshi__get_new_capability"), "unknown_broker_tool")

    def test_exact_current_kalshi_surface_and_renamed_private_tools_fail_closed(self):
        expected_public = {
            f"mcp__kalshi__{basename}" for basename in {
                "get_exchange_status", "list_markets", "get_market", "get_orderbook",
                "get_recent_trades", "get_candlesticks", "list_events", "get_event",
                "list_series", "get_series",
            }
        }
        expected_private_basenames = {
            "get_balance", "get_positions", "get_orders", "get_fills", "get_settlements",
        }
        self.assertEqual(audit.KALSHI_PUBLIC_READ_TOOLS, expected_public)
        self.assertEqual(
            audit.KALSHI_PUBLIC_READ_BASENAMES,
            {audit.permission_policy._basename(name) for name in expected_public},
        )
        self.assertEqual(audit.KALSHI_PRIVATE_ACCOUNT_BASENAMES,
                         expected_private_basenames)
        with mock.patch.object(audit, "_kalshi_source_current", return_value=True):
            for tool_name in sorted(expected_public):
                with self.subTest(tool_name=tool_name):
                    self.assertEqual(audit.classify_tool(tool_name), "broker_read_only")
                    renamed = tool_name.replace(
                        "mcp__kalshi__", "mcp__renamed_connector__", 1
                    )
                    self.assertEqual(
                        audit.classify_tool(renamed),
                        "unknown_broker_tool",
                    )
        for basename in sorted(expected_private_basenames):
            for server in ("kalshi", "renamed_connector"):
                tool_name = f"mcp__{server}__{basename}"
                with self.subTest(tool_name=tool_name):
                    self.assertEqual(
                        audit.classify_tool(tool_name), "unknown_broker_tool")

    def test_prediction_market_aliases_are_broker_shaped(self):
        for tool_name in (
                "mcp__event_market__get_market",
                "mcp__event-market__list_events",
                "mcp__prediction_market__get_series",
                "mcp__prediction-market__list_markets"):
            with self.subTest(tool_name=tool_name):
                self.assertEqual(
                    audit.classify_tool(tool_name), "unknown_broker_tool")

    def test_namespace_parser_rejects_empty_and_wrong_shaped_registries(self):
        valid = {
            "schema_version": "1.0",
            "recognized_broker_servers": ["fixture_broker"],
            "servers": {
                "fixture_broker": {
                    "tools": {
                        "read_only": ["mcp__fixture_broker__get_quote"],
                    },
                },
            },
        }
        recognized, state = audit._parse_namespace_registry(valid)
        self.assertEqual(recognized, frozenset({"fixture_broker"}))
        self.assertEqual(state, audit.NAMESPACE_REGISTRY_HEALTHY)

        alias_without_inventory = json.loads(json.dumps(valid))
        alias_without_inventory["recognized_broker_servers"].append("renamed_alias")
        recognized, state = audit._parse_namespace_registry(alias_without_inventory)
        self.assertEqual(
            recognized, frozenset({"fixture_broker", "renamed_alias"})
        )
        self.assertEqual(state, audit.NAMESPACE_REGISTRY_HEALTHY)
        with mock.patch.object(audit, "RECOGNIZED_SERVERS", recognized):
            self.assertEqual(
                audit.classify_tool("mcp__renamed_alias__neutral_new_capability"),
                "unknown_broker_tool",
            )

        invalid_documents = (
            None,
            {},
            {"schema_version": "1.0", "recognized_broker_servers": [],
             "servers": {}},
            {"schema_version": "1.0", "recognized_broker_servers": "fixture_broker",
             "servers": valid["servers"]},
            {"schema_version": "1.0",
             "recognized_broker_servers": ["fixture_broker"], "servers": {}},
            {"schema_version": "1.0",
             "recognized_broker_servers": ["fixture_broker"],
             "servers": {"fixture_broker": {"tools": {}}}},
            {"schema_version": "1.0",
             "recognized_broker_servers": ["fixture_broker"],
             "servers": {"fixture_broker": {"tools": {
                 "read_only": ["mcp__different_server__get_quote"],
             }}}},
        )
        for document in invalid_documents:
            with self.subTest(document=document):
                recognized, state = audit._parse_namespace_registry(document)
                self.assertEqual(recognized, frozenset())
                self.assertNotEqual(state, audit.NAMESPACE_REGISTRY_HEALTHY)

    def test_namespace_loader_rejects_missing_empty_and_malformed_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = {
                "missing.json": None,
                "empty.json": b"",
                "malformed.json": b"{not-json",
            }
            for filename, content in cases.items():
                path = root / filename
                if content is not None:
                    path.write_bytes(content)
                with self.subTest(filename=filename):
                    recognized, state = audit._load_namespace_registry(
                        path, root=root)
                    self.assertEqual(recognized, frozenset())
                    self.assertNotEqual(state, audit.NAMESPACE_REGISTRY_HEALTHY)

    def test_unhealthy_namespace_registry_denies_every_mcp_and_changes_policy_hash(self):
        healthy_policy_sha256 = audit.policy_sha256()
        with mock.patch.object(
                audit, "NAMESPACE_REGISTRY_STATE", "namespace_source_json_invalid"), \
                mock.patch.object(audit, "RECOGNIZED_SERVERS", frozenset()):
            unhealthy_policy_sha256 = audit.policy_sha256()
            for tool_name in (
                    "mcp__robinhood-trading__get_equity_quotes",
                    "mcp__kalshi__get_market",
                    "mcp__github__search_repositories"):
                with self.subTest(tool_name=tool_name):
                    self.assertEqual(
                        audit.classify_tool(tool_name), "unknown_broker_tool")
        self.assertNotEqual(healthy_policy_sha256, unhealthy_policy_sha256)

    def test_post_import_control_file_drift_denies_before_stale_classification(self):
        healthy_policy_sha256 = audit.policy_sha256()
        with tempfile.TemporaryDirectory() as temporary:
            changed = Path(temporary) / "broker_tool_namespace.json"
            changed.write_text('{"schema_version":"changed"}\n')
            with mock.patch.object(audit, "SNAPSHOT_PATH", changed):
                self.assertFalse(audit._control_plane_files_current())
                self.assertEqual(
                    audit.classify_tool(
                        "mcp__robinhood-trading__get_equity_quotes"
                    ),
                    "unknown_broker_tool",
                )
                self.assertNotEqual(healthy_policy_sha256, audit.policy_sha256())

    def test_exact_kalshi_read_requires_the_pinned_server_source_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "server.py"
            source.write_bytes(b"pinned fixture source\n")
            expected = audit._sha_file(source)
            with mock.patch.object(audit, "KALSHI_SERVER_SOURCE_PATH", source), \
                    mock.patch.object(
                        audit, "KALSHI_EXPECTED_SOURCE_SHA256", expected
                    ):
                self.assertTrue(audit._kalshi_source_current())
                self.assertEqual(
                    audit.classify_tool("mcp__kalshi__get_market"),
                    "broker_read_only",
                )
                source.write_bytes(b"changed semantics under the same tool names\n")
                self.assertFalse(audit._kalshi_source_current())
                self.assertEqual(
                    audit.classify_tool("mcp__kalshi__get_market"),
                    "unknown_broker_tool",
                )

    def test_only_explicit_non_broker_tools_are_classified_non_broker(self):
        self.assertEqual(
            audit.KNOWN_NON_BROKER_READ_TOOLS,
            frozenset({"mcp__github__search_repositories"}),
        )
        self.assertEqual(
            audit.classify_tool("mcp__github__search_repositories"),
            "non_broker_mcp",
        )
        for tool_name in (
            "mcp__github__neutral_new_capability",
            "mcp__opaque_uuid__neutral_new_capability",
            "mcp__renamed_connector__neutral_new_capability",
            "mcp__future_data_source__neutral_new_capability",
        ):
            with self.subTest(tool_name=tool_name):
                self.assertEqual(
                    audit.classify_tool(tool_name),
                    "unknown_broker_tool",
                )

    def test_caller_cannot_override_audit_or_bypass_permissions(self):
        for arguments in (["-p", "x", "--settings", "other.json"],
                          ["-p", "x", "--dangerously-skip-permissions"],
                          ["-p", "x", "--permission-mode", "bypassPermissions"]):
            with self.assertRaises(audit.AuditError):
                wrapper._validate_child_args(arguments)


class TestHooksAndReceipts(unittest.TestCase):
    def test_read_and_mutation_attempts_are_hashed_and_mutation_is_denied(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx = context(root)
            with mock.patch.dict(os.environ, env_for(root, ctx), clear=False):
                audit.hook_session({"hook_event_name": "SessionStart",
                                    "session_id": "raw-session-secret"})
                _read, denied_read = audit.hook_tool({
                    "hook_event_name": "PreToolUse", "session_id": "raw-session-secret",
                    "tool_name": "mcp__robinhood-trading__get_equity_quotes",
                    "tool_input": {"symbol": "SPY", "account_number": "raw-account-secret"},
                })
                _write, denied_write = audit.hook_tool({
                    "hook_event_name": "PreToolUse", "session_id": "raw-session-secret",
                    "tool_name": "mcp__robinhood-trading__place_equity_order",
                    "tool_input": {"symbol": "SPY", "token": "raw-token-secret"},
                })
                _kalshi_account, denied_kalshi_account = audit.hook_tool({
                    "hook_event_name": "PreToolUse", "session_id": "raw-session-secret",
                    "tool_name": "mcp__kalshi__get_balance",
                    "tool_input": {},
                })
            receipt = audit.finalize_invocation(
                ctx, child_exit_code=0, started_utc="2026-07-10T18:00:00Z",
                finished_utc="2026-07-10T18:00:01Z")
            raw = ctx["event_log"].read_text() + ctx["receipt"].read_text()
        self.assertFalse(denied_read)
        self.assertTrue(denied_write)
        self.assertTrue(denied_kalshi_account)
        self.assertEqual(receipt["instrumentation_coverage"], "complete")
        self.assertEqual(receipt["broker_read_attempt_total"], 1)
        self.assertEqual(receipt["broker_preview_write_attempt_total"], 1)
        self.assertEqual(receipt["unknown_broker_attempt_total"], 1)
        self.assertTrue(receipt["violation_detected"])
        for secret in ("raw-session-secret", "raw-account-secret", "raw-token-secret"):
            self.assertNotIn(secret, raw)

    def test_missing_environment_blocks_pretool_hook(self):
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "broker_runtime_audit.py"), "hook-tool"],
            input=json.dumps({
                "hook_event_name": "PreToolUse",
                "tool_name": "mcp__github__search_repositories", "tool_input": {},
            }), text=True, capture_output=True, env=env)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("audit_environment_missing", completed.stderr)

    def test_hook_cli_emits_pretool_deny_decision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx = context(root)
            env = dict(os.environ)
            env.update(env_for(root, ctx))
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "broker_runtime_audit.py"),
                 "hook-tool"],
                input=json.dumps({
                    "hook_event_name": "PreToolUse", "session_id": "s",
                    "tool_name": "mcp__robinhood-trading__review_equity_order",
                    "tool_input": {"symbol": "SPY"},
                }), text=True, capture_output=True, env=env)
            decision = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0)
        output = decision["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "deny")

    def test_tampered_event_log_makes_coverage_unobservable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx = context(root)
            with mock.patch.dict(os.environ, env_for(root, ctx), clear=False):
                audit.hook_session({"hook_event_name": "SessionStart", "session_id": "s"})
            with ctx["event_log"].open("ab") as handle:
                handle.write(b"not-json\n")
            receipt = audit.finalize_invocation(
                ctx, child_exit_code=0, started_utc="2026-07-10T18:00:00Z",
                finished_utc="2026-07-10T18:00:01Z")
            summary = audit.summarize_target(audit_root=root, target=TARGET)
        self.assertEqual(receipt["instrumentation_coverage"], "unobservable")
        self.assertTrue(receipt["coverage_errors"])
        self.assertEqual(summary["status"], "unobservable")
        self.assertFalse(summary["zero_preview_write_verified"])

    def test_orphan_event_log_cannot_be_reported_not_observed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context(root, "orphan")
            summary = audit.summarize_target(audit_root=root, target=TARGET)
        self.assertEqual(summary["status"], "violation")
        self.assertEqual(summary["receipt_count"], 0)
        self.assertEqual(summary["invalid_receipt_count"], 1)
        self.assertEqual(summary["invalid_receipts"][0]["errors"],
                         ["receipt_missing_for_event_log"])


class TestSourceAwareSli(unittest.TestCase):
    def _valid_sli(self, root: Path) -> tuple[dict, dict]:
        ctx = context(root)
        with mock.patch.dict(os.environ, env_for(root, ctx), clear=False):
            audit.hook_session({"hook_event_name": "SessionStart", "session_id": "s"})
            audit.hook_tool({
                "hook_event_name": "PreToolUse", "session_id": "s",
                "tool_name": "mcp__robinhood-trading__get_equity_quotes",
                "tool_input": {"symbol": "SPY"},
            })
        audit.finalize_invocation(
            ctx, child_exit_code=0, started_utc="2026-07-10T18:00:00Z",
            finished_utc="2026-07-10T18:00:01Z")
        return ctx, audit.summarize_target(
            audit_root=root, target=TARGET,
            now=dt.datetime(2026, 7, 10, 18, 1, tzinfo=dt.timezone.utc))

    def test_valid_sli_exactly_rederives_from_receipts_and_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _ctx, sli = self._valid_sli(root)
            errors = audit.validate_sli_against_sources(
                sli, audit_root=root, target=TARGET)
        self.assertEqual(errors, [])

    def test_coherently_rehashed_forged_sli_is_rejected_by_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _ctx, sli = self._valid_sli(root)
            forged = json.loads(json.dumps(sli))
            forged["broker_read_attempt_total"] += 9
            forged["sli_sha256"] = audit._sha(
                audit._without(forged, "sli_sha256"))
            self.assertEqual(audit.validate_sli(forged), [])
            errors = audit.validate_sli_against_sources(
                forged, audit_root=root, target=TARGET)
        self.assertIn(
            "sli_source_projection_mismatch:broker_read_attempt_total", errors)

    def test_tool_classification_is_rederived_from_the_tool_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            ctx, _sli = self._valid_sli(root)
            events = [json.loads(line) for line in ctx["event_log"].read_text().splitlines()]
            tool_event = events[-1]
            self.assertEqual(tool_event["classification"], "broker_read_only")
            tool_event["tool_name"] = (
                "mcp__robinhood-trading__review_equity_order")
            tool_event["event_sha256"] = audit._sha(
                audit._without(tool_event, "event_sha256"))
            ctx["event_log"].write_text(
                "\n".join(json.dumps(row, sort_keys=True, separators=(",", ":"))
                          for row in events) + "\n")
            receipt = json.loads(ctx["receipt"].read_text())
            receipt["event_log_sha256"] = audit._sha_file(ctx["event_log"])
            receipt["receipt_sha256"] = audit._sha(
                audit._without(receipt, "receipt_sha256"))
            ctx["receipt"].write_text(
                json.dumps(receipt, indent=2, sort_keys=True) + "\n")
            rebuilt, errors = audit._validate_receipt_file(
                ctx["receipt"], root, TARGET)
        self.assertIsNone(rebuilt)
        self.assertIn("receipt_event_classification_mismatch:2", errors)

    def test_receipt_and_events_are_bound_to_current_settings_and_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            ctx, _sli = self._valid_sli(root)
            changed_settings = root / "changed-audit-settings.json"
            changed_settings.write_text('{"hooks":{"changed":true}}\n')
            with mock.patch.object(audit, "SETTINGS_PATH", changed_settings):
                rebuilt, errors = audit._validate_receipt_file(
                    ctx["receipt"], root, TARGET
                )
        self.assertIsNone(rebuilt)
        self.assertIn("audit_settings_sha256_stale", errors)
        self.assertIn("policy_sha256_stale", errors)

    def test_invalid_receipt_rows_must_be_sorted_and_unique(self):
        with tempfile.TemporaryDirectory() as temporary:
            _ctx, sli = self._valid_sli(Path(temporary))
        rows = [
            {"path_sha256": "b" * 64, "errors": ["second"]},
            {"path_sha256": "a" * 64, "errors": ["first"]},
        ]
        for label, invalid_rows in (
                ("unsorted", rows), ("duplicate", [rows[0], rows[0]])):
            with self.subTest(label=label):
                malformed = json.loads(json.dumps(sli))
                malformed.update({
                    "status": "violation",
                    "zero_preview_write_verified": False,
                    "invalid_receipt_count": len(invalid_rows),
                    "invalid_receipts": invalid_rows,
                })
                malformed["sli_sha256"] = audit._sha(
                    audit._without(malformed, "sli_sha256"))
                self.assertIn(
                    "sli_invalid_receipts_not_canonical",
                    audit.validate_sli(malformed))

    def test_symlinked_target_and_tampered_event_content_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx, sli = self._valid_sli(root)
            events = ctx["event_log"].read_text().splitlines()
            first = json.loads(events[0])
            first["target_session_date"] = "2026-07-09"
            first["event_sha256"] = audit._sha(
                audit._without(first, "event_sha256"))
            events[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
            ctx["event_log"].write_text("\n".join(events) + "\n")
            content_errors = audit.validate_sli_against_sources(
                sli, audit_root=root, target=TARGET)
            self.assertTrue(any(
                error.startswith("sli_source_projection_mismatch:")
                for error in content_errors), content_errors)

            target_root = root / TARGET
            alternate = root / f"{TARGET}-alternate"
            target_root.rename(alternate)
            target_root.symlink_to(alternate, target_is_directory=True)
            symlink_errors = audit.validate_sli_against_sources(
                sli, audit_root=root, target=TARGET)
        self.assertIn("sli_source_target_path_not_canonical", symlink_errors)


class TestAuditedWrapper(unittest.TestCase):
    def _run(self, root: Path, mode: str, *, require_coverage: bool = False):
        fake = root / "fake-claude"
        write_fake_claude(fake)
        env = {
            "TEST_AUDIT_SCRIPT": str(ROOT / "scripts" / "broker_runtime_audit.py"),
            "TEST_FAKE_MODE": mode,
        }
        with mock.patch.dict(os.environ, env, clear=False):
            return wrapper.run_audited(
                root=root, audit_root=root / "audit", target=TARGET, workflow="morning",
                run_id="morning-wrapper", lane=mode, claude=fake, arguments=["-p", "fixture"],
                settings_path=audit.SETTINGS_PATH, require_coverage=require_coverage)

    def test_read_only_attempt_produces_complete_zero_verified_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, rc = self._run(Path(temporary), "read")
            text = Path(result["receipt_path"]).read_text()
        self.assertEqual(rc, 0)
        self.assertEqual(result["instrumentation_coverage"], "complete")
        self.assertEqual(result["broker_read_attempt_total"], 1)
        self.assertEqual(result["broker_preview_write_attempt_total"], 0)
        self.assertEqual(result["daily_sli_status"], "zero_verified")
        self.assertNotIn("must-not-persist", text)

    def test_mutation_attempt_overrides_success_with_exit_86(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, rc = self._run(root, "mutation")
            fake = root / "fake-claude"
            with self.assertRaisesRegex(audit.AuditViolation, "prior_broker_runtime_violation"):
                wrapper.run_audited(
                    root=root, audit_root=root / "audit", target=TARGET, workflow="post_close",
                    run_id="postclose-after-violation", lane="blocked", claude=fake,
                    arguments=["-p", "must-not-run"], settings_path=audit.SETTINGS_PATH)
        self.assertEqual(result["child_exit_code"], 0)
        self.assertEqual(rc, 86)
        self.assertTrue(result["violation_detected"])
        self.assertEqual(result["broker_preview_write_attempt_total"], 1)
        self.assertEqual(result["daily_sli_status"], "violation")

    def test_missing_heartbeat_is_not_zero_and_promotion_can_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, rc = self._run(Path(temporary), "no-heartbeat")
        self.assertEqual(rc, 0)
        self.assertEqual(result["instrumentation_coverage"], "unobservable")
        self.assertEqual(result["daily_sli_status"], "unobservable")
        with tempfile.TemporaryDirectory() as temporary:
            _result, strict_rc = self._run(
                Path(temporary), "no-heartbeat", require_coverage=True)
        self.assertEqual(strict_rc, 87)

    def test_audit_finalization_failure_still_quarantines_child_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, rc = self._run(root, "audit-finalization-failure")
            report = root / "reports" / "post_close" / f"{TARGET}.md"
            quarantined = (root / "data" / "runtime" / "control_plane_quarantine"
                           / TARGET / result["invocation_id"] / "authority_outputs"
                           / "reports" / "post_close" / f"{TARGET}.md")
            self.assertEqual(rc, 87, result)
            self.assertIsNotNone(result["audit_finalization_error"])
            self.assertIsNone(result["receipt_sha256"])
            self.assertFalse(report.exists())
            self.assertEqual(
                quarantined.read_text(),
                "must be quarantined after audit failure\n")
            self.assertEqual(result["authority_outputs_quarantined"], 1)
            self.assertTrue(result["authority_quarantine_ok"])

    def test_model_child_receives_no_credentials_and_file_access_is_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = root / "fake-claude"
            marker = root / "env.json"
            write_fake_claude(fake)
            env = {
                "TEST_AUDIT_SCRIPT": str(ROOT / "scripts" / "broker_runtime_audit.py"),
                "TEST_FAKE_MODE": "env-probe",
                "TEST_ENV_MARKER": str(marker),
                **{name: f"fixture-{name}" for name in wrapper.secrets_loader.ALLOWLIST},
            }
            with mock.patch.dict(os.environ, env, clear=False):
                _result, rc = wrapper.run_audited(
                    root=root, audit_root=root / "audit", target=TARGET,
                    workflow="morning", run_id="morning-env-probe", lane="env-probe",
                    claude=fake, arguments=["-p", "fixture"],
                    settings_path=audit.SETTINGS_PATH)
            observed = json.loads(marker.read_text())
        self.assertEqual(rc, 0)
        self.assertEqual(observed["credential_names_present"], [])
        self.assertEqual(observed["model_access_disabled"], "1")

    def test_production_os_write_guard_profile_covers_every_control_surface(self):
        profile = wrapper._control_write_guard_profile(
            ROOT, audit.SETTINGS_PATH,
            extra_control_files=wrapper._lane_control_files("postclose-main", TARGET),
            extra_control_trees=wrapper._lane_control_trees("postclose-main"))
        self.assertIn("(deny file-write*", profile)
        for relative in wrapper.integrity.CONTROL_TREES:
            rendered = str((ROOT / relative).resolve())
            self.assertIn(f'(literal "{rendered}")', profile)
            self.assertIn(f'(subpath "{rendered}")', profile)
        for relative in wrapper.integrity.CONTROL_FILES:
            rendered = str((ROOT / relative).resolve())
            self.assertIn(f'(literal "{rendered}")', profile)
        source = (ROOT / "scripts" / "audited_claude.py").read_text()
        self.assertNotIn("--no-os-write-guard", source)
        self.assertNotIn("--disable-os-write-guard", source)
        self.assertIn(str((ROOT / "data" / "research_ledger.json").resolve()), profile)
        self.assertNotIn(str((ROOT / "data" / "watchlist.json").resolve()), profile)
        morning_profile = wrapper._control_write_guard_profile(
            ROOT, audit.SETTINGS_PATH,
            extra_control_files=wrapper._lane_os_guard_files("morning-main", TARGET),
            extra_control_trees=wrapper._lane_control_trees("morning-main"))
        self.assertIn(str((ROOT / "data" / "watchlist.json").resolve()), morning_profile)
        self.assertIn(str((ROOT / "data" / "research_ledger.json").resolve()),
                      morning_profile)
        self.assertIn(str((ROOT / "reports").resolve()), morning_profile)
        self.assertIn(str((ROOT / "data" / "runtime" / "research" / TARGET
                           / "freeze_receipt.json").resolve()), morning_profile)
        for relative in (
                f"data/runtime/preflight/{TARGET}.json",
                f"data/runtime/macro_actual_refresh/{TARGET}.json",
                f"data/runtime/robinhood/snapshot_{TARGET}.json",
                f"data/runtime/asset_allocation/{TARGET}/context.json"):
            self.assertIn(str((ROOT / relative).resolve()), morning_profile)
        sibling_owned = wrapper._lane_sibling_owned_files("morning-main", TARGET)
        self.assertEqual(set(sibling_owned), {
            f"data/runtime/robinhood/candidate_quote_request_{TARGET}.json",
            f"data/runtime/robinhood/candidate_quotes_{TARGET}.json",
            f"data/runtime/health/{TARGET}_candidate_quote_health.json",
        })
        self.assertTrue(set(sibling_owned).isdisjoint(
            wrapper._lane_control_files("morning-main", TARGET)))
        for relative in sibling_owned:
            self.assertIn(str((ROOT / relative).resolve()), morning_profile)

    def test_canonical_root_cannot_disable_os_write_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / "fake-claude"
            write_fake_claude(fake)
            with self.assertRaisesRegex(
                    audit.AuditError,
                    "canonical_control_plane_os_write_guard_may_not_be_disabled"):
                wrapper.run_audited(
                    root=ROOT, audit_root=Path(temporary) / "audit", target="2099-01-03",
                    workflow="morning", run_id="guard-disable-probe", lane="guard-disable",
                    claude=fake, arguments=["-p", "fixture"],
                    settings_path=audit.SETTINGS_PATH, enforce_os_write_guard=False)

    def test_missing_os_guard_fails_before_child_command_is_returned(self):
        with mock.patch.object(wrapper, "SANDBOX_EXEC", Path("/definitely/missing/sandbox-exec")):
            with self.assertRaisesRegex(audit.AuditError,
                                        "control_plane_os_write_guard_unavailable"):
                wrapper._guarded_child_command(
                    ["/usr/bin/true"], root=ROOT, settings_path=audit.SETTINGS_PATH)

    def test_macos_os_guard_blocks_dynamic_control_paths_but_allows_report(self):
        if sys.platform != "darwin" or not wrapper.SANDBOX_EXEC.is_file():
            self.skipTest("macOS sandbox-exec unavailable")
        probe = subprocess.run(
            [str(wrapper.SANDBOX_EXEC), "-p", "(version 1)(allow default)",
             "/usr/bin/true"], capture_output=True, text=True, check=False)
        if probe.returncode == 71 and "Operation not permitted" in probe.stderr:
            self.skipTest("nested sandbox unavailable in the current test host")
        self.assertEqual(probe.returncode, 0, probe.stderr)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "schemas").mkdir()
            (root / "launchd").mkdir()
            (root / "reports" / "post_close").mkdir(parents=True)
            settings = root / ".claude" / "settings.json"
            settings.parent.mkdir()
            settings.write_text("{}\n")
            profile = wrapper._control_write_guard_profile(root, settings)
            # Use a small on-disk helper because Python compound try/except statements cannot be
            # expressed safely as a one-line -c comprehension. It is outside every protected
            # control path and is removed before the temporary project root is discarded.
            helper = root / "sandbox-guard-probe.py"
            helper.write_text(
                "import pathlib, sys\n"
                "blocked = []\n"
                "for raw in sys.argv[1:3]:\n"
                "    try:\n"
                "        pathlib.Path(raw).write_text('blocked-write\\n')\n"
                "        blocked.append(False)\n"
                "    except OSError:\n"
                "        blocked.append(True)\n"
                "pathlib.Path(sys.argv[3]).write_text('allowed-report\\n')\n"
                "raise SystemExit(0 if all(blocked) else 9)\n"
            )
            try:
                guarded = subprocess.run(
                    [str(wrapper.SANDBOX_EXEC), "-p", profile, sys.executable, str(helper),
                     str(root / "schemas" / "dynamic.json"),
                     str(root / "launchd" / "dynamic.plist"),
                     str(root / "reports" / "post_close" / "allowed.md")],
                    capture_output=True, text=True, check=False)
            finally:
                helper.unlink(missing_ok=True)
            self.assertEqual(guarded.returncode, 0, guarded.stderr)
            self.assertFalse((root / "schemas" / "dynamic.json").exists())
            self.assertFalse((root / "launchd" / "dynamic.plist").exists())
            self.assertEqual(
                (root / "reports" / "post_close" / "allowed.md").read_text(),
                "allowed-report\n")

    def test_macos_morning_guard_blocks_publisher_outputs_but_allows_drafts(self):
        if sys.platform != "darwin" or not wrapper.SANDBOX_EXEC.is_file():
            self.skipTest("macOS sandbox-exec unavailable")
        probe = subprocess.run(
            [str(wrapper.SANDBOX_EXEC), "-p", "(version 1)(allow default)",
             "/usr/bin/true"], capture_output=True, text=True, check=False)
        if probe.returncode == 71 and "Operation not permitted" in probe.stderr:
            self.skipTest("nested sandbox unavailable in the current test host")
        self.assertEqual(probe.returncode, 0, probe.stderr)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            research = root / "data" / "runtime" / "research" / TARGET
            research.mkdir(parents=True)
            (root / "reports" / "morning").mkdir(parents=True)
            (root / "data" / "research_ledger.json").write_text("[]\n")
            (root / "data" / "watchlist.json").write_text("{}\n")
            settings = root / ".claude" / "settings.json"
            settings.parent.mkdir()
            settings.write_text("{}\n")
            profile = wrapper._control_write_guard_profile(
                root, settings,
                extra_control_files=wrapper._lane_os_guard_files("morning-main", TARGET),
                extra_control_trees=wrapper._lane_control_trees("morning-main"))
            helper = root / "morning-sandbox-guard-probe.py"
            helper.write_text(
                "import pathlib, sys\n"
                "blocked = []\n"
                "for raw in sys.argv[1:6]:\n"
                "    try:\n"
                "        pathlib.Path(raw).write_text('forbidden-write\\n')\n"
                "        blocked.append(False)\n"
                "    except OSError:\n"
                "        blocked.append(True)\n"
                "for raw in sys.argv[6:9]:\n"
                "    pathlib.Path(raw).write_text('allowed-draft\\n')\n"
                "raise SystemExit(0 if all(blocked) else 9)\n"
            )
            blocked = [
                root / "reports" / "morning" / f"{TARGET}.md",
                root / "data" / "research_ledger.json",
                research / "freeze_receipt.json",
                research / "candidates.json",
                root / "data" / "watchlist.json",
            ]
            allowed = [
                research / "draft.md",
                research / "candidates_draft.json",
                research / "freeze_request.json",
            ]
            try:
                guarded = subprocess.run(
                    [str(wrapper.SANDBOX_EXEC), "-p", profile, sys.executable, str(helper),
                     *map(str, blocked), *map(str, allowed)],
                    capture_output=True, text=True, check=False)
            finally:
                helper.unlink(missing_ok=True)
            self.assertEqual(guarded.returncode, 0, guarded.stderr)
            for path in blocked:
                with self.subTest(blocked=path):
                    if path.name == "research_ledger.json":
                        self.assertEqual(path.read_text(), "[]\n")
                    elif path.name == "watchlist.json":
                        self.assertEqual(path.read_text(), "{}\n")
                    else:
                        self.assertFalse(path.exists())
            for path in allowed:
                with self.subTest(allowed=path):
                    self.assertEqual(path.read_text(), "allowed-draft\n")

    def test_canonical_root_audited_child_runs_inside_os_guard(self):
        if sys.platform != "darwin" or not wrapper.SANDBOX_EXEC.is_file():
            self.skipTest("macOS sandbox-exec unavailable")
        probe = subprocess.run(
            [str(wrapper.SANDBOX_EXEC), "-p", "(version 1)(allow default)",
             "/usr/bin/true"], capture_output=True, text=True, check=False)
        if probe.returncode == 71 and "Operation not permitted" in probe.stderr:
            self.skipTest("nested sandbox unavailable in the current test host")
        self.assertEqual(probe.returncode, 0, probe.stderr)
        with tempfile.TemporaryDirectory() as temporary:
            scratch = Path(temporary)
            fake = scratch / "fake-claude"
            write_fake_claude(fake)
            env = {
                "TEST_AUDIT_SCRIPT": str(ROOT / "scripts" / "broker_runtime_audit.py"),
                "TEST_FAKE_MODE": "read",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            with mock.patch.dict(os.environ, env, clear=False):
                result, rc = wrapper.run_audited(
                    root=ROOT, audit_root=scratch / "audit", target="2099-01-02",
                    workflow="morning", run_id="os-guard-canonical-probe", lane="guard-probe",
                    claude=fake, arguments=["-p", "fixture"],
                    settings_path=audit.SETTINGS_PATH)
            self.assertEqual(rc, 0, result)
            self.assertTrue(result["control_plane_os_write_guard_enforced"])
            self.assertEqual(result["control_plane_integrity"], "verified_unchanged")
            self.assertEqual(result["broker_read_attempt_total"], 1)
            self.assertEqual(result["broker_preview_write_attempt_total"], 0)

    def test_termination_kills_descendant_process_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = root / "fake-claude"
            marker = root / "orphan-marker"
            write_fake_claude(fake)
            env = dict(os.environ)
            env.update({
                "TEST_AUDIT_SCRIPT": str(ROOT / "scripts" / "broker_runtime_audit.py"),
                "TEST_FAKE_MODE": "sleep-tree", "TEST_ORPHAN_MARKER": str(marker),
            })
            command = [
                sys.executable, str(ROOT / "scripts" / "audited_claude.py"),
                "--root", str(root), "--audit-root", str(root / "audit"),
                "--target", TARGET, "--workflow", "morning", "--run-id", "morning-tree",
                "--lane", "tree", "--claude", str(fake), "--settings", str(audit.SETTINGS_PATH),
                "--", "-p", "fixture",
            ]
            process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
            # Wait for the audited child heartbeat instead of assuming a fixed startup speed. A
            # cold or loaded machine can take longer than 350 ms to import the wrapper and install
            # its signal handler; killing before that point tests Python startup, not descendant
            # process-group termination.
            heartbeat_seen = False
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and process.poll() is None:
                event_logs = list((root / "audit" / TARGET).glob("*/*.events.jsonl"))
                if any('"event_type":"session_start"' in path.read_text()
                       or '"event_type": "session_start"' in path.read_text()
                       for path in event_logs):
                    heartbeat_seen = True
                    break
                time.sleep(0.02)
            self.assertTrue(heartbeat_seen, "audited child did not reach its heartbeat")
            os.kill(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
            time.sleep(1.4)
            self.assertFalse(marker.exists())
            audited_markers = (
                root / "data" / "runtime" / "audited_window").glob("*.json")
            self.assertEqual(
                list(audited_markers), [],
                "SIGTERM cleanup must remove the audited-window marker, not only descendants",
            )
            receipts = list((root / "audit" / TARGET).glob("*/*.receipt.json"))
            self.assertEqual(len(receipts), 1)

    def test_normal_exit_kills_remaining_descendant_process_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = root / "fake-claude"
            marker = root / "normal-exit-orphan-marker"
            write_fake_claude(fake)
            env = {
                "TEST_AUDIT_SCRIPT": str(ROOT / "scripts" / "broker_runtime_audit.py"),
                "TEST_FAKE_MODE": "exit-zero-tree",
                "TEST_ORPHAN_MARKER": str(marker),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                result, rc = wrapper.run_audited(
                    root=root, audit_root=root / "audit", target=TARGET,
                    workflow="morning", run_id="morning-normal-exit-tree",
                    lane="normal-exit-tree", claude=fake, arguments=["-p", "fixture"],
                    settings_path=audit.SETTINGS_PATH)
            self.assertEqual(result["child_exit_code"], 0, result)
            self.assertEqual(rc, 0, result)
            time.sleep(1.4)
            self.assertFalse(marker.exists())


class TestPostChildReceiptDurability(unittest.TestCase):
    """A termination signal in the post-child window must not destroy the audit receipt.

    Regression for the 2026-08-12 candidate-quotes lane, which reported
    ``collector_audit_receipt_missing`` for a child that had already exited 0. The wrapper was
    still inside the post-child control-plane comparison when the outer ``run_with_timeout``
    escalation delivered SIGTERM, and ``finalize_invocation`` had not run yet, so the completed
    child left no receipt at all.

    The window is widened DETERMINISTICALLY rather than by sleeping and hoping: a test-only
    ``sitecustomize`` shim inside the wrapper's own interpreter wraps
    ``control_plane_integrity.compare_control_plane`` so its first call publishes a marker and
    then blocks. The shim only DELAYS -- it changes no integrity semantics, it never fabricates
    a receipt, and it exists only inside this subprocess.
    """

    def test_sigterm_during_post_child_window_still_persists_the_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shim_dir = root / "shim"
            shim_dir.mkdir()
            entered = root / "compare-entered"
            fake = root / "fake-claude"
            fake.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n")
            fake.chmod(0o755)
            (shim_dir / "sitecustomize.py").write_text(
                "import pathlib, sys, time\n"
                f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
                "import control_plane_integrity as _integrity\n"
                "_original = _integrity.compare_control_plane\n"
                f"_marker = pathlib.Path({str(entered)!r})\n"
                "def _paused(*args, **kwargs):\n"
                "    _marker.write_text('entered')\n"
                "    time.sleep(60)\n"
                "    return _original(*args, **kwargs)\n"
                "_integrity.compare_control_plane = _paused\n"
            )
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join(
                part for part in (str(shim_dir), env.get("PYTHONPATH", "")) if part)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            command = [
                sys.executable, str(ROOT / "scripts" / "audited_claude.py"),
                "--root", str(root), "--audit-root", str(root / "audit"),
                "--target", TARGET, "--workflow", "morning",
                "--run-id", "postchild-signal", "--lane", "postchild-signal",
                "--claude", str(fake), "--settings", str(audit.SETTINGS_PATH),
                "--", "-p", "fixture",
            ]
            process = subprocess.Popen(
                command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            deadline = time.monotonic() + 60
            while (time.monotonic() < deadline and not entered.exists()
                   and process.poll() is None):
                time.sleep(0.02)
            if not entered.exists():
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                process.wait(timeout=10)
                self.fail("wrapper never reached the post-child control-plane comparison")
            # The wrapper is now provably past the child and inside post-child finalization.
            os.kill(process.pid, signal.SIGTERM)
            rc = process.wait(timeout=30)
            self.assertEqual(rc, 143, "an operator SIGTERM must still surface as 128+SIGTERM")
            receipts = sorted((root / "audit" / TARGET).glob("*/*.receipt.json"))
            self.assertEqual(
                len(receipts), 1,
                "a child that exited 0 must leave exactly one broker-audit receipt even when "
                "the wrapper is terminated during post-child finalization")
            receipt = json.loads(receipts[0].read_text())
            self.assertEqual(receipt["schema"], audit.RECEIPT_SCHEMA)
            self.assertEqual(receipt["target_session_date"], TARGET)
            # The receipt describes the real child, never a synthesized one.
            self.assertEqual(receipt["child_exit_code"], 0)
            self.assertFalse(receipt["violation_detected"])
            self.assertTrue(
                receipts[0].name.startswith(receipt["invocation_id"]), receipts[0].name)


class TestProductionWiring(unittest.TestCase):
    def test_all_scheduled_claude_lanes_use_audited_wrapper(self):
        morning = (ROOT / "scripts" / "run_morning_scan.sh").read_text()
        post_open = (ROOT / "scripts" / "run_premarket_refresh.sh").read_text()
        post_close = (ROOT / "scripts" / "run_post_close_review.sh").read_text()
        collector = (ROOT / "scripts" / "run_robinhood_readonly_collector.sh").read_text()
        common = (ROOT / "scripts" / "lib" / "common.sh").read_text()
        self.assertIn("scripts/audited_claude.py", morning)
        self.assertIn("run_audited_claude", post_open)
        self.assertIn("run_audited_claude", post_close)
        self.assertIn("scripts/audited_claude.py", collector)
        self.assertIn("ensure_broker_audit_run_id", common)
        self.assertIn("retry_audited_claude", common)
        self.assertNotIn("retry_audited_claude", post_open)
        self.assertIn("retry_audited_claude", post_close)
        for text in (morning, post_open, post_close, collector):
            self.assertNotRegex(text, r'"\$CLAUDE"\s+-p')
        self.assertNotIn("MultiEdit", collector)
        self.assertNotIn(
            "MultiEdit",
            (ROOT / "scripts" / "run_candidate_quote_hydrator.sh").read_text())

    def test_coverage_enforcement_is_not_silently_promoted(self):
        paths = [
            ROOT / "scripts" / "run_morning_scan.sh",
            ROOT / "scripts" / "run_premarket_refresh.sh",
            ROOT / "scripts" / "run_post_close_review.sh",
            ROOT / "scripts" / "run_robinhood_readonly_collector.sh",
        ]
        self.assertTrue(all("--require-coverage" not in path.read_text() for path in paths))
        self.assertIn("require_coverage", (ROOT / "scripts" / "audited_claude.py").read_text())

    def test_control_plane_violation_is_nonretryable_in_every_retry_path(self):
        morning = (ROOT / "scripts" / "run_morning_scan.sh").read_text()
        common = (ROOT / "scripts" / "lib" / "common.sh").read_text()
        self.assertIn('if [ "$crc" -eq 88 ]; then FAIL_CLASS="control_plane_integrity_violation"',
                      morning)
        self.assertIn('"$rc" -eq 88', common)


if __name__ == "__main__":
    unittest.main(verbosity=2)
