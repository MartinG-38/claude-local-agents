#!/usr/bin/env python3
"""Estimate of the Claude tokens saved by the local delegations.

Usage:
  savings.py [id1 id2 ...]   tasks and questions of this repository (no argument: all of them)
  savings.py --conversation  the current Claude Code conversation, and its last 3 prompts
                             (what /local saved-tokens shows)

Method (rough estimate, ~3.5 characters per token):
  Claude alone    : read the files the agent read (input) + write the added code (output)
  With delegation : write the instruction (output) + read the report and the diff (input)
                    + ~300 fixed tokens per call (tool call, notification, commands)
                    Only what the task did counts: the user's untracked files are left out, and the files
                    the agent read count with their size before the task.
  Savings         = Claude alone - delegation, if the task succeeded.
                    If it failed, the delegation counts as lost (negative savings),
                    since Claude had to take the task over.
  Questions       : Claude only writes the question and reads the answer, instead of reading the content.
  Skill cost      : (--conversation) each load of the skill's instructions found in the transcript, and
                    each read of its reference files or models.md, is subtracted: "Net savings".
  Measured        : (--conversation) what the transcript shows Claude really wrote for the delegations and
                    read of their outputs, reports and logs, in place of the estimated cost: "Net savings,
                    measured" (the estimate stays the reference for what Claude alone would have spent).
  Equivalent      : input + 5 x output (usual input/output price ratio of Claude models).
Does not count the round trips Claude would have made alone (re-reading context), nor the skill's
text staying in the context at later turns.
"""
import calendar
import datetime
import glob
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

tok = common.tok


def n(x):
    return f"{x:+,}" if x < 0 else f"{x:,}"


def read_jsonl(path):
    rows = []
    try:
        for line in open(path, encoding="utf-8"):
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return rows


def size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def fixed_cost_note(conversation=False):
    if conversation:
        return ("Not counted: the context re-reads Claude would have done alone (savings), and the skill's text "
                "staying in the context, re-read at each later turn at the cached price (cost).")
    skill = tok(size(os.path.join(common.SKILL_DIR, "SKILL.md")))
    once = tok(size(os.path.join(common.SKILL_DIR, "delegation.md")))
    return (f"Not counted here: the context re-reads Claude would have done alone (savings), and the cost of the "
            f"skill: ~{skill:,} tokens per /local, plus ~{once:,} once per conversation that delegates "
            f"(/local saved-tokens counts it).")


# ---------------------------------------------------------------------------- one repository
def repository(wanted):
    gitdir = common.git_common_dir(os.getcwd())
    if not gitdir:
        sys.exit("ERROR: no git repository here (or use --conversation).")
    folder = os.path.join(gitdir, "local-agents")
    root = os.path.realpath(common.git(["rev-parse", "--show-toplevel"], os.getcwd()).strip() or os.getcwd())
    common.record_manual_fixes(root)
    rows, tot = [], {"in": 0, "out": 0, "secs": 0, "ok": 0, "unknown": 0}
    for path in sorted(glob.glob(os.path.join(folder, "*.stats.jsonl")), key=os.path.getmtime):
        tid = os.path.basename(path)[: -len(".stats.jsonl")]
        if wanted and tid not in wanted:
            continue
        data = read_jsonl(path)
        agents = [d for d in data if d.get("type") == "agent"]
        tasks = [d for d in data if d.get("type") == "task"]
        if not agents and not tasks:
            continue
        result = (tasks[-1] if tasks else {}).get("result", "?")       # latest state (a task can be resumed)
        saved = common.task_savings([d for d in data if d.get("type") == "estimate"], result)
        if saved is None:
            tot["unknown"] += 1
        else:
            tot["in"] += saved[0]
            tot["out"] += saved[1]
        secs = sum(a.get("duration_s", 0) for a in agents)
        tot["secs"] += secs
        tot["ok"] += result in ("passed", "approved by review")
        attempts = str(sum(t.get("attempts", 0) for t in tasks) or len(agents))
        fixes = sum(t.get("in_session_fixes", 0) for t in tasks)
        if fixes:
            attempts += f" (+{fixes} in-session fix{'es' if fixes > 1 else ''})"
        model = agents[-1].get("model", "?") if agents else "—"
        rows.append((tid, result, attempts, model, secs, saved))

    asks = read_jsonl(os.path.join(folder, "questions.jsonl")) if not wanted else []
    q = [common.question_savings(a["prompt"], a["content"], a["answer"]) for a in asks]
    ask_in, ask_out = sum(x[0] for x in q), sum(x[1] for x in q)
    if not rows and not asks:
        sys.exit("No measured local delegation in this repository.")
    if rows:
        print("| Task | Result | Attempts | Model | Local time | Saved input / output (− = cost) |")
        print("|---|---|---|---|---|---|")
        for tid, res, k, model, secs, saved in rows:
            eco = "not measured" if saved is None else f"{n(saved[0])} / {n(saved[1])}"
            print(f"| {tid} | {res} | {k} | {model} | {secs / 60:.1f} min | {eco} |")
    if asks:
        k_ask = sum(1 for a in asks if a.get("type") != "explore")
        print(f"\nSimple questions: {k_ask}, code explorations: {len(asks) - k_ask}; "
              f"savings {n(ask_in)} input / {n(ask_out)} output.")
    fixes = [r for r in common.ledger_rows() if r.get("kind") == "manual fix" and r.get("cwd") == root
             and (not wanted or r.get("id") in wanted)]
    fix_in, fix_out = sum(r.get("in", 0) for r in fixes), sum(r.get("out", 0) for r in fixes)
    if fixes:
        print(f"\nFinished by hand after a failed task: {len(fixes)} ({', '.join(r['id'] for r in fixes[:5])}); "
              f"cost ~{abs(fix_in):,} input / ~{abs(fix_out):,} output.")
    task_rows = [[d for d in read_jsonl(p) if d.get("type") == "task"][-1:] for p in
                 glob.glob(os.path.join(folder, "*.stats.jsonl"))]
    outcomes = common.outcomes_line([r[0] for r in task_rows if r])
    print(f"\nTasks: {len(rows)}, {tot['ok']} of them checked OK; {tot['secs'] / 60:.0f} min of local compute.")
    if outcomes:
        print(outcomes)
    print("Estimated savings: " + common.savings_line(tot["in"] + ask_in + fix_in, tot["out"] + ask_out + fix_out) + ".")
    if tot["unknown"]:
        print(f"({tot['unknown']} task(s) without measurement, done with an older version of the skill.)")
    print(common.SAVINGS_FORMULA + "\n" + fixed_cost_note())


