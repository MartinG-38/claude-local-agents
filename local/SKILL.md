---
name: local
description: Delegates large reads and code in volume to local open models (Ollama, any model; never the cloud) to save Claude tokens without lowering quality — checks, escalation, hardened security. Use when a task needs reading a lot (big files, logs, test output, exploring code) or writing a lot of code, for /local plan, or when the user types /local.
---

# Local agents (Ollama)

Local models (Ollama, this machine) do the volume work, so that you spend as few tokens as possible
without lowering quality. `$S` = `~/.claude/skills/local/scripts` (full path; `python3`, Windows
`python`). **Never the cloud**: the scripts refuse it; never work around them.

## Delegating (`/local <task>`, or a task with a lot to read or write)
**Before your first delegation in this conversation, read `delegation.md` (this folder) once, and follow
it**; read it again only if the conversation was compacted since. Never delegate without it.

**`/local --force-model <m1,m2> <task>`** (or `--force-models`): the user's models, for this request
only. `python3 $S/models.py resolve "<m1,m2>"` gives their exact names (on its error, tell the user and
stop); then every call uses only them: `dispatch_task.py --force-models "<names>"` (that order, no
reordering, no fallback), `ask_local.py` / `delegate_local.py --model <first>`. Nothing is saved: the
next prompts without the flag go back to choosing from what was learned (delegation.md).

## `/local <command>`: run it, read nothing else
| Command | Run |
|---|---|
| `doctor` | `python3 $S/doctor.py`, then summarize. `--suggest`: models to propose for this machine (the user installs them, never pull one); `--capacity`: memory, what fits together |
| `saved-tokens` | `python3 $S/savings.py --conversation`; show its output as is (`savings.py` alone: every task of this repository) |
| `size [tiny\|small\|medium\|large\|xlarge\|35%]` | `python3 $S/models.py size [<size>] [--force \| --no-force]`: no size shows the table; a size sets the general one (models with a learned size keep theirs); `--force` imposes it on all; `--no-force` lifts it |
| `settings` | `python3 $S/ollama_settings.py --show` (changes: `--context 128k`, `--keep-alive 1h`, `--reset`, `--block-cloud` / `--allow-cloud`); pass on its "TO DO" lines |
| `unload` (reset, free the memory) | `python3 $S/unload.py`; models in use by a running agent are kept unless the user asks for `--force`; `--list` only lists |
| `models`, `calibrate`, `plan` | read `commands.md` and follow it |
