# Delegating to local agents

Read once per conversation, before the first delegation (SKILL.md); again only after the conversation was
compacted. You are the architect; local models do the volume work: **spend as few of your tokens as
possible without lowering quality**. `$S` = `~/.claude/skills/local/scripts`. Other files of this folder
are read **only when needed**: `tasks.md`, `reference.md`, `parallel.md`, `orchestration.md`.

## Start of session
1. `python3 $S/doctor.py --quick`: report a problem once. If it says the Ollama settings are not applied,
   run `python3 $S/ollama_settings.py` and pass on its "TO DO" lines. Its "Learned from real tasks" lines
   are your best guide: size code tasks below the "passed up to N lines" of the model (none yet: ~100
   lines), prefer the model with the best record on that kind of task, and split rather than retry when
   it says "lessons not enough". The scripts already add each model's lessons to its instructions.
2. Its "models.md:" lines stand for `~/.claude/local-agents/models.md` (preferences, models, what not to
   delegate): apply them. Read the whole file only to plan, calibrate, update it, or when choosing between
   models for code is still open. Missing file: copy `models.template.md` from this folder; no model
   listed → `/local models`.

## When delegating pays off
This file costs you ~2.5k tokens once per conversation, and each delegation ~1k (instruction, report,
diff): delegate only what clearly recovers that. Code you write costs ~50 input-equivalent tokens a line (output ×5), a
delegated task ~3k all in: code pays off from ~60 lines. The `Delegation` preference (`models.md`) sets
the thresholds **R** (lines left to read) and **C** (new or changed lines of code):

| Delegation | R | C | Also delegate |
|---|---|---|---|
| maximum | ~120 | ~80 | everything below that is volume: notebook builders, plots, fixtures, glue |
| balanced | ~200 | ~200 | large and simple tasks |
| cautious | ~400 | ~300 | only summaries and repetitive code |

The user names local agents or roles → each role is a local call (`dispatch_task.py`, `ask_local.py`,
`--review`); take a role yourself only if it is judgment (spec tests, conclusions), and say so first.
- **Data files, for free**: `python3 $S/profile_data.py <folder> --key <id>` (encoding, separator,
  decimal, column types and ranges, duplicates, overlaps between files) replaces reading them; pass the
  facts in the instruction and `--no-read <folder>` (agents may not read a data file over 16 KB whole).
- **Filter first, for free**: a deterministic cut often gives the same answer with no model and no
  wait: `pytest -q --tb=no -rfE -p no:warnings`, `grep -c`, `sort | uniq -c`, `tail`. Use a model only
  for what is still over R lines after that. Counts, totals, exact lists: always a tool, never a model
  (it miscounts).
- **Reads, the best payoff**: content over R lines you would otherwise read (logs, test output, big
  files, docs) → `ask_local.py`; finding things across the code → `delegate_local.py --readonly`.
- **Code in volume**: C+ new or changed lines, or many files of repetitive edits → `dispatch_task.py`.
  Count the whole job before writing anything: several pieces of ~40 lines (a module and the script
  that builds its notebook) are one task over C, not small jobs to do yourself.
  Batch small edits into one task. Each task has a fixed cost (instruction, report, review): aim each one
  near the model's "passed up to N lines" (at least half of it), never two tasks where one fits.
- **Delegate the bulk, keep the judgment**: plotting, notebook building, CLI/IO glue, data generation and
  boilerplate are volume too. What you write costs ×5 (output): write only the spec tests and what
  depends on results (interpretations, conclusions), not the code around them. Put those texts in a file
  (e.g. `texts.md`) that the delegated script inserts: never let the agent write the interpretations.
- **Instruction = pointer, not copy**: when your tests define the behavior, name the tests, the files, the
  constraints and what not to touch, plus only what the tests do not show. Do not restate signatures or
  cases; an instruction as long as the code saves nothing.
  The user already wrote the spec (questions, requirements, formats): never retype it, add
  `--user-text "<its first words>"` (their prompt is appended verbatim from the transcript) and write only
  which part to do, the files, the constraints. Same spec, next task: `--user-text` again.
- Below R/C, and architecture, decisions, security, hard debugging → yourself. A report line "Small
  task:" means that one did not pay off.
- **Sonnet** (Agent tool, `general-purpose`, `model: "sonnet"`) per `models.md` (default: specification
  tests, tasks that failed locally). Reads beyond the local models (cross-module, subtle bug, security):
  `Explore` subagent, `"sonnet"` (or `"opus"` per `models.md`), "≤ 400 words, `file:line`, no copies".

