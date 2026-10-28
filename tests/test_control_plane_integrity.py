#!/usr/bin/env python3
"""Hermetic adversarial tests for the unattended child control-plane envelope."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from public_snapshot import skip_private_module  # noqa: E402

try:  # the public snapshot leaves some imported modules out
    import audited_claude as wrapper  # noqa: E402
    import broker_runtime_audit as audit  # noqa: E402
    import control_plane_integrity as integrity  # noqa: E402
    import run_lock  # noqa: E402
except ModuleNotFoundError as exc:
    raise skip_private_module(exc) from exc

TARGET = "2026-07-16"


def write_fake(path: Path) -> None:
    source = f"""#!{sys.executable}
import json, os, pathlib, subprocess, sys
root = pathlib.Path.cwd()
audit_script = os.environ['TEST_AUDIT_SCRIPT']
def hook(command, payload):
    return subprocess.run(
        [sys.executable, audit_script, command], input=json.dumps(payload), text=True,
        capture_output=True, env=os.environ.copy(), check=False)
hook('hook-session', {{'hook_event_name':'SessionStart','session_id':'fixture'}})
mode = os.environ.get('TEST_INTEGRITY_MODE', 'clean')
if mode in ('control-and-authority', 'control-symlink'):
    control = root / 'scripts' / 'research_freeze.py'
    if mode == 'control-symlink':
        control.unlink()
        control.symlink_to(root / 'attacker-copy.py')
    else:
        control.write_text('compromised-control-plane\\n')
    report = root / 'reports' / 'morning' / 'official_research' / '{TARGET}.md'
    receipt = root / 'data' / 'runtime' / 'research' / '{TARGET}' / 'freeze_receipt.json'
    raw_agent = root / 'data' / 'runtime' / 'research' / '{TARGET}' / 'agents' / 'raw.json'
    for item in (report, receipt, raw_agent): item.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('forged official\\n')
    receipt.write_text('{{"forged":true}}\\n')
    raw_agent.write_text('{{"status":"forensic-only"}}\\n')
elif mode == 'broker-violation-authority':
    hook('hook-tool', {{'hook_event_name':'PreToolUse','session_id':'fixture',
         'tool_name':'mcp__robinhood-trading__place_equity_order',
         'tool_input':{{'symbol':'SPY'}}}})
    report = root / 'reports' / 'post_close' / '{TARGET}.md'
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('must not remain authoritative\\n')
elif mode == 'child-failure-authority':
    report = root / 'reports' / 'post_close' / '{TARGET}.md'
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('partial failed-child output\\n')
    (root / 'data' / 'watchlist.json').write_text('{{"compromised":true}}\\n')
    sys.exit(23)
elif mode == 'schema-launchd-control':
    schema = root / 'schemas' / 'authority.schema.json'
    scheduler = root / 'launchd' / 'workflow.plist'
    schema.write_text('{{"compromised":true}}\\n')
    scheduler.write_text('compromised scheduler\\n')
    (root / 'schemas' / 'injected.schema.json').write_text('{{}}\\n')
    report = root / 'reports' / 'post_close' / '{TARGET}.md'
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('forged under changed schema\\n')
elif mode == 'morning-success-crosslane':
    preflight = root / 'data' / 'runtime' / 'preflight' / '{TARGET}.json'
    preflight.write_text('{{"mutated":true}}\\n')
    report = root / 'reports' / 'post_close' / '{TARGET}.md'
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('cross-lane forged report\\n')
elif mode == 'morning-success-allowed':
    research = root / 'data' / 'runtime' / 'research' / '{TARGET}'
    research.mkdir(parents=True, exist_ok=True)
    (research / 'draft.md').write_text('draft-only\\n')
    (research / 'evidence.json').write_text('{{"fixture":true}}\\n')
    (research / 'freeze_request.json').write_text('{{"fixture":true}}\\n')
elif mode == 'hardlink-delayed':
    control = root / 'scripts' / 'research_freeze.py'
    alias = root / 'child-control-alias.py'
    os.link(control, alias)
    delayed = (
        "import pathlib,time; time.sleep(1.0); "
        "pathlib.Path(" + repr(str(alias)) + ").write_text('delayed-alias-mutation\\\\n')"
    )
    subprocess.Popen([sys.executable, '-c', delayed])