# ---------------------------------------------------------------------------- one conversation
SKILL_BASE = re.compile(r"Base directory for this skill:\s*(\S+)")


def row_texts(row):
    """Plain texts of a transcript row (user or assistant), tool results included."""
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return [content]
    out = []
    for c in content if isinstance(content, list) else []:
        if not isinstance(c, dict):
            continue
        if isinstance(c.get("text"), str):
            out.append(c["text"])
        elif isinstance(c.get("content"), str):
            out.append(c["content"])
        elif isinstance(c.get("content"), list):
            out += [x.get("text", "") for x in c["content"] if isinstance(x, dict)]
    return out


def is_skill_file(path):
    """The skill's instructions and reference files, and models.md: what using the skill makes Claude read.
    Its scripts are not (reading them is working on the skill, not using it)."""
    path = os.path.realpath(os.path.expanduser(str(path or "")))
    skill = os.path.realpath(common.SKILL_DIR)
    return (path.startswith(skill + os.sep) and path.endswith(".md")) or path == os.path.realpath(common.MODELS_MD)


def skill_loads(lines):
    """[(time, input tokens)] of what using the skill cost Claude: its instructions loaded (Skill tool or
    /local command), and the reads of its reference files and of models.md."""
    loads, invoked, injected = [], [], 0
    for row in lines:
        t = epoch(row.get("timestamp"))
        if not t or row.get("isSidechain"):
            continue
        for text in row_texts(row) if row.get("type") == "user" else []:
            m = SKILL_BASE.search(text)
            if m and os.path.basename(m.group(1).rstrip("/\\")) == "local":
                loads.append((t, tok(len(text))))
                injected += 1
            if re.search(r"<command-name>/?local</command-name>", text):
                invoked.append(t)
        content = (row.get("message") or {}).get("content")
        for c in content if row.get("type") == "assistant" and isinstance(content, list) else []:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            inp = c.get("input") or {}
            if c.get("name") == "Skill" and str(inp.get("skill", "")).split(":")[-1] == "local":
                invoked.append(t)
            elif c.get("name") == "Read" and is_skill_file(inp.get("file_path")):
                loads.append((t, tok(size(os.path.expanduser(inp["file_path"])))))
    if not injected:          # transcript without the injected text: count the instructions' file size
        skill = tok(size(os.path.join(common.SKILL_DIR, "SKILL.md")))
        loads += [(t, skill) for t in invoked]
    return loads
SAVINGS_PROMPT = re.compile(r"saved-tokens?|savings\.py|tokens?\s+saved|saved\s+tokens?", re.I)


# Running a delegation script, or reading what it left (.git/local-agents/, ~/.claude/local-agents/); not
# working on the skill itself (claude-local-agents/, grep in the scripts).
DELEGATION_CALL = re.compile(r"python[\w.]*\s+(?:-\S+\s+)*\S*(?:dispatch_task|ask_local|delegate_local)\.py"
                             r"|(?<![\w-])local-agents[/\\]")