```bash
python3 $S/ask_local.py "List the distinct errors with their count." --file big.log --model <m>
.venv/bin/python -m pytest -q 2>&1 | python3 $S/ask_local.py "Which tests fail, and why?" --stdin
python3 $S/delegate_local.py "Where and how is authentication handled?" --readonly
python3 $S/dispatch_task.py <id> "<instruction>" --models "<m1> <m2>" --write "src/utils.py" \
  --check ".venv/bin/python -m pytest -q tests/test_utils.py"
```
Code tasks: in the background (`run_in_background`), one at a time; you are notified, **do not poll**.
Values to get right (data answers, key logic): reference answers or tests in
`python3 $S/common.py hidden-dir <project>-<id>`, then `--hidden-check` (never named in the instruction).
Then read **only** `.git/local-agents/<id>.report.txt`. Always `--write`; `--check` with the project's
interpreter, never `uv run` or an install; a check's temporary files go to `$LOCAL_TMP`, never a fixed
`/tmp/…` path. Other options, report outcomes, recipes (data questions, notebooks) → `tasks.md`.

**Task size**: a task must fit the model's context (`models.md` "Task size"; `--size`). Split bigger work
into tasks run one after the other, each coherent and checkable (one module or a group of related
functions with their tests). "too big" (exit 6), or a time / output / step limit → split; never escalate
just for size. Size by what the agent must WRITE, not only its line count: a script full of text (notebook
cells, comments) is heavier than its lines suggest. Notebooks: the agent never edits the `.ipynb`; the logic
in a `.py` module (checked task), then **no builder task**: write a short spec for
`$S/make_notebook.py spec.md -o x.ipynb --module m.py --texts texts.md --run` (`%% md`, `%% imports`,
`%% getsource f, g`, `%% include key`, `%% code`); `--run` executes it (`run_notebook.py`, also in a check).
Parts known in advance and dependent → **one** `$S/dispatch_queue.py plan.json` (one notification;
a passed task's new files are committed by the next one).
**Model**: the best for this kind of task among those that fit in memory (`doctor.py --capacity`);
never a smaller model to go faster. With one model in `--models`, the script adds one fallback that fits
the task (report line "Fallback:"; `--no-fallback` for none); say at launch which models will try.

## Quality and review
The agent does not see this conversation: give the goal, files, constraints, what not to touch, the
success criterion. A test defining new behavior is written by you or Sonnet, never by the implementer.
After an exploration, its "Citations:" line has checked every file:line against the files: read **only
the doubtful ones**, and the cited lines of a decisive claim with no citation or one that hangs on a
default value or an edge case. "Do not delegate
locally" (`models.md`) wins. Review per "Review of results" (`models.md`; decide **before** dispatching,
`--review` = a model other than the author):
- **Local review enough**: routine code, covered by your (or held-out) tests, check passed, verdict
  APPROVED (not self-review, not inconclusive) → read only `git diff --stat`, new public signatures and
  the reviewer's remarks. Reading the whole diff "to be safe" costs about what the task saved.
- **Key parts**: important logic, tests leaving cases open, weak verdict → `--review` + the parts that matter.
- **Everything** (you, or Sonnet / Opus): security, concurrency, subtle algorithm, data loss, public API,
  a "Do not delegate locally" case. Preference `always Claude` → always the full diff.
Code not dispatched (a diff by hand): `git diff | python3 $S/ask_local.py "<review prompt>" --stdin`.
Say which level you used. **passed** → review, commit, and pass on the "saved" line in a few words.
Anything else → `tasks.md`.

## Security and manual changes
Agents run restricted (project only, writes only in `--write`, no `.git`, secrets, web). Repeated or odd
blocked actions → reject. Never touch the user's manual changes: modified tracked files stop a task
(suggest `git add -A && git commit -m "Manual changes"`); their untracked files outside `--write` stay.
Never `git checkout -- .`, `git reset --hard`, `git clean`: to reject, `git stash`.

## models.md (60 lines max)
Update it after a plan (once), a calibration, or a task that taught something the counters do not show:
"Observed", "Settings", "Do not delegate locally", **yourself, without asking**, in words no counter
gives ("loses track of imports in long files"). No counters, no line per task (the scripts count,
reorder and add lessons themselves). Nothing about private code. Preferences, the model list and task
sizes belong to the user: suggest, do not change. Ollama unreachable → say so, do the task another way.
Always say briefly what you delegated, and to whom.
