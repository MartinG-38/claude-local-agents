# /local commands

Read when the user types `/local models`, `calibrate` or `plan` (the other commands are in SKILL.md). `$S` = `~/.claude/skills/local/scripts`.

| Command | What to do |
|---|---|
| `/local calibrate [--quick] [--force] [--only=5,6] [model]` | `calibrate.py` in the background (15–40 min per model; models already calibrated and unchanged are skipped; `--only=` tries some code tasks without recording a calibration). Then update `models.md`: code results, **the reading score** (who to trust for reading), the behaviours noted after a verdict (tiny edits, looked into .venv, going in circles, step limit: words for "Observed"), and each model's "Task size" line from its "Suggested task size" (your knowledge of the model may correct it). Task 5 (a module from tests) is the closest to real work: a model that fails it should not get new modules. The skill then adjusts sizes and lessons itself after real tasks (calibration learns nothing and erases nothing of that). |
| `/local plan <goal>` | Follow `orchestration.md`. |
| `/local models` | `python3 $S/models.py` (installed models, capabilities, fit in memory); ask which unlisted ones to add (multiple choice), then `models.py add <m> [...] [--default <m>]`. |
| `/local <task>` | Delegate it (`delegation.md`). |
