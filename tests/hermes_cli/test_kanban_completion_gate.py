"""CARD-GATE-01c-b: complete_task() runs the evidence gate (unittest; pytest absent on brute)."""
import os
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


if __name__ == "__main__":
    unittest.main()
