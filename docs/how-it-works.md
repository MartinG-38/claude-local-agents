# How it works

[← README](../README.md) · [How it works](how-it-works.md) · [Configuration](configuration.md) · [Token savings](savings.md) · [Reference](reference.md)

## How a task is verified

Before the agent starts, the check runs once on the unchanged project (not with `--check-writable`,
which could have side effects; `--no-baseline` to skip it). The report then tells failures that were
already there from new ones (possible regressions), and flags a check that already passed before any
change: it probably does not test what was asked.

A task is reported as "passed" only after these gates, in this order:

1. **Scope** (deterministic): any change outside `--write` — for example a test rewritten through a
   script — is set aside (`git stash`) *before* the check, so the check always runs against the
   original tests.
2. **Check** (deterministic): the `--check` command (tests, linters) runs in a sandbox with the
   project **read-only**, so the checked code can neither hide files from git, nor delete ignored
   files such as `.env`, nor rewrite `.git` (`--check-writable` if the tests must write in the
   project; `.git` and virtual environments stay read-only). A check passes only with positive
   evidence: for pytest, a test report with at least one test and no failure; for unittest,
   "Ran N tests" and "OK". A run that stops early or runs no test fails; a command that cannot run is
   reported as such, not as a failure of the code. The checks run when the agent says it is
   done, through Claude Code's native **Stop hook**: on failure, the agent keeps working in the same
   session with the error as feedback, so it remembers what it already tried (`--retries` rounds,
   2 by default; then the next model takes over). The feedback is the useful part of the output: the
   error lines and the frames that point into the project, library warnings left out (pytest checks
   also get `-p no:warnings --tb=short` unless the command says otherwise); the same error twice is
   pointed out with the line to look at. A session is not restarted with the same model when it makes
   no progress (same error, nothing changed since the last round), runs out of steps or time, or
   generates without end: the next model takes over at once, with the failure summary. If some files
   of the failed attempt cannot be set aside, the next model still starts (it is told they are there)
   as long as they lie within `--write`, and the report says why they stayed. When only one model is
   given, one fallback that fits the task (context and memory) is added automatically; after a failure
   by size (time, output or step limit), a model with a smaller task size is skipped: the report says to
   split the task instead. A session cut off by its time limit is summarized (files written, last
   actions), and agents are told to write early and in parts so that a cut session keeps its work.
3. **Held-out tests** (optional, deterministic): `--hidden-check` runs tests stored outside the
   project, in a folder that neither the agent nor the visible check can read, and which never
   appears in its instructions or its environment; on failure it only learns that "an additional
   check failed". This catches code tailored to the visible tests. Checks write their temporary files
   to `$LOCAL_TMP`; a check that cannot run (a write refused outside the project, a missing command) is
   reported as "CHECK IMPOSSIBLE" and never counted against the model: fix the check, then
   `dispatch_task.py <id> --recheck` reruns only the checks on the work left in place.
4. **Local review** (optional, heuristic): another local model judges the diff against the
   instruction. Only a clear "NEEDS WORK" blocks; an approval counts only if the reviewer gave a
   clear verdict on the whole diff; self-reviews are flagged.
5. **Claude reviews** before committing, and can reject the result with precise remarks. How much
   it reads depends on the task (preference "Review of results", decided before dispatching):
   - *local review enough*: routine code covered by Claude's own (or held-out) tests, check passed and
     a clear APPROVED from another model → Claude reads only `git diff --stat`, the new public
     signatures and the reviewer's remarks;
   - *key parts*: important logic, tests that leave cases open, or a weak verdict (inconclusive,
     self-review) → the local review plus the parts that matter;
   - *everything* (Claude, or Sonnet / Opus): security, concurrency, subtle algorithms, data loss,
     public APIs, or a "Do not delegate locally" case.
   With `always Claude`, Claude always reads the full diff. Code that was not delegated (a diff made
   by hand) can be reviewed locally too: `git diff | ask_local.py "<review prompt>" --stdin`.

Gates 1 to 3 are only as good as the tests themselves: that is why the tests that define new
behavior are written by Claude or Sonnet, never by the agent that implements it. If Claude Code ever
did not apply the hooks (guard and Stop hook), the result is refused and the work set aside. Gate 4 is a useful filter,
not a proof: a local model reviewing another local model catches obvious problems (hard-coded
values, missed requirements), not every subtle bug.

## Following a task live

A code task runs as a background command, and its output (in Claude Code, `/tasks` lists the running
shells and shows their output) tells what happens as it happens:

```
[00:00] ▶ Task 3: gpt-oss:20b → qwen3.8:27b · task size medium (~9,800 of 22,900 tokens before any work)
[00:00] ▶ attempt 1 · gpt-oss:20b
[00:04]   · reads src/export.py
[00:21]   · edits src/export.py
[00:52]   ✗ CHECK FAILED — .venv/bin/python -m pytest -q tests/test_export.py → the agent fixes it in the same session (round 1/2)
[01:30]   agent finished: done · 1.5 min · 9 steps
[01:31] ✗ gpt-oss:20b: failed → qwen3.8:27b starts over with its summary
[01:31] ▶ attempt 2 · qwen3.8:27b
...
[04:02]   ✓ CHECK OK — .venv/bin/python -m pytest -q tests/test_export.py
[04:02] ■ passed (check OK) — report: .git/local-agents/3.report.txt
```

Claude itself only reads the final report (saved in `.git/local-agents/<id>.report.txt`), not these
lines, so following along costs no Claude tokens. `LOCAL_PROGRESS=off` turns them off.

Every script prints its help with `--help`, for example
`python3 ~/.claude/skills/local/scripts/dispatch_task.py --help`.

## Plan mode

`/local plan <goal>`: Claude explores the repository (through local agents, or a Sonnet or Opus
subagent for the big picture), proposes the architecture if needed, then a plan of attack:

| # | Task | Who | Models | Files | Check |
|---|---|---|---|---|---|
| 1 | Export tests (the specification) | Sonnet | — | `tests/test_export.py` | the tests fail cleanly |
| 2 | Export implementation | local | your best model for code → a second one | `src/export.py` | `pytest tests/test_export.py` |
| 3 | Documentation | local | your default model | `docs/export.md` | local review |

The models are the ones of your `models.md`, chosen per task from what the skill learned about them
(kind of task, size of the tasks they passed): the plan names them, whatever they are.

It waits for your approval, works on a branch, commits each approved task, then wraps up (who did
what, final tests, tokens saved) and updates `models.md`.
