# Contributing

Thanks for your interest! Bug reports, results on your machine and pull requests are all welcome.

## Reporting a bug or a result

Open an issue with the bug report form. The output of `/local doctor` (or
`python3 ~/.claude/skills/local/scripts/doctor.py`) and of `python3 tests/acceptance.py` helps a lot.
Results on **Windows** and on **real Linux machines** are especially useful: they have not been
validated yet. Security problems: see [SECURITY.md](SECURITY.md), not a public issue.

## Changing the code

1. Edit the skill in `local/` (never only the installed copy in `~/.claude/skills/local/`: the next
   `install.py` replaces it).
2. Run the tests (Linux or macOS; no Ollama or Claude account needed):
   ```bash
   python3 tests/run_tests.py          # ~2 min; -k <name> runs some scenarios, -v shows details
   python3 tests/test_tools.py
   ```
   Add a scenario to `tests/run_tests.py` for any new behavior or fixed bug.
3. If you have Ollama and Claude Code: `python3 install.py`, then `python3 tests/acceptance.py`.
4. Keep the documentation in sync: `README.md`, `docs/`, and the files Claude reads in `local/`
   (`SKILL.md`, `delegation.md`, …). Those are loaded into Claude's context: keep them short, since
   every line costs tokens on each use.

Constraints:

- Python 3.8+ and the standard library only: no dependencies.
- macOS, Linux and Windows: use `common.py` helpers for paths, processes and the sandbox.
- Never weaken a security layer (guard, sandbox, environment scrubbing, cloud refusal) without a test
  showing what still holds.

## What never goes in the repository

The repository stays generic. Do not commit:

- your `models.md`, `memory.json`, calibration results or savings ledger;
- lessons or settings specific to your models or machine (they belong in your own `models.md`);
- logs or reports from `.git/local-agents/`, `acceptance-report.md`, private code or data.

Model names in the README and tests are examples only.

## License

By contributing, you agree that your contributions are licensed under the [MIT license](LICENSE).
