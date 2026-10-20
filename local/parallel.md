# Parallel tasks (optional)

**Quality comes before speed.** Only parallelize **independent** tasks (different files, no
dependency between them), and only without lowering quality.

## Choosing the models

1. For each task, pick the model you would have picked sequentially. **Never pick a smaller model
   to be able to parallelize** if the result may suffer: two large agents one after the other beat
   two small ones at the same time.
2. `python3 $S/doctor.py --capacity` tells what fits in memory:
   - **same model** for two tasks: the weights are shared ("simultaneous agents" column), but Ollama
     processes one request at a time per model unless `OLLAMA_NUM_PARALLEL` ≥ 2: without that
     setting, the two tasks take turns and can even be slower than one after the other (each
     switch invalidates the model's cache): stay sequential; with it, a modest gain (same chip);
   - **different models**: only the combinations listed.
3. If it does not fit, launch anyway: the **memory guard** makes the second task wait until memory
   frees up (they run one after the other automatically, without degrading the choice).

## Launching

Each task in its own copy of the project (worktree), in the background:
```bash
python3 $S/dispatch_task.py <id> "<instruction>" --worktree --models "<m>" --write "…" --check "…"
```
The report gives the path of the copy. `python3 $S/worktree.py list`: tasks in progress.

## Merging

For each finished task, review `git -C "<worktree>" diff`, then:
- `python3 $S/worktree.py merge <id>`: commits the work and merges it into your current branch;
  on a conflict, resolve it or `git merge --abort` (the copy is kept);
- `python3 $S/worktree.py discard <id>`: drops the task; its work stays on a
  `local-agents/rejected/…` branch (nothing is deleted);
- not good enough: `dispatch_task.py <id> --worktree --reject "<remarks>"` (see `reference.md`).
Merge one task at a time and rerun the project's tests after each merge.
