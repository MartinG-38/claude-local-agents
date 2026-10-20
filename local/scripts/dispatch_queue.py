#!/usr/bin/env python3
"""Runs several dispatch_task.py tasks one after the other, with ONE notification for Claude.
Uses no Claude tokens. For work split in advance into dependent parts (the loaders, then the
merge, then the notebook builder): Claude is woken up once, at the end or at the first task
that does not pass, instead of once per task.

Usage: dispatch_queue.py plan.json
plan.json:
  {"common": ["--models", "m1", "--user-text", "Le dossier"],          options of every task
   "tasks": [{"id": "a", "instruction": "...", "args": ["--write", "x.py", "--check", "..."]},
             {"id": "b", "instruction_file": "b.txt", "args": [...]}]}
The result of a passed task is committed by the next one (dispatch_task.py does it when its files
are exactly what the passed task left). Output: the tasks' progress, then a summary; read only
the report of the task named on the last line. Exit code: that of the first task that did not pass.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    common.utf8_stdio()
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    try:
        plan = json.load(open(sys.argv[1], encoding="utf-8"))
        tasks = plan["tasks"]
        assert tasks and all(t.get("id") and (t.get("instruction") or t.get("instruction_file")) for t in tasks)
    except (OSError, ValueError, KeyError, AssertionError, TypeError):
        print("ERROR: unreadable plan: {\"tasks\": [{\"id\", \"instruction\" or \"instruction_file\", \"args\"}]}",
              file=sys.stderr)
        sys.exit(2)
    logdir = os.path.join(common.git_common_dir(os.getcwd()) or ".git", "local-agents")
    summary, code = [], 0
    for t in tasks:
        text = t.get("instruction") or open(t["instruction_file"], encoding="utf-8").read()
        argv = [sys.executable, os.path.join(HERE, "dispatch_task.py"), t["id"], "-"] \
            + list(plan.get("common", [])) + list(t.get("args", []))
        code = subprocess.run(argv, input=text, text=True, encoding="utf-8").returncode
        report = os.path.join(logdir, t["id"] + ".report.txt")
        try:
            first = open(report, encoding="utf-8").readline().strip()
        except OSError:
            first = f"== Task {t['id']}: no report (exit {code}) =="
        summary.append(first)
        if code != 0:
            break
    print("\n== Queue ==")
    print("\n".join(summary))
    done = len(summary) == len(tasks) and code == 0
    left = [t["id"] for t in tasks[len(summary):]]
    if not done:
        print(f"Stopped at task {tasks[len(summary) - 1]['id']} (exit {code})"
              + (f"; not run: {', '.join(left)}" if left else "") + ".")
    ids = [t["id"] for t in tasks[:len(summary)]] if done else [tasks[len(summary) - 1]["id"]]
    print("Read: " + " ".join(os.path.relpath(os.path.join(logdir, i + ".report.txt")) for i in ids))
    sys.exit(code)


if __name__ == "__main__":
    main()
