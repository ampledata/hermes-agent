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
        manifest_path = self.create_manifest(manifest)
        
        result = evaluate(manifest, self.workspace)
        self.assertTrue(result.passed)

    def test_runtime_failure(self):
        manifest = {
            "min_runtime_s": 10.0
        }
        manifest_path = self.create_manifest(manifest)
        
        result = evaluate(manifest, self.workspace, elapsed_time=5.0)
        self.assertFalse(result.passed)
        self.assertEqual(result.checks[0]['name'], "Runtime Check")

    def test_artifact_verification_failure(self):
        manifest = {
            "artifact_verification_required": True
        }
        manifest_path = self.create_manifest(manifest)
        
        result = evaluate(manifest, self.workspace, elapsed_time=15.0, artifact_verified=False)
        self.assertFalse(result.passed)
        self.assertEqual(result.checks[0]['name'], "Artifact Verification")

    def test_artifact_verification_success(self):
        manifest = {
            "artifact_verification_required": True
        }
        manifest_path = self.create_manifest(manifest)
        
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
        return subprocess.run(cmd, capture_output=True, text=True).returncode

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