def delegation_traffic(lines):
    """(input, output) tokens measured in the transcript for the delegations: the commands Claude wrote
    to run them or to read their reports and logs (output), and what came back (input)."""
    ids, out_chars, in_chars = set(), 0, 0
    for row in lines:
        if row.get("isSidechain"):
            continue
        content = (row.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            if row.get("type") == "assistant" and c.get("type") == "tool_use":
                inp = c.get("input") or {}
                what = str(inp.get("command") or inp.get("file_path") or "")
                if c.get("name") in ("Bash", "Read") and DELEGATION_CALL.search(what):
                    ids.add(c.get("id"))
                    out_chars += len(json.dumps(inp, ensure_ascii=False))
            elif row.get("type") == "user" and c.get("type") == "tool_result" and c.get("tool_use_id") in ids:
                body = c.get("content")
                in_chars += len(body) if isinstance(body, str) else sum(
                    len(x.get("text", "")) for x in body or [] if isinstance(x, dict))
    return tok(in_chars), tok(out_chars)


def epoch(ts):
    try:
        return calendar.timegm(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def prompt_text(row):
    """Text of a prompt typed by the user, on one line, or None (tool results, meta lines, subagents)."""
    raw = prompt_raw(row)
    return " ".join(raw.split()) or None if raw else None


def prompt_raw(row):
    """Text of a prompt typed by the user with its line breaks ("/local <its arguments>" for a command),
    or None (tool results, meta lines, subagents)."""
    if row.get("type") != "user" or row.get("isMeta") or row.get("isSidechain"):
        return None
    content = (row.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            return None
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    if not isinstance(content, str) or not content.strip():
        return None
    if content.lstrip().startswith(("<local-command", "Caveat:", "<task-notification>")):
        return None
    cmd = re.search(r"<command-name>(.*?)</command-name>", content, re.S)
    if cmd:
        args = re.search(r"<command-args>(.*?)</command-args>", content, re.S)
        content = cmd.group(1).strip() + (" " + args.group(1).strip() if args and args.group(1).strip() else "")
    content = re.sub(r"<system-reminder>.*?</system-reminder>", "", content, flags=re.S)
    return content.strip() or None


def transcript():
    """Path of the current conversation's transcript: the Claude Code session given by the environment,
    else the most recently updated one for this folder."""
    base = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(common.HOME, ".claude"), "projects")
    sid = os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
    if sid:
        found = glob.glob(os.path.join(base, "*", sid + ".jsonl"))
        if found:
            return found[0]
    files = sorted(glob.glob(os.path.join(base, "*", "*.jsonl")), key=os.path.getmtime, reverse=True)[:15]
    here = os.path.realpath(os.getcwd())
    for path in files:
        for row in read_jsonl_head(path, 30):
            cwd = row.get("cwd")
            if cwd and (here == os.path.realpath(cwd) or here.startswith(os.path.realpath(cwd) + os.sep)):
                return path
    return files[0] if files else None


def transcripts_here(limit=15):
    """The transcripts of this folder, the current conversation's first, then the most recently updated."""
    base = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(common.HOME, ".claude"), "projects")
    here = os.path.realpath(os.getcwd())
    out = [p for p in [transcript()] if p]
    for path in sorted(glob.glob(os.path.join(base, "*", "*.jsonl")), key=os.path.getmtime, reverse=True)[:limit]:
        if path not in out and any(row.get("cwd") and related(row["cwd"], here) for row in read_jsonl_head(path, 30)):
            out.append(path)
    return out


def user_request(start):
    """The latest prompt of the user (this conversation first) whose text, or whose arguments for a
    /command, starts with `start` (case and spacing ignored): given to an agent without Claude retyping it."""
    want = " ".join(str(start).split()).lower()
    if not want:
        return None
    for path in transcripts_here():
        try:
            rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
        except (OSError, ValueError):
            continue
        for row in reversed(rows):
            raw = prompt_raw(row)
            if not raw:
                continue
            args = raw.split(None, 1)[1] if raw.startswith("/") and len(raw.split(None, 1)) > 1 else ""
            for text in (raw, args):
                if text and " ".join(text.split()).lower().startswith(want):
                    return text
    return None


def read_jsonl_head(path, limit):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= limit:
                    break
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return rows


def related(a, b):
    a, b = os.path.realpath(a), os.path.realpath(b)
    return a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep)


def summarize(rows):
    tasks = sum(1 for r in rows if r.get("kind") == "task")
    fixes = sum(1 for r in rows if r.get("kind") == "manual fix")
    questions = len(rows) - tasks - fixes
    s_in, s_out = sum(r.get("in", 0) for r in rows), sum(r.get("out", 0) for r in rows)
    what = ", ".join(x for x in (f"{tasks} task(s)" if tasks else "", f"{questions} question(s)" if questions else "",
                                 f"{fixes} finished by hand" if fixes else "") if x)
    return what or "no delegation", s_in, s_out


