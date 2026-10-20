#!/usr/bin/env python3
"""Runs a local agent: non-interactive Claude Code connected to a local Ollama model.
Uses no Claude tokens. macOS, Linux and Windows.

Usage: delegate_local.py "instruction" [options]       (instruction "-" = read from stdin)
       delegate_local.py "question about the code" --readonly   (read-only exploration)
Options (else LOCAL_* environment variables, else defaults):
  --readonly         exploration: Read Glob Grep tools only, concise answer with file:line
  --model M          local Ollama model (default: the one in models.md)          LOCAL_MODEL
  --tools "Read ..." agent tools (default: Read Edit Write Glob Grep)            LOCAL_TOOLS
  --write "a b/"     the only writable files/folders                             LOCAL_WRITE_PATHS
  --no-read "d/ f"   files/folders the agent must not open (facts given in the instruction)  LOCAL_NO_READ
  --think V          off | on | low | medium | high (default off)                LOCAL_THINK
  --timeout MIN      limit per attempt in minutes, 0 = unlimited (default 20)    LOCAL_TIMEOUT
  --max-turns N      maximum steps (default 30)                                  LOCAL_MAX_TURNS

Security: Claude Code --restricted mode (no user settings, no MCP), limited tools, no permission
prompts (anything not allowed is refused), guard.py on every action, Claude Code sandbox for the
shell (no network, .git protected).
Exit codes: 0 done, 3 time limit, 4 step limit, 1 error, 2 usage.
"""
import json
import re
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

_CLAUDE_HELP = None


def claude_help(claude):
    global _CLAUDE_HELP
    if _CLAUDE_HELP is None:
        _, out, err, _ = common.run([claude, "--help"], timeout=30)
        _CLAUDE_HELP = out + err
    return _CLAUDE_HELP


def options(argv=None):
    env = os.environ.get
    ap = common.Parser(__doc__)
    ap.add_argument("task")
    ap.add_argument("--readonly", action="store_true")
    ap.add_argument("--model", default=env("LOCAL_MODEL", ""))
    ap.add_argument("--tools", default=env("LOCAL_TOOLS", "Read Edit Write Glob Grep"))
    ap.add_argument("--write", default=env("LOCAL_WRITE_PATHS", ""))
    ap.add_argument("--no-read", default=env("LOCAL_NO_READ", ""))
    ap.add_argument("--think", default=env("LOCAL_THINK", "off"))
    ap.add_argument("--timeout", default=env("LOCAL_TIMEOUT", "20"))
    ap.add_argument("--max-turns", default=env("LOCAL_MAX_TURNS", "30"))
    return ap.parse_args(argv)


def parse_minutes(raw):
    raw = str(raw).strip().lower().replace(",", ".")
    if raw in ("0", "", "unlimited", "none"):
        return 0.0
    return float(raw)


def build_settings(model, root, stop_hook=False):
    guard = os.path.join(common.SCRIPTS, "guard.py")
    settings = {
        "env": {
            "ANTHROPIC_BASE_URL": common.HOST, "ANTHROPIC_AUTH_TOKEN": "ollama", "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_MODEL": model, "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model, "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "CLAUDE_CODE_SUBAGENT_MODEL": model, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_ADDOPTS": "-p no:cacheprovider",
        },
        "permissions": {"deny": [
            "WebFetch", "WebSearch", "Agent", "Task",
            *[f"Read(**/{p})" for p in common.SECRET_PATTERNS],            # also applies to Grep and Glob
            "Read(~/.ssh/**)", "Read(~/.aws/**)", "Read(~/.gnupg/**)", "Read(~/.claude/**)",
            "Edit(./.git/**)", "Edit(./.claude/**)",
            "Bash(sudo *)", "Bash(curl *)", "Bash(wget *)", "Bash(ssh *)", "Bash(git push *)", "Bash(rm -rf *)",
        ]},
        "sandbox": {
            "enabled": True, "allowUnsandboxedCommands": False, "failIfUnavailable": True,
            "network": {"allowedDomains": []},
            "filesystem": {
                "denyRead": [f"~/{x}" for x in common.SECRET_DIRS + common.SECRET_FILES]
                            + [common.hidden_dir()]                     # held-out tests (--hidden-check)
                            + common.project_secrets(root),             # .env & co., even through grep -r
                "denyWrite": [os.path.join(root, ".git")],
            },
        },
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": f"{common.quote(common.PY)} -I {common.quote(guard)}", "timeout": 120},
        ]}]},
    }
    if stop_hook:
        # Native Claude Code mechanism: when the agent says it is done, stop_check.py runs the checks;
        # on failure the agent keeps working in the same session (see stop_check.py).
        try:
            check_s = int(os.environ.get("LOCAL_CHECK_TIMEOUT", "300"))
        except ValueError:
            check_s = 300
        stop = os.path.join(common.SCRIPTS, "stop_check.py")
        settings["hooks"]["Stop"] = [{"hooks": [
            {"type": "command", "command": f"{common.quote(common.PY)} -I {common.quote(stop)}",
             "timeout": 2 * check_s + 120},
        ]}]
    return settings


