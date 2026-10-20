#!/usr/bin/env python3
"""Working copies of parallel tasks (dispatch_task.py --worktree). Nothing is ever deleted.

Usage (from the main repository):
  worktree.py list            tasks in progress: working copy, changed files, last result
  worktree.py merge <id>      commits the work of the copy and merges it into the current branch,
                              then removes the copy and its (already merged) branch
  worktree.py discard <id>    drops the task: its work is committed on the branch
                              local-agents/rejected/<id>-<date> (recoverable), then the copy is removed
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402


def fail(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def git(args, cwd):
    return common.run(["git"] + args, cwd=cwd, timeout=120)


def last_result(main, task_id):
    path = os.path.join(common.git_common_dir(main), "local-agents", f"{task_id}.stats.jsonl")
    result = "?"
    try:
        for line in open(path, encoding="utf-8"):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") == "task":
                result = row.get("result", "?")
    except OSError:
        pass
    return result


def commit_all(wt, message):
    """Commits all the work of the copy (if any). Returns True if a commit was made."""
    if not common.git(["status", "--porcelain"], wt).strip():
        return False
    git(["add", "-A"], wt)
    code, out, err, _ = git(["commit", "-q", "-m", message], wt)
    if code != 0:
        fail(f"ERROR: cannot commit in {wt}: {(err or out).strip()[:300]}")
    return True


def main():
    common.utf8_stdio()
    if len(sys.argv) < 2 or sys.argv[1] not in ("list", "merge", "discard"):
        print(__doc__.strip())
        sys.exit(0 if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help") else 2)
    cmd = sys.argv[1]
    main_dir = common.main_root(os.getcwd())
    if not main_dir:
        fail("ERROR: not a git repository.")

    if cmd == "list":
        out = git(["worktree", "list", "--porcelain"], main_dir)[1]
        rows = []
        path = None
        for line in out.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):]
            elif line.startswith("branch refs/heads/local-agents/") and path \
                    and os.path.realpath(path) != os.path.realpath(main_dir):     # the plan branch is not a task
                tid = line.rsplit("/", 1)[-1]
                changes = len([l for l in common.git(["status", "--porcelain"], path).splitlines() if l.strip()])
                rows.append(f"| {tid} | {changes} changed file(s) | {last_result(main_dir, tid)} | {path} |")
        if not rows:
            print("No parallel task in progress.")
            return
        print("| Task | Changes | Last result | Working copy |\n|---|---|---|---|")
        print("\n".join(rows))
        return

    if len(sys.argv) < 3:
        fail(f"Usage: worktree.py {cmd} <id>", 2)
    tid = sys.argv[2]
    wt = common.worktree_path(main_dir, tid)
    branch = f"local-agents/{tid}"
    if not os.path.isdir(wt):
        fail(f"ERROR: no working copy for task {tid} ({wt}).")

    if cmd == "merge":
        dirty = [l for l in common.git(["status", "--porcelain", "--untracked-files=no"], main_dir).splitlines() if l]
        if dirty:
            fail("STOP: the main repository has uncommitted changes; merging now could mix them up with the "
                 "task (and aborting a conflicted merge could lose them). Commit them first: "
                 'git add -A && git commit -m "Manual changes"\n' + "\n".join(dirty[:10]), 2)
        commit_all(wt, f"Task {tid} (local agent)")
        code, out, err, _ = git(["merge", "--no-ff", "-q", "-m", f"Merge task {tid} (local agent)", branch], main_dir)
        if code != 0:
            conflicts = common.git(["diff", "--name-only", "--diff-filter=U"], main_dir).split()
            fail("MERGE CONFLICT" + (f" in: {', '.join(conflicts)}" if conflicts else "") +
                 f". Resolve it then commit, or cancel with: git merge --abort. "
                 f"The copy {wt} is kept.\n{(err or out).strip()[:300]}")
        git(["worktree", "remove", wt], main_dir)
        git(["branch", "-d", branch], main_dir)
        print(f"Task {tid} merged into the current branch; working copy removed.")
        return

    # discard: nothing is deleted, the work stays on a dedicated branch
    commit_all(wt, f"Task {tid} discarded (kept for reference)")
    keep = f"local-agents/rejected/{tid}-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    code, _, err, _ = git(["worktree", "remove", wt], main_dir)
    if code != 0:
        fail(f"ERROR: cannot remove the working copy: {err.strip()[:300]}")
    git(["branch", "-m", branch, keep], main_dir)
    print(f"Task {tid} discarded. Its work can still be recovered on branch {keep}.")


if __name__ == "__main__":
    main()
