#!/usr/bin/env python3
"""Adversarial tests for cutoff-aware corporate-action adjustment views."""
from __future__ import annotations

import copy
import datetime as dt
import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import adjustment_policy as ap  # noqa: E402
import data_lake_ingest as ingest  # noqa: E402
import data_lake_quality  # noqa: E402
import data_lake_snapshot  # noqa: E402
from provider_adapters import base  # noqa: E402

PROVIDER = "massive_bakeoff"
OBSERVED = "2020-09-10T20:00:00+00:00"
CUTOFF = dt.datetime(2020, 9, 11, 0, 0, tzinfo=dt.timezone.utc)
DATES = ("2020-08-28", "2020-08-31", "2020-09-01", "2020-09-03",
         "2020-09-04", "2020-09-08")
AAPL = {
    "2020-08-28": (496.0, 505.0, 490.0, 500.0, 100),
    "2020-08-31": (127.0, 131.0, 126.0, 129.0, 400),
    "2020-09-01": (130.0, 134.0, 128.0, 132.0, 420),
    "2020-09-03": (129.0, 132.0, 128.0, 130.0, 430),
    "2020-09-04": (128.0, 131.0, 127.0, 129.0, 440),
    "2020-09-08": (126.0, 128.0, 123.0, 124.0, 450),
}
SPY = {
    date: (330.0 + index, 332.0 + index, 329.0 + index, 331.0 + index, 1000 + index)
    for index, date in enumerate(DATES)
}


def policy() -> dict:
    return json.loads((ROOT / "config" / "adjustment_policy.json").read_text())


def lake_config() -> dict:
    document = json.loads((ROOT / "config" / "data_lake.json").read_text())
    document["providers"][PROVIDER]["enabled"] = True
    return document


def action(*, action_id: str, action_type: str, effective_date: str,
           observed: str, revision: int = 1, status: str = "active",
           previous: str | None = None, numerator: str | None = None,
           denominator: str | None = None, cash: str | None = None,
           symbol: str = "AAPL") -> dict:
    payload = {
        "action_id": action_id,
        "action_type": action_type,
        "symbol": symbol,
        "security_id": f"security:{symbol.lower()}",
        "effective_date": effective_date,
        "status": status,
        "revision": revision,
        "previous_record_sha256": previous,
        "split_ratio_numerator": numerator,
        "split_ratio_denominator": denominator,
        "cash_amount": cash,
        "currency": "USD" if action_type == "cash_dividend" else None,
        "original_provider": PROVIDER,
    }
    return base.build_record(
        dataset="corporate_actions", provider=PROVIDER,
        record_id=f"{action_id}:{revision}", symbol=symbol,
        observed_at_utc=observed, publication_time_utc=observed,
        availability_precision="provider_point_in_time_revision", payload=payload)


def default_actions() -> list[dict]:
    return [
        action(action_id="aapl-split-20200831", action_type="split",
               effective_date="2020-08-31", observed="2020-08-20T12:00:00+00:00",
               numerator="4", denominator="1"),
        action(action_id="aapl-div-20200904", action_type="cash_dividend",
               effective_date="2020-09-04", observed="2020-09-01T12:00:00+00:00",
               cash="1.00"),
    ]


class LakeFixture:
    def __init__(self, root: Path, actions: list[dict] | None = None,
                 include_actions: bool = True, action_batch_retrieved: str = OBSERVED,
                 bars_adjusted: bool = False):
        self.root = root
        self.lake = root / "lake"
        self.snapshots = root / "snapshots"
        self.config = lake_config()
        self.bars_adjusted = bars_adjusted
        self._ingest_bars()
        if include_actions:
            self._ingest_actions(actions if actions is not None else default_actions(),
                                 action_batch_retrieved)

    def _ingest_bars(self) -> None:
        records = []
        for symbol, source in (("AAPL", AAPL), ("SPY", SPY)):
            for date in DATES:
                o, h, low, close, volume = source[date]
                records.append(base.build_record(
                    dataset="daily_bars", provider=PROVIDER,
                    record_id=f"{symbol}:{date}", symbol=symbol,
                    observed_at_utc=OBSERVED, availability_precision="eod_observation",
                    payload={
                        "symbol": symbol, "bar_date": date, "open": o, "high": h,
                        "low": low, "close": close, "volume": volume,
                        "adjusted": self.bars_adjusted,
                        "original_provider": PROVIDER, "source_vintage": "fixture-v1",
                    }))
        batch = base.build_batch(
            provider=PROVIDER, authority_source=None, dataset="daily_bars",
            retrieved_at_utc=OBSERVED, source_endpoint="local://adjustment-bars",
            source_payload_sha256=base.sha256_obj(records), records=records,
            license_class="commercial_unverified",
            retention_policy="prohibited_until_license_review")
        result = ingest.ingest_batch(batch, config=self.config, lake_root=self.lake)
        if not result.get("ok"):
            raise AssertionError(result)

    def _ingest_actions(self, records: list[dict], retrieved: str) -> None:
        batch = base.build_batch(
            provider=PROVIDER, authority_source=None, dataset="corporate_actions",
            retrieved_at_utc=retrieved, source_endpoint="local://adjustment-actions",
            source_payload_sha256=base.sha256_obj(records), records=records,
            license_class="commercial_unverified",
            retention_policy="prohibited_until_license_review")
        result = ingest.ingest_batch(batch, config=self.config, lake_root=self.lake)
        if not result.get("ok"):
            raise AssertionError(result)

    def snapshot(self, cutoff: dt.datetime = CUTOFF) -> dict:
        result = data_lake_snapshot.create_snapshot(self.lake, self.snapshots, cutoff)
        if not result.get("ok"):
            raise AssertionError(result)
        return result["snapshot"]


