#!/usr/bin/env python3
"""Replay FENCE-DEFERRED watchdog writes after the audited window closes.

WHY THIS EXISTS (2026-07-23)
----------------------------
The 2026-07-22 audited-window fence (scripts/run_autonomy_watchdog.sh,
fenced_record_state + staged health/alert evaluation) correctly defers every durable
control-plane-adjacent write while an audited window is open, staging into
data/runtime/health/fence_deferred/. But nothing ever replayed the staging: deferred
entries were permanently dropped, masked only because later unfenced ticks rewrite the
same live files. A fence overlapping the day's LAST tick would silently lose that day's
watchdog record. This helper closes the loop:

  stage   append one deferred record_state (task, status, detail, staged_utc) tuple as a
          JSON line to fence_deferred/record_state.staged.jsonl (called by
          fenced_record_state while the window is OPEN — the fence semantics inside the
          window are unchanged; this only makes the deferral durable).
  replay  called at the start of a tick once the window is CLOSED. Staged health/alert
          files move to their live paths ONLY if the live copy is not newer (newest-wins
          by file mtime — never overwrite newer live data; a superseded older live file
          is copied into the archive first, so no bytes are ever destroyed). Staged
          record_state rows re-apply oldest-first with the record_state-equivalent
          atomic write, skipping any task whose live system_state entry is already
          newer (or cannot be ordered — unordered live data is never overwritten).
          Every processed staging file is MOVED to
          fence_deferred/replayed/<utc-ts>/ together with a replay report — evidence is
          never deleted. Malformed staged files/rows are archived, recorded, and never
          crash the replay.

SAFETY CONTRACT
---------------
* Stdlib only; no network, no broker surface, no model invocation.
* Writes only: fence_deferred/** (staging + archive), data/system_state.json (the same
  comparison-exempt heartbeat record_state already rewrites every unfenced tick), and
  the same data/runtime/health/ receipt paths the unfenced watchdog already writes.
* Fail-open toward the watchdog: any structural failure returns nonzero and leaves the
  staging in place for the next unfenced tick; the caller never fails the tick on it.
* Never weakens the fence: replay is only ever invoked when the audited-window probe
  says CLOSED, exactly where the pre-fence watchdog wrote these files directly.

Exit codes: 0 = staged artifacts processed (or would be, --dry-run);
            3 = nothing staged (clean no-op); 1 = structural failure (staging left).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_STAGED_ROW = "fence_deferred_record_state/1.0"
SCHEMA_REPORT = "fence_deferred_replay_report/1.0"
STAGING_PARTS = ("data", "runtime", "health", "fence_deferred")
ARCHIVE_DIRNAME = "replayed"
STAGED_JSONL = "record_state.staged.jsonl"
HEALTH_STAGED = "autonomy_service_health.latest.json"
ALERT_STATE_STAGED = "autonomy_service_alert_state.json"
ALERT_RECEIPT_STAGED = "autonomy_service_alert.json"
TARGET_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_STAGED_BYTES = 5_000_000
MAX_ROWS = 10_000


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc(value):
    """Tolerant ISO-8601 → aware-UTC datetime; None when unparseable."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _staging_dir(root: Path) -> Path:
    return Path(root).resolve().joinpath(*STAGING_PARTS)


def _health_dir(root: Path) -> Path:
    return Path(root).resolve() / "data" / "runtime" / "health"


def _atomic_write_json(path: Path, document) -> None:
    """record_state-equivalent atomic write (temp + os.replace, indent=2, sort_keys)."""
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(directory), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(document, fh, indent=2, sort_keys=True)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    os.replace(tmp, str(path))


def _readable_staged(path: Path):
    """Refuse symlinks and oversized staging (evidence stays in place); else bytes."""
    if path.is_symlink():
        return None, "refused_symlink"
    try:
        if path.stat().st_size > MAX_STAGED_BYTES:
            return None, "refused_oversize"
        return path.read_bytes(), None
    except OSError as exc:
        return None, f"unreadable:{type(exc).__name__}"


HISTORY_NAME = "record_state_history.jsonl"


