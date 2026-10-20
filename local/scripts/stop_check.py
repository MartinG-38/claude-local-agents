#!/usr/bin/env python3
"""Stop hook of the local agents (Claude Code "Stop" event). Uses no Claude tokens.

When the local agent says it is done, this hook verifies its work before letting it stop:
  1. scope: changes outside LOCAL_WRITE_PATHS are set aside (git stash), so the checks always run
     against the original tests;
  2. the check command (LOCAL_CHECK_CMD), in a sandbox;
  3. the hidden check (command read from LOCAL_HIDDEN_CHECK_FILE), which the agent never sees.
If something fails, the hook exits with code 2: Claude Code keeps the agent working IN THE SAME
SESSION with the failure as feedback (it remembers what it already tried, no re-reading). At most
LOCAL_STOP_ROUNDS times; after that the agent may stop and dispatch_task.py decides what comes next.
The agent may also stop when it asks for information ("RESEARCH: ..."): more attempts would not help.

Each round is logged in LOCAL_STOP_LOG (JSON lines) for dispatch_task.py, which re-checks everything
after the session (it reuses a passing result only if nothing changed since).
Variables: LOCAL_PROJECT_ROOT, LOCAL_WRITE_PATHS, LOCAL_CHECK_CMD, LOCAL_HIDDEN_CHECK_FILE,
           LOCAL_STOP_ROUNDS, LOCAL_STOP_LOG, LOCAL_STATS_FILE, LOCAL_TASK_ID.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from check_run import run_check  # noqa: E402

ROOT = os.path.realpath(os.environ.get("LOCAL_PROJECT_ROOT") or os.getcwd())
common.load_baseline(os.environ.get("LOCAL_BASELINE_FILE", ""))      # the user's untracked files: untouched
WRITE = os.environ.get("LOCAL_WRITE_PATHS", "")
CHECK = os.environ.get("LOCAL_CHECK_CMD", "")
HIDDEN = ""
try:      # the hidden check command comes through a file the agents cannot read (not the environment)
    if os.environ.get("LOCAL_HIDDEN_CHECK_FILE"):
        HIDDEN = json.load(open(os.environ["LOCAL_HIDDEN_CHECK_FILE"], encoding="utf-8")).get("cmd", "")
except (OSError, ValueError):
    pass
LOG = os.environ.get("LOCAL_STOP_LOG", "")
STATS = os.environ.get("LOCAL_STATS_FILE") or None
TASK = os.environ.get("LOCAL_TASK_ID", "task")
WRITABLE = os.environ.get("LOCAL_CHECK_WRITABLE") == "1"
try:
    ROUNDS = int(os.environ.get("LOCAL_STOP_ROUNDS", "0"))
except ValueError:
    ROUNDS = 0


def rows():
    try:
        return [json.loads(l) for l in open(LOG, encoding="utf-8") if l.strip()]
    except (OSError, ValueError):
        return []


def record(row):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def set_aside_stray(when):
    stray = common.out_of_scope(ROOT, WRITE)
    if not stray:
        return [], True, ""
    label = f"local-agents {TASK}: changes outside --write {when}"
    moved = common.set_aside_paths(ROOT, stray, label)
    names = ", ".join(stray[:5]) + ("…" if len(stray) > 5 else "")
    note = (f"You modified files outside your allowed scope ({WRITE}): {names}. Those changes were "
            "removed. Modify only the allowed files.")
    return stray, moved, note if moved else note + " (they could not be removed: the work is rejected)"


def main():
    common.utf8_stdio()
    try:
        data = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        data = {}
    if not (CHECK or HIDDEN):
        sys.exit(0)
    done = [r for r in rows() if r.get("blocked")]
    last_message = str(data.get("last_assistant_message") or "")
    if common.CONTEXT_FULL.search(last_message):
        record({"round": len(done) + 1, "allowed": "context full"})
        sys.exit(0)                              # nothing more fits: dispatch_task reports "too big"
    if common.RESEARCH_RE.search(last_message):
        record({"round": len(done) + 1, "allowed": "research"})
        sys.exit(0)                              # missing information: dispatch_task handles it
    if len(done) >= ROUNDS:
        record({"round": len(done) + 1, "allowed": "rounds exhausted"})
        sys.exit(0)

    row = {"round": len(done) + 1, "blocked": False}
    stray, moved, scope_note = set_aside_stray("(found by the stop hook)")
    if stray:
        row["scope"] = {"paths": stray, "set_aside": moved}
    feedback = []
    if stray and not moved:
        feedback.append(scope_note)
    else:
        if scope_note:
            feedback.append(scope_note)
        ok, text = True, ""
        if CHECK:
            ok, text = run_check(ROOT, CHECK, STATS, writable=WRITABLE)
            row["check"] = text.splitlines()[0] if text else ""
            if text.startswith("CHECK IMPOSSIBLE"):
                row["allowed"] = "check could not run"
                record(row)
                sys.exit(0)                      # not the agent's fault: dispatch_task reports it
            if not ok:
                digest = common.check_digest(text, 30)
                row["sig"] = common.error_signature(text)
                row["fingerprint"] = common.worktree_fingerprint(ROOT)
                prev = done[-1] if done else {}
                if prev.get("sig") == row["sig"] and prev.get("fingerprint") == row["fingerprint"]:
                    # Same error and not a single change since the last round: the agent is stuck.
                    row["allowed"] = "no progress"
                    record(row)
                    sys.exit(0)                  # dispatch_task moves on (next model) instead of waiting
                frames = common.project_frames(text.splitlines())
                same = ""
                if prev.get("sig") == row["sig"]:
                    same = ("SAME ERROR as in the previous round: your last change did not fix it. "
                            + (f"Read the code around {frames[-1]} first, then fix that line. " if frames else ""))
                feedback.append(f"{same}The check failed:\n{digest}\n\nFix the code, then finish again. "
                                "Do not modify the tests unless the instruction asks for it.")
        if ok and HIDDEN:
            hidden_ok, hidden_text = run_check(ROOT, HIDDEN, STATS, project_on_path=True, mask_hidden=False,
                                               strict_tmp=True)
            row["hidden"] = hidden_text.splitlines()[0] if hidden_text else ""
            if hidden_text.startswith("CHECK IMPOSSIBLE"):
                row["allowed"] = "check could not run"
                record(row)
                sys.exit(0)
            if not hidden_ok:
                ok = False
                feedback.append(common.HIDDEN_FEEDBACK)
        if ok:
            after, moved_after, _ = set_aside_stray("(made by the check)")
            if after:
                row["scope_after_check"] = {"paths": after, "set_aside": moved_after}
            row["passed"] = True
            row["fingerprint"] = common.worktree_fingerprint(ROOT)
            record(row)
            sys.exit(0)
    row["blocked"] = True
    record(row)
    print(f"Round {row['round']}/{ROUNDS} of automatic verification.\n" + "\n\n".join(feedback), file=sys.stderr)
    sys.exit(2)                                  # Claude Code: keep working, with this feedback


if __name__ == "__main__":
    main()
