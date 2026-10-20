# Reference

[← README](../README.md) · [How it works](how-it-works.md) · [Configuration](configuration.md) · [Token savings](savings.md) · [Reference](reference.md)


## Commands in Claude Code

| Command | What it does |
|---|---|
| `/local <task>` | delegates this task to the local models (Claude picks the tool and the models) |
| `/local plan <goal>` | plan mode: design, plan of attack for your approval, execution task by task, wrap-up |
| `/local --force-model <m1,m2> <task>` | this prompt only (nothing is saved) uses exactly these models, in this order: no reordering by record, no fallback, none skipped. Exact names or a part of exactly one installed name (`27b,glm`), separated by commas (not `-`: model names contain it). Without it, Claude and the scripts choose from what was learned |
| `/local doctor` | full diagnostics: Claude Code, Ollama, sandbox self-test, context, each model's capabilities, memory, cloud block |
| `/local models` | screens the models installed in Ollama and adds the ones you pick to `models.md` |
| `/local calibrate [--quick] [--force] [--only=5,6] [model]` | measures the models: speed, code tasks (2 with `--quick`, else 6: skeleton, bug, tests, docstrings, a module from tests of real size, continuing someone else's partial work without rewriting it) with how the model worked (tiny edits, looked into .venv, going in circles, step limit), 8 reading questions, Claude Code's fixed cost, a suggested task size; models already calibrated and unchanged are skipped unless `--force`. The models keep their lessons during calibration, but nothing is learned from it: what was learned from real tasks is left as it is |
| `/local size` | shows the task size: general, forced, and each model's own (learned) size |
| `/local size <size>` | sets the general size (tiny, small, medium, large, xlarge or a %), for the models without their own |
| `/local size <size> --force` | imposes it on every model; their learned sizes are kept and nothing is learned meanwhile |
| `/local size [<size>] --no-force` | lifts the forced size (and, with a size, sets the general one) |
| `/local settings` | shows the Ollama settings: chosen, set on this system, really used |
| `/local settings context 96k` · `keep-alive 1h` · `reset` | changes them, durably (restart Ollama afterwards) |
| `/local settings block-cloud` · `allow-cloud` | optional: blocks Ollama's cloud on Ollama's side, for every use of Ollama, or lifts that block (the skill never uses the cloud either way) |
| `/local unload [model]` | unloads the models Ollama keeps in memory (not those in use, unless you ask to force it) |
| `/local saved-tokens` | tokens saved in this conversation, and in each of its last 3 prompts |

You can also ask in plain words ("set the context to 96k", "free the memory", "how many tokens did we
save?"). Each command runs one of the scripts below, which you can also run yourself
(`python3 ~/.claude/skills/local/scripts/<script> --help`; on Windows, `python`).

## Scripts and options

**`dispatch_task.py <id> "instruction" [options]`** — one checked code task (instruction `-` = stdin):

| Option | Meaning (default) |
|---|---|
| `--models "a b"` | local models to try, in order; the next one starts over with the previous one's failure summary, or continues its work if it made tests pass (default model). With one model, one fallback that fits the task is added |
| `--no-fallback` | no automatic fallback when only one model is given |
| `--force-models "a,b"` | exactly these models, in this order, as the user asked (`/local --force-model`): no reordering, no fallback, none skipped after a failure by size; short names resolved |
| `--write "files folders/"` | the only paths the agent may modify (always set it) |
| `--no-read "files folders/"` | paths the agent must not open: the instruction gives their facts (data files over 16 KB can only be read by their head, `LOCAL_DATA_READ_KB`) |
| `--check "cmd"` | check command (tests, linter), run sandboxed, project read-only |
| `--check-writable` | lets the checked code write in the project (`.git`, virtual environments and secrets stay protected) |
| `--hidden-check "cmd"` | held-out tests the agent never sees (folder from `common.py hidden-dir <name>`) |
| `--retries N` | fixes per model, in the same session through the Stop hook (2) |
| `--review [model]` | review of the diff by another local model ("auto") |
| `--reject "remarks"` · `--keep` | rejects the last result with remarks and reruns; `--keep` improves the work instead of starting over |
| `--resume --info-file F` | resumes a task stopped on "needs information", with the answer in F |
| `--user-text "S"` | appends verbatim the user's prompt that starts with S (from the Claude Code transcript, this conversation first): a spec the user wrote is not retyped by Claude |
| `--recheck` | reruns only the checks (the same, corrected `--check` / `--hidden-check`) on the work left by the task, no agent; a pass is recorded as the task's result |
| `--worktree` | works in a separate copy of the repository (parallel tasks; then `worktree.py`) |
| `--size S` | task size for this task only (tiny … xlarge, or a %) |
| `--keep-order` | keeps your order of `--models` (by default, the model with the clearly best record on this kind of task goes first, and the report says so) |
| `--no-baseline` | does not run the check before the task |
| `--think V` | reasoning: off, on, low, medium, high (off) |
| `--timeout MIN` · `--budget MIN` | per attempt (20) · for the whole task, all models included (unlimited) |
| `--tools "..."` · `--max-turns N` · `--model M` | agent tools (Read Edit Write Glob Grep; add Bash for a shell) · steps per session (30 + 10 per fix round: 50 by default) · a single model |

| Script | Usage |
|---|---|
| `ask_local.py "instruction" --file F` (or `--stdin`) | question about content you provide; `--model`, `--think`, `--temp`, `--ctx`, `--max-tokens`, `--keep-alive` |
| `delegate_local.py "question" --readonly` | read-only code exploration, answer with `file:line`, each citation checked against the files (Claude rereads only the doubtful ones); same options as the agent (`--model`, `--think`, `--timeout`, `--max-turns`) |
| `worktree.py list` · `merge <id>` · `discard <id>` | parallel tasks: list, merge into the current branch, drop (kept on a `local-agents/rejected/…` branch) |
| `models.py` · `models.py add <m> [...] [--default <m>]` | screen the installed models · add them to `models.md` |
| `models.py resolve "27b,glm"` | exact names of the installed models a user named (what `/local --force-model` runs) |
| `models.py size [<size>] [--force \| --no-force]` | task size (what `/local size` runs) |
| `doctor.py` · `--quick` · `--capacity` · `--suggest` | diagnostics · one-line check at session start · memory and what fits · models to install for this machine |
| `calibrate.py [--quick] [--force] [--only=5,6] [model ...]` | calibration (results in the data folder, `results.md`) |
| `ollama_settings.py` · `--show` · `--context N` · `--keep-alive D` · `--reset` · `--block-cloud` · `--allow-cloud` · `--check` | Ollama settings, applied durably |
| `unload.py [model ...]` · `--force` · `--list` | free the memory |
| `savings.py [id ...]` · `--conversation` | tokens saved: this repository · this conversation |
| `common.py hidden-dir <name>` | creates and prints a folder for held-out tests |
| `dispatch_queue.py plan.json` | dependent tasks one after the other, one notification; stops at the first that does not pass |
| `profile_data.py PATH... [--key COL] [--rows N]` | compact profile of CSV, JSON, XML, SQLite files (encoding, separator, types, ranges, duplicates, overlaps), no model |
| `make_notebook.py SPEC -o X.ipynb [--module M] [--texts T] [--run]` | notebook from a short spec (markdown, imports and functions copied from a module, texts), no model |
| `run_notebook.py X.ipynb [--out F]` | executes a notebook, also in a check's sandbox (run with the project's interpreter) |
| `install.py` · `--ollama-settings` · `--block-cloud` · `--claude-md` · `--uninstall` | install or update, Ollama settings, optional cloud block on Ollama's side, CLAUDE.md hint, removal (backed up) |
| `tests/acceptance.py [--model M] [--keep]` | acceptance test on the real installation (`--keep` keeps its temporary repositories) |

## Exit codes of `dispatch_task.py`

| Code | Meaning |
|---|---|
| 0 | passed (or approved by the review, or done without a check) |
| 1 | failed, needs work, or impossible (model without tools, Ollama stopped, check that cannot run, hooks not run) |
| 2 | usage error |
| 5 | needs information (`RESEARCH:` from the agent): web research then `--resume --info-file` |
| 6 | too big: not run, or the context filled up; split it |

`delegate_local.py`: 0 done, 1 error, 2 usage, 3 time limit, 4 step limit (or session cut: the agent
kept calling a tool it does not have). `ask_local.py`: 3 when the
content does not fit in the model's context.

## Environment variables

| Variable | Effect |
|---|---|
| `LOCAL_ALLOW_UNSANDBOXED=1` | accepts checks without a sandbox (Windows, Linux without bubblewrap) |
| `LOCAL_KEEP_ENV="NAME ..."` | passes these variables to agents and checks (otherwise anything that looks secret is removed) |
| `LOCAL_MEMORY_GB` | forces the memory usable by the models, if the estimate is wrong |
| `LOCAL_MODEL` · `LOCAL_MODELS` · `LOCAL_THINK` · `LOCAL_TASK_SIZE` | defaults for `--model`, `--models`, `--think`, `--size` |
| `LOCAL_TIMEOUT` · `LOCAL_RETRIES` · `LOCAL_BUDGET` · `LOCAL_MAX_TURNS` | defaults for `--timeout`, `--retries`, `--budget`, `--max-turns` |
| `LOCAL_CHECK_TIMEOUT` | time limit of one check, in seconds (300) |
| `LOCAL_THINK_BUDGET` | reasoning budget in tokens when reasoning is on |
| `LOCAL_PROGRESS=off` | no live progress lines in the shell of a task (on by default) |
| `LOCAL_WAIT_MAX` | how long a task may wait for memory held by parallel tasks, in minutes (120) |
| `OLLAMA_HOST_URL` | Ollama's address (default `http://127.0.0.1:11434`); anything not local is refused |

## Files and folders

| Where | What |
|---|---|
| `~/.claude/skills/local/` | the installed skill |
| `~/.claude/local-agents/models.md` | your models, preferences, task sizes and lessons learned (never overwritten by an update) |
| `~/.claude/local-agents/memory.json` | per model: measured memory, context and fixed cost; successes per kind of task, failure modes and the lessons they triggered, size in lines of passed tasks, learned output room |
| `~/.local/share/local-agents/` (Windows: `%LOCALAPPDATA%\local-agents\`) | calibration results, held-out tests, chosen Ollama settings, savings ledger (sizes only), backups of previous versions |
| `<repository>/.git/local-agents/` | per task (never committed): `<id>.report.txt` the report, `<id>.log` the log, `<id>.actions.log` every action of the agents (tool and target), `<id>.baseline.json` your untracked files left alone, `<id>.security.log` the guard's decisions, statistics |
| `~/.ollama/server.json` | Ollama's own settings; holds the cloud block if you chose it |
| `~/Library/LaunchAgents/local.ollama.env.plist` (macOS) | sets the Ollama variables at each login |

## Repository layout

```
install.py                 install, update, uninstall
README.md                  overview, installation, usage
docs/                      how it works, configuration, token savings, reference (this file)
CHANGELOG.md               versions
CONTRIBUTING.md            how to contribute (tests, what never goes in the repository)
SECURITY.md                how to report a vulnerability
LICENSE                    MIT license
local/                     the skill (copied to ~/.claude/skills/local/)
  SKILL.md                 router loaded by Claude: the common /local commands (short, to save tokens)
  delegation.md            how to delegate (read once per conversation, before the first delegation)
  commands.md              /local models, calibrate, plan (read only when one is typed)
  tasks.md                 dispatch_task.py options and reports (read only when needed)
  orchestration.md         plan mode
  reference.md             escalation, rejection, web research (read only when needed)
  parallel.md              parallel tasks (read only when needed)
  models.template.md       template for models.md
  scripts/
    common.py              system, sandbox, Ollama, memory
    ask_local.py           question about content you provide
    delegate_local.py      local agent (restricted Claude Code + Ollama)
    dispatch_task.py       checked task, model chain, rejection, resume, worktree
    dispatch_queue.py      dependent tasks one after the other, one notification
    profile_data.py        data file profiles (CSV, JSON, XML, SQLite), no model
    make_notebook.py       notebook from a short spec, no model
    run_notebook.py        executes a notebook (also inside the checks' sandbox)
    guard.py               guard called before every action of an agent (PreToolUse hook)
    stop_check.py          checks run when an agent says it is done (Stop hook)
    check_run.py           sandboxed check
    worktree.py            parallel tasks: list, merge, discard
    doctor.py              diagnostics, memory capacity, model suggestions (--suggest)
    models.py              screens the installed models and adds the chosen ones to models.md
    unload.py              unloads the loaded models to free memory
    ollama_settings.py     Ollama settings kept after a restart (context, keep-alive, optional cloud block)
    calibrate.py           model calibration
    savings.py             estimate of the tokens saved
tests/run_tests.py         tests with a fake Ollama and a fake claude (also run by GitHub CI)
tests/test_tools.py        tests of profile_data.py and make_notebook.py
tests/acceptance.py        acceptance test on the real installation (Ollama + Claude Code)
.github/workflows/         CI: the tests on Linux (bubblewrap) and macOS (sandbox-exec), Python 3.8 and 3.13
.github/ISSUE_TEMPLATE/    bug report and feature request forms
```