class PolicyContractTests(unittest.TestCase):
    def test_shipped_policy_and_schemas_are_fail_closed(self):
        document, errors = ap.load_policy()
        self.assertEqual(errors, [])
        self.assertIsNotNone(document)
        self.assertEqual(document["supported_views"], list(ap.SUPPORTED_VIEWS))
        self.assertFalse(document["safety"]["execution_enabled"])
        self.assertFalse(document["safety"]["feeds_production_scoring"])
        for name in ("adjustment_policy.schema.json", "corporate_action_payload.schema.json",
                     "adjusted_bar_view.schema.json"):
            schema = json.loads((ROOT / "schemas" / name).read_text())
            self.assertIsInstance(schema, dict)

    def test_semantic_mutation_invalidates_policy(self):
        document = policy()
        document["split_price_rule"] = "multiply"
        self.assertIn("adjustment_policy_rule_invalid:split_price_rule",
                      ap.validate_policy(document))
        document = policy()
        document["safety"]["activation_authority"] = True
        self.assertIn("adjustment_policy_safety_invalid", ap.validate_policy(document))


class MaterializationTests(unittest.TestCase):
    def test_raw_view_is_byte_value_preserving_and_needs_no_action_dataset(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = LakeFixture(Path(td), include_actions=False)
            result = ap.materialize_view(
                fixture.snapshot(), fixture.lake, policy=policy(), view="raw",
                source_provider=PROVIDER)
            self.assertTrue(result["ok"], result)
            artifact = result["artifact"]
            row = artifact["bars"]["AAPL"][0]
            self.assertEqual(row["open"], AAPL["2020-08-28"][0])
            self.assertEqual(row["volume"], AAPL["2020-08-28"][4])
            self.assertEqual(artifact["action_lineage"], [])
            self.assertTrue(artifact["roundtrip"]["all_passed"])
            self.assertTrue(artifact["quant_compatible"])

    def test_derived_view_requires_corporate_action_dataset(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = LakeFixture(Path(td), include_actions=False)
            result = ap.materialize_view(
                fixture.snapshot(), fixture.lake, policy=policy(), view="split_only",
                source_provider=PROVIDER)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "corporate_action_chain_invalid")
            self.assertIn("corporate_action_dataset_missing_or_empty", result["errors"])

    def test_provider_adjusted_bars_cannot_be_relabelled_as_policy_raw(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = LakeFixture(Path(td), bars_adjusted=True)
            result = ap.materialize_view(
                fixture.snapshot(), fixture.lake, policy=policy(), view="raw",
                source_provider=PROVIDER)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "raw_bar_materialization_failed")

    def test_split_only_math_price_volume_and_effective_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = LakeFixture(Path(td))
            result = ap.materialize_view(
                fixture.snapshot(), fixture.lake, policy=policy(), view="split_only",
                source_provider=PROVIDER)
            self.assertTrue(result["ok"], result)
            artifact = result["artifact"]
            rows = {row["date"]: row for row in artifact["bars"]["AAPL"]}
            self.assertEqual(rows["2020-08-28"]["close"], 125.0)
            self.assertEqual(rows["2020-08-28"]["volume"], 400.0)
            self.assertEqual(rows["2020-08-31"]["close"], 129.0)
            self.assertEqual(rows["2020-08-31"]["volume"], 400.0)
            self.assertEqual(artifact["counts"]["active_effective_action_count"], 2)
            self.assertEqual(artifact["counts"]["applied_action_count"], 1)
            self.assertEqual(ap.validate_view_artifact(artifact), [])

    def test_total_return_uses_prior_unadjusted_close(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = LakeFixture(Path(td))
            result = ap.materialize_view(
                fixture.snapshot(), fixture.lake, policy=policy(), view="total_return",
                source_provider=PROVIDER)
            self.assertTrue(result["ok"], result)
            artifact = result["artifact"]
            rows = {row["date"]: row for row in artifact["bars"]["AAPL"]}
            dividend_factor = Decimal("129") / Decimal("130")
            expected_pre_split = round(float(Decimal("500") / Decimal("4")
                                             * dividend_factor), 10)
            self.assertEqual(rows["2020-08-28"]["close"], expected_pre_split)
            self.assertEqual(rows["2020-09-03"]["close"], 129.0)
            self.assertEqual(rows["2020-09-04"]["close"], 129.0)
            dividend_lineage = next(row for row in artifact["action_lineage"]
                                    if row["action_type"] == "cash_dividend")
            self.assertTrue(dividend_lineage["applied_to_view"])
            self.assertTrue(artifact["roundtrip"]["all_passed"])

    def test_data_lake_quality_reports_policy_ready_without_auto_consumption(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = LakeFixture(Path(td))
            receipt = data_lake_quality.assess(
                fixture.lake, fixture.config, CUTOFF)
            summary = receipt["adjustment_policy"]
            self.assertTrue(summary["valid"])
            self.assertTrue(summary["adjustment_view_ready"])
            self.assertEqual(summary["contract_error_count"], 0)
            self.assertFalse(summary["automatic_quant_consumption"])
            self.assertFalse(summary["production_authority"])

    def test_future_effective_action_is_visible_but_never_applied(self):
        actions = default_actions() + [
            action(action_id="future-split", action_type="split",
                   effective_date="2020-10-01", observed="2020-09-02T12:00:00+00:00",
                   numerator="2", denominator="1")]
        with tempfile.TemporaryDirectory() as td:
            fixture = LakeFixture(Path(td), actions=actions)
            result = ap.materialize_view(
                fixture.snapshot(), fixture.lake, policy=policy(), view="split_only",
                source_provider=PROVIDER)
            self.assertTrue(result["ok"], result)
            artifact = result["artifact"]
            future = next(row for row in artifact["action_lineage"]
                          if row["action_id"] == "future-split")
            self.assertEqual(future["state"], "known_future_not_effective")
            self.assertFalse(future["applied_to_view"])
            self.assertEqual(artifact["counts"]["known_future_action_count"], 1)

    def test_action_observed_after_cutoff_is_rejected_even_if_manifest_is_included(self):
        poisoned = default_actions() + [
            action(action_id="future-observation", action_type="split",
                   effective_date="2020-09-01", observed="2020-10-01T12:00:00+00:00",
                   numerator="2", denominator="1")]
        with tempfile.TemporaryDirectory() as td:
            fixture = LakeFixture(Path(td), actions=poisoned,
                                  action_batch_retrieved="2020-09-01T12:00:00+00:00")
            result = ap.materialize_view(
                fixture.snapshot(), fixture.lake, policy=policy(), view="split_only",
                source_provider=PROVIDER)
            self.assertFalse(result["ok"])
            self.assertTrue(any("action_observed_after_snapshot_cutoff" in error
                                for error in result["errors"]))

    def test_post_cutoff_correction_manifest_cannot_change_old_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = LakeFixture(Path(td))
            snapshot_before = fixture.snapshot()
            first_view = ap.materialize_view(
                snapshot_before, fixture.lake, policy=policy(), view="split_only",
                source_provider=PROVIDER)["artifact"]
            original_split = default_actions()[0]
            correction = action(
                action_id="aapl-split-20200831", action_type="split",
                effective_date="2020-08-31", observed="2020-10-01T12:00:00+00:00",
                revision=2, status="corrected", previous=original_split["record_sha256"],
                numerator="5", denominator="1")
            fixture._ingest_actions([correction], "2020-10-01T12:00:00+00:00")
            snapshot_after = fixture.snapshot()
            second_view = ap.materialize_view(
                snapshot_after, fixture.lake, policy=policy(), view="split_only",
                source_provider=PROVIDER)["artifact"]
            self.assertEqual(snapshot_before["snapshot_id"], snapshot_after["snapshot_id"])
            self.assertEqual(first_view, second_view)


class RevisionAndIdentityTests(unittest.TestCase):
    def test_correction_latest_revision_wins(self):
        first = default_actions()[0]
        corrected = action(
            action_id="aapl-split-20200831", action_type="split",
            effective_date="2020-08-31", observed="2020-08-25T12:00:00+00:00",
            revision=2, status="corrected", previous=first["record_sha256"],
            numerator="5", denominator="1")
        actions = [first, corrected, default_actions()[1]]
        with tempfile.TemporaryDirectory() as td:
            fixture = LakeFixture(Path(td), actions=actions)
            artifact = ap.materialize_view(
                fixture.snapshot(), fixture.lake, policy=policy(), view="split_only",
                source_provider=PROVIDER)["artifact"]
            row = artifact["bars"]["AAPL"][0]
            self.assertEqual(row["close"], 100.0)
            split = next(item for item in artifact["action_lineage"]
                         if item["action_id"] == "aapl-split-20200831")
            self.assertEqual(len(split["revision_record_sha256s"]), 2)
            self.assertEqual(split["selected_record_sha256"], corrected["record_sha256"])

    def test_rescind_removes_action_without_rewriting_history(self):
        first = default_actions()[0]
        rescind = action(
            action_id="aapl-split-20200831", action_type="split",
            effective_date="2020-08-31", observed="2020-08-25T12:00:00+00:00",
            revision=2, status="rescinded", previous=first["record_sha256"],
            numerator="4", denominator="1")
        with tempfile.TemporaryDirectory() as td:
            fixture = LakeFixture(Path(td), actions=[first, rescind, default_actions()[1]])
            artifact = ap.materialize_view(
                fixture.snapshot(), fixture.lake, policy=policy(), view="split_only",
                source_provider=PROVIDER)["artifact"]
            self.assertEqual(artifact["bars"]["AAPL"][0]["close"], 500.0)
            self.assertEqual(artifact["counts"]["rescinded_action_count"], 1)

    def test_broken_parent_chain_fails_closed(self):
        first = default_actions()[0]
        broken = action(
            action_id="aapl-split-20200831", action_type="split",
            effective_date="2020-08-31", observed="2020-08-25T12:00:00+00:00",
            revision=2, status="corrected", previous="f" * 64,
            numerator="5", denominator="1")
        with tempfile.TemporaryDirectory() as td:
            fixture = LakeFixture(Path(td), actions=[first, broken, default_actions()[1]])
            result = ap.materialize_view(
                fixture.snapshot(), fixture.lake, policy=policy(), view="split_only",
                source_provider=PROVIDER)
            self.assertFalse(result["ok"])
            self.assertTrue(any("action_revision_parent_mismatch" in error
                                for error in result["errors"]))

    def test_policy_version_changes_dataset_identity_not_values(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = LakeFixture(Path(td))
            snapshot = fixture.snapshot()
            first = ap.materialize_view(
                snapshot, fixture.lake, policy=policy(), view="split_only",
                source_provider=PROVIDER)["artifact"]
            revised = policy()
            revised["version"] = "1.0.1"
            second = ap.materialize_view(
                snapshot, fixture.lake, policy=revised, view="split_only",
                source_provider=PROVIDER)["artifact"]
            self.assertEqual(first["bars"], second["bars"])
            self.assertNotEqual(first["policy_sha256"], second["policy_sha256"])
            self.assertNotEqual(first["dataset_id"], second["dataset_id"])

    def test_artifact_tamper_and_independent_rebuild(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = LakeFixture(Path(td))
            snapshot = fixture.snapshot()
            artifact = ap.materialize_view(
                snapshot, fixture.lake, policy=policy(), view="total_return",
                source_provider=PROVIDER)["artifact"]
            verification = ap.verify_view(artifact, snapshot, fixture.lake, policy())
            self.assertTrue(verification["ok"], verification)
            tampered = copy.deepcopy(artifact)
            tampered["bars"]["AAPL"][0]["close"] += 1
            self.assertIn("adjusted_view_bars_sha256_mismatch",
                          ap.validate_view_artifact(tampered))

    def test_cli_materialize_validate_verify_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture = LakeFixture(root / "fixture")
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(json.dumps(fixture.snapshot()))
            output = root / "view.json"
            base_command = [sys.executable, str(ROOT / "scripts" / "adjustment_policy.py")]
            proc = subprocess.run(
                base_command + ["materialize", "--snapshot", str(snapshot_path),
                                "--lake-root", str(fixture.lake), "--view", "split_only",
                                "--source-provider", PROVIDER, "--out", str(output)],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            validate = subprocess.run(base_command + ["validate", "--artifact", str(output)],
                                      cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(validate.returncode, 0, validate.stdout + validate.stderr)
            verify = subprocess.run(
                base_command + ["verify", "--artifact", str(output),
                                "--snapshot", str(snapshot_path),
                                "--lake-root", str(fixture.lake)],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)

    def test_data_lake_snapshot_derive_bars_entrypoint(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture = LakeFixture(root / "fixture")
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(json.dumps(fixture.snapshot()))
            output = root / "derived.json"
            proc = subprocess.run([
                sys.executable, str(ROOT / "scripts" / "data_lake_snapshot.py"),
                "derive-bars", "--snapshot", str(snapshot_path),
                "--lake-root", str(fixture.lake), "--view", "total_return",
                "--source-provider", PROVIDER, "--out", str(output),
            ], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            artifact = json.loads(output.read_text())
            self.assertEqual(ap.validate_view_artifact(artifact), [])
            self.assertFalse(artifact["execution_enabled"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