def _append_history(root: Path, rows: list, *, dry_run: bool) -> None:
    """Append-only record_state event history keyed by event identity (2026-07-28).

    Identity = sha256(task|status|detail|staged_utc); duplicate replays are deduplicated so
    re-running a replay is idempotent, and a same-second (or any later) slot overwrite can
    never silently erase a staged warning — the losing row survives here.
    """
    if dry_run or not rows:
        return
    import hashlib
    path = _health_dir(root) / HISTORY_NAME
    for row in rows:
        row["event_sha256"] = hashlib.sha256("\x1f".join((
            str(row.get("task")), str(row.get("status")), str(row.get("detail")),
            str(row.get("staged_utc")))).encode("utf-8")).hexdigest()
    existing = set()
    try:
        if path.is_file() and path.stat().st_size <= 8 * 1024 * 1024:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    existing.add(json.loads(line).get("event_sha256"))
                except ValueError:
                    continue
    except OSError:
        return
    fresh = [row for row in rows if row["event_sha256"] not in existing]
    if not fresh:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            for row in fresh:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        return


def _live_not_older(live: Path, staged: Path) -> bool:
    """Newest-wins guard: True => live copy is at least as new; never overwrite it."""
    try:
        return live.stat().st_mtime >= staged.stat().st_mtime
    except OSError:
        return False


