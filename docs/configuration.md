# Configuration

[← README](../README.md) · [How it works](how-it-works.md) · [Configuration](configuration.md) · [Token savings](savings.md) · [Reference](reference.md)

## Ollama settings

```
/local settings                          shows the chosen values, what is set, what Ollama really uses
/local settings context 128k             or ask in plain words: "set the context to 128k"
/local settings block-cloud              optional: block Ollama's cloud on Ollama's side (allow-cloud lifts it)
```

Without Claude Code: `python3 ~/.claude/skills/local/scripts/ollama_settings.py` with `--show`,
`--context 128k` (32k minimum: an agent needs it), `--keep-alive 1h` (`-1` = until Ollama stops),
`--reset` (64k, 30m), `--block-cloud` or `--allow-cloud`. The new value is remembered and applied the
same lasting way (it says whether your default model still fits in memory with it); restart Ollama
afterwards. A larger context lets an agent keep more files in mind but needs more memory; a smaller one
frees memory. Blocking the cloud on Ollama's side is optional (off unless you ask for it, or unless it
was already blocked); the skill itself never uses the cloud either way.

## Settings: `models.md`

`~/.claude/local-agents/models.md` belongs to you: you choose the models and your preferences in
it; Claude keeps the lessons learned in it.

| Preference | Values (default in bold) |
|---|---|
| Delegation | **maximum** (reads over ~120 lines, code from ~80 lines, notebook builders and glue included) · balanced (~200 / ~200: large and simple tasks) · cautious (~400 / ~300: summaries and repetitive code) |
| Local fallback | **Claude's choice** (order of the models per task; with one model given, one fallback that fits the task is added) · auto (all of them, in file order) · none (only the models Claude gives) |
| Task size | tiny · small · **medium** · large · xlarge, or a percentage: how big one delegated task may be (see below) |
| Review of results | **Claude's choice** (per task: local review enough, key parts, or everything; see [How a task is verified](how-it-works.md#how-a-task-is-verified)) · always Claude (always the full diff) |
| Use of Sonnet | none · **moderate** (specification tests, tasks that failed locally) · broad |
| Reading by a Claude subagent | none · sonnet · **sonnet+opus** |
| Web research by a Claude subagent | **yes** · no |
| Research model | **auto** (Haiku for a simple lookup, Sonnet for an analysis) · haiku · sonnet |

In the file, each preference is one line, its options in a note between brackets (`- Task size:
medium  (tiny · small · …)`); the scripts ignore the note and keep it when they change the value.
Each model then has its block (`## name:tag`, the exact name shown by `ollama list`, "DEFAULT" on exactly one;
`/local models` writes it for you)
with its role, what has been observed and its settings (reasoning, etc.).

The skill improves with use in two ways. After every task the scripts record each model's results
(passes, size reached, causes of failure) and add the matching lessons to that model's next
instructions; `/local doctor --quick` shows them at the start of a session. And when a task taught
something no counter shows (a model that wanders on long files, needs a longer time limit…), Claude
writes it into the model's "Observed" and "Settings" lines on its own, keeping the file under 60 lines;
your preferences, model list and task sizes only change when you ask.

## Choosing models

Any local Ollama model works, as long as it supports tool calling for code tasks: install the ones
that suit your machine. To get proposals for your memory, and the installed models not listed yet:

```
python3 ~/.claude/skills/local/scripts/doctor.py --suggest
```

The rule of thumb: the model's weights plus room for a 64k context (several GB) must fit in the memory
usable by the models: the GPU's memory, about 70 % of the RAM on Apple Silicon, or the RAM for CPU-only
inference (much slower).

