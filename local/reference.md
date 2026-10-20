# Reference: escalation, rejection, web research

Read this when a `dispatch_task.py` report is not "passed". Same options as the original call
(`--write`, `--check`, `--worktree`…); the original instruction is reused.

## Failed or needs work

Every model in `--models` failed, each after its in-session fixes (their work is set aside in
`git stash`, except the best attempt, the one with the most tests passing, left in the files so that
you can see it and finish it).
- Small mistake → fix it yourself, then commit.
- Otherwise, rerun locally **only** if a more precise instruction, `--think on` or another model
  really changes the odds; else **Sonnet** (if `models.md` allows it), then you.

## Result not good enough on your review (before committing)

```bash
python3 $S/dispatch_task.py <id> --reject "<precise, actionable remarks>" [--keep] [--models "…"]
```
Your remarks become requirements, passed to every attempt and to the local review.
`--keep`: the agent improves the existing work; without `--keep`: the work is set aside and the
agent starts over (possibly with another model). To give up:
`git stash push -u -m "task <id> rejected"` (or `worktree.py discard <id>` for a parallel task).

## Held-out tests (`--hidden-check`)

For important logic, or when the visible tests are few (a local model may special-case them):
1. `python3 $S/common.py hidden-dir <project>-<id>` creates and prints a folder outside the project,
   readable by the checks and unreadable by the agents.
2. Write extra tests there (other inputs and edge cases than the visible tests), importing the
   project normally (its root is on `PYTHONPATH`).
3. Add `--hidden-check ".venv/bin/python -m pytest -q --rootdir . <folder>"` (plus `-c pyproject.toml`
   if the project's pytest settings live there).
The agent only learns that "an additional check failed"; the report shows you the full output.
Never mention the hidden tests in the instruction.

## Review verdicts

`--review` only blocks on a clear NEEDS WORK. "inconclusive" (no clear verdict, review impossible,
or diff too large to be reviewed in full) and "(self-review)" (only one model available) are weak
signals: read the diff more carefully (never a "local review enough" case, see delegation.md).

## Needs information (the agent wrote `RESEARCH: …`)

Local agents never have Internet access. According to `models.md`:
- **Research allowed** → Claude subagent (Agent tool, `general-purpose`):
  `model: "haiku"` for a simple lookup (docs, error message), `"sonnet"` for an analysis (or the
  model set in `models.md`). Instruction: the question, the language, the library and its version;
  "answer ≤ 250 words, minimal example, source links". **Never private code or secrets.**
  Write its answer to a file outside the repository (e.g. in your temporary folder), then:
  `python3 $S/dispatch_task.py <id> --resume --info-file <file>` — the agent resumes its work.
- **Not allowed** → no research: answer yourself if you know, otherwise escalate.
In plan mode, if a task depends on a recent library, do the research first and pass it with
`--info-file` from the first call.

## Security: useful details

- Layers: Claude Code `--restricted` mode, limited tools, guard on every action (denylist,
  allowlist, local security expert), network-less sandbox for the shell and for `--check`.
- **Windows** (and Linux without bubblewrap): no sandbox, so checks are refused unless the user set
  `LOCAL_ALLOW_UNSANDBOXED=1` (the report then says "[no sandbox]"); no shell for the agents on Windows.
- "CHECK IMPOSSIBLE": not a failure of the code. Sandbox did not start → `/local doctor`; command
  not found → fix `--check`; writes in the read-only project → rerun with `--check-writable`.
- Variables that look secret (tokens, keys…) are removed for agents and checks: if the tests need one
  (a dummy key), the user can pass it with `LOCAL_KEEP_ENV="NAME"`.
- A rejection or a resume is refused if the repository changed in the meantime (commit, manual
  edit): start a new task instead.
