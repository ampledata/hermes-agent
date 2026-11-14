"""NEEDS-INFO honest stop: typed, sticky, not a failure, listable."""
import os, tempfile, unittest
from pathlib import Path
from unittest import mock


class NeedsInfo(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        home = Path(self.d) / ".hermes"
        home.mkdir()
        self.p = mock.patch.dict(os.environ, {"HERMES_HOME": str(home)})
        self.p.start()
        for k in [k for k in os.environ if k.startswith("HERMES_KANBAN") or k == "HERMES_DELEGATED_CHILD_CONTEXT"]:
            self.addCleanup(os.environ.__setitem__, k, os.environ[k])
            del os.environ[k]
        self.h = mock.patch.object(Path, "home", lambda: Path(self.d))
        self.h.start()
        from hermes_cli import kanban_db as kb
        self.kb = kb
        kb.init_db()

    def tearDown(self):
        self.h.stop(); self.p.stop()

    def _blocked(self, conn, title, reason, kind=None):
        kb = self.kb
        tid = kb.create_task(conn, title=title, assignee="q")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        conn.commit()
        self.assertTrue(kb.block_task(conn, tid, reason=reason, kind=kind))
        return tid

    def test_prefix_types_and_lists(self):
        from hermes_cli import kanban_db_connect as kbc
        with kbc.connect_closing() as conn:
            a = self._blocked(conn, "a", "NEEDS-INFO: which VLAN?", kind="transient")
            b = self._blocked(conn, "b", "waiting on creds", kind="capability")
            t = self.kb.get_task(conn, a)
            self.assertEqual((t.status, t.block_kind), ("blocked", "needs_input"))
            self.assertEqual(self.kb.get_task(conn, b).block_kind, "capability")
            ids = [x.id for x in self.kb.list_needs_info(conn)]
            self.assertEqual(ids, [a])
            n = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='gave_up'", (a,)).fetchone()[0]
            self.assertEqual(n, 0)
            self.assertEqual(self.kb.get_task(conn, a).status, "blocked")

    def test_unblocked_leaves_list(self):
        from hermes_cli import kanban_db_connect as kbc
        with kbc.connect_closing() as conn:
            a = self._blocked(conn, "a", "NEEDS-INFO: x?")
            self.kb.unblock_task(conn, a)
            self.assertEqual(self.kb.list_needs_info(conn), [])


if __name__ == "__main__":
    unittest.main()
