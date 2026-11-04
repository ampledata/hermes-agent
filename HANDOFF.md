# HANDOFF: SAIL-01-08, evidence gate ignores kanban runtime-generated files

## BLUF

The kanban evidence gate no longer counts files the kanban runtime writes itself
(the completion auto-memo, the task's worker log, the board DB and its
sidecars/locks) as worker artifacts. A completion whose only files are of that
kind is scored summary-only and gets the existing refusal / review routing and
retry guidance. Completions with at least one real artifact behave exactly as
before. Branch `fix/gate-automemo`, base `966c94bab7`.

## What changed

`hermes_cli/kanban_db.py`, inside the existing no-manifest evaluator only:

- New `_gate_runtime_generated(path, task_id)`: true for
  - `.../attachments/<task_id>/<task_id>_memo.md` (auto-memo; covers
    `boards/*/attachments/...` and the default board's `kanban/attachments/...`)
  - `.../logs/<task_id>.log` and rotated `.log.N` (dispatcher worker log,
    `_open_worker_log` in `kanban_db_dispatch.py`)
  - `kanban.db`, `kanban.db-wal|-shm|-journal`, `kanban.db.*.lock` (board DB,
    written on every completion and heartbeat)
  Memos and logs of other task ids still count.
- New `_gate_git_paths()`: parses `git status --porcelain -z` (exact paths,
  rename sources skipped).
- `_gate_physical_evidence(workspace, started_at, task_id=None)`: drops
  runtime-generated paths from both the git-status count and the mtime walk;
  the failure detail notes `(ignored N kanban runtime-generated file(s))`.
- `_evidence_gate` passes `task_id` through. Nothing else changes: manifest
  evaluation, the 5-minute review/failed split, `force=`, the
  review-sourced exemption (966c94bab7) and the CLI/tool retry guidance
  (a4521b32a8) are untouched.

`tests/hermes_cli/test_kanban_completion_gate.py`: 7 new tests

| Test | Expect |
|---|---|
| `test_auto_memo_only_is_refused` | auto-memo only, 15 min run -> `failed`, still `running` |
| `test_auto_memo_only_in_git_workspace_is_refused` | same, git workspace with untracked memo |
| `test_runtime_log_and_db_only_is_refused` | memo + worker log + kanban.db/-wal/lock -> `failed` |
| `test_auto_memo_plus_real_file_is_done` | memo + real file -> `done` |
| `test_auto_memo_plus_real_git_change_is_done` | memo + tracked edit -> `done` |
| `test_real_file_only_is_done_after_fast_window` | real file only -> `done` (unchanged) |
| `test_other_tasks_memo_still_counts` | another task's memo is a real file -> `done` |

## Tests

```
cd /home/hermes/bletchley-ops/sail-01-retier/wt-gate && env -u PYTHONPATH uv run --with pytest python -m pytest tests/hermes_cli/test_kanban_completion_gate.py -q -p no:cacheprovider
14 passed in 10.32s
```

Before the fix the 3 refusal tests failed (`3 failed, 11 passed`), receipt
`pytest-gate-red.txt`. Broader `-k kanban` run: see "Broader run" below.
`uvx ruff check` clean on both files.

Receipts: `/home/hermes/bletchley-ops/sail-01-retier/receipts/SAIL-01-08/`.

## Broader run

`pytest tests -k kanban -q -p no:cacheprovider --continue-on-collection-errors -rfE`,
run from `git archive` extractions of the base and the branch so both trees
see the same environment:

| Tree | Result |
|---|---|
| base `966c94bab7` | `98 failed, 916 passed, 42 skipped, 60514 deselected, 5 xfailed, 3102 warnings, 70 errors in 1439.28s` |
| branch `888e1fcd4b` | `98 failed, 923 passed, 42 skipped, 60514 deselected, 5 xfailed, 3102 warnings, 70 errors in 1130.67s` |

The 168 FAILED/ERROR node IDs are identical in both runs (`failset.diff` is
empty). All of them were already failing before this change: 63 collection
errors from missing optional deps (`acp`, `mcp`, gateway extras) plus
unrelated kanban notify/dispatch/e2e failures. The 7 extra passes are the
new tests.

A first attempt ran this inside the worktree and was killed by its 1700 s
timeout (exit 124, `pytest-kanban-worktree-timeout-killed.txt`). The cause:
tests that run `git status` inside this `blob:none` partial clone trigger lazy
`git fetch origin` calls to GitHub. That run is not evidence either way.

## Findings the brief did not anticipate

1. No memo writer exists in this tree or in the live tree
   (`grep -rn "_memo.md"` finds nothing in `hermes_cli/`, `tools/`, `gateway/`,
   `plugins/`). The exclusion is pattern-based, so it holds whichever
   component writes the memo.
2. The DB record for t_055717cb (read-only query of
   `~/.hermes/kanban/boards/dev/kanban.db`) shows no `task_attachments` rows,
   run metadata `changed_files: ["agent/tool_executor.py"]`, and no memo on disk
   under `boards/dev/attachments/`. Its workspace was
   `/home/hermes/bletchley-ops/sail-01-retier`, inside the
   `/home/hermes/bletchley-ops` repo, where `git status` currently fails
   (`fatal: bad object HEAD`). In that case the gate falls back to the mtime
   walk, and anything written anywhere under the workspace during the run
   passes it (`chainwatch.log`, `faultwatch.log`, `poll-*.log`, `receipts/`).
   That was probably what let t_055717cb through; this change does not close
   it. A follow-up card should scope evidence to the run (for example, git
   changes limited to the workspace pathspec, and refusing completions when
   git errors on a workspace inside a repo).
3. `git status` run from a subdirectory reports changes across the whole repo.
   Pre-existing uncommitted changes anywhere in the repo still count as
   evidence. This is unchanged and outside this card.

## Deploy

BT deploys the live tree separately; this branch only prepares the PR.

1. Merge order: this branch sits on the pre-rebase gate stack (`966c94bab7`),
   so the PR against `main` also shows the base commits (`df153cb376`,
   `07a368aaa8`, `b6ec523d5c`, `49c488b001`, `a4521b32a8`, `966c94bab7`), the
   same gate work as PR #1 (`feat/kanban-acceptance-gate`, rebased). The fix
   commit `888e1fcd4b` applies cleanly on PR #1's head `c22a0d1dd7`
   (`git merge-tree --merge-base 966c94bab7` reports no conflicts). The
   simplest path is to merge PR #1, then cherry-pick `888e1fcd4b` (plus this
   HANDOFF if wanted) instead of merging this branch whole.
2. In `/home/hermes/.hermes/hermes-agent`, pull or cherry-pick, then run the
   test command above from that tree.
3. Restart the kanban dispatcher/gateway processes so workers import the new
   `kanban_db.py`. Running workers keep the old code until they respawn.
4. No schema, config or env changes. Rollback: revert the commit.
