import argparse
import json
import sys
import os
import re
import subprocess
import time
import csv
from typing import Any, Dict, List, Optional, Tuple

class GateResult:
    def __init__(self, passed: bool, checks: List[Dict[str, Any]]):
        self.passed = passed
        self.checks = checks

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": self.checks
        }

def evaluate(manifest: Dict[str, Any], workspace: str, elapsed_time: float = 0.0, artifact_verified: bool = False) -> GateResult:
    checks = []
    passed = True

    # 1. Files Check
    if "files" in manifest:
        for file_spec in manifest["files"]:
            path = file_spec.get("path")
            if not path:
                continue
            abs_path = os.path.join(workspace, path)
            check_name = f"File: {path}"
            ok = True
            detail = "OK"

            if not os.path.exists(abs_path):
                ok = False
                detail = "File not found"
            else:
                if "min_bytes" in file_spec:
                    size = os.path.getsize(abs_path)
                    if size < file_spec["min_bytes"]:
                        ok = False
                        detail = f"Bytes {size} < {file_spec['min_bytes']}"
                if ok and "min_words" in file_spec:
                    with open(abs_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                        words = len(content.split())
                        if words < file_spec["min_words"]:
                            ok = False
                            detail = f"Words {words} < {file_spec['min_words']}"
                if ok and "must_match" in file_spec:
                    with open(abs_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                        for pattern in file_spec["must_match"]:
                            if not re.search(pattern, content):
                                ok = False
                                detail = f"Pattern '{pattern}' not found"
                                break
                if ok and "must_not_match" in file_spec:
                    with open(abs_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                        for pattern in file_spec["must_not_match"]:
                            if re.search(pattern, content):
                                ok = False
                                detail = f"Forbidden pattern '{pattern}' found"
                                break
            
            checks.append({"name": check_name, "ok": ok, "detail": detail})
            if not ok:
                passed = False

    # 2. Forbidden Text Check
    if "forbidden_text" in manifest:
        patterns = manifest["forbidden_text"]
        if isinstance(patterns, str):
            patterns = [patterns]
        else:
            if not patterns:
                patterns = ["TODO", "TBD", "PLACEHOLDER", "lorem ipsum", "\\[insert", "XXX"]

        for file_spec in manifest.get("files", []):
            path = file_spec.get("path")
            if not path: continue
            abs_path = os.path.join(workspace, path)
            if not os.path.exists(abs_path): continue
            
            with open(abs_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            
            for pattern in patterns:
                if re.search(pattern, content):
                    checks.append({"name": f"Forbidden text: {path}", "ok": False, "detail": f"Found pattern '{pattern}'"})
                    passed = False
                    break

    # 3. Commands Check
    if "commands" in manifest:
        for i, cmd_spec in enumerate(manifest["commands"]):
            run_cmd = cmd_spec.get("run")
            expected_exit = cmd_spec.get("exit", 0)
            timeout_s = cmd_spec.get("timeout_s", 120)
            cwd = cmd_spec.get("cwd", workspace)
            check_name = f"Command {i}: {run_cmd}"
            ok = True
            detail = "OK"
            
            try:
                proc = subprocess.run(run_cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout_s)
                if proc.returncode != expected_exit:
                    ok = False
                    detail = f"Exit code {proc.returncode} != {expected_exit}"
            except subprocess.TimeoutExpired:
                ok = False
                detail = f"Timeout after {timeout_s}s"
            except Exception as e:
                ok = False
                detail = str(e)
            
            checks.append({"name": check_name, "ok": ok, "detail": detail})
            if not ok:
                passed = False

    # 4. SHA256SUMS Check
    if "sha256sums" in manifest:
        sha_path = os.path.join(workspace, manifest["sha256sums"])
        check_name = "SHA256SUMS"
        ok = True
        detail = "OK"
        if not os.path.exists(sha_path):
            ok = False
            detail = "SHA256SUMS file not found"
        else:
            try:
                result = subprocess.run(["sha256sum", "-c", sha_path], cwd=workspace, capture_output=True, text=True)
                if result.returncode != 0:
                    ok = False
                    detail = result.stderr.strip() or result.stdout.strip() or "Checksum mismatch"
            except Exception as e:
                ok = False
                detail = str(e)
        checks.append({"name": check_name, "ok": ok, "detail": detail})
        if not ok:
            passed = False

    # 5. CSV Check
    if "csv" in manifest:
        for i, csv_spec in enumerate(manifest["csv"]):
            path = csv_spec.get("path")
            if not path: continue
            abs_path = os.path.join(workspace, path)
            check_name = f"CSV: {path}"
            ok = True
            detail = "OK"
            if not os.path.exists(abs_path):
                ok = False
                detail = "File not found"
            else:
                try:
                    with open(abs_path, 'r', encoding='utf-8') as f:
                        reader = csv.reader(f)
                        header = next(reader, [])
                        if "min_rows" in csv_spec:
                            f.seek(0)
                            row_count = sum(1 for _ in csv.reader(f))
                            if row_count < csv_spec["min_rows"]:
                                ok = False
                                detail = f"Rows {row_count} < {csv_spec['min_rows']}"
                        if ok and "required_columns" in csv_spec:
                            for col in csv_spec["required_columns"]:
                                if col not in header:
                                    ok = False
                                    detail = f"Missing column: {col}"
                                    break
                except Exception as e:
                    ok = False
                    detail = str(e)
            checks.append({"name": check_name, "ok": ok, "detail": detail})
            if not ok:
                passed = False

    # 6. Runtime & Artifact Verification Check
    if "min_runtime_s" in manifest:
        if elapsed_time < manifest["min_runtime_s"]:
            passed = False
            checks.append({"name": "Runtime Check", "ok": False, "detail": f"Elapsed time {elapsed_time}s < required {manifest['min_runtime_s']}s"})

    if manifest.get("artifact_verification_required", False):
        if not artifact_verified:
            passed = False
            checks.append({"name": "Artifact Verification", "ok": False, "detail": "Artifact verification was required but not verified."})

    return GateResult(passed, checks)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id", help="The task ID to evaluate")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--workspace", help="Path to the task workspace")
    parser.add_argument("--manifest", help="Path to the manifest file")
    parser.add_argument("--force", action="store_true", help="Bypass the gate")
    parser.add_argument("--elapsed", type=float, default=0.0, help="Elapsed time in seconds")
    parser.add_argument("--verified", action="store_true", help="Was artifact verified?")
    args = parser.parse_args()

    if not args.workspace or not args.manifest:
        print("Error: --workspace and --manifest are required.")
        sys.exit(1)

    try:
        with open(args.manifest, 'r') as f:
            manifest = json.load(f)
    except Exception as e:
        try:
            import yaml
            with open(args.manifest, 'r') as f:
                manifest = yaml.safe_load(f)
        except:
            print(f"Error loading manifest: {e}", file=sys.stderr)
            sys.exit(1)

    result = evaluate(manifest, args.workspace, elapsed_time=args.elapsed, artifact_verified=args.verified)

    # 1. Check for Unverified (Exit 2)
    if args.elapsed < 300.0 and not args.verified:
        print("Status: UNVERIFIED (Too early or no artifact verification)")
        sys.exit(2)

    # 2. Check for Summary-Only (Exit 1)
    if len(result.checks) == 0 or not result.passed:
        if args.json:
            print(json.dumps(result.to_dict()))
        else:
            print(f"Status: {'PASSED' if result.passed else 'FAILED'}")
            for c in result.checks:
                status = "OK" if c['ok'] else "FAILED"
                print(f"  [{status}] {c['name']}: {c['detail']}")
        sys.exit(1)

    # 3. Success (Exit 0)
    if args.json:
        print(json.dumps(result.to_dict()))
    else:
        print(f"Status: PASSED")
        for c in result.checks:
            status = "OK" if c['ok'] else "FAILED"
            print(f"  [{status}] {c['name']}: {c['detail']}")

if __name__ == '__main__':
    main()
