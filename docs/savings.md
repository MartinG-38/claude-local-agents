# How many tokens are saved?

[← README](../README.md) · [How it works](how-it-works.md) · [Configuration](configuration.md) · [Token savings](savings.md) · [Reference](reference.md)

Using the skill has a cost of its own: `SKILL.md` (~0.6k tokens) each time it is loaded (every
`/local`, or when Claude picks the skill), `delegation.md` (~2.5k tokens) once per conversation, before
the first delegation, a few lines standing for `models.md` at the start of a session (`doctor --quick`
prints its preferences, models and "do not delegate" items; Claude reads the whole file only to plan,
calibrate or update it), and each delegation about 1k tokens (instruction, report, diff, tool calls). So
delegation pays off on volume: reading large files, logs or test output, exploring code, writing a few
hundred lines. For a small edit, Claude does it itself. To keep the fixed cost low, `SKILL.md` is only a
router: the common commands (`doctor`, `saved-tokens`, `size`, `settings`, `unload`) run from it with
nothing else to read; the rest is in files Claude reads only when needed (`delegation.md` for
delegating, `commands.md` for `models`, `calibrate` and `plan`, `tasks.md` for options and reports,
`reference.md`, `parallel.md`, `orchestration.md`).

The instruction Claude writes is the largest cost of a task (Claude's output costs ~5× its input). When
the user has already written the spec, Claude does not retype it: `--user-text "<first words>"` appends
the user's prompt verbatim, found in the Claude Code transcript.

A model is not always needed to shrink what Claude reads: a filter often gives the same answer at once
and for free (`pytest -q --tb=no -rfE -p no:warnings`, `grep -c`, `sort | uniq -c`, `tail`). Claude tries
that first and uses a local model only for what is still long after it.

Every task report ends with its estimated savings, and with a "Small task:" line when the task saved
less than ~3k tokens (input equivalent): the hint to batch tasks of that size, or do them directly.
"Input equivalent" = input + 5 × output tokens (an output token costs ~5× an input token); each figure says
whether it is a saving or a cost. For totals:

```
/local saved-tokens       this conversation, and each of its last 3 prompts
python3 ~/.claude/skills/local/scripts/savings.py    all the tasks and questions of the current repository
```

The estimate is rough (~3.5 characters per token). Claude alone: reading the files the agent read
(at their size before the task; a file the agent created and read back does not count) and writing the
code it added. With delegation: writing the instruction, reading the report and the diff, plus ~300
fixed tokens per call. A data file the agent read (`.csv`, `.json`, `.log`…) counts as its first 20
lines only: Claude alone would have looked at its head, not read it whole. Only what the task did counts: your own untracked files (a notebook in
progress…) are never counted as written by the task. `/local saved-tokens` also subtracts the skill's
own cost found in the conversation (each load of its instructions, each read of its reference files
or of `models.md`) and shows the **net savings**; it says so when the delegations did not pay for it.
It then **measures** what the delegations really cost Claude in the transcript (the commands it wrote to
run them, the reports, logs and outputs it read) and shows the **net savings, measured**, which replaces
the estimated cost of each delegation by that real one: the honest figure when Claude read more than
the reports.
Not counted: the context re-reads Claude would have done on its own (savings), and the skill's text
staying in the context at later turns (cost). A failed task counts as a loss, and so does what Claude then finishes
by hand in the task's files: it is measured from git (lines added, files read) the next time a task
starts or the savings are shown in that repository, once per task. Both outputs also give each
model's success rate (tasks passed / tried). The conversation is found from
Claude Code's transcripts (`~/.claude/projects`), and each delegation is recorded with its time in
`~/.local/share/local-agents/savings.jsonl` (sizes only, no content).
