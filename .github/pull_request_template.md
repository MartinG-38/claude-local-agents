## What and why

<!-- What this changes, and the problem it solves (link the issue if there is one). -->

## Checks

- [ ] `python3 tests/run_tests.py` and `python3 tests/test_tools.py` pass
- [ ] A scenario in `tests/run_tests.py` covers the new behavior or the fixed bug
- [ ] Documentation kept in sync (`README.md`, `docs/`, the files Claude reads in `local/`)
- [ ] No dependency added; works on Python 3.8+
- [ ] Nothing specific to my models or machine (`models.md`, lessons, logs, private code or data)
- [ ] If it touches a security layer (guard, sandbox, environment scrubbing, cloud refusal): a test
      shows what still holds

Tested with Ollama and Claude Code (`tests/acceptance.py`)? <!-- yes, with which OS / models — or no -->
