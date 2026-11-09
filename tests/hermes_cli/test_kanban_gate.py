import unittest
import sys
import os
import tempfile
import json
import shutil
import subprocess
from hermes_cli.kanban_gate import evaluate, GateResult

class TestKanbanGate(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.workspace = self.test_dir
        
    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def create_manifest(self, data):
        path = os.path.join(self.test_dir, 'manifest.json')
        with open(path, 'w') as f:
            json.dump(data, f)
        return path

    def test_files_pass(self):
        file_path = os.path.join(self.test_dir, 'test.txt')
        with open(file_path, 'w') as f:
            f.write("hello world")
        
        manifest = {
            "files": [{"path": "test.txt", "min_words": 1, "min_bytes": 1}]
        }
        self.create_manifest(manifest)
        
        result = evaluate(manifest, self.workspace)
        self.assertTrue(result.passed)

    def test_runtime_failure(self):
        manifest = {
            "min_runtime_s": 10.0
        }
        self.create_manifest(manifest)
        
        result = evaluate(manifest, self.workspace, elapsed_time=5.0)
        self.assertFalse(result.passed)
        self.assertEqual(result.checks[0]['name'], "Runtime Check")

    def test_artifact_verification_failure(self):
        manifest = {
            "artifact_verification_required": True
        }
        self.create_manifest(manifest)
        
        result = evaluate(manifest, self.workspace, elapsed_time=15.0, artifact_verified=False)
        self.assertFalse(result.passed)
        self.assertEqual(result.checks[0]['name'], "Artifact Verification")

    def test_artifact_verification_success(self):
        manifest = {
            "artifact_verification_required": True
        }
        self.create_manifest(manifest)
        
        result = evaluate(manifest, self.workspace, elapsed_time=15.0, artifact_verified=True)
        self.assertTrue(result.passed)

if __name__ == '__main__':
    unittest.main()


class TestKanbanGateCLI(unittest.TestCase):
    """Exit-code contract of the gate as a script: 0 artifacts verified, 1 summary-only/failed, 2 under 5 min unverified."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        with open(os.path.join(self.d, "out.txt"), "w") as f:
            f.write("real artifact\n")
        self.script = os.path.join(os.path.dirname(__file__), "..", "..", "hermes_cli", "kanban_gate.py")

    def tearDown(self):
        shutil.rmtree(self.d)

    def run_gate(self, manifest, elapsed, verified=False):
        mp = os.path.join(self.d, "m.json")
        with open(mp, "w") as f:
            json.dump(manifest, f)
        cmd = [sys.executable, self.script, "t_test", "--workspace", self.d, "--manifest", mp, "--elapsed", str(elapsed)]
        if verified:
            cmd.append("--verified")
        return subprocess.run(cmd, capture_output=True, text=True, check=False).returncode

    def test_exit0_artifact_after_5min(self):
        self.assertEqual(self.run_gate({"files": [{"path": "out.txt", "min_bytes": 1}]}, 600), 0)

    def test_exit0_passing_command(self):
        self.assertEqual(self.run_gate({"commands": [{"run": "true"}]}, 600), 0)

    def test_exit1_summary_only(self):
        self.assertEqual(self.run_gate({}, 600), 1)

    def test_exit1_missing_artifact(self):
        self.assertEqual(self.run_gate({"files": [{"path": "missing.txt", "min_bytes": 1}]}, 600), 1)

    def test_exit1_failing_command(self):
        self.assertEqual(self.run_gate({"commands": [{"run": "false"}]}, 600), 1)

    def test_exit2_under_5min_unverified(self):
        self.assertEqual(self.run_gate({"files": [{"path": "out.txt", "min_bytes": 1}]}, 120), 2)

    def test_exit0_under_5min_verified(self):
        self.assertEqual(self.run_gate({"files": [{"path": "out.txt", "min_bytes": 1}]}, 120, verified=True), 0)


class TestDevopsGate(unittest.TestCase):
    def _git(self, *a):
        subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True)

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.repo = os.path.join(self.d, "r")
        os.mkdir(self.repo)
        self._git("init", "-q", "-b", "master")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        open(os.path.join(self.repo, "a.txt"), "w").write("a\n")
        self._git("add", "a.txt")
        self._git("commit", "-qm", "base")
        self._git("checkout", "-qb", "card")
        self.spec = {"worktree": self.repo, "branch": "card", "base": "master",
                     "log_dir": os.path.join(self.d, "logs"),
                     "commands": [{"run": "test -f {tree}/b.txt", "exit": 0}]}

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _gate(self):
        return evaluate({"devops_gate": self.spec}, self.repo)

    def test_pass_when_committed(self):
        open(os.path.join(self.repo, "b.txt"), "w").write("b\n")
        self._git("add", "b.txt"); self._git("commit", "-qm", "b")
        self.assertTrue(self._gate().passed, self._gate().checks)

    def test_uncommitted_refused_names_porcelain(self):
        open(os.path.join(self.repo, "b.txt"), "w").write("b\n")
        self._git("add", "b.txt"); self._git("commit", "-qm", "b")
        open(os.path.join(self.repo, "a.txt"), "w").write("changed\n")
        r = self._gate()
        self.assertFalse(r.passed)
        self.assertIn("porcelain", json.dumps(r.checks))

    def test_check_passes_only_on_uncommitted_file_is_refused(self):
        open(os.path.join(self.repo, "b.txt"), "w").write("b\n")  # untracked
        r = self._gate()
        self.assertFalse(r.passed)
        self.assertTrue(any("HEAD check" in c["name"] and not c["ok"] for c in r.checks))

    @unittest.skipUnless(shutil.which("gitleaks"), "gitleaks not installed")
    def test_gitleaks_planted_secret(self):
        secret = "ghp_" + "aB3dE5gH7jK9mN1pQ3rS5tU7vW9xY1zA3bC5"
        p = os.path.join(self.repo, "b.txt")
        open(p, "w").write(f"token = {secret}\n")
        self._git("add", "b.txt"); self._git("commit", "-qm", "b")
        r = self._gate()
        self.assertFalse(r.passed)
        self.assertIn("gitleaks found", json.dumps(r.checks))
        self.assertNotIn(secret, json.dumps(r.checks))
        # remove the secret in a rewritten branch: gate passes
        self._git("reset", "-q", "--hard", "master")
        open(p, "w").write("b\n")
        self._git("add", "b.txt"); self._git("commit", "-qm", "b clean")
        self.assertTrue(self._gate().passed, self._gate().checks)
