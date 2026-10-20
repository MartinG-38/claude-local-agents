# Changelog

## 0.1.0 — 2026-10-20

First public version.

- Three tools: questions about content (`ask_local.py`), read-only code exploration with checked
  citations (`delegate_local.py --readonly`), checked code tasks (`dispatch_task.py`).
- Checks in a network-less sandbox (macOS `sandbox-exec`, Linux `bubblewrap`), run through Claude
  Code's Stop hook so the agent fixes failures in the same session; held-out tests; local review;
  rejection with remarks.
- Model chain with failure summaries, automatic fallback, best attempt kept; task size learned per
  model; lessons learned from failure modes added to each model's instructions.
- Guard on every agent action (PreToolUse hook), environment scrubbing, refusal of Ollama's cloud.
- Plan mode, parallel tasks in worktrees, dependent task queues (`dispatch_queue.py`).
- No-model helpers: `profile_data.py`, `make_notebook.py`, `run_notebook.py`.
- Diagnostics, model screening and calibration, durable Ollama settings, memory capacity, token
  savings estimated and measured from the transcript.
- Installer for macOS, Linux and Windows; `--uninstall` also removes the `CLAUDE.md` instruction.
- `/local --force-model <m1,m2> <task>`: the user's models only, in order (short names resolved).
- A pass whose agent asked for research is flagged as a possible guess.
- An agent that keeps calling a tool it does not have (e.g. a shell to run the tests) is stopped
  after 3 calls instead of running until the step limit; agents without a shell are told the check
  runs by itself when they finish.
- Agent sessions are not saved to `~/.claude/projects` (`--no-session-persistence`): no pile of
  transcripts, and none mistaken for the user's conversation by `savings.py`.
