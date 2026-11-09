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
    def __init__(self, passed: bool, checks: list[dict[str, Any]]):
        self.passed = passed
        self.checks = checks

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": self.checks
        }

def _wr(path, text):
    with open(path, "w") as fh:
        fh.write(text)


def _run(argv, cwd, timeout=300):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)


def evaluate_devops_gate(spec: dict[str, Any], log_dir: Optional[str] = None) -> list[dict[str, Any]]:
    """DEVOPS completion gate (FINISH-LINE-01). Returns check dicts.

    spec keys: worktree (card worktree path), branch (card branch), base
    (default ``gh/master``), commands (list of {run, exit, timeout_s}; run
    against a clean checkout of committed HEAD, ``{tree}`` expands to it),
    gitleaks (binary, default ``gitleaks``). Checks: (a) gitleaks on
    base..branch, (b) ``git status --porcelain`` empty, (c) command re-run on
    committed HEAD. A gitleaks hit is reported by rule/file/line only, never
    the secret, and files nothing.
    """
    import shutil
    import tempfile
    checks: list[dict[str, Any]] = []
    wt = spec.get("worktree")
    branch = spec.get("branch", "HEAD")
    base = spec.get("base", "gh/master")
    log_dir = log_dir or tempfile.mkdtemp(prefix="devops-gate-")
    os.makedirs(log_dir, exist_ok=True)
    if not wt or not os.path.isdir(wt):
        return [{"name": "devops_gate: worktree", "ok": False, "detail": f"worktree missing: {wt}"}]

    # (b) committed tree
    try:
        p = _run(["git", "status", "--porcelain"], wt)
        log = os.path.join(log_dir, "porcelain.log")
        _wr(log, p.stdout + p.stderr)
        ok = p.returncode == 0 and not p.stdout.strip()
        detail = "OK" if ok else (
            f"git status --porcelain not empty (uncommitted work; log {log}): "
            + "; ".join(p.stdout.strip().splitlines()[:5]) if p.returncode == 0
            else f"git status --porcelain failed rc={p.returncode} (log {log})")
    except Exception as e:
        ok, detail = False, f"git status --porcelain error: {e}"
    checks.append({"name": "devops_gate: git status --porcelain", "ok": ok, "detail": detail})

    # (a) gitleaks on branch range
    rng = f"{base}..{branch}"
    report = os.path.join(log_dir, "gitleaks.json")
    glog = os.path.join(log_dir, "gitleaks.log")
    cmd = [spec.get("gitleaks", "gitleaks"), "detect", "--source", wt, "--log-opts", rng,
           "--no-banner", "--redact", "--report-format", "json", "--report-path", report, "--exit-code", "7"]
    try:
        p = _run(cmd, wt)
        _wr(glog, p.stdout + p.stderr)
        if p.returncode == 0:
            ok, detail = True, "OK"
        elif p.returncode == 7:
            try:
                with open(report) as fh:
                    finds = json.load(fh)
            except Exception:
                finds = []
            names = "; ".join(f"{f.get('RuleID')} {f.get('File')}:{f.get('StartLine')} commit {str(f.get('Commit'))[:8]}"
                              for f in finds[:5])
            ok, detail = False, f"gitleaks found {len(finds)} leak(s) in {rng}: {names} (report {report}, log {glog}); no automatic filing, BT files clean-up card"
        else:
            ok, detail = False, f"gitleaks error rc={p.returncode} on {rng} (log {glog})"
    except Exception as e:
        ok, detail = False, f"gitleaks error: {e}"
    checks.append({"name": f"devops_gate: gitleaks {rng}", "ok": ok, "detail": detail})

    # (c) re-run card checks on committed HEAD (clean detached checkout)
    cmds = spec.get("commands", [])
    if cmds:
        tree = tempfile.mkdtemp(prefix="devops-gate-head-")
        added = False
        try:
            p = _run(["git", "worktree", "add", "--detach", tree, branch], wt)
            if p.returncode != 0:
                raise RuntimeError(f"checkout of {branch} failed: {p.stderr.strip()[:200]}")
            added = True
            for i, c in enumerate(cmds):
                run_cmd = c.get("run", "").replace("{tree}", tree)
                want = c.get("exit", 0)
                clog = os.path.join(log_dir, f"check-{i}.log")
                try:
                    r = subprocess.run(run_cmd, shell=True, cwd=tree, capture_output=True, text=True,
                                       timeout=c.get("timeout_s", 120), check=False)
                    _wr(clog, r.stdout + r.stderr)
                    ok = r.returncode == want
                    detail = "OK" if ok else f"exit {r.returncode} != {want} on committed HEAD (log {clog}): {run_cmd[:200]}"
                except subprocess.TimeoutExpired:
                    ok, detail = False, f"timeout on committed HEAD: {run_cmd[:200]}"
                checks.append({"name": f"devops_gate: HEAD check {i}", "ok": ok, "detail": detail})
        except Exception as e:
            checks.append({"name": "devops_gate: HEAD checks", "ok": False, "detail": str(e)})
        finally:
            if added:
                _run(["git", "worktree", "remove", "--force", tree], wt)
            shutil.rmtree(tree, ignore_errors=True)
    return checks


def evaluate(manifest: dict[str, Any], workspace: str, elapsed_time: float = 0.0, artifact_verified: bool = False) -> GateResult:
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
                proc = subprocess.run(run_cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout_s, check=False)
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

    # 3b. DEVOPS completion gate (gitleaks, committed tree, HEAD re-run)
    if "devops_gate" in manifest:
        for c in evaluate_devops_gate(manifest["devops_gate"], manifest["devops_gate"].get("log_dir")):
            checks.append(c)
            if not c["ok"]:
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
                result = subprocess.run(["sha256sum", "-c", sha_path], cwd=workspace, capture_output=True, text=True, check=False)
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
        except Exception:
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
        print("Status: PASSED")
        for c in result.checks:
            status = "OK" if c['ok'] else "FAILED"
            print(f"  [{status}] {c['name']}: {c['detail']}")

if __name__ == '__main__':
    main()