> **These are proposals at the time this page was written (October 2026), not requirements.** The
> skill is not tied to any model: it works with whichever local Ollama models fit your computer, and
> learns how each one does. New models come out often: look at [ollama.com](https://ollama.com/search)
> for newer ones, and let `/local calibrate` tell how each one really does on your machine.
> `doctor.py --suggest` gives the same list, filtered by your memory.

| Usable memory | Proposals in October 2026 (approximate weights) |
|---|---|
| ~10–15 GB | `ministral-3:8b` (6 GB) or `ministral-3:14b` (9 GB): questions and summaries; agent tasks are unreliable at this size |
| ~22–25 GB | `gpt-oss:20b` (14 GB, fast, reasoning by levels), `devstral-small-2:24b` (15 GB, code, Mistral) |
| ~26–32 GB | `qwen3.8:27b` (18 GB, strong on code), plus the above |
| ~32–40 GB | `qwen3.6:35b-a3b` (24 GB, fast: mixture of experts, ~3B active parameters), plus the above |
| ~40–75 GB | `qwen3.8:27b-q8_0` (30 GB: the same 27b, more precise quantization), plus the above |
| 75 GB and more | `gpt-oss:120b` (65 GB), for the hardest tasks |

Once downloaded, `/local models` lists your installed models with what each can do, and adds the
ones you pick to `models.md` (a block each, a role guessed from its capabilities, the first one with
tool calling as DEFAULT if there is none yet). Without Claude Code:
`python3 ~/.claude/skills/local/scripts/models.py` (asks in the terminal) or
`models.py add <model> [...] [--default <model>]`. Cloud and embedding models are never added.

- On Apple Silicon, prefer the MLX builds when they exist (tags with `nvfp4`, `mxfp8` or `mlx`; in October
  2026 e.g. `qwen3.8:27b-nvfp4` or `qwen3.8:27b-mxfp8`, which
  `doctor.py --suggest` proposes on a Mac): much faster with Ollama 0.40 or later.
- Two models of different families (e.g. Qwen and Mistral) make a better pair than two sizes of the
  same one: the second one reviews the first and takes over when it fails, with other blind spots.
- The skill keeps track of how each model does on each kind of task (new code or changes, with or
  without a check). When the order Claude chose puts first a model with a clearly worse record (at
  least 2 tries each, a third fewer passes), the script puts the better one first and says so in the
  report (`--keep-order` to keep Claude's order).

## Task size

Claude Code keeps part of an agent's context free for its next answer: by default 32,000 tokens for a
model it does not know, half of a 64k context, after which it compacts the conversation (the local
model summarizes it and forgets details). The skill limits that reserve to one answer (8,192 tokens, plus
the reasoning budget when reasoning is on), so agents can use almost all of their context.

A local model fails when its context fills up: it re-reads its files, and everything it writes and
every fix stays in its context. So Claude splits the work into tasks run one after the other, each one
checkable on its own and close to what the model has passed so far (every task has a fixed cost:
instruction, report, review):

- one module, or a group of related functions with their tests, per task;
- a notebook: one task for the logic in a `.py` module, then `make_notebook.py` builds and runs the
  notebook from a short spec, with no model;
- dependent tasks known in advance run as one queue (`dispatch_queue.py`), with one notification for Claude;
- data files are profiled by `profile_data.py` instead of being read, by Claude or by the agents;
- Claude writes only the spec tests (or held-out reference answers) and the interpretation of results,
  in a file the notebook builder inserts.

A task is sized by what the agent must write: a script full of text (notebook cells) is heavier than its
line count suggests.

The "Task size" preference sets how big a task may be, as the share of the model's context the task
may fill before any work (the rest is for reading more, writing and fixing):

| Task size | Share | Typical task |
|---|---|---|
| tiny | 20 % | one function, a few dozen lines, 1 file: small models or a 32k context |
| small | 30 % | one function or class with its tests, 1–2 files |
| medium (default) | 40 % | a small module (~200 lines) or a few related functions, 2–3 files |
| large | 50 % | a module of a few hundred lines, a few files: strong models |
| xlarge | 65 % | several files at once: 128k context or more, strong models only |

Change it with `/local size small` (or in `models.md`: `- Task size: small`), or give a percentage
(`/local size 35%`). `--size` overrides everything for one task.

**Per-model sizes, learned.** Each model can have its own size, a "- Task size:" line in its block of
`models.md`, which wins over the general one:

- `/local models` writes a first guess from the model's size and context;
- calibration suggests a size from how the model did on the code tasks;
- after real tasks, the skill adjusts it for every model tried: one step down at once when the model's
  context fills up, one step down after 2 sessions in a row that ran out of steps or time or went in
  circles, one step up after 3 passed tasks that came close to the limit (never xlarge below a 128k
  context). The report says when it changes.

To impose a size on every model anyway: `/local size small --force` (learned sizes are kept, and nothing
is learned while a size is forced); `/local size --no-force` goes back to them.

**Before and during a task.** `dispatch_task.py` estimates what the agent must take in before any work
(instruction, files to read or modify, Claude Code's own instructions, measured per model during
calibration). Over the limit, it does not run the task and reports "too big" (exit code 6) with a
suggested split: how many tasks, which files in each, and which file is too large on its own (the agent
then changes only named functions or line ranges). Agents are told to search first, read only the lines
they need, and edit in small steps rather than rewrite files. If the context still fills up during the
work, the task stops at once with the same verdict (no fixes in the same session, no other model with
the same context), and Claude splits it instead of handing it to Sonnet.

## Memory and parallelism

```
python3 ~/.claude/skills/local/scripts/doctor.py --capacity
```

shows the usable memory and, for each model, its weights, what one agent needs (weights + context
cache) and what fits together. Needs are first estimated from the model's architecture, then
**measured** from the first use on.

- Claude picks the best model for the task among those that fit, never a smaller one to go faster
  if quality may suffer.
- If two parallel tasks do not fit in memory together, the second one **waits** (memory guard)
  instead of switching to a smaller model.
- The same model used for two tasks shares its weights, but Ollama processes only one request at a
  time per model as long as `OLLAMA_NUM_PARALLEL` is 1 (its default).
- Parallel tasks work in separate copies (`--worktree`); Claude merges them one by one
  (`worktree.py merge`), or drops them (`worktree.py discard`, the work is kept on a branch).