"""
    path.write_text(source)
    path.chmod(0o755)


class ControlPlaneEnvelopeTests(unittest.TestCase):
    def _run(self, root: Path, mode: str, *, lane: str = "integrity") -> tuple[dict, int]:
        fake = root / "fake-claude"
        write_fake(fake)
        env = {
            "TEST_AUDIT_SCRIPT": str(ROOT / "scripts" / "broker_runtime_audit.py"),
            "TEST_INTEGRITY_MODE": mode,
        }
        with mock.patch.dict(os.environ, env, clear=False):
            return wrapper.run_audited(
                root=root,
                audit_root=root / "audit",
                target=TARGET,
                workflow="morning",
                run_id="integrity-fixture",
                lane=lane,
                claude=fake,
                arguments=["-p", "fixture"],
                settings_path=audit.SETTINGS_PATH,
            )

    def _root(self, temporary: str) -> Path:
        root = Path(temporary)
        control = root / "scripts" / "research_freeze.py"
        control.parent.mkdir(parents=True)
        control.write_text("original-control-plane\n")
        schema = root / "schemas" / "authority.schema.json"
        schema.parent.mkdir(parents=True)
        schema.write_text('{"original":true}\n')
        scheduler = root / "launchd" / "workflow.plist"
        scheduler.parent.mkdir(parents=True)
        scheduler.write_text("original scheduler\n")
        watchlist = root / "data" / "watchlist.json"
        watchlist.parent.mkdir(parents=True, exist_ok=True)
        watchlist.write_text('{"original":true}\n')
        (root / "attacker-copy.py").write_text("external-target\n")
        preflight = root / "data" / "runtime" / "preflight" / f"{TARGET}.json"
        preflight.parent.mkdir(parents=True, exist_ok=True)
        preflight.write_text('{"original":true}\n')
        return root

    def test_regular_file_link_count_is_projected_and_alias_drift_is_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            control = root / "scripts" / "research_freeze.py"
            before = integrity.capture_control_plane(root)
            self.assertEqual(
                before["projection"]["scripts/research_freeze.py"]["nlink"], 1)
            os.link(control, root / "allowed-alias.py")
            comparison = integrity.compare_control_plane(before)
            self.assertFalse(comparison["ok"])
            self.assertIn("scripts/research_freeze.py", comparison["changed_paths"])
            self.assertEqual(
                comparison["after"]["projection"]["scripts/research_freeze.py"]["nlink"], 2)

    def test_preexisting_hardlink_on_unenumerated_control_file_blocks_child_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            control = root / "scripts" / "research_freeze.py"
            os.link(control, root / "preexisting-control-alias.py")
            child_ran = root / "child-ran"
            fake = root / "fake-claude"
            fake.write_text(
                "#!/bin/sh\nprintf ran > \"$TEST_CHILD_RAN\"\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            with mock.patch.dict(
                    os.environ, {"TEST_CHILD_RAN": str(child_ran)}, clear=False):
                with self.assertRaisesRegex(
                        audit.AuditError,
                        "control_plane_preflight_failed:"
                        "control_plane_hardlink_forbidden:scripts/research_freeze.py"):
                    wrapper.run_audited(
                        root=root,
                        audit_root=root / "audit",
                        target=TARGET,
                        workflow="morning",
                        run_id="preexisting-control-hardlink",
                        lane="integrity",
                        claude=fake,
                        arguments=["-p", "fixture"],
                        settings_path=audit.SETTINGS_PATH,
                        enforce_os_write_guard=False,
                    )
            self.assertFalse(child_ran.exists(), "preflight rejection must precede child launch")
            marker_dir = root / "data" / "runtime" / "audited_window"
            self.assertEqual(list(marker_dir.glob("*.json")), [])

    def test_child_hardlink_with_delayed_mutator_fails_closed_before_outer_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            acquired = run_lock.acquire(root, pid=os.getpid())
            self.assertTrue(acquired["ok"], acquired)
            try:
                result, rc = self._run(root, "hardlink-delayed")
            finally:
                released = run_lock.release(
                    root, pid=os.getpid(), nonce=str(acquired["nonce"]))
            self.assertTrue(released["ok"], released)
            self.assertEqual(rc, wrapper.CONTROL_PLANE_VIOLATION_EXIT, result)
            self.assertEqual(result["child_exit_code"], 0)
            integrity_receipt = json.loads(
                Path(result["integrity_receipt_path"]).read_text())
            self.assertIn(
                "scripts/research_freeze.py",
                integrity_receipt["changed_control_paths"],
            )
            self.assertEqual(
                (root / "scripts" / "research_freeze.py").read_text(),
                "original-control-plane\n",
            )
            # The wrapper kills the whole child process group before integrity comparison and
            # before the outer controller releases its lock. A delayed alias writer cannot outlive
            # that boundary even though this hermetic test deliberately disables Seatbelt.
            __import__("time").sleep(1.2)
            self.assertEqual(
                (root / "child-control-alias.py").read_text(),
                "original-control-plane\n",
            )

    def test_persistent_control_drift_is_restored_and_authority_is_quarantined(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            result, rc = self._run(root, "control-and-authority")
            control = root / "scripts" / "research_freeze.py"
            official = root / "reports" / "morning" / "official_research" / f"{TARGET}.md"
            receipt = (root / "data" / "runtime" / "research" / TARGET
                       / "freeze_receipt.json")
            raw_agent = (root / "data" / "runtime" / "research" / TARGET
                         / "agents" / "raw.json")
            quarantine = (root / "data" / "runtime" / "control_plane_quarantine"
                          / TARGET / result["invocation_id"])
            integrity_receipt = json.loads(Path(result["integrity_receipt_path"]).read_text())

            self.assertEqual(rc, wrapper.CONTROL_PLANE_VIOLATION_EXIT, result)
            self.assertEqual(result["child_exit_code"], 0)
            self.assertEqual(result["control_plane_integrity"], "violation_restored")
            self.assertTrue(result["control_plane_restored_exactly"])
            self.assertEqual(control.read_text(), "original-control-plane\n")
            self.assertFalse(official.exists())
            self.assertFalse(receipt.exists())
            self.assertFalse(raw_agent.exists())
            self.assertEqual(result["authority_outputs_quarantined"], 3)
            self.assertEqual(
                (quarantine / "control_plane" / "scripts" / "research_freeze.py").read_text(),
                "compromised-control-plane\n",
            )
            self.assertEqual(
                (quarantine / "authority_outputs" / "reports" / "morning"
                 / "official_research" / f"{TARGET}.md").read_text(),
                "forged official\n",
            )
            self.assertEqual(
                (quarantine / "authority_outputs" / "data" / "runtime" / "research"
                 / TARGET / "agents" / "raw.json").read_text(),
                '{"status":"forensic-only"}\n',
            )
            self.assertEqual(integrity_receipt["schema"], integrity.SCHEMA)
            self.assertFalse(integrity_receipt["control_plane_unchanged"])
            self.assertTrue(
                integrity_receipt["control_plane_restoration"]["restored_exactly"])

    def test_symlink_replacement_is_preserved_for_forensics_then_restored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            result, rc = self._run(root, "control-symlink")
            control = root / "scripts" / "research_freeze.py"
            quarantined = (root / "data" / "runtime" / "control_plane_quarantine"
                           / TARGET / result["invocation_id"] / "control_plane"
                           / "scripts" / "research_freeze.py")
            self.assertEqual(rc, wrapper.CONTROL_PLANE_VIOLATION_EXIT, result)
            self.assertFalse(control.is_symlink())
            self.assertEqual(control.read_text(), "original-control-plane\n")
            self.assertTrue(quarantined.is_symlink())
            self.assertEqual(os.readlink(quarantined), str((root / "attacker-copy.py").resolve()))

    def test_broker_write_attempt_also_revokes_same_child_authority_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            result, rc = self._run(root, "broker-violation-authority")
            post_close = root / "reports" / "post_close" / f"{TARGET}.md"
            quarantined = (root / "data" / "runtime" / "control_plane_quarantine"
                           / TARGET / result["invocation_id"] / "authority_outputs"
                           / "reports" / "post_close" / f"{TARGET}.md")
            self.assertEqual(rc, 86, result)
            self.assertTrue(result["violation_detected"])
            self.assertEqual(result["control_plane_integrity"], "verified_unchanged")
            self.assertFalse(post_close.exists())
            self.assertEqual(quarantined.read_text(), "must not remain authoritative\n")
            self.assertEqual(result["authority_outputs_quarantined"], 1)

    def test_nonzero_child_output_is_quarantined_before_any_possible_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            result, rc = self._run(
                root, "child-failure-authority", lane="postclose-main")
            post_close = root / "reports" / "post_close" / f"{TARGET}.md"
            quarantined = (root / "data" / "runtime" / "control_plane_quarantine"
                           / TARGET / result["invocation_id"] / "authority_outputs"
                           / "reports" / "post_close" / f"{TARGET}.md")
            watchlist = root / "data" / "watchlist.json"
            quarantined_watchlist = (
                root / "data" / "runtime" / "control_plane_quarantine"
                / TARGET / result["invocation_id"] / "authority_outputs"
                / "data" / "watchlist.json")
            self.assertEqual(rc, 23, result)
            self.assertEqual(result["child_exit_code"], 23)
            self.assertEqual(result["control_plane_integrity"], "verified_unchanged")
            self.assertFalse(post_close.exists())
            self.assertEqual(quarantined.read_text(), "partial failed-child output\n")
            self.assertEqual(watchlist.read_text(), '{"original":true}\n')
            self.assertEqual(
                quarantined_watchlist.read_text(), '{"compromised":true}\n')
            self.assertEqual(result["authority_outputs_quarantined"], 2)
            self.assertTrue(result["authority_quarantine_ok"])

    def test_schema_and_launchd_drift_revoke_output_and_restore_exactly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            result, rc = self._run(root, "schema-launchd-control")
            schema = root / "schemas" / "authority.schema.json"
            injected = root / "schemas" / "injected.schema.json"
            scheduler = root / "launchd" / "workflow.plist"
            post_close = root / "reports" / "post_close" / f"{TARGET}.md"
            changed = json.loads(Path(result["integrity_receipt_path"]).read_text())[
                "changed_control_paths"]

            self.assertEqual(rc, wrapper.CONTROL_PLANE_VIOLATION_EXIT, result)
            self.assertTrue(result["control_plane_restored_exactly"])
            self.assertEqual(schema.read_text(), '{"original":true}\n')
            self.assertEqual(scheduler.read_text(), "original scheduler\n")
            self.assertFalse(injected.exists())
            self.assertFalse(post_close.exists())
            self.assertIn("schemas/authority.schema.json", changed)
            self.assertIn("schemas/injected.schema.json", changed)
            self.assertIn("launchd/workflow.plist", changed)
            self.assertEqual(result["authority_outputs_quarantined"], 1)

    def test_successful_morning_child_cannot_mutate_inputs_or_cross_lane_reports(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            result, rc = self._run(
                root, "morning-success-crosslane", lane="morning-main")
            preflight = root / "data" / "runtime" / "preflight" / f"{TARGET}.json"
            post_close = root / "reports" / "post_close" / f"{TARGET}.md"
            quarantine = (root / "data" / "runtime" / "control_plane_quarantine"
                          / TARGET / result["invocation_id"] / "authority_outputs")
            self.assertEqual(rc, wrapper.CONTROL_PLANE_VIOLATION_EXIT, result)
            self.assertEqual(result["child_exit_code"], 0)
            self.assertEqual(preflight.read_text(), '{"original":true}\n')
            self.assertFalse(post_close.exists())
            self.assertIn(
                f"reports/post_close/{TARGET}.md",
                result["unauthorized_authority_paths"])
            self.assertEqual(
                (quarantine / "reports" / "post_close" / f"{TARGET}.md").read_text(),
                "cross-lane forged report\n")

    def test_successful_morning_child_may_write_only_research_drafts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            result, rc = self._run(
                root, "morning-success-allowed", lane="morning-main")
            research = root / "data" / "runtime" / "research" / TARGET
            self.assertEqual(rc, 0, result)
            self.assertEqual(result["unauthorized_authority_paths"], [])
            self.assertEqual(result["authority_outputs_quarantined"], 0)
            self.assertEqual((research / "draft.md").read_text(), "draft-only\n")
            self.assertTrue((research / "evidence.json").is_file())
            self.assertTrue((research / "freeze_request.json").is_file())

    def test_outer_publisher_and_runtime_lock_names_are_model_immutable(self):
        self.assertIn(
            "data/runtime/research/.publisher_locks", integrity.CONTROL_TREES)
        self.assertIn("data/research_ledger.lock", integrity.CONTROL_FILES)
        self.assertNotIn(".run.lock", integrity.CONTROL_TREES)
        self.assertIn(".run.lock", integrity.MODEL_IMMUTABLE_RUNTIME_TREES)
        self.assertIn(
            "data/runtime/audited_window",
            integrity.MODEL_IMMUTABLE_RUNTIME_TREES,
        )
        profile = wrapper._control_write_guard_profile(
            ROOT, audit.SETTINGS_PATH)
        self.assertIn(
            str((ROOT / "data" / "runtime" / "research" / ".publisher_locks").resolve()),
            profile,
        )
        self.assertIn(str((ROOT / "data" / "research_ledger.lock").resolve()), profile)
        for relative in integrity.MODEL_IMMUTABLE_RUNTIME_TREES:
            rendered = str((ROOT / relative).resolve())
            self.assertIn(f'(deny file-write* (literal "{rendered}"))', profile)
            self.assertIn(f'(deny file-write* (subpath "{rendered}"))', profile)

    def test_outer_run_lock_lifecycle_never_enters_persistent_control_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = integrity.capture_control_plane(root, reject_unsafe=False)
            acquired = run_lock.acquire(root, pid=os.getpid())
            self.assertTrue(acquired["ok"], acquired)
            during = integrity.capture_control_plane(root, reject_unsafe=False)
            self.assertEqual(
                integrity.comparison_digest(before),
                integrity.comparison_digest(during),
            )
            self.assertFalse(any(
                label.startswith(".run.lock")
                for label in during["projection"]
            ))
            released = run_lock.release(
                root, pid=os.getpid(), nonce=str(acquired["nonce"]))
            self.assertTrue(released["ok"], released)
            after = integrity.capture_control_plane(root, reject_unsafe=False)
            self.assertEqual(
                integrity.comparison_digest(before),
                integrity.comparison_digest(after),
            )
            self.assertFalse((root / ".run.lock").exists())

    def test_outer_only_entrypoints_are_read_and_exec_denied_to_child(self):
        profile = wrapper._control_write_guard_profile(
            ROOT, audit.SETTINGS_PATH)
        self.assertEqual(
            set(integrity.OUTER_ONLY_CHILD_ENTRYPOINTS),
            {
                "scripts/audited_claude.py",
                "scripts/run_candidate_quote_hydrator.sh",
                "scripts/candidate_quote_barrier.py",
                "scripts/run_robinhood_readonly_collector.sh",
                "scripts/lib/run_lock.py",
            },
        )
        for relative in integrity.OUTER_ONLY_CHILD_ENTRYPOINTS:
            rendered = str((ROOT / relative).resolve())
            self.assertIn(f'(deny file-read* (literal "{rendered}"))', profile)
            self.assertIn(f'(deny process-exec (literal "{rendered}"))', profile)
        for relative in integrity.OUTER_ONLY_CHILD_READ_TREES:
            rendered = str((ROOT / relative).resolve())
            self.assertIn(f'(deny file-read* (literal "{rendered}"))', profile)
            self.assertIn(f'(deny file-read* (subpath "{rendered}"))', profile)


class Test20260720ProblemsDoNotRecur(unittest.TestCase):
    """Regression guard for every problem that appeared on 2026-07-20.

    (1) watchdog-vs-audited-morning control-plane race (rc=88 false quarantine),
    (2) bash-3.2 nounset empty-array crash in the failure-marker path,
    (3) self-masking watchdog (silent fail-closed preflight refusals).
    See journal/system_changes.md 2026-07-20 entries.
    """
    TGT = "2026-07-20"

    def _root(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        root = Path(tmp)
        (root / "data").mkdir()
        (root / "data" / "system_state.json").write_text('{"runs":{},"v":1}')
        (root / "CLAUDE.md").write_text("# constitution v1\n")
        (root / "data" / "runtime" / "research" / self.TGT).mkdir(parents=True)
        (root / "data" / "runtime" / "research" / self.TGT / "evidence.json").write_text("{}")
        (root / "data" / "runtime" / "health").mkdir(parents=True)
        (root / "data" / "runtime" / "health" / (self.TGT + "_autonomy_service_health.json")).write_text("{}")
        return root

    def test_sibling_heartbeat_write_is_not_a_violation(self):
        root = self._root()
        before = integrity.capture_control_plane(root, reject_unsafe=False)
        (root / "data" / "system_state.json").write_text('{"runs":{"watchdog":"tick"},"v":2}')
        cmp = integrity.compare_control_plane(before)
        self.assertTrue(cmp["ok"], "sibling system_state.json heartbeat must not be a violation")
        self.assertNotIn("data/system_state.json", cmp["changed_paths"])

    def test_real_control_tamper_still_fails_closed(self):
        root = self._root()
        before = integrity.capture_control_plane(root, reject_unsafe=False)
        (root / "CLAUDE.md").write_text("# TAMPERED\n")
        cmp = integrity.compare_control_plane(before)
        self.assertFalse(cmp["ok"])
        self.assertIn("CLAUDE.md", cmp["changed_paths"])

    def test_combined_tamper_plus_heartbeat_catches_only_tamper(self):
        root = self._root()
        before = integrity.capture_control_plane(root, reject_unsafe=False)
        (root / "data" / "system_state.json").write_text('{"v":2}')
        (root / "CLAUDE.md").write_text("# TAMPERED\n")
        cmp = integrity.compare_control_plane(before)
        self.assertFalse(cmp["ok"])
        self.assertEqual(cmp["changed_paths"], ["CLAUDE.md"])

    def test_watchdog_health_file_excluded_from_authority_surface(self):
        root = self._root()
        paths = {p.as_posix() for p in integrity._authority_paths(root, self.TGT)}
        health = (root / "data" / "runtime" / "health" / (self.TGT + "_autonomy_service_health.json")).as_posix()
        evidence = (root / "data" / "runtime" / "research" / self.TGT / "evidence.json").as_posix()
        self.assertNotIn(health, paths, "sibling watchdog health file must not enter the authority surface")
        self.assertIn(evidence, paths, "genuine research output must still be captured")

    def test_os_write_guard_denylist_preserved(self):
        self.assertIn("data/system_state.json", integrity.CONTROL_FILES)
        self.assertIn("data/system_state.json", integrity.COMPARISON_EXEMPT_CONTROL_FILES)

    def test_morning_lane_allows_sibling_system_state_but_not_health(self):
        self.assertTrue(wrapper._morning_authority_change_allowed("data/system_state.json", self.TGT))
        self.assertFalse(wrapper._morning_authority_change_allowed(
            "data/runtime/health/%s_autonomy_service_health.json" % self.TGT, self.TGT))

    def test_morning_runner_uses_bash32_safe_empty_array(self):
        src = (ROOT / "scripts" / "run_morning_scan.sh").read_text()
        for name in ("ckpt_args", "DT_SHA_ARGS", "RECON_DETERMINISTIC_ARGS"):
            safe = '${%s[@]+"${%s[@]}"}' % (name, name)
            self.assertIn(safe, src)
            self.assertNotIn('"${%s[@]}"' % name, src.replace(safe, ""))
        self.assertNotIn('--task "$TASK" "${ckpt_args[@]}"', src)

    def test_morning_runner_fails_closed_on_reconciler_infrastructure_failure(self):
        src = (ROOT / "scripts" / "run_morning_scan.sh").read_text()
        self.assertIn("terminal_reconcile_infrastructure_failure", src)
        self.assertIn('record_state "$TASK" failed '
                      '"terminal_reconcile_infrastructure_failure:${RECON_INFRA_REASON:-unknown}"',
                      src)
        self.assertIn('RUNNER_FINAL_RC=1', src)
        infra_branch = src.index('if [ "$RECON_INFRA_FAILURE" = "True" ]; then',
                                 src.index('CLASSIFICATION=""'))
        terminal_branch = src.index(
            'elif [ -f "$OFFICIAL" ] && [ -f "$RESEARCH_OFFICIAL" ]; then',
            infra_branch,
        )
        self.assertLess(infra_branch, terminal_branch)

    def test_watchdog_preflight_refusals_are_loud(self):
        wd = (ROOT / "scripts" / "run_autonomy_watchdog.sh").read_text()
        for token in ("integrity_manifest_failed", "broker_permission_validator_failed",
                      "service_alert_contract_invalid", "market_day_validator_failed"):
            idx = wd.find(token)
            self.assertGreater(idx, 0, "missing preflight branch: %s" % token)
            self.assertIn("notify ", wd[max(0, idx - 400):idx],
                          "preflight refusal '%s' must notify (loud fail-closed)" % token)


if __name__ == "__main__":
    unittest.main(verbosity=2)
