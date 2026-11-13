"""CARD-GATE-01c-b: complete_task() runs the evidence gate (unittest; pytest absent on brute)."""
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


class TestCompletionEvidenceGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        home = Path(self.tmp) / ".hermes"
        home.mkdir()
        self._env = mock.patch.dict(os.environ, {"HERMES_HOME": str(home), "HERMES_KANBAN_EVIDENCE_GATE": "1"})
        self._env.start()
        self._home = mock.patch.object(Path, "home", lambda: Path(self.tmp))
        self._home.start()
        kb.init_db()
        self.ws = Path(self.tmp) / "ws"
        self.ws.mkdir()

    def tearDown(self):
        self._home.stop()
        self._env.stop()

    def _running_task(self, conn, started_ago):
        tid = kb.create_task(conn, title="gate", assignee="development",
                             workspace_kind="dir", workspace_path=str(self.ws))
        kb.claim_task(conn, tid)
        kbd._set_worker_pid(conn, tid, os.getpid())
        started = int(time.time()) - started_ago
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET started_at = ?, worker_started_at = ? WHERE id = ?", (started, started, tid))
            conn.execute("UPDATE task_runs SET started_at = ? WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)", (started, tid))
        return tid, kb._current_run_id(conn, tid)

    def test_fast_summary_only_lands_in_review(self):
        with kbc.connect() as conn:
            tid, run_id = self._running_task(conn, started_ago=60)
            with self.assertRaises(kb.CompletionGatedError) as cm:
                kb.complete_task(conn, tid, result="Implemented everything, all tests pass.", expected_run_id=run_id)
            self.assertEqual(cm.exception.verdict, "review")
            self.assertEqual(kb.get_task(conn, tid).status, "review")

    def test_artifact_completion_is_done(self):
        with kbc.connect() as conn:
            tid, run_id = self._running_task(conn, started_ago=60)
            (self.ws / "out.txt").write_text("real artifact\n")
            self.assertTrue(kb.complete_task(conn, tid, result="wrote out.txt", expected_run_id=run_id))
            self.assertEqual(kb.get_task(conn, tid).status, "done")

    def test_slow_summary_only_is_refused(self):
        with kbc.connect() as conn:
            tid, run_id = self._running_task(conn, started_ago=900)
            with self.assertRaises(kb.CompletionGatedError) as cm:
                kb.complete_task(conn, tid, result="Done, see summary.", expected_run_id=run_id)
            self.assertEqual(cm.exception.verdict, "failed")
            self.assertEqual(kb.get_task(conn, tid).status, "running")

    def test_manifest_failure_is_refused_even_with_artifacts(self):
        with kbc.connect() as conn:
            tid, run_id = self._running_task(conn, started_ago=60)
            (self.ws / "out.txt").write_text("x\n")
            (self.ws / "GATE.json").write_text('{"files":[{"path":"missing.csv","min_bytes":1}]}')
            with self.assertRaises(kb.CompletionGatedError) as cm:
                kb.complete_task(conn, tid, result="done", expected_run_id=run_id)
            self.assertEqual(cm.exception.verdict, "failed")

    # SAIL-01-08: files the kanban runtime writes itself (completion memo,
    # worker log, board DB) are not worker artifacts.
    def _auto_memo(self, tid, board="dev"):
        memo = self.ws / "boards" / board / "attachments" / tid / f"{tid}_memo.md"
        memo.parent.mkdir(parents=True, exist_ok=True)
        memo.write_text(f"# {tid} completion memo\n")
        return memo

    def _git_ws(self, started_ago):
        subprocess.run(["git", "init", "-q", str(self.ws)], check=True)
        readme = self.ws / "README.md"
        readme.write_text("base\n")
        subprocess.run(["git", "-C", str(self.ws), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.ws), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "-m", "base"], check=True)
        old = time.time() - started_ago - 600
        os.utime(readme, (old, old))
        return readme

    def test_auto_memo_only_is_refused(self):
        with kbc.connect() as conn:
            tid, run_id = self._running_task(conn, started_ago=900)
            self._auto_memo(tid)
            with self.assertRaises(kb.CompletionGatedError) as cm:
                kb.complete_task(conn, tid, result="Done, see memo.", expected_run_id=run_id)
            self.assertEqual(cm.exception.verdict, "failed")
            self.assertIn("summary-only", cm.exception.detail)
            self.assertEqual(kb.get_task(conn, tid).status, "running")

    def test_auto_memo_only_in_git_workspace_is_refused(self):
        with kbc.connect() as conn:
            tid, run_id = self._running_task(conn, started_ago=900)
            self._git_ws(started_ago=900)
            self._auto_memo(tid)
            with self.assertRaises(kb.CompletionGatedError) as cm:
                kb.complete_task(conn, tid, result="Done, see memo.", expected_run_id=run_id)
            self.assertEqual(cm.exception.verdict, "failed")
            self.assertEqual(kb.get_task(conn, tid).status, "running")

    def test_runtime_log_and_db_only_is_refused(self):
        with kbc.connect() as conn:
            tid, run_id = self._running_task(conn, started_ago=900)
            board = self.ws / "boards" / "dev"
            (board / "logs").mkdir(parents=True)
            (board / "logs" / f"{tid}.log").write_text("worker output\n")
            for name in ("kanban.db", "kanban.db-wal", "kanban.db.dispatch.lock"):
                (board / name).write_text("x")
            self._auto_memo(tid)
            with self.assertRaises(kb.CompletionGatedError) as cm:
                kb.complete_task(conn, tid, result="Done.", expected_run_id=run_id)
            self.assertEqual(cm.exception.verdict, "failed")

    def test_auto_memo_plus_real_file_is_done(self):
        with kbc.connect() as conn:
            tid, run_id = self._running_task(conn, started_ago=900)
            self._auto_memo(tid)
            (self.ws / "out.txt").write_text("real artifact\n")
            self.assertTrue(kb.complete_task(conn, tid, result="wrote out.txt", expected_run_id=run_id))
            self.assertEqual(kb.get_task(conn, tid).status, "done")

    def test_auto_memo_plus_real_git_change_is_done(self):
        with kbc.connect() as conn:
            tid, run_id = self._running_task(conn, started_ago=900)
            readme = self._git_ws(started_ago=900)
            self._auto_memo(tid)
            readme.write_text("base\nchanged\n")
            self.assertTrue(kb.complete_task(conn, tid, result="edited README", expected_run_id=run_id))
            self.assertEqual(kb.get_task(conn, tid).status, "done")

    def test_real_file_only_is_done_after_fast_window(self):
        with kbc.connect() as conn:
            tid, run_id = self._running_task(conn, started_ago=900)
            (self.ws / "out.txt").write_text("real artifact\n")
            self.assertTrue(kb.complete_task(conn, tid, result="wrote out.txt", expected_run_id=run_id))
            self.assertEqual(kb.get_task(conn, tid).status, "done")

    def test_other_tasks_memo_still_counts(self):
        """Only the completing task's own memo is runtime-generated."""
        with kbc.connect() as conn:
            tid, run_id = self._running_task(conn, started_ago=900)
            self._auto_memo("t_deadbeef")
            self.assertTrue(kb.complete_task(conn, tid, result="done", expected_run_id=run_id))

    def test_reviewer_run_is_exempt(self):
        """A review-sourced run (reviewer approving) must not be re-gated into review."""
        with kbc.connect() as conn:
            tid, run_id = self._running_task(conn, started_ago=60)
            with self.assertRaises(kb.CompletionGatedError):
                kb.complete_task(conn, tid, result="summary only", expected_run_id=run_id)
            self.assertEqual(kb.get_task(conn, tid).status, "review")
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET worker_pid = NULL, claim_lock = NULL WHERE id = ?", (tid,))
            kb.claim_review_task(conn, tid)
            review_run = kb._current_run_id(conn, tid)
            kbd._set_worker_pid(conn, tid, os.getpid())
            self.assertTrue(kb.complete_task(conn, tid, result="approved", expected_run_id=review_run))
            self.assertEqual(kb.get_task(conn, tid).status, "done")

    def test_gate_defaults_on_in_worker_context(self):
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_worker"}, clear=False):
            os.environ.pop("HERMES_KANBAN_EVIDENCE_GATE", None)
            self.assertTrue(kb._gate_enabled())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_KANBAN_EVIDENCE_GATE", None)
            os.environ.pop("HERMES_KANBAN_TASK", None)
            self.assertFalse(kb._gate_enabled())

    def test_gate_disabled_by_env(self):
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_EVIDENCE_GATE": "0"}):
            with kbc.connect() as conn:
                tid, run_id = self._running_task(conn, started_ago=60)
                self.assertTrue(kb.complete_task(conn, tid, result="summary only", expected_run_id=run_id))
                self.assertEqual(kb.get_task(conn, tid).status, "done")

    def test_broken_repo_no_manifest_refused(self):
        """A git error other than 'not a git repository' must fail the gate."""
        with kbc.connect() as conn:
            tid, run_id = self._running_task(conn, started_ago=60)
            with mock.patch('subprocess.run') as mocked_run:
                mocked_run.return_value = mock.Mock(returncode=1, stdout="", stderr="fatal: some other git error")
                with self.assertRaises(kb.CompletionGatedError) as cm:
                    kb.complete_task(conn, tid, result="done", expected_run_id=run_id)
                self.assertNotEqual(cm.exception.verdict, "pass")
                self.assertIn("git error", cm.exception.detail.lower())

    def test_broken_repo_with_manifest_accepted(self):
        """Manifest check happens before physical evidence; corrupted git doesn't block a passing manifest."""
        with kbc.connect() as conn:
            tid, run_id = self._running_task(conn, started_ago=60)
            git_dir = self.ws / ".git"
            git_dir.mkdir()
            (git_dir / "config").write_text("corrupted content\n")
            (self.ws / "GATE.json").write_text('{"files":[], "checks":[{"name":"all-done","ok":true}]}')
            self.assertTrue(kb.complete_task(conn, tid, result="done", expected_run_id=run_id))

    def test_mtime_scan_ignores_noise(self):
        """*.log, poll/, and watch/ must be ignored by the mtime scan."""
        with kbc.connect() as conn:
            tid, run_id = self._running_task(conn, started_ago=900)
            (self.ws / "test.log").write_text("noise\n")
            poll_dir = self.ws / "poll"
            poll_dir.mkdir()
            (poll_dir / "file.txt").write_text("noise\n")
            watch_dir = self.ws / "watch"
            watch_dir.mkdir()
            (watch_dir / "file.txt").write_text("noise\n")
            
            with self.assertRaises(kb.CompletionGatedError) as cm:
                kb.complete_task(conn, tid, result="done", expected_run_id=run_id)
            self.assertEqual(cm.exception.verdict, "failed")
            self.assertIn("summary-only", cm.exception.detail)

    def test_long_summary_still_refused(self):
        """A long summary without any artifacts must still be refused."""
        with kbc.connect() as conn:
            tid, run_id = self._running_task(conn, started_ago=900)
            long_summary = "Done " * 200 # 1000 chars
            with self.assertRaises(kb.CompletionGatedError) as cm:
                kb.complete_task(conn, tid, result=long_summary, expected_run_id=run_id)
            self.assertEqual(cm.exception.verdict, "failed")
            self.assertIn("summary-only", cm.exception.detail)

if __name__ == "__main__":
    unittest.main()
