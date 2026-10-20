# Plan mode: orchestrating a project with local agents

You are the architect and the project lead. Local agents execute; Sonnet is backup according to the
`models.md` preference.

## 1. Design

**Existing project**: understand the repository (structure, conventions, dependencies, tests and how
to run them) **through local explorations** (`delegate_local.py --readonly`, one precise question at
a time) rather than reading the files yourself; for a cross-cutting understanding the local models
cannot provide, use an `Explore` subagent with Sonnet (or even Opus) according to `models.md`. Read
directly only the cited lines that matter for your decisions. If the goal raises an architecture
question, share your thinking before breaking the work down.

**New project**: design the architecture with the user. Ask only the questions that change the
design (goal, stack, constraints), then propose: file tree, modules and responsibilities,
interfaces, technical choices, testing strategy. Local agents execute well but design poorly: the
more precise your design (files, signatures, data formats), the better they will do.

## 2. Plan of attack

Read `delegation.md` if not done yet in this conversation, and do its "Start of session". Present a table:

| # | Task | Who | Models | Files | Check | Review |
|---|------|-----|--------|-------|-------|--------|

- **Who**: `local` for simple, checkable volume; `Sonnet` according to the preference (`none` /
  `moderate` / `broad`, see `models.md`); `Claude` (you) for architecture and tricky points.
  Group micro-tasks: a local task must be worth its instruction, report and diff (~1k tokens): above
  the thresholds of the `Delegation` preference (delegation.md, "When delegating pays off"), or many
  repetitive edits; below that, `Claude`. A "Review" column: the level
  of "Review of results" (delegation.md) planned for each local task.
- **Tests first, as the specification**: for each new behavior, a "tests" task written by you or by
  Sonnet comes before the "code" task given to a local model. Its check (`--check`) runs those
  tests; the local agent must not modify them (left out of `--write`). For important logic, also
  write held-out tests (`--hidden-check`, see `reference.md`) that the agent never sees.
- **Models**: for each local task, the ordered list to try (`--models`, one is fine), based on
  `models.md` and `doctor.py --capacity` (the best one that fits in memory; tool calling required);
  add `--review` for important code whose tests do not tell everything. A "Settings" column if useful.
- **Parallel** (optional, `parallel.md`): only for independent tasks and without picking smaller
  models; mark them in the table (e.g. "3 ∥ 4").
- Each task touches few files, named ones (→ `--write`), with a targeted check (→ `--check`), and
  respects the "Task size" of `models.md` (delegation.md): a module = its skeleton (signatures, docstrings),
  then tasks as large as the model has passed (a group of related functions or classes, never one
  small task per function), in order; a notebook = its logic in a `.py` module (a checked task), then
  `make_notebook.py` from a short spec (no model, no builder task; delegation.md, "Task size"); Claude
  keeps only the spec tests and the interpretations (a texts file the notebook includes).
- Order by dependencies: foundations first (interfaces, types, tests).
- If a task depends on a recent library and web research is allowed, have the research done before
  delegating and pass the result with `--info-file` (saves a round trip).

**Wait for the user's approval before executing.**

## 3. Preparation

**Existing project**: clean git repository; with the user's agreement,
`git switch -c local-agents/<short-name>`.

**New project**: lay the foundations yourself, then commit: `git init`, `.gitignore`, dependencies
(e.g. `pyproject.toml`), test environment; file tree and skeletons (signatures, types, docstrings
describing each function); a first passing test. Local tasks then "fill in" these skeletons, one by one.

## 4. Execution (one task at a time, except parallel tasks)

- `local`: `python3 $S/dispatch_task.py <#> "<instruction>" --models "<m1> <m2>" --write "<files>" --check "<check>"`
  (Windows: `python`), in the background; then read the report (`tasks.md`, "Reports").
  Parallel tasks: `--worktree`, then merge them one by one (`parallel.md`).
- `Sonnet`: Sonnet subagent with the same self-contained instruction, then check.
- `Claude`: do it yourself.

Review each result before committing: if it is not good enough, `--reject "<remarks>"` (see
`reference.md`). After each approved task: `git add -A && git commit -m "Task <#>: <title> (<who did it>)"`.
If a task reveals a problem in the plan, stop, explain and propose an adjusted plan.

## 5. Wrap-up

Run `python3 $S/savings.py`, then summarize briefly: who did each task (local on the first try /
after fixes / Sonnet / you), final tests, estimated token savings (one line), branch to review
before merging. Update `models.md` once, here, and sum up what you learned in 2–3 lines.