ENV_DIRS = re.compile(r"(^|[\s/'\"])(\.?venv|env|site-packages|dist-packages|node_modules|\.tox|__pypackages__)(/|\b)")


def describe_tool(name, inp, root):
    """Short description of an agent's action, for the live progress."""
    def rel(path):
        try:
            r = os.path.relpath(str(path), root)
            if r.startswith(".."):      # /var vs /private/var (macOS), or another symlinked path to the project
                r = os.path.relpath(os.path.realpath(str(path)), os.path.realpath(root))
            return r if not r.startswith("..") else str(path)
        except ValueError:
            return str(path)
    if name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        verb = {"Read": "reads", "Write": "writes", "NotebookEdit": "edits"}.get(name, "edits")
        what = rel(inp.get("file_path") or inp.get("notebook_path") or "?")
        if name == "Read" and inp.get("offset"):
            what += f" (from line {inp.get('offset')})"
        return f"{verb} {what}"
    if name in ("Grep", "Glob"):
        return f"searches {inp.get('pattern', '')!r}" + (f" in {rel(inp['path'])}" if inp.get("path") else "")
    if name == "Bash":
        cmd = " ".join(str(inp.get("command", "")).split())
        return "runs " + (cmd[:70] + "…" if len(cmd) > 70 else cmd)
    return name


UNKNOWN_TOOL_STOP = 3     # calls in a row to a tool the agent does not have before its session is stopped