class _Replay:
    def __init__(self, root: Path, dry_run: bool):
        self.root = Path(root).resolve()
        self.dry_run = dry_run
        self.staging = _staging_dir(self.root)
        self.now = _now_utc()
        self.actions = []
        self.failures = 0
        self.processed = 0
        self._archive_dir = None

    # ------------------------------------------------------------------ plumbing
    def note(self, name: str, action: str, **extra) -> None:
        row = {"name": name, "action": action}
        row.update(extra)
        self.actions.append(row)

    def archive_dir(self) -> Path:
        """Create (once) fence_deferred/replayed/<utc-ts>/ — never in --dry-run."""
        if self._archive_dir is None:
            base = self.staging / ARCHIVE_DIRNAME
            stamp = self.now.strftime("%Y%m%dT%H%M%SZ")
            candidate = base / stamp
            counter = 1
            while candidate.exists():
                candidate = base / f"{stamp}-{counter}"
                counter += 1
            if not self.dry_run:
                candidate.mkdir(parents=True, exist_ok=True)
            self._archive_dir = candidate
        return self._archive_dir

    def archive(self, staged: Path) -> None:
        """Move a processed staging file into the archive (evidence, never deleted)."""
        self.processed += 1
        if self.dry_run:
            return
        shutil.move(str(staged), str(self.archive_dir() / staged.name))

    def preserve_superseded_live(self, live: Path) -> None:
        """Before installing over an OLDER live file, copy it into the archive."""
        if self.dry_run or not live.exists():
            return
        shutil.copy2(str(live), str(self.archive_dir() / f"superseded_live_{live.name}"))

    def install(self, staged: Path, live: Path) -> None:
        """Atomic install preserving the staged mtime (honest ordering for future runs)."""
        if self.dry_run:
            return
        live.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(live.parent), suffix=".replay.tmp")
        os.close(fd)
        try:
            shutil.copy2(str(staged), tmp)  # copies bytes + mtime
            os.replace(tmp, str(live))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ------------------------------------------------------------------ file units
    def replay_file(self, name: str, kind: str, live: Path, need_target: bool) -> None:
        staged = self.staging / name
        if not staged.exists():
            return
        raw, refuse = _readable_staged(staged)
        if raw is None:
            self.note(name, "left_in_place", kind=kind, reason=refuse)
            self.failures += 1
            return
        document = None
        try:
            document = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            document = None
        if not isinstance(document, dict):
            self.note(name, "archived_malformed", kind=kind, reason="not_a_json_object")
            self.archive(staged)
            return
        if need_target:
            target = document.get("target_session_date")
            if not isinstance(target, str) or not TARGET_RE.match(target):
                self.note(name, "archived_malformed", kind=kind,
                          reason="target_session_date_missing_or_invalid")
                self.archive(staged)
                return
            live = _health_dir(self.root) / f"{target}_autonomy_service_health.json"
        if live.exists() and _live_not_older(live, staged):
            self.note(name, "skipped_live_newer", kind=kind,
                      live_path=str(live.relative_to(self.root)))
            self.archive(staged)
            return
        try:
            self.preserve_superseded_live(live)
            self.install(staged, live)
        except OSError as exc:
            self.note(name, "left_in_place", kind=kind,
                      reason=f"install_failed:{type(exc).__name__}")
            self.failures += 1
            return
        self.note(name, "installed", kind=kind,
                  live_path=str(live.relative_to(self.root)))
        self.archive(staged)

    # ------------------------------------------------------------------ record_state
    def replay_record_state(self) -> None:
        staged = self.staging / STAGED_JSONL
        if not staged.exists():
            return
        raw, refuse = _readable_staged(staged)
        if raw is None:
            self.note(STAGED_JSONL, "left_in_place", kind="record_state", reason=refuse)
            self.failures += 1
            return
        rows, malformed = [], []
        lines = raw.decode("utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines[:MAX_ROWS], start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                malformed.append({"line": index, "reason": "invalid_json"})
                continue
            task = row.get("task") if isinstance(row, dict) else None
            status = row.get("status") if isinstance(row, dict) else None
            staged_ts = _parse_utc(row.get("staged_utc")) if isinstance(row, dict) else None
            if (not isinstance(task, str) or not task.strip()
                    or not isinstance(status, str) or not status.strip()
                    or staged_ts is None):
                malformed.append({"line": index, "reason": "missing_or_invalid_fields"})
                continue
            rows.append({
                "task": task.strip(),
                "status": status.strip(),
                "detail": str(row.get("detail") or ""),
                "staged_utc": row.get("staged_utc"),
                "_ts": staged_ts,
                "_line": index,
            })
        if len(lines) > MAX_ROWS:
            malformed.append({"line": MAX_ROWS, "reason": "row_cap_exceeded_rest_archived_unapplied"})
        rows.sort(key=lambda item: (item["_ts"], item["_line"]))  # oldest first

        state_path = self.root / "data" / "system_state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        runs = state.get("runs")
        if not isinstance(runs, dict):
            runs = {}
            state["runs"] = runs

        applied = 0
        history_rows: list = []
        for row in rows:
            entry = runs.get(row["task"])
            live_ts = _parse_utc(entry.get("timestamp_utc")) if isinstance(entry, dict) else None
            base_history = {
                "task": row["task"], "status": row["status"], "detail": row["detail"],
                "staged_utc": row["staged_utc"], "line": row["_line"],
                "replayed_from_fence_utc": self.now.isoformat(),
            }
            if isinstance(entry, dict) and live_ts is None:
                self.note(STAGED_JSONL, "row_skipped_live_unordered", kind="record_state",
                          task=row["task"], line=row["_line"])
                history_rows.append({**base_history, "outcome": "skipped_live_unordered"})
                continue
            # Strictly-newer live wins; an EQUAL timestamp applies in staged line order so two
            # same-second rows both land (2026-07-28: the >= comparison silently dropped the
            # later staged row). Every row — applied or not — is retained in the append-only
            # history, so latest-wins slot semantics can never erase a historical warning.
            if live_ts is not None and live_ts > row["_ts"]:
                self.note(STAGED_JSONL, "row_skipped_live_newer", kind="record_state",
                          task=row["task"], line=row["_line"])
                history_rows.append({**base_history, "outcome": "skipped_live_newer"})
                continue
            runs[row["task"]] = {
                "status": row["status"],
                "detail": row["detail"],
                "timestamp_utc": row["staged_utc"],
                "replayed_from_fence_utc": self.now.isoformat(),
            }
            applied += 1
            self.note(STAGED_JSONL, "row_applied", kind="record_state",
                      task=row["task"], status=row["status"], line=row["_line"])
            history_rows.append({**base_history, "outcome": "applied"})
        _append_history(self.root, history_rows, dry_run=self.dry_run)
        for item in malformed:
            self.note(STAGED_JSONL, "row_archived_malformed", kind="record_state", **item)

        if applied and not self.dry_run:
            state["last_update_utc"] = self.now.isoformat()
            try:
                _atomic_write_json(state_path, state)
            except OSError as exc:
                self.note(STAGED_JSONL, "left_in_place", kind="record_state",
                          reason=f"state_write_failed:{type(exc).__name__}")
                self.failures += 1
                return
        self.note(STAGED_JSONL, "archived", kind="record_state",
                  rows_applied=applied, rows_malformed=len(malformed),
                  rows_total=len(rows) + len(malformed))
        self.archive(staged)

    # ------------------------------------------------------------------ driver
    def run(self) -> int:
        if not self.staging.is_dir():
            print(json.dumps({"schema": SCHEMA_REPORT, "result": "no_staging_dir"}))
            return 3
        known = {HEALTH_STAGED, ALERT_STATE_STAGED, ALERT_RECEIPT_STAGED, STAGED_JSONL}
        present = []
        for child in sorted(self.staging.iterdir()):
            if child.name.startswith(".") or child.name == ARCHIVE_DIRNAME:
                continue  # hidden lock artifacts / prior archives are not staging
            if child.name in known:
                present.append(child.name)
            else:
                self.note(child.name, "left_in_place_unrecognized", kind="unknown")
        if not present:
            print(json.dumps({"schema": SCHEMA_REPORT, "result": "nothing_staged",
                              "unrecognized": [a["name"] for a in self.actions]}))
            return 3

        health_dir = _health_dir(self.root)
        self.replay_file(ALERT_STATE_STAGED, "alert_state",
                         health_dir / ALERT_STATE_STAGED, need_target=False)
        self.replay_file(ALERT_RECEIPT_STAGED, "alert_receipt",
                         health_dir / ALERT_RECEIPT_STAGED, need_target=False)
        self.replay_file(HEALTH_STAGED, "health_receipt", health_dir, need_target=True)
        self.replay_record_state()

        report = {
            "schema": SCHEMA_REPORT,
            "replay_utc": self.now.isoformat(),
            "dry_run": self.dry_run,
            "staged_present": present,
            "processed_count": self.processed,
            "failure_count": self.failures,
            "archive_dir": (str(self._archive_dir.relative_to(self.root))
                            if self._archive_dir is not None else None),
            "actions": self.actions,
        }
        if not self.dry_run and self._archive_dir is not None:
            try:
                _atomic_write_json(self._archive_dir / "replay_report.json", report)
            except OSError:
                pass  # the stdout/log copy remains; report loss never fails replay
        print(json.dumps(report, sort_keys=True))
        if self.failures:
            return 1
        return 0 if self.processed else 3


def cmd_stage(args) -> int:
    root = Path(args.root).resolve()
    staging = _staging_dir(root)
    staging.mkdir(parents=True, exist_ok=True)
    row = {
        "schema": SCHEMA_STAGED_ROW,
        "task": args.task,
        "status": args.status,
        "detail": args.detail or "",
        "staged_utc": _now_utc().isoformat(),
    }
    with open(staging / STAGED_JSONL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    print(json.dumps({"schema": SCHEMA_STAGED_ROW, "staged_task": row["task"],
                      "staged_status": row["status"]}))
    return 0


def cmd_replay(args) -> int:
    return _Replay(Path(args.root), bool(args.dry_run)).run()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    stage = sub.add_parser("stage", help="append one deferred record_state tuple")
    stage.add_argument("--root", required=True)
    stage.add_argument("--task", required=True)
    stage.add_argument("--status", required=True)
    stage.add_argument("--detail", default="")
    stage.set_defaults(func=cmd_stage)
    replay = sub.add_parser("replay", help="replay staged artifacts (window CLOSED only)")
    replay.add_argument("--root", required=True)
    replay.add_argument("--dry-run", action="store_true",
                        help="read-only: report what would happen, move/write nothing")
    replay.set_defaults(func=cmd_replay)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # structural failure: staging stays for the next tick
        print(json.dumps({"schema": SCHEMA_REPORT, "result": "structural_failure",
                          "error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