def conversation():
    root = common.git(["rev-parse", "--show-toplevel"], os.getcwd()).strip()
    if root:
        common.record_manual_fixes(os.path.realpath(root))
    path = transcript()
    if not path:
        sys.exit("No Claude Code conversation found (transcripts in ~/.claude/projects).")
    lines = read_jsonl(path)
    cwd = next((r["cwd"] for r in lines if r.get("cwd")), os.getcwd())
    stamps = [epoch(r["timestamp"]) for r in lines if r.get("timestamp")]
    stamps = [s for s in stamps if s]
    if not stamps:
        sys.exit("Empty conversation.")
    start = min(stamps)
    prompts = [(epoch(r.get("timestamp")), prompt_text(r)) for r in lines]
    prompts = [(t, txt) for t, txt in prompts if t and txt]
    ledger = [r for r in common.ledger_rows() if r.get("ts", 0) >= start and related(r.get("cwd", ""), cwd)]
    what, s_in, s_out = summarize(ledger)
    loads = skill_loads(lines)
    skill_in = sum(k for _, k in loads)
    local = lambda t: datetime.datetime.fromtimestamp(t).strftime("%H:%M")  # noqa: E731
    print(f"This conversation (since {local(start)}, {len(prompts)} prompt(s)): {what}.")
    print("  Delegations: " + common.savings_line(s_in, s_out) + "."
          + (" Manual fixes after failed tasks are counted as a cost." if any(r.get("kind") == "manual fix" for r in ledger) else ""))
    if loads:
        print(f"  Cost of the skill itself (instructions loaded, reference files and models.md read): "
              f"{len(loads)} time(s), ~{skill_in:,} input tokens (a cost).")
    net = s_in - skill_in + common.OUTPUT_WEIGHT * s_out
    print("  Net savings: " + common.savings_line(s_in - skill_in, s_out) + "."
          + (" The delegations did not pay for the skill's cost: delegate only large reads and code in volume."
             if net < 0 and loads else ""))
    # Measured: what the delegations really cost Claude in this transcript (reports, logs and outputs it read,
    # commands it wrote), instead of the estimate. Needs the estimated cost recorded with each delegation.
    m_in, m_out = delegation_traffic(lines)
    if ledger and (m_in or m_out) and all("cost_in" in r for r in ledger if r.get("kind") != "manual fix"):
        c_in, c_out = sum(r.get("cost_in", 0) for r in ledger), sum(r.get("cost_out", 0) for r in ledger)
        print(f"  Measured in this conversation: ~{n(m_in)} input tokens read and ~{n(m_out)} output tokens written "
              f"around the delegations (estimate: ~{n(c_in)} and ~{n(c_out)}).")
        r_in, r_out = s_in + c_in - m_in - skill_in, s_out + c_out - m_out
        print("  Net savings, measured: " + common.savings_line(r_in, r_out) + "."
              + (" Claude spent more around the delegations than they saved: fewer, larger delegations; "
                 "read only the reports." if r_in + common.OUTPUT_WEIGHT * r_out < 0 else ""))
    elif m_in or m_out:
        print(f"  Measured in this conversation: ~{n(m_in)} input tokens read and ~{n(m_out)} output tokens written "
              "around the delegations (some delegations predate cost tracking: no measured net).")
    outcomes = common.outcomes_line([r for r in ledger if r.get("kind") == "task"])
    if outcomes:
        print("  " + outcomes)
    # The last 3 prompts, the current one excluded when it is this very request.
    ends = [t for t, _ in prompts[1:]] + [time.time() + 1]
    windows = list(zip(prompts, ends))
    if windows and SAVINGS_PROMPT.search(windows[-1][0][1]):
        windows = windows[:-1]
    if windows:
        print("\nLast 3 prompts:")
    for (t, txt), end in windows[-3:]:
        rows = [r for r in ledger if t <= r.get("ts", 0) < end]
        what, p_in, p_out = summarize(rows)
        p_skill = sum(k for s, k in loads if t <= s < end)
        short = txt if len(txt) <= 70 else txt[:67] + "…"
        print(f"  {local(t)} \"{short}\": {what}"
              + (f" → {common.savings_line(p_in - p_skill, p_out)}" if rows or p_skill else "")
              + (f", skill cost ~{p_skill:,} included" if p_skill else "") + ".")
    print("\n" + common.SAVINGS_FORMULA + "\n" + fixed_cost_note(conversation=True))


def main():
    common.utf8_stdio()
    if "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__.strip())
        return
    if "--conversation" in sys.argv:
        conversation()
    else:
        repository(set(a for a in sys.argv[1:] if not a.startswith("-")))


if __name__ == "__main__":
    main()