class LiveProgress:
    """Turns Claude Code's stream-json events into progress lines; also watches the Stop hook's log
    (checks run in the agent's session) to say when a check failed and the agent fixes it."""

    def __init__(self, root, stop_env, actions_log=None, show=True, tools=None):
        self.root, self.stop_log, self.seen = root, (stop_env or {}).get("LOCAL_STOP_LOG"), 0
        self.tools, self.unknown, self.abort = set(tools or ()), 0, ""
        self.rounds = int((stop_env or {}).get("LOCAL_STOP_ROUNDS") or 0)
        self.actions_log, self.show, self.t0 = actions_log, show, time.time()
        self.reads, self.loops, self.edits, self.steps, self.env_looks = {}, {}, {}, 0, 0

    def say(self, msg):
        if self.show:
            common.progress(msg)

    def hook_rows(self):
        if not self.stop_log:
            return
        try:
            rows = [json.loads(l) for l in open(self.stop_log, encoding="utf-8") if l.strip()]
        except (OSError, ValueError):
            return
        for row in rows[self.seen:]:
            if row.get("blocked"):
                what = (row.get("check") or "").strip() or ("hidden check failed" if row.get("hidden") else "check failed")
                self.say(f"  ✗ {what[:90]} → the agent fixes it in the same session "
                         f"(round {row.get('round', '?')}/{self.rounds})")
            elif row.get("passed"):
                self.say("  ✓ checks passed")
            elif row.get("allowed"):
                why = {"rounds exhausted": "its fix rounds are used up", "research": "it asks for information",
                       "context full": "its context is full",
                       "no progress": "same error and no change since the last round"}.get(row["allowed"], row["allowed"])
                self.say(f"  · the agent stops: {why}")
        self.seen = len(rows)

    def __call__(self, line):
        self.hook_rows()
        event = json.loads(line)
        if event.get("type") != "assistant":
            return
        for part in (event.get("message") or {}).get("content") or []:
            if isinstance(part, dict) and part.get("type") == "tool_use":
                name, inp = part.get("name", "?"), part.get("input") or {}
                desc = describe_tool(name, inp, self.root)
                self.steps += 1
                # Going in circles = the same read (file and starting line) again and again with no change to
                # that file in between; reading other parts of a file one is editing is normal work.
                # The same search again and again counts the same way.
                if name in ("Read", "Grep", "Glob"):
                    self.reads[desc] = self.reads.get(desc, 0) + 1
                    self.loops[desc] = max(self.loops.get(desc, 0), self.reads[desc])
                elif name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                    edited = desc.split(" ", 1)[-1]
                    for key in [k for k in self.reads if k.split(" (from line")[0] == "reads " + edited]:
                        del self.reads[key]
                    self.edits[edited] = self.edits.get(edited, 0) + 1
                # Inspecting the environment (virtualenv, installed packages) instead of the task; running the
                # project's interpreter is normal.
                if name != "Bash" and ENV_DIRS.search(desc):
                    self.env_looks += 1
                self.say("  · " + desc)
                if self.actions_log:
                    secs = int(time.time() - self.t0)
                    try:
                        with open(self.actions_log, "a", encoding="utf-8") as f:
                            f.write(f"[{secs // 60:02d}:{secs % 60:02d}] {desc}\n")
                    except OSError:
                        pass
                # A tool it does not have (e.g. a shell to run the tests) only gets an error back: a model
                # that keeps calling it would do so until the step limit. Stop the session instead.
                self.unknown = self.unknown + 1 if self.tools and name not in self.tools else 0
                if self.unknown >= UNKNOWN_TOOL_STOP:
                    self.abort = f"kept calling {name}, a tool it does not have ({self.unknown} times in a row)"
                    self.say(f"  ✗ the agent {self.abort}: stopped")
                    return True

    def circles(self):
        """A note when the agent kept re-reading the same part of a file, unchanged (going in circles)."""
        target, n = max(self.loops.items(), key=lambda x: x[1], default=("", 0))
        return f"{target[6:] if target.startswith('reads ') else target} read {n} times" if n >= 4 else ""

    def wander(self):
        """A note when the agent kept looking into the environment (virtualenv, packages) instead of the task."""
        return f"looked into the environment (.venv, packages) {self.env_looks} times" if self.env_looks >= 5 else ""

    def churn(self):
        """A note when the agent edited one file in many tiny steps (each one costs a full turn)."""
        target, n = max(self.edits.items(), key=lambda x: x[1], default=("", 0))
        return f"{target} edited {n} times (many tiny edits?)" if n >= 20 else ""


# Harmless lines of Claude Code's output when it talks to a local model: kept out of logs and reports.
NOISE = re.compile(r"unrecognized_model|claude\.ai connectors are disabled|connectors? (are |is )?disabled", re.I)


