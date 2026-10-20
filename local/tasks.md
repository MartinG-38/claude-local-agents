# Code tasks: options and reports

Read before a `dispatch_task.py` call that needs more than `--models`, `--write` and `--check`, or when a
report is not a plain "passed". `$S` = `~/.claude/skills/local/scripts`.

## Options

| Option | Values (default) |
|---|---|
| `--models` | **you choose**, per task, the models to try in order; tool calling required. With one model, one fallback that fits the task is added (`--no-fallback`: none). After a failure by size (time, output or step limit) a model with a smaller task size is skipped |
| `--force-models` | the user's models (`/local --force-model`, SKILL.md): replaces `--models`, `--keep-order`, `--no-fallback` |
| `--write` | files/folders the agent may modify — **always set it** |
| `--no-read` | files/folders the agent must not open (their facts are in the instruction); data files over 16 KB can only be read by their head anyway (`LOCAL_DATA_READ_KB`) |
| `--check` · `--retries` · `--review` | check command · fixes per model (2; the agent fixes a failed check in the same session) · review by another local model (see "Quality and review" in delegation.md) |
| `--hidden-check` | held-out tests the agent never sees (`reference.md`): catches code tailored to the visible tests |
| `--think` | `off` (default, simple volume) · `on` (tricky logic, or after a failure) · `low`/`medium`/`high` |
| `--keep-order` · `--no-baseline` | by default the model with the clearly best record on this kind of task goes first, and the check runs once before the task (to tell old failures from new ones); these turn that off |
| `--size` | tiny · small · medium · large · xlarge, or a % (default: `models.md` "Task size"): over it, the task is not run ("too big", exit 6, with a suggested split) |
| `--max-turns` | steps per session: 30 + 10 per fix round (50 by default) |
| `--timeout` · `--budget` | per attempt, minutes, `0` = unlimited (20) · per task in total (unlimited) |
| `--tools` | add `Bash` if the agent must run commands (otherwise no shell) |
| `--check-writable` | the check builds or writes in the project (make, tsc, coverage xml): set it from the start |
| `--worktree` | parallel tasks (`parallel.md`) |
| `--user-text "S"` | appends verbatim the user's prompt starting with S (transcript): a spec the user wrote, never retyped by you |
| `--recheck` | reruns only the checks (give the same, corrected `--check` / `--hidden-check`) on the work left by task `<id>`, no agent: after "check could not run" |

Checks run sandboxed (no network, project read-only, temporary files in `$LOCAL_TMP` = `$TMPDIR`; a fixed
`/tmp/…` path is refused in hidden checks) with the project's interpreter
(`.venv/bin/python -m pytest`, Windows `.venv\Scripts\python`). Prefer pytest or unittest checks: they
prove tests really ran. Long instruction: `-` and stdin. The shell of a background task shows the user
live progress (`LOCAL_PROGRESS=off` turns it off).

## Reports

A failed check is first fixed by the agent itself, in the same session. A model that still fails has its
work set aside (`git stash`) and the next one in `--models` starts over with its summary.
- **passed** → review (delegation.md), commit. "Scope:" lines = changes outside `--write` set aside; if
  really needed, widen `--write` and rerun. "Small task:" → batch tasks of that size next time, or do them yourself.
- **impossible** → read the message (model without tools, missing, Ollama stopped…), fix the choice.
  "check could not run" (CHECK IMPOSSIBLE): your check command is at fault, not the model (not counted
  against it): fix the command, then `dispatch_task.py <id> --recheck` with the fixed checks.
- **too big** (exit 6, or context filled up), or "→ … ran out" (time, output or step limit) → split,
  dispatch the parts one after the other; a smaller model would fail the same way.
- "Loop:" (an agent re-read the same file again and again), or "step limit reached" with the agent going
  in circles → split, or make the instruction precise. Never read `<id>.actions.log` (for the user);
  at most `tail -n 15` of it if the report does not explain a failure.
- "stopped: kept calling …, a tool it does not have": the agent tried, e.g., to run the tests without a
  shell; its session was cut after 3 calls and the next model takes over (the lesson "tools" is learned).
- **failed**, **needs work**, **needs information** (`RESEARCH: …`), or not good enough on your review
  (`--reject`) → `reference.md`.
- **passed** with "the code may be a guess" (the agent asked `RESEARCH:` yet the check passed) → read
  the whole diff; if it guessed, research, then `--reject "<the facts>" --keep`.
- "Before the task …" lines: failures already there, or a check that already passed (then it does not
  test the change: fix the check rather than trust a "passed").
- What you finish by hand after a failed task is counted as a cost automatically.

## Recipes

**Questions on data** (counts, means, "the most frequent…"): compute the reference answers yourself first
(a short pandas script, outside the project, e.g. the scratchpad), then dispatch the code with
- a visible `--check` on the **format**: keys (`q01`…, not `q1`), types, files produced;
- a `--hidden-check` on the **values**: your script compares the produced answers with the reference.
The pass then needs no reading of the code. Temporary files of the checks: `$LOCAL_TMP`.

**Notebook**: the module task, then `make_notebook.py` (no model; see delegation.md). Former recipe,
when a builder script is really needed: two tasks, one after the other. 1) the module (one function per question or plot), checked
by the hidden reference; 2) the builder script (nbformat, then executed: `--check-writable`) that calls the
module. The texts that interpret results (figure comments, synthesis) are yours: write them in a file the
builder inserts as is.
