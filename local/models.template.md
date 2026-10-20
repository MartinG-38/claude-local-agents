# Local models

Which models to use + what Claude has learned about them. One `## name:tag` block per model, exactly as
in `ollama list` (tag required), "DEFAULT" on exactly one; tool calling is required for code tasks.
Claude updates "Observed" and "Settings" (lessons in words, never a line per task). Limit: 60 lines.
Options of each preference: the repository's docs/configuration.md, "models.md". ✅ confirmed (≥ 3 attempts) · 🔸 impression.

Machine: (to fill in: system, chip or GPU, memory) — `/local doctor --capacity` shows what fits.

## Preferences
- Delegation: maximum  (maximum: reads over ~120 lines, code from ~80 lines · balanced: ~200 / ~200, large and simple tasks · cautious: ~400 / ~300, summaries and repetitive code)
- Local fallback: Claude's choice  (Claude's choice: Claude picks the models and their order per task, one fallback that fits is added when Claude gives one model · auto: every agent model of this file, in order · none: never a model Claude did not give)
- Task size: medium  (tiny · small · medium · large · xlarge, or a % of the context; a model's own line wins; /local size)
- Review of results: Claude's choice  (Claude's choice: per task, local review enough / Claude reads the key parts / reads everything · always Claude: Claude always reads the full diff)
- Use of Sonnet: moderate  (none · moderate: specification tests and tasks failed locally · broad: also tasks too hard for the local models)
- Reading by a Claude subagent: sonnet+opus  (none · sonnet · sonnet+opus: Opus for the most critical analyses)
- Web research by a Claude subagent: yes  (yes · no; local agents never have Internet access)
- Research model: auto  (auto: Haiku for a lookup, Sonnet for an analysis · haiku · sonnet)

<!-- Your models go here, one block each (/local models writes them). Example (move it out of this
comment and adapt it; the name is the exact one shown by `ollama list`):
## <name>:<tag> — DEFAULT
- Role: agent, summaries, everyday tasks.
- Task size: medium
- Observed: —
- Settings: —
-->

## Selection rules
- Summarize, extract, docs, repetitive code, multi-step agent → default model.
- Tricky logic after a failure of the default → reasoning on, then a stronger model, then Sonnet.
- Quality before speed: two large models one after the other rather than two small ones in parallel.

## Do not delegate locally (go straight to Sonnet / Claude)
- —