def run_agent(task, opts, security_log=None, stats_file=None, cwd=None, stop_env=None, live=False,
              actions_log=None, show=None):
    """Runs the agent. Returns a dict: code, status, text, stats, notes, circles. live: follow its actions
    as they happen (progress lines unless show is False; one line per action in actions_log)."""
    cwd = os.path.abspath(cwd or os.getcwd())
    root = (common.git(["rev-parse", "--show-toplevel"], cwd).strip() or cwd)
    root = os.path.realpath(root)
    if root == os.path.realpath(common.HOME) or os.path.dirname(root) == root:
        return {"code": 1, "status": "error", "text": f"ERROR: run the agent from a project folder, not from {root}.", "stats": {}, "notes": ""}

    try:
        minutes = parse_minutes(opts.timeout)
        max_turns = int(opts.max_turns)
    except ValueError:
        return {"code": 2, "status": "error", "text": "ERROR: --timeout (minutes) and --max-turns must be numbers.", "stats": {}, "notes": ""}

    # Model: any local Ollama model that supports tool calling.
    notes = []
    try:
        model = common.default_model(opts.model)
        p = common.check_model(model, need_tools=True)
        if p["context"] and p["context"] < common.AGENT_MIN_CTX:
            raise common.ModelError(f"'{model}' has a maximum context of {p['context']} tokens: too small for an agent.")
        if p["context"] and p["context"] < common.AGENT_GOOD_CTX:
            notes.append(f"maximum context of {p['context']} tokens: small tasks only")
        think_value, note = common.think_param(p, opts.think)
        if note:
            notes.append(note)
    except common.ModelError as e:
        return {"code": 1, "status": "error", "text": f"ERROR: {e}", "stats": {}, "notes": ""}
    budget = ("0" if think_value in (None, False) else
              os.environ.get("LOCAL_THINK_BUDGET") or str(common.THINK_BUDGETS.get(str(opts.think).lower(), 16000)))

    claude = shutil.which("claude")
    if not claude:
        return {"code": 1, "status": "error", "text": "ERROR: Claude Code (the 'claude' command) not found.", "stats": {}, "notes": ""}

    if not security_log:
        gitdir = common.git_common_dir(cwd)
        logdir = os.path.join(gitdir, "local-agents") if gitdir else tempfile.gettempdir()
        os.makedirs(logdir, exist_ok=True)
        security_log = os.path.join(logdir, "security.log" if gitdir else "local-agents-security.log")

    readonly = getattr(opts, "readonly", False)
    tools = ["Read", "Glob", "Grep"] if readonly else opts.tools.split()
    scope = f"only: {opts.write}" if opts.write.strip() else "the whole project"
    if readonly:
        prompt = (f"{task}\n\nRules: you are exploring this project read-only (you change nothing). "
                  "Answer concisely (300 words maximum). For every important claim, cite the source as "
                  "file:line. If you are not sure, say so instead of guessing; if the information is not in "
                  "the project, say so. When describing what code or tests expect, give the default values "
                  "and edge cases the assertions imply (e.g. a default argument a test relies on). "
                  "Your memory is limited: search with Grep first, then read only the "
                  "relevant lines of large files (Read with offset and limit).")
    else:
        prompt = (f"{task}\n\nRules: work only inside this project. You may modify {scope}.\n"
                  "Your memory is limited: do not read whole large files; search with Grep, then read only the "
                  "lines you need (Read with offset and limit), and change files with small targeted edits "
                  "rather than rewriting them whole.\n"
                  "Write early and in parts: a new file of more than ~100 lines is created with a first Write "
                  "(its first part), then one Edit per function or section, so that your work is kept even if "
                  "time runs out.\n"
                  "Do only what is asked. If an action is blocked by security, do not try to work around "
                  "it: do without it, or explain in your summary what you were missing.\n"
                  "If you are stuck on information you do not have (recent function or API, unknown error "
                  "message, library version), do not make it up: end your answer with a line "
                  "\"RESEARCH: <precise question>\".\n"
                  "At the end, reply with a short summary of the files created or modified.")

    fd, settings_path = tempfile.mkstemp(prefix="local-agent-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(build_settings(model, root, stop_hook=bool(stop_env)), f)

    cmd = [claude, "-p"]
    if "--restricted" in claude_help(claude):
        cmd.append("--restricted")
    if "--no-session-persistence" in claude_help(claude):
        # No transcript in ~/.claude/projects: it would pile up, and pass for the user's conversation there.
        cmd.append("--no-session-persistence")
    cmd += ["--strict-mcp-config", "--permission-mode", "dontAsk", "--model", model,
            "--settings", settings_path, "--tools", ",".join(tools), "--max-turns", str(max_turns),
            "--output-format", "stream-json" if live else "json"] + (["--verbose"] if live else []) + [
            "--allowedTools"] + tools

    env = common.scrub_env(os.environ)      # no tokens, keys or cloud credentials for local agents
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "ANTHROPIC_API_KEY", "MAX_THINKING_TOKENS"):
        env.pop(k, None)
    env.update({
        "ANTHROPIC_BASE_URL": common.HOST, "ANTHROPIC_AUTH_TOKEN": "ollama", "ANTHROPIC_API_KEY": "",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "MAX_THINKING_TOKENS": budget,
        "LOCAL_MODEL": model, "LOCAL_PROJECT_ROOT": root, "LOCAL_WRITE_PATHS": opts.write,
        "LOCAL_NO_READ": getattr(opts, "no_read", ""),
        "LOCAL_READONLY": "1" if readonly else "0",
        "LOCAL_SECURITY_LOG": security_log, "LOCAL_REVIEW_MODEL": os.environ.get("LOCAL_REVIEW_MODEL", model),
        "OLLAMA_HOST_URL": common.HOST, "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_ADDOPTS": "-p no:cacheprovider",
    })
    no_proxy = ",".join(x for x in (env.get("NO_PROXY", ""), "localhost,127.0.0.1,::1") if x)
    env["NO_PROXY"] = env["no_proxy"] = no_proxy                       # Ollama is local: never via a proxy
    # Claude Code assumes a context window for unknown model ids: give it Ollama's real one, so that it
    # compacts the conversation before Ollama would silently cut the beginning (the instructions).
    env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(common.agent_context(model))
    # Room kept free for one answer: small, otherwise Claude Code compacts the conversation (with the
    # local model, losing details) once half of a 64k context is used.
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(common.agent_output_reserve(budget, model))
    wt_path = common.worktree_pythonpath(root)
    if wt_path:
        env["PYTHONPATH"] = os.pathsep.join(wt_path + [x for x in [env.get("PYTHONPATH", "")] if x])
    if stop_env:
        env.update({k: str(v) for k, v in stop_env.items()})

    # Memory guard: if other parallel agents use the memory, WAIT rather than switching to a
    # smaller model (quality before speed).
    def waiting(total, usable, busy):
        print(f"(waiting: {model} does not fit in memory alongside {', '.join(busy)} "
              f"— {total:.0f} GB needed for {usable:.0f} GB usable; the task waits)", file=sys.stderr, flush=True)
    try:
        max_wait = float(os.environ.get("LOCAL_WAIT_MAX", "120")) * 60
    except ValueError:
        max_wait = 7200
    token, waited = common.acquire_memory(model, max_wait_s=max_wait, on_wait=waiting)
    if token is None:
        os.remove(settings_path)
        return {"code": 1, "status": "error", "stats": {}, "notes": "",
                "text": f"ERROR: memory still used by other agents after {waited / 60:.0f} min."}
    if waited > 30:
        notes.append(f"waited {waited / 60:.1f} min for memory to free up")

    start = time.time()
    try:
        watcher = None
        if live:
            watcher = LiveProgress(root, stop_env, actions_log, common.progress_on() if show is None else show, tools)
            code, out, err, timed_out = common.run_stream(cmd, cwd=cwd, env=env, input_text=prompt,
                                                          timeout=minutes * 60 if minutes > 0 else None,
                                                          on_line=watcher)
            watcher.hook_rows()
        else:
            code, out, err, timed_out = common.run(cmd, cwd=cwd, env=env, input_text=prompt,
                                                   timeout=minutes * 60 if minutes > 0 else None)
    finally:
        common.record_loaded(agent_model=model)   # actual memory and context, for the next choices
        common.release_memory(token)
        try:
            os.remove(settings_path)
        except OSError:
            pass
    elapsed = time.time() - start

    data = parse_result(out)
    usage = data.get("usage") or {}
    tokens_in = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    tokens_out = int(usage.get("output_tokens") or 0)
    if timed_out:
        status, rc = f"stopped: {minutes:g}-min limit reached", 3
    elif watcher and watcher.abort:
        status, rc = f"stopped: {watcher.abort}", 4
    elif data.get("subtype") == "error_max_turns":
        status, rc = "stopped: step limit reached", 4
    elif data and not data.get("is_error"):
        status, rc = "done", 0
    else:
        status, rc = "error", 1

    text = (data.get("result") or "").strip()
    if not text and not data and out.strip():
        text = stream_summary(out)
    err = "\n".join(l for l in err.splitlines() if not NOISE.search(l))
    if rc == 1 and err.strip():
        text = (text + "\n" + err.strip()[-1500:]).strip()
    text = "\n".join(l for l in text.splitlines() if not NOISE.search(l))
    stats = {"model": model, "think": opts.think, "status": status, "turns": data.get("num_turns"),
             "duration_s": round(elapsed), "tokens_in": tokens_in, "tokens_out": tokens_out}
    if stats_file:
        with open(stats_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(type="agent", **stats), ensure_ascii=False) + "\n")
    return {"code": rc, "status": status, "text": text, "stats": stats, "notes": "; ".join(notes),
            "circles": watcher.circles() if watcher else "", "churn": watcher.churn() if watcher else "",
            "wander": watcher.wander() if watcher else ""}


