"""HARNESS-WT-01: worktree workspace inside another checkout must be refused or isolated."""
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw
from tests.hermes_cli.test_kanban_db import _init_git_repo, kanban_home


def _task(conn, path, branch=None):
    t = kb.create_task(conn, title="x", workspace_kind="worktree",
                       workspace_path=str(path), branch_name=branch)
    return kb.get_task(conn, t)


def test_subdir_of_shared_checkout_is_refused(kanban_home, tmp_path):
    """Negative control: <repo>/opsdesk (existing) must not become a worker cwd."""
    repo = tmp_path / "devops"
    _init_git_repo(repo)
    (repo / "opsdesk").mkdir()
    (repo / "opsdesk" / "f.txt").write_text("a\n")
    with kbc.connect() as conn:
        task = _task(conn, repo / "opsdesk")
        with pytest.raises(ValueError, match="subdirectory of the existing git checkout"):
            kbw.resolve_workspace(task)
    listed = subprocess.run(["git", "-C", str(repo), "worktree", "list"],
                            capture_output=True, text=True, check=True).stdout
    assert len(listed.strip().splitlines()) == 1


def test_subdir_of_linked_worktree_is_refused(kanban_home, tmp_path):
    repo = tmp_path / "devops"
    _init_git_repo(repo)
    with kbc.connect() as conn:
        other = repo / ".worktrees" / "other"
        kbw.resolve_workspace(_task(conn, other, "wt/other"))
        (other / "sub").mkdir()
        with pytest.raises(ValueError, match="subdirectory"):
            kbw.resolve_workspace(_task(conn, other / "sub"))


def test_repo_root_still_anchors_real_worktree(kanban_home, tmp_path):
    repo = tmp_path / "devops"
    _init_git_repo(repo)
    with kbc.connect() as conn:
        task = _task(conn, repo)
        ws = kbw.resolve_workspace(task)
    assert ws == repo / ".worktrees" / task.id
    assert ws.is_dir() and ws != repo