def parse_result(out):
    out = (out or "").strip()
    if not out:
        return {}
    try:
        obj = json.loads(out)
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        pass
    for line in reversed(out.splitlines()):
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and obj.get("type") == "result":
                return obj
        except ValueError:
            continue
    return {}


def stream_summary(out, keep=6):
    """What a session cut off before its final answer did, from its stream-json output: the files it
    wrote and its last actions and messages (never the raw JSON events: thinking counters and the like)."""
    actions, written, plain = [], [], []
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            if line.strip() and not re.search(r'"(type|session_id|uuid)"\s*:', line):    # not a cut JSON event
                plain.append(line.strip())
            continue
        if not isinstance(ev, dict) or ev.get("type") != "assistant":
            continue
        for block in (ev.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                actions.append("said: " + " ".join(block["text"].split())[:160])
            elif block.get("type") == "tool_use":
                inp = block.get("input") or {}
                target = inp.get("file_path") or inp.get("path") or inp.get("pattern") or inp.get("command") or ""
                actions.append(f"{block.get('name')} {str(target)[:120]}".strip())
                if block.get("name") in ("Write", "Edit") and inp.get("file_path") and inp["file_path"] not in written:
                    written.append(inp["file_path"])
    if not actions and not written:
        return "\n".join(plain[-keep:])[-1500:] or "(no final answer: the session was cut off before any action)"
    rows = ["(no final answer: the session was cut off)",
            "Files written: " + (", ".join(written) if written else "none")]
    return "\n".join(rows + ["Last actions:"] + [f"- {a}" for a in actions[-keep:]])


def agent_line(r):
    s = r["stats"]
    if not s:
        return ""
    return (f"[agent] {s['model']} | {r['status']} | steps {s['turns'] or '?'} | {s['duration_s'] / 60:.1f} min | "
            f"tokens processed locally: {s['tokens_in'] or '?'} read, {s['tokens_out'] or '?'} written")


def record_exploration(task, r, sec):
    """Volumes for savings.py: what the agent read (Claude did not have to read it) and the answer."""
    root = (common.git(["rev-parse", "--show-toplevel"], os.getcwd()).strip() or os.getcwd())
    gitdir = common.git_common_dir(os.getcwd())
    read = set()
    try:
        for line in open(sec, encoding="utf-8"):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("tool") == "Read" and row.get("decision", "").startswith("ALLOWED"):
                read.add(row.get("detail", ""))
        os.remove(sec)
    except OSError:
        pass
    chars = 0
    for rel in read:
        try:
            chars += len(open(os.path.join(root, rel), encoding="utf-8", errors="replace").read())
        except OSError:
            pass
    if gitdir and r["code"] == 0:
        try:
            os.makedirs(os.path.join(gitdir, "local-agents"), exist_ok=True)
            with open(os.path.join(gitdir, "local-agents", "questions.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "explore", "model": r["stats"].get("model", ""), "prompt": len(task),
                                    "content": chars, "answer": len(r["text"])}) + "\n")
        except OSError:
            pass
    if r["code"] == 0:
        cost = common.question_cost(len(task), len(r["text"]))
        common.ledger_add("exploration", "", r["stats"].get("model", ""), "answered",
                          *common.question_savings(len(task), chars, len(r["text"])), cost_in=cost[0], cost_out=cost[1])


def main():
    common.utf8_stdio()
    opts = options()
    task = common.read_stdin_all() if opts.task == "-" else opts.task
    if not task.strip():
        print("ERROR: empty instruction.", file=sys.stderr)
        sys.exit(2)
    cwd = os.getcwd()
    top = os.path.realpath(common.git(["rev-parse", "--show-toplevel"], cwd).strip() or cwd)
    opts.write = common.normalize_write(opts.write, top, cwd)
    sec = None
    if opts.readonly:  # security log specific to this exploration, to measure what was read
        fd, sec = tempfile.mkstemp(prefix="local-explore-", suffix=".log")
        os.close(fd)
    r = run_agent(task, opts, security_log=sec, stats_file=os.environ.get("LOCAL_STATS_FILE"))
    if sec:
        record_exploration(task, r, sec)
    if r["notes"]:
        print(f"(note: {r['notes']})")
    print(r["text"])
    if opts.readonly and r["code"] == 0 and r["text"].strip():
        print("\n" + common.citations_note(r["text"], top))     # Claude rereads only the doubtful ones
    line = agent_line(r)
    if line:
        print("\n" + line)
    sys.exit(r["code"])


if __name__ == "__main__":
    main()
