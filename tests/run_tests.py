#!/usr/bin/env python3
"""Tests of the /local skill without Ollama or Claude: a fake Ollama server and a fake `claude` replay
scenarios (a model that succeeds, one that fails, one that asks for research, dangerous actions…).

Usage: python3 tests/run_tests.py [-k pattern] [-v]
Linux and macOS (the fake `claude` is an executable script). Duration: ~50 s.
"""
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = os.path.join(ROOT, "local", "scripts")
PY = sys.executable
VERBOSE = "-v" in sys.argv
PATTERN = sys.argv[sys.argv.index("-k") + 1] if "-k" in sys.argv else ""
GB = 2**30

# --------------------------------------------------------------------------- fake Ollama server
MODELS = {
    "qwen3.6:35b-a3b": {"caps": ["completion", "tools", "thinking"], "think": {"values": [False, True], "default": True},
                        "ctx": 262144, "family": "qwen35moe", "gb": 24},
    "gpt-oss:20b": {"caps": ["completion", "tools", "thinking"], "think": {"values": ["low", "medium", "high"],
                    "default": "medium"}, "ctx": 131072, "family": "gptoss", "gb": 13},
    "devstral-small-2:24b": {"caps": ["completion", "tools"], "ctx": 393216, "family": "mistral3", "gb": 15,
                             "arch": {"general.architecture": "mistral3", "mistral3.block_count": 40,
                                      "mistral3.attention.head_count_kv": 8, "mistral3.attention.key_length": 128,
                                      "mistral3.attention.value_length": 128}},
    "gemma3:12b": {"caps": ["completion", "vision"], "ctx": 131072, "family": "gemma3", "gb": 8},
    "nomic-embed-text:latest": {"caps": ["embedding"], "ctx": 2048, "family": "nomic-bert", "gb": 0.3},
    "glm-4.7:cloud": {"caps": ["completion", "tools"], "ctx": 200000, "family": "glm", "remote": True},
}


LOADED = {"qwen3.6:35b-a3b": 28}          # models in memory (GB), as /api/ps reports them


class FakeOllama(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _j(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            self._j({"models": [dict({"name": n, "model": n, "size": int(m.get("gb", 0) * GB)},
                                     **({"remote_host": "https://ollama.com"} if m.get("remote") else {}))
                                for n, m in MODELS.items()]})
        elif self.path == "/api/ps":
            self._j({"models": [{"name": n, "context_length": 65536, "size": gb * GB} for n, gb in LOADED.items()]})
        else:
            self._j({"version": "0.20.1"})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        name = req.get("model", "")
        m = MODELS.get(name if ":" in name else name + ":latest")
        if self.path == "/api/generate" and req.get("keep_alive") == 0:
            LOADED.pop(name if ":" in name else name + ":latest", None)
            return self._j({"done": True, "done_reason": "unload"})
        if self.path == "/api/show":
            if not m:
                return self._j({"error": "not found"}, 404)
            out = {"capabilities": m["caps"], "details": {"family": m["family"]},
                   "model_info": dict({m["family"] + ".context_length": m["ctx"]}, **m.get("arch", {}))}
            if "think" in m:
                out["thinking"] = m["think"]
            if m.get("remote"):
                out["remote_host"] = "https://ollama.com"
            return self._j(out)
        if self.path == "/api/chat":
            if not m:
                return self._j({"error": "not found"}, 404)
            if "think" in req and "thinking" not in m["caps"]:
                return self._j({"error": "does not support thinking"}, 400)
            c = req["messages"][0]["content"]
            if "demanding code reviewer" in c:
                ans = "NEEDS WORK\n- The function returns a hard-coded 5 to pass the test." if "return 5" in c else "APPROVED"
                if "# style: markdown" in c:            # verdict wrapped in Markdown, as models often do
                    ans = "**Verdict: APPROVED**\nClean and correct."
                if "# style: chatty" in c:              # no verdict at all
                    ans = "The code looks reasonable overall."
                if "# style: notyet" in c:              # a negation around the word APPROVED
                    ans = "NOT YET APPROVED: the edge cases are missing."
            elif "Command:" in c:                         # security expert
                cmd = c.split("Command:")[1].split("\n")[0]
                ans = "DENY: writes files" if (">" in cmd or "black" in cmd) else "ALLOW"
            else:
                ans = f"[{name}] received {len(c)} characters"
            return self._j({"message": {"content": ans}, "eval_count": 120, "eval_duration": 2 * 10**9,
                            "prompt_eval_count": 3000, "prompt_eval_duration": 10**9, "total_duration": 3 * 10**9})
        self._j({"error": "?"}, 404)


# --------------------------------------------------------------------------- fake claude
FAKE_CLAUDE = r'''#!{py}
# Behaves like Claude Code for what the skill relies on: PreToolUse hook before each action, and the
# Stop hook at the end of each turn (exit 2 = keep working with the hook's message as feedback).
import json, os, subprocess, sys, time
a = sys.argv
if "--help" in a:
    print("--restricted --no-session-persistence --tools --strict-mcp-config --permission-mode --settings "
          "--allowedTools"); sys.exit()
if "--version" in a:
    print("2.1.294 (Claude Code)"); sys.exit()
prompt = sys.stdin.read()
model = a[a.index("--model") + 1]
hooks = json.load(open(a[a.index("--settings") + 1], encoding="utf-8"))["hooks"]
pre = hooks["PreToolUse"][0]["hooks"][0]["command"]
stop = hooks["Stop"][0]["hooks"][0]["command"] if "Stop" in hooks else None
rules = json.load(open(os.environ["FAKE_SCENARIO"], encoding="utf-8"))
probe = os.environ.get("FAKE_SECRET_PROBE", "")
leak = bool(probe) and any(probe in v for k, v in os.environ.items() if k != "FAKE_SECRET_PROBE")
if os.environ.get("FAKE_PID_FILE"):
    open(os.environ["FAKE_PID_FILE"], "w").write(str(os.getpid()))
text, turn = prompt, 0
while True:
    rule = next((r for r in rules if r.get("model", "*") in ("*", model) and r.get("if", "") in text), {{}})
    t0 = time.time()
    time.sleep(rule.get("sleep", 0))
    tools = []
    def guard(name, inp):           # PreToolUse hook, as Claude Code calls it before every action
        if "stream-json" in a:      # live events, as Claude Code streams them
            print(json.dumps({{"type": "assistant", "message": {{"content": [
                {{"type": "tool_use", "name": name, "input": inp}}]}}}}), flush=True)
        if os.environ.get("FAKE_NO_HOOKS") == "1":
            return True
        ok = subprocess.run(pre, shell=True, input=json.dumps({{"tool_name": name, "tool_input": inp}}),
                            text=True, capture_output=True).returncode == 0
        tools.append([name, ok])
        return ok
    def write(path, content):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        open(path, "w", encoding="utf-8").write(content)
    for name, inp in rule.get("tools", []):
        guard(name, inp)
    import re as _re
    for path in _re.findall(r"(/[^\s'\"]+)", text) if rule.get("read_paths_in_prompt") else []:
        guard("Read", {{"file_path": path}})
    for path, content in rule.get("write", {{}}).items():          # Write tool
        if guard("Write", {{"file_path": os.path.abspath(path), "content": content}}):
            write(path, content)
    for path, content in rule.get("raw_write", {{}}).items():      # done by a script run through Bash
        if guard("Bash", {{"command": "python3 helper.py"}}):
            write(path, content)
    answer = rule.get("text", "Done.")
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({{"model": model, "cwd": os.getcwd(), "prompt": text, "turn": turn, "tools": tools,
                            "max_turns": a[a.index("--max-turns") + 1] if "--max-turns" in a else "",
                            "persist": "--no-session-persistence" not in a,
                            "leak": leak, "t0": t0, "t1": time.time()}}) + "\n")
    if not stop or turn >= 10:
        break
    r = subprocess.run(stop, shell=True, text=True, capture_output=True,
                       input=json.dumps({{"hook_event_name": "Stop", "stop_hook_active": turn > 0,
                                         "last_assistant_message": answer}}))
    if r.returncode != 2:
        break
    text, turn = r.stderr, turn + 1
sub = rule.get("subtype", "success")
print(json.dumps({{"type": "result", "subtype": sub, "is_error": sub != "success", "num_turns": 3 * (turn + 1),
                  "result": answer, "usage": {{"input_tokens": 9000, "output_tokens": 150}}}}))
'''

MODELS_MD = """# Local models
## Preferences
- Delegation: maximum
- Local fallback: Claude's choice
## qwen3.6:35b-a3b — DEFAULT
- Role: agent.
## gpt-oss:20b
- Role: alternative.
## devstral-small-2:24b
- Role: alternative.
"""

CALC_OK = "def add(a, b):\n    return a + b\n"
CALC_BAD = "def add(a, b):\n    return a - b\n"
CALC_CHEAT = "def add(a, b):\n    return 5\n"
CHECK = "import calc, sys\nsys.exit(0 if calc.add(2, 3) == 5 and calc.add(1, 1) == 2 else 1)\n"


class Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="local-tests-")
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.bin = os.path.join(self.tmp, "bin")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.bin)
        os.makedirs(os.path.join(self.home, ".claude", "local-agents"))
        with open(os.path.join(self.home, ".claude", "local-agents", "models.md"), "w", encoding="utf-8") as f:
            f.write(MODELS_MD)
        claude = os.path.join(self.bin, "claude")
        with open(claude, "w", encoding="utf-8") as f:
            f.write(FAKE_CLAUDE.format(py=PY))
        os.chmod(claude, 0o755)
        self.log = os.path.join(self.tmp, "fake.log")
        self.scenario = os.path.join(self.tmp, "scenario.json")
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("LOCAL_", "OLLAMA_", "CLAUDE")) and k != "ANTHROPIC_API_KEY"}
        self.env.update({"HOME": self.home, "USERPROFILE": self.home, "PATH": self.bin + os.pathsep + os.environ["PATH"],
                         "OLLAMA_HOST_URL": f"http://127.0.0.1:{self.server.server_address[1]}",
                         "LOCAL_MEMORY_GB": "40", "LOCAL_WAIT_POLL": "0.5", "FAKE_LOG": self.log,
                         "FAKE_SCENARIO": self.scenario, "XDG_DATA_HOME": os.path.join(self.home, ".local", "share"),
                         "LOCAL_PROGRESS": "off"})

    def scenario_set(self, rules):
        with open(self.scenario, "w", encoding="utf-8") as f:
            json.dump(rules, f)
        open(self.log, "w").close()

    def calls(self):
        return [json.loads(l) for l in open(self.log, encoding="utf-8") if l.strip()]

    def run(self, script, *args, cwd=None, stdin=None, env=None, background=False):
        e = dict(self.env, **(env or {}))
        cmd = [PY, os.path.join(S, script)] + list(args)
        if background:
            return subprocess.Popen(cmd, cwd=cwd, env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8")
        r = subprocess.run(cmd, cwd=cwd, env=e, input=stdin, capture_output=True, text=True, encoding="utf-8",
                           timeout=300)
        if VERBOSE:
            print(f"$ {script} {' '.join(args)}\n{r.stdout}{r.stderr}")
        return r

    def repo(self, name, files):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        for rel, content in files.items():
            os.makedirs(os.path.dirname(os.path.join(path, rel)), exist_ok=True)
            with open(os.path.join(path, rel), "w", encoding="utf-8") as f:
                f.write(content)
        for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                    ["add", "-A"], ["commit", "-qm", "init"]):
            subprocess.run(["git"] + cmd, cwd=path, check=True, capture_output=True)
        return path

    def close(self):
        self.server.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)


def git(path, *args):
    return subprocess.run(["git"] + list(args), cwd=path, capture_output=True, text=True).stdout


# --------------------------------------------------------------------------- tests
def t_guard(E):
    root = E.repo("g", {"src/a.py": "x = 1\n", "src/b.py": "", "docs/x.md": "", ".env": "SECRET=1\n"})
    env = dict(E.env, LOCAL_PROJECT_ROOT=root, LOCAL_WRITE_PATHS="src/a.py docs/",
               LOCAL_SECURITY_LOG=os.path.join(E.tmp, "sec.log"), LOCAL_REVIEW_MODEL="qwen3.6:35b-a3b")
    j = lambda *p: os.path.join(root, *p)  # noqa: E731
    cases = [
        ("Read", {"file_path": j("src/a.py")}, {}, True),
        ("Read", {"file_path": "/etc/passwd"}, {}, False),
        ("Read", {"file_path": j(".env")}, {}, False),
        ("Read", {"file_path": j(".git/config")}, {}, False),
        ("Read", {"file_path": j("../elsewhere.py")}, {}, False),
        ("Write", {"file_path": j("src/a.py"), "content": ""}, {}, True),
        ("Write", {"file_path": j("docs/x.md"), "content": ""}, {}, True),
        ("Write", {"file_path": j("src/b.py"), "content": ""}, {}, False),
        ("Edit", {"file_path": j("src/b.py"), "old_string": "a", "new_string": "b"}, {}, False),
        ("Write", {"file_path": j("src/a.py"), "content": ""}, {"LOCAL_READONLY": "1"}, False),
        ("Grep", {"pattern": "x", "path": j("src")}, {}, True),
        ("Glob", {"pattern": "**/*.py", "path": "/"}, {}, False),
        ("Bash", {"command": "ls src"}, {}, True),
        ("Bash", {"command": "wc -l src/a.py"}, {}, True),
        ("Bash", {"command": "python3 -m pytest -q"}, {}, True),
        ("Bash", {"command": ".venv/bin/python -m pytest -q tests"}, {}, True),
        ("Bash", {"command": "rm -rf src"}, {}, False),
        ("Bash", {"command": "curl http://example.com"}, {}, False),
        ("Bash", {"command": "cat /etc/passwd"}, {}, False),
        ("Bash", {"command": "cat .env"}, {}, False),
        ("Bash", {"command": "git push"}, {}, False),
        ("Bash", {"command": "find . -name '*.pyc' -delete"}, {}, False),
        ("Bash", {"command": "echo x > src/b.py"}, {}, False),
        ("Bash", {"command": "echo x | tee src/b.py"}, {}, False),
        ("Bash", {"command": "pip install requests"}, {}, False),
        ("Bash", {"command": "black src"}, {}, False),           # gray zone: the local expert denies
        ("PowerShell", {"command": "Remove-Item src -Recurse"}, {}, False),
        ("WebFetch", {"url": "https://example.com"}, {}, False),
        # shell tricks found by the audit, and false blocks it reported
        ("Bash", {"command": "cat <(python3 x.py)"}, {}, False),
        ("Bash", {"command": "cat .en?"}, {}, False),
        ("Bash", {"command": "cat .gi*/config"}, {}, False),
        ("Bash", {"command": "cat ${HOME}/.git-credentials"}, {}, False),
        ("Bash", {"command": "git show HEAD:.env"}, {}, False),
        ("Bash", {"command": "env FOO=1 open ."}, {}, False),
        ("Bash", {"command": "systemd-run --user sh"}, {}, False),
        ("Bash", {"command": "grep -rn 'open(' src"}, {}, True),
        ("Bash", {"command": "ls docs/source"}, {}, True),
        ("Bash", {"command": "python3 -m pytest -q -k 'open or close'"}, {}, True),
        ("Bash", {"command": "cat {.e,}nv"}, {}, False),
        ("Bash", {"command": "cat $'\\x2eenv'"}, {}, False),
        ("Bash", {"command": "cat .gi''t/config"}, {}, False),
        ("Read", {"file_path": j("src/credentials.py")}, {}, True),        # code, not a secret
        ("Read", {"file_path": j("config/credentials.json")}, {}, False),
    ]
    bad = []
    for tool, inp, extra, expected in cases:
        r = subprocess.run([PY, os.path.join(S, "guard.py")], input=json.dumps({"tool_name": tool, "tool_input": inp}),
                           env=dict(env, **extra), cwd=root, capture_output=True, text=True, encoding="utf-8")
        if (r.returncode == 0) != expected:
            bad.append(f"{tool} {inp} {extra} → {'allowed' if r.returncode == 0 else 'blocked'}: {r.stderr.strip()[:120]}")
    assert not bad, "\n".join(bad)
    return f"{len(cases)} cases"


def t_ask_local(E):
    f = os.path.join(E.tmp, "big.log")
    open(f, "w").write("error A\n" * 500)
    r = E.run("ask_local.py", "List the errors.", "--file", f, "--model", "qwen3.6:35b-a3b", cwd=E.tmp)
    assert r.returncode == 0 and "received" in r.stdout, r.stderr
    r = E.run("ask_local.py", "Summarize.", "--stdin", stdin="content\n" * 10, cwd=E.tmp)
    assert r.returncode == 0 and "[qwen3.6:35b-a3b]" in r.stdout, r.stderr
    r = E.run("ask_local.py", "x", "--model", "glm-4.7:cloud", stdin="", cwd=E.tmp)
    assert r.returncode != 0 and "cloud" in r.stderr.lower(), r.stderr
    r = E.run("ask_local.py", "x", "--model", "qwen3.6:35b-a3b", stdin="", cwd=E.tmp,
              env={"OLLAMA_HOST_URL": "http://192.0.2.10:11434"})
    assert r.returncode != 0, "remote server accepted"
    # A log full of digits holds ~1.8 characters per token: sized for it, not for code.
    f2 = os.path.join(E.tmp, "dense.log")
    open(f2, "w").write("".join(f"2026-10-12T{i % 24:02d}:{i % 60:02d}:{i % 60:02d} worker-{i % 8} batch {i}\n"
                                for i in range(3500)))
    r = E.run("ask_local.py", "Count.", "--file", f2, "--model", "qwen3.6:35b-a3b", cwd=E.tmp)
    assert r.returncode == 0 and "'num_ctx': 131072" in r.stderr, r.stderr
    sys.path.insert(0, os.path.join(ROOT, "local", "scripts"))
    import common
    assert 0.9 < common.local_tokens(open(f2).read()) / (len(open(f2).read()) / 1.84) < 1.3
    return "file, stdin, cloud and remote server refused, digit-dense log sized right"


def t_dispatch_success(E):
    repo = E.repo("d1", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "qwen3.6:35b-a3b", "write": {"calc.py": CALC_OK}, "text": "add fixed."}])
    r = E.run("dispatch_task.py", "t1", "Fix add() in calc.py.", "--models", "qwen3.6:35b-a3b",
              "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo)
    assert r.returncode == 0 and "passed" in r.stdout.splitlines()[0], r.stdout + r.stderr
    assert E.calls()[-1]["max_turns"] == "50", E.calls()[-1]                 # 30 + 10 per fix round (2)
    assert not E.calls()[-1]["persist"], E.calls()[-1]                       # no transcript left behind
    r = E.run("dispatch_task.py", "t1b", "Fix add() in calc.py.", "--write", "calc.py", "--retries", "0",
              cwd=E.repo("d1b", {"calc.py": CALC_BAD}))
    assert E.calls()[-1]["max_turns"] == "30", E.calls()[-1]
    return r.stdout.splitlines()[0]


def t_dispatch_chain(E):
    repo = E.repo("d2", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "qwen3.6:35b-a3b", "write": {"calc.py": CALC_BAD.replace("-", "*")}, "text": "I did a * b."},
                    {"model": "devstral-small-2:24b", "write": {"calc.py": CALC_OK}}])
    r = E.run("dispatch_task.py", "t2", "Fix add() in calc.py.", "--keep-order", "--models", "qwen3.6:35b-a3b devstral-small-2:24b",
              "--retries", "0", "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo)
    assert r.returncode == 0 and "passed" in r.stdout.splitlines()[0], r.stdout + r.stderr
    calls = E.calls()
    assert [c["model"] for c in calls] == ["qwen3.6:35b-a3b", "devstral-small-2:24b"], calls
    assert "Its summary" in calls[1]["prompt"] and "I did a * b." in calls[1]["prompt"], calls[1]["prompt"]
    assert "failure of qwen3.6:35b-a3b" in git(repo, "stash", "list"), git(repo, "stash", "list")
    return "the 2nd model gets the summary; the 1st one's work is set aside (git stash)"


def t_unknown_tool(E):
    # A model that keeps calling a tool it does not have (a shell, to run the tests) is stopped after 3 calls
    # instead of running until the step limit; the next model is told the check runs by itself.
    repo = E.repo("d2u", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "qwen3.6:35b-a3b", "tools": [["DeferredToolPlaceholder", {}]] * 10,
                     "write": {"calc.py": CALC_OK}},
                    {"model": "devstral-small-2:24b", "write": {"calc.py": CALC_OK}}])
    r = E.run("dispatch_task.py", "tu", "Fix add() in calc.py.", "--keep-order", "--models",
              "qwen3.6:35b-a3b devstral-small-2:24b", "--retries", "0", "--write", "calc.py",
              "--check", f"{PY} check.py", cwd=repo)
    assert r.returncode == 0 and "passed" in r.stdout.splitlines()[0], r.stdout + r.stderr
    assert "a tool it does not have (3 times in a row)" in r.stdout, r.stdout
    actions = open(os.path.join(repo, ".git", "local-agents", "tu.actions.log"), encoding="utf-8").read()
    assert actions.count("DeferredToolPlaceholder") == 3, actions
    calls = E.calls()
    assert [c["model"] for c in calls] == ["devstral-small-2:24b"], calls
    assert "You cannot run commands (no shell)" in calls[0]["prompt"], calls[0]["prompt"]
    return "stopped after 3 calls to a missing tool; the next model is told the check runs by itself"


def t_review(E):
    repo = E.repo("d3", {"calc.py": CALC_BAD})
    E.scenario_set([{"model": "qwen3.6:35b-a3b", "write": {"calc.py": CALC_CHEAT}},
                    {"model": "gpt-oss:20b", "write": {"calc.py": CALC_OK}}])
    r = E.run("dispatch_task.py", "t3", "Fix add().", "--keep-order", "--models", "qwen3.6:35b-a3b gpt-oss:20b", "--retries", "0",
              "--review", "--write", "calc.py", cwd=repo)
    assert r.returncode == 0 and "approved by review" in r.stdout.splitlines()[0], r.stdout + r.stderr
    assert "NEEDS WORK" in r.stdout and "hard-coded 5" in E.calls()[1]["prompt"]
    return "negative local review → next model → approved"


def t_reject(E):
    repo = E.repo("d4", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_OK}, "text": "Version 1."}])
    E.run("dispatch_task.py", "t4", "Fix add().", "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo)
    r = E.run("dispatch_task.py", "t4", "--reject", "Add a docstring.", "--keep", "--write", "calc.py",
              "--check", f"{PY} check.py", cwd=repo)
    p = E.calls()[-1]["prompt"]
    assert r.returncode == 0 and "Reviewer requirements" in p and "improve it" in p and "Version 1." in p, r.stdout
    assert "Fix add()" in p, "original instruction not reused"
    r = E.run("dispatch_task.py", "t4", "--reject", "Another approach.", "--write", "calc.py",
              "--check", f"{PY} check.py", cwd=repo)
    assert r.returncode == 0 and "rejected by Claude" in git(repo, "stash", "list") and "Start over" in E.calls()[-1]["prompt"]
    open(os.path.join(repo, "notes.txt"), "w").write("manual edit\n")
    r = E.run("dispatch_task.py", "t4", "--reject", "Again.", "--write", "calc.py", cwd=repo)
    assert r.returncode != 0 and "has changed" in r.stdout + r.stderr
    assert open(os.path.join(repo, "notes.txt")).read() == "manual edit\n"
    repo = E.repo("d4b", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_OK}}])
    E.run("dispatch_task.py", "t4b", "Fix add().", "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo)
    open(os.path.join(repo, "calc.py"), "a").write("# my own edit\n")       # same file: same git status
    r = E.run("dispatch_task.py", "t4b", "--reject", "Again.", "--write", "calc.py", cwd=repo)
    assert r.returncode != 0 and "has changed" in r.stdout + r.stderr, r.stdout + r.stderr
    assert open(os.path.join(repo, "calc.py")).read().endswith("# my own edit\n"), "manual edit lost"
    return "--keep, start over, refused if the repository changed (even inside a file the agent edited)"


def t_research(E):
    repo = E.repo("d5", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"if": "Useful information", "write": {"calc.py": CALC_OK}},
                    {"model": "*", "text": "I do not know this API.\nRESEARCH: signature of add() in calc 2.0"}])
    r = E.run("dispatch_task.py", "t5", "Fix add().", "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo)
    assert r.returncode == 5 and "RESEARCH" in r.stdout, r.stdout + r.stderr
    info = os.path.join(E.tmp, "info.txt")
    open(info, "w").write("add(a, b) returns a + b.")
    r = E.run("dispatch_task.py", "t5", "--resume", "--info-file", info, "--write", "calc.py",
              "--check", f"{PY} check.py", cwd=repo)
    assert r.returncode == 0 and "add(a, b) returns a + b." in E.calls()[-1]["prompt"], r.stdout + r.stderr
    # The check passes although the agent says it lacked information: a pass, flagged as a possible guess.
    repo = E.repo("d5b", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_OK}, "text": "Done.\nRESEARCH: what does calc 2.0 add() return?"}])
    r = E.run("dispatch_task.py", "t5b", "Fix add().", "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo)
    assert r.returncode == 0 and "may be a guess" in r.stdout and "RESEARCH: what does calc 2.0" in r.stdout, r.stdout + r.stderr
    sys.path.insert(0, os.path.join(ROOT, "local", "scripts"))
    import common
    for line, q in (("RESEARCH: what API?", "what API?"), ("**RESEARCH:** what API?", "what API?"),
                    ("- **RESEARCH**: what `x` does?", "what `x` does?"), ("> `RESEARCH: v9.3 API`", "v9.3 API")):
        assert common.RESEARCH_RE.findall("Some text.\n" + line) == [q], (line, common.RESEARCH_RE.findall(line))
    assert not common.RESEARCH_RE.findall("I did the research: none needed.\nResearching: no")
    return "stop on \"needs information\" (exit code 5, also in Markdown), resume with --info-file; a pass with a research request is flagged"


def t_manual_changes(E):
    repo = E.repo("d6", {"calc.py": CALC_BAD})
    open(os.path.join(repo, "calc.py"), "a").write("# manual edit\n")
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_OK}}])
    r = E.run("dispatch_task.py", "t6", "Fix add().", "--write", "calc.py", cwd=repo)
    assert r.returncode != 0 and "STOP" in r.stdout + r.stderr and not E.calls()
    assert open(os.path.join(repo, "calc.py")).read().endswith("# manual edit\n")
    return "dirty repository → stop, nothing touched"


def t_blocked_actions(E):
    repo = E.repo("d7", {"calc.py": CALC_BAD})
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_OK},
                     "tools": [["Bash", {"command": "rm -rf /"}], ["Read", {"file_path": "/etc/passwd"}],
                               ["Read", {"file_path": os.path.join(repo, "calc.py")}]]}])
    r = E.run("dispatch_task.py", "t7", "Fix add().", "--write", "calc.py", "--tools", "Read Edit Bash", cwd=repo)
    assert E.calls()[0]["tools"][:3] == [["Bash", False], ["Read", False], ["Read", True]], E.calls()[0]["tools"]
    assert "2 blocked" in r.stdout, r.stdout
    return "dangerous actions blocked and reported"


def t_check_integrity(E):
    # The checked code runs with the project read-only: it cannot delete ignored files (.env),
    # and a test run that stops early is not a success.
    repo = E.repo("i1", {"calc.py": CALC_BAD, "check.py": CHECK, ".gitignore": ".env\n", ".env": "SECRET=1\n"})
    evil = "import os\ntry:\n    os.remove('.env')\nexcept OSError:\n    pass\n" + CALC_OK
    E.scenario_set([{"model": "*", "write": {"calc.py": evil}}])
    r = E.run("dispatch_task.py", "i1", "Fix add().", "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo)
    assert os.path.exists(os.path.join(repo, ".env")), "the checked code deleted .env"
    repo = E.repo("i2", {"calc.py": CALC_BAD, "test_calc.py":
                         "import unittest, calc\nclass T(unittest.TestCase):\n    def test_add(self):\n"
                         "        self.assertEqual(calc.add(2, 3), 5)\n"})
    E.scenario_set([{"model": "*", "write": {"calc.py": "import os\nos._exit(0)\n"}}])
    r = E.run("dispatch_task.py", "i2", "Fix add().", "--write", "calc.py", "--retries", "0",
              "--check", f"{PY} -m unittest test_calc", cwd=repo)
    assert "passed" not in r.stdout.splitlines()[0], "a run that exits before the tests passed:\n" + r.stdout
    # A check that writes into the read-only project, or a command that does not exist: "impossible".
    repo = E.repo("i3", {"calc.py": CALC_BAD, "check.py": "open('out.txt', 'w').write('x')\n"})
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_OK}}])
    r = E.run("dispatch_task.py", "i3", "Fix add().", "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo)
    assert "check could not run" in r.stdout.splitlines()[0] and "--check-writable" in r.stdout, r.stdout
    repo = E.repo("i3b", {"calc.py": CALC_BAD, "check.py": "open('out.txt', 'w').write('x')\n",
                          ".gitignore": "out.txt\n"})
    r = E.run("dispatch_task.py", "i3b", "Fix add().", "--write", "calc.py", "--check", f"{PY} check.py",
              "--check-writable", cwd=repo)
    assert "passed" in r.stdout.splitlines()[0] and git(repo, "status", "--porcelain").count("out.txt") == 0, r.stdout
    repo = E.repo("i4", {"calc.py": CALC_BAD})
    r = E.run("dispatch_task.py", "i4", "Fix add().", "--write", "calc.py", "--check", "no-such-command-xyz", cwd=repo)
    assert "check could not run" in r.stdout.splitlines()[0] and "127" in r.stdout, r.stdout
    # --write given as ./path works.
    repo = E.repo("i5", {"calc.py": CALC_BAD, "check.py": CHECK})
    r = E.run("dispatch_task.py", "i5", "Fix add().", "--write", "./calc.py", "--check", f"{PY} check.py", cwd=repo)
    assert "passed" in r.stdout.splitlines()[0], r.stdout
    # --check-writable: the check may write, but .env stays intact and ignored files it creates are reported.
    repo = E.repo("i6", {"calc.py": CALC_BAD, ".gitignore": ".env\nout/\n", ".env": "SECRET=1\n", "check.py":
                         "import os\nos.makedirs('out', exist_ok=True)\nopen('out/x', 'w').write('x')\n"
                         "try:\n    os.remove('.env')\nexcept OSError:\n    pass\n"})
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_OK}}])
    r = E.run("dispatch_task.py", "i6", "Fix add().", "--write", "calc.py", "--check", f"{PY} check.py",
              "--check-writable", cwd=repo)
    assert os.path.exists(os.path.join(repo, ".env")), "writable mode: .env deleted"
    assert "ignored by git: out/" in r.stdout, r.stdout
    # Writable mode: whatever happens to ignored files during the whole task is reported (here a deletion).
    repo = E.repo("i6b", {"calc.py": CALC_BAD, ".gitignore": "data.db\n", "data.db": "rows\n", "check.py":
                          "import os\ntry:\n    os.remove('data.db')\nexcept OSError:\n    pass\n"})
    r = E.run("dispatch_task.py", "i6b", "Fix add().", "--write", "calc.py", "--check", f"{PY} check.py",
              "--check-writable", cwd=repo)
    assert "deleted data.db" in r.stdout, r.stdout
    # Variables that look secret are removed from the check, unless explicitly passed through.
    repo = E.repo("i7", {"calc.py": CALC_BAD, "check.py": "import os, sys\nsys.exit(os.environ.get('MY_API_KEY') != 'dummy')\n"})
    r = E.run("dispatch_task.py", "i7", "Fix add().", "--write", "calc.py", "--retries", "0",
              "--check", f"{PY} check.py", cwd=repo, env={"MY_API_KEY": "dummy"})
    assert "passed" not in r.stdout.splitlines()[0], "a secret-looking variable reached the checked code"
    repo = E.repo("i8", {"calc.py": CALC_BAD, "check.py": "import os, sys\nsys.exit(os.environ.get('MY_API_KEY') != 'dummy')\n"})
    r = E.run("dispatch_task.py", "i8", "Fix add().", "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo,
              env={"MY_API_KEY": "dummy", "LOCAL_KEEP_ENV": "MY_API_KEY"})
    assert "passed" in r.stdout.splitlines()[0], r.stdout
    return ".env safe, early exit ≠ success, read-only project, missing command, ./ paths, writable mode, env"


def t_no_sandbox(E):
    if not sys.platform.startswith("linux"):
        return "skipped (Linux only)"
    gitbin = os.path.join(E.tmp, "gitbin")
    os.makedirs(gitbin, exist_ok=True)
    if not os.path.exists(os.path.join(gitbin, "git")):
        os.symlink(shutil.which("git"), os.path.join(gitbin, "git"))
    repo = E.repo("n1", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_OK}}])
    r = E.run("dispatch_task.py", "n1", "Fix add().", "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo,
              env={"PATH": gitbin + os.pathsep + E.bin})
    assert "check could not run" in r.stdout.splitlines()[0] and "bubblewrap" in r.stdout, r.stdout + r.stderr
    repo = E.repo("n2", {"calc.py": CALC_BAD, "check.py": CHECK})
    r = E.run("dispatch_task.py", "n2", "Fix add().", "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo,
              env={"PATH": gitbin + os.pathsep + E.bin, "LOCAL_ALLOW_UNSANDBOXED": "1"})
    assert "passed" in r.stdout.splitlines()[0] and "[no sandbox]" in r.stdout, r.stdout + r.stderr
    return "no sandbox: the check is refused unless explicitly allowed (then flagged)"


def t_worktree_editable(E):
    # The linked venv may hold an editable install of the MAIN repository: the worktree's code must win.
    repo = E.repo("e1", {"src/pkg/__init__.py": "VALUE = 'main'\n", ".gitignore": ".venv\n", ".venv/marker": "v\n"})
    E.scenario_set([{"model": "*", "write": {"src/pkg/__init__.py": "VALUE = 'worktree'\n"}}])
    check = f"{PY} -c \"import pkg, sys; sys.exit(pkg.VALUE != 'worktree')\""
    r = E.run("dispatch_task.py", "e1", "Change VALUE.", "--worktree", "--write", "src/", "--check", check,
              cwd=repo, env={"PYTHONPATH": os.path.join(repo, "src")})    # what an editable install does
    assert "passed" in r.stdout.splitlines()[0], r.stdout
    exclude = open(os.path.join(repo, ".git", "info", "exclude")).read()
    assert "/.venv" in exclude and "/node_modules" not in exclude, exclude
    return "worktree checks import the worktree's code, not the main repository's (editable installs)"


def t_signal(E):
    import signal as sig
    repo = E.repo("g1", {"calc.py": CALC_BAD})
    pid_file = os.path.join(E.tmp, "agent.pid")
    E.scenario_set([{"model": "*", "sleep": 30, "write": {"calc.py": CALC_OK}}])
    p = E.run("dispatch_task.py", "g1", "Fix add().", "--write", "calc.py", cwd=repo, background=True,
              env={"FAKE_PID_FILE": pid_file})
    for _ in range(100):
        if os.path.exists(pid_file):
            break
        time.sleep(0.1)
    time.sleep(0.5)
    p.send_signal(sig.SIGTERM)
    p.communicate(timeout=30)
    agent = int(open(pid_file).read())
    time.sleep(0.5)
    try:
        os.kill(agent, 0)
        alive = True
    except OSError:
        alive = False
    running = os.path.join(E.home, ".claude", "local-agents", "running")
    left = [f for f in os.listdir(running) if f.endswith(".json")] if os.path.isdir(running) else []
    assert not alive and not left, f"agent alive={alive}, memory reservations left={left}"
    return "dispatch interrupted → the agent is stopped and its memory released"


def t_hooks_not_running(E):
    # If Claude Code did not apply the hooks, the work is not trusted: fail closed, work set aside.
    repo = E.repo("k1", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_OK}}])
    r = E.run("dispatch_task.py", "k1", "Fix add().", "--keep-order", "--models", "qwen3.6:35b-a3b devstral-small-2:24b",
              "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo, env={"FAKE_NO_HOOKS": "1"})
    assert r.returncode != 0 and "security hook did not run" in r.stdout.splitlines()[0], r.stdout
    assert len(E.calls()) == 1 and open(os.path.join(repo, "calc.py")).read() == CALC_BAD, "work not set aside"
    assert "unguarded work" in git(repo, "stash", "list")
    return "hooks not applied → result refused, work set aside, no other model tried"


def t_capacity(E):
    r = E.run("doctor.py", "--capacity")
    out = r.stdout
    assert "40 GB" in out and "(measured)" in out and "(estimated)" in out and "(approximate)" in out, out
    assert "no combination fits" in out and "gemma" not in out and "nomic" not in out, out
    return "memory measured / estimated / approximate"


def t_parallel(E):
    repo = E.repo("p", {"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n", ".gitignore": ".venv/\n",
                        ".venv/marker": "venv\n"})
    E.scenario_set([{"if": "a.txt", "sleep": 2, "write": {"a.txt": "A\n"}},
                    {"if": "b.txt", "sleep": 2, "write": {"b.txt": "B\n"}},
                    {"if": "c3", "sleep": 2, "write": {"c.txt": "three\n"}},
                    {"if": "c4", "sleep": 2, "write": {"c.txt": "four\n"}}])
    # Two different models that do not fit in memory together: the second one waits.
    p1 = E.run("dispatch_task.py", "w1", "Edit a.txt.", "--worktree", "--models", "qwen3.6:35b-a3b",
               "--write", "a.txt", cwd=repo, background=True)
    time.sleep(0.7)
    p2 = E.run("dispatch_task.py", "w2", "Edit b.txt.", "--worktree", "--models", "devstral-small-2:24b",
               "--write", "b.txt", cwd=repo, background=True)
    out2 = p2.communicate()[0]
    p1.communicate()
    c = {x["model"]: x for x in E.calls()}
    assert c["devstral-small-2:24b"]["t0"] >= c["qwen3.6:35b-a3b"]["t1"] - 0.05, "no waiting"
    assert "waiting" in out2, out2
    # Same model twice: shared weights, in parallel.
    open(E.log, "w").close()
    p3 = E.run("dispatch_task.py", "w3", "Write c3 into c.txt.", "--worktree", "--models", "qwen3.6:35b-a3b",
               "--write", "c.txt", cwd=repo, background=True)
    p4 = E.run("dispatch_task.py", "w4", "Write c4 into c.txt.", "--worktree", "--models", "qwen3.6:35b-a3b",
               "--write", "c.txt", cwd=repo, background=True)
    p3.communicate(), p4.communicate()
    a, b = E.calls()
    assert a["t0"] < b["t1"] and b["t0"] < a["t1"], "same model: not in parallel"
    assert git(repo, "status", "--porcelain") == "", "the main repository was modified"
    lst = E.run("worktree.py", "list", cwd=repo).stdout
    assert all(t in lst for t in ("w1", "w2", "w3", "w4")), lst
    open(os.path.join(repo, "b.txt"), "w").write("manual\n")             # uncommitted manual edit
    r = E.run("worktree.py", "merge", "w1", cwd=repo)
    assert r.returncode != 0 and "uncommitted" in r.stderr, r.stdout + r.stderr
    open(os.path.join(repo, "b.txt"), "w").write("b\n")
    wt = os.path.join(E.home, ".local", "share", "local-agents", "worktrees")
    assert any(os.path.islink(os.path.join(root, ".venv")) for root, dirs, _ in os.walk(wt)), "venv not linked"
    for t in ("w1", "w2", "w3"):
        r = E.run("worktree.py", "merge", t, cwd=repo)
        assert r.returncode == 0, r.stdout + r.stderr
    r = E.run("worktree.py", "merge", "w4", cwd=repo)
    assert r.returncode != 0 and "MERGE CONFLICT" in r.stderr, r.stderr
    subprocess.run(["git", "merge", "--abort"], cwd=repo)
    r = E.run("worktree.py", "discard", "w4", cwd=repo)
    assert r.returncode == 0 and "local-agents/rejected/w4-" in git(repo, "branch")
    assert open(os.path.join(repo, "a.txt")).read() == "A\n" and open(os.path.join(repo, "c.txt")).read() == "three\n"
    assert "No parallel task" in E.run("worktree.py", "list", cwd=repo).stdout
    assert "w4" in E.run("savings.py", cwd=repo).stdout
    return "memory guard, same model in parallel, merge, conflict, discard, savings"


def t_sandbox_down(E):
    if not sys.platform.startswith("linux"):
        return "skipped (Linux only)"
    fake = os.path.join(E.tmp, "badbwrap")
    os.makedirs(fake, exist_ok=True)
    with open(os.path.join(fake, "bwrap"), "w") as f:
        f.write("#!/bin/sh\necho 'bwrap: No permissions to create new namespace' >&2\nexit 1\n")
    os.chmod(os.path.join(fake, "bwrap"), 0o755)
    repo = E.repo("d8", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_OK}}])
    r = E.run("dispatch_task.py", "t8", "Fix add().", "--write", "calc.py", "--check", f"{PY} check.py",
              cwd=repo, env={"PATH": fake + os.pathsep + E.env["PATH"]})
    assert r.returncode != 0 and "check could not run" in r.stdout.splitlines()[0], r.stdout
    assert "sandbox did not start" in r.stdout and len(E.calls()) == 1, r.stdout
    return "broken sandbox → \"impossible\", no false failure and no new attempt"


def t_scope_tamper(E):
    # The agent (e.g. through a Python script run with Bash) rewrites the test outside --write.
    repo = E.repo("s1", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_BAD.replace("-", "*")}, "raw_write": {"check.py": "import sys\nsys.exit(0)\n"}}])
    r = E.run("dispatch_task.py", "s1", "Fix add().", "--write", "calc.py", "--retries", "0",
              "--check", f"{PY} check.py", cwd=repo)
    assert "passed" not in r.stdout.splitlines()[0], "tampered test accepted:\n" + r.stdout
    assert open(os.path.join(repo, "check.py")).read() == CHECK, "the test was not restored"
    assert "outside --write" in r.stdout and "check.py" in git(repo, "stash", "show", "--name-only", "stash@{0}") + r.stdout
    # Same with the Stop hook active: the hook sets the rewrite aside and tells the agent.
    repo = E.repo("s1b", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_BAD.replace("-", "*")}, "raw_write": {"check.py": "import sys\nsys.exit(0)\n"}}])
    r = E.run("dispatch_task.py", "s1b", "Fix add().", "--write", "calc.py", "--models", "qwen3.6:35b-a3b",
              "--no-fallback", "--check", f"{PY} check.py", cwd=repo)
    calls = E.calls()
    assert "passed" not in r.stdout.splitlines()[0] and open(os.path.join(repo, "check.py")).read() == CHECK, r.stdout
    # Round 1: told about the scope; round 2: same error and nothing changed → stopped (no progress).
    assert len(calls) == 2 and "outside your allowed scope" in calls[1]["prompt"], calls
    assert '"allowed": "no progress"' in open(os.path.join(repo, ".git", "local-agents", "s1b.stop.jsonl")).read()
    return "test rewritten outside --write → set aside before the check (also inside the session), task not accepted"


def t_review_verdicts(E):
    repo = E.repo("s2", {"calc.py": CALC_BAD})
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_OK + "# style: markdown\n"}}])
    r = E.run("dispatch_task.py", "s2", "Fix add().", "--models", "qwen3.6:35b-a3b", "--review", "--retries", "0",
              "--write", "calc.py", cwd=repo)
    assert "approved by review" in r.stdout.splitlines()[0], "Markdown verdict misread:\n" + r.stdout
    repo = E.repo("s3", {"calc.py": CALC_BAD})
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_OK + "# style: chatty\n"}}])
    r = E.run("dispatch_task.py", "s3", "Fix add().", "--models", "qwen3.6:35b-a3b", "--review", "--retries", "0",
              "--write", "calc.py", cwd=repo)
    first = r.stdout.splitlines()[0]
    assert "approved" not in first and "needs work" not in first and "inconclusive" in r.stdout.lower(), r.stdout
    repo = E.repo("s3b", {"calc.py": CALC_BAD})
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_OK + "# style: notyet\n"}}])
    r = E.run("dispatch_task.py", "s3b", "Fix add().", "--models", "qwen3.6:35b-a3b", "--review", "--retries", "0",
              "--write", "calc.py", cwd=repo)
    assert "approved" not in r.stdout.splitlines()[0], "\"NOT YET APPROVED\" read as an approval:\n" + r.stdout
    return "Markdown verdict understood; no verdict or \"NOT YET APPROVED\" → never counted as an approval"


def t_zero_tests(E):
    repo = E.repo("s4", {"calc.py": CALC_BAD, "check.py": "print('Ran 0 tests in 0.000s')\nprint('OK')\n"})
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_OK}}])
    r = E.run("dispatch_task.py", "s4", "Fix add().", "--write", "calc.py", "--retries", "0",
              "--check", f"{PY} check.py", cwd=repo)
    assert "passed" not in r.stdout.splitlines()[0] and "no test" in r.stdout.lower(), r.stdout
    return "a check that runs no test is a failure"


def t_hidden_check(E):
    # Code tailored to the visible test passes it; the held-out test (outside the project) catches it.
    hidden_dir = os.path.join(E.tmp, "hidden-tests")
    os.makedirs(hidden_dir)
    hidden = os.path.join(hidden_dir, "hidden_check.py")
    open(hidden, "w").write("import calc, sys\nsys.exit(0 if calc.add(10, 5) == 15 and calc.add(-1, 1) == 0 else 1)\n")
    special = "def add(a, b):\n    return {(2, 3): 5, (1, 1): 2}[(a, b)]\n"
    repo = E.repo("s5", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"if": "cannot see failed", "write": {"calc.py": CALC_OK}},
                    {"model": "*", "write": {"calc.py": special}}])
    r = E.run("dispatch_task.py", "s5", "Fix add().", "--write", "calc.py", "--check", f"{PY} check.py",
              "--hidden-check", f"{PY} {hidden}", cwd=repo, env={"FAKE_SECRET_PROBE": hidden})
    calls = E.calls()
    assert r.returncode == 0 and "1 attempt(s) and 1 in-session fix" in r.stdout.splitlines()[0], r.stdout
    assert [c["turn"] for c in calls] == [0, 1], calls
    assert all(hidden not in c["prompt"] and "10, 5" not in c["prompt"] and not c["leak"] for c in calls), \
        "hidden test leaked to the agent"
    assert "Hidden CHECK OK" in r.stdout, r.stdout
    return "special-cased code passes the visible test, caught by the hidden one, fixed in the same session"


def t_stop_hook(E):
    # A failed check does not restart the agent: the Stop hook keeps it working in the same session.
    repo = E.repo("h1", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"if": "The check failed", "write": {"calc.py": CALC_OK}, "text": "Fixed."},
                    {"model": "*", "write": {"calc.py": CALC_BAD.replace("-", "*")}, "text": "Done."}])
    r = E.run("dispatch_task.py", "h1", "Fix add().", "--models", "qwen3.6:35b-a3b", "--write", "calc.py",
              "--check", f"{PY} check.py", cwd=repo)
    calls = E.calls()
    assert r.returncode == 0 and "passed" in r.stdout.splitlines()[0], r.stdout
    assert "1 attempt(s) and 1 in-session fix" in r.stdout.splitlines()[0], r.stdout
    assert [c["turn"] for c in calls] == [0, 1] and "CHECK FAILED" in calls[1]["prompt"], calls
    checks = [json.loads(l) for l in open(os.path.join(repo, ".git", "local-agents", "h1.stats.jsonl"))
              if '"check"' in l]
    assert len(checks) == 2, f"the final check was not reused: {len(checks)} runs"
    # Cap: with --retries 1, one in-session fix, then the next model takes over.
    repo = E.repo("h2", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "devstral-small-2:24b", "write": {"calc.py": CALC_OK}},
                    {"model": "*", "write": {"calc.py": CALC_BAD.replace("-", "*")}}])
    r = E.run("dispatch_task.py", "h2", "Fix add().", "--keep-order", "--models", "qwen3.6:35b-a3b devstral-small-2:24b",
              "--retries", "1", "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo)
    turns = [(c["model"], c["turn"]) for c in E.calls()]
    assert turns == [("qwen3.6:35b-a3b", 0), ("qwen3.6:35b-a3b", 1), ("devstral-small-2:24b", 0)], turns
    assert r.returncode == 0 and "passed" in r.stdout.splitlines()[0], r.stdout
    return "fix in the same session after a failed check; capped rounds; final check reused"


def t_acceptance_script(E):
    # The acceptance test for the real installation, run here against the simulated Ollama and claude.
    E.scenario_set([{"if": "must start with the exact line", "write": {"calc.py": "# checked by acceptance\n" + CALC_OK}},
                    {"if": "Follow what the automatic check reports", "write": {"calc.py": CALC_OK}},
                    {"if": "returns a wrong result", "write": {"calc.py": CALC_OK}},
                    {"if": "quote its content exactly", "read_paths_in_prompt": True, "text": "I cannot read it."},
                    {"if": "grep -r ACCEPT", "tools": [["Bash", {"command": "grep -r ACCEPT ."}]], "text": "(no output)"},
                    {"if": "zorblax", "text": "I do not know this library.\nRESEARCH: what is the zorblax 9.3 API?"}])
    out_dir = os.path.join(E.tmp, "acceptance")
    os.makedirs(out_dir, exist_ok=True)
    r = subprocess.run([PY, os.path.join(ROOT, "tests", "acceptance.py")], cwd=out_dir, timeout=600,
                       env=dict(E.env, LOCAL_ACCEPTANCE_REPORT=os.path.join(out_dir, "acceptance-report.md")),
                       capture_output=True, text=True, encoding="utf-8")
    report = open(os.path.join(out_dir, "acceptance-report.md"), encoding="utf-8").read()
    assert r.returncode == 0 and "0 failure(s)" in report, r.stdout + r.stderr
    for step in ("delegated task", "guard hook (PreToolUse) fired", "Stop hook fired", "fix in the same session",
                 "guard (read outside the project)", "shell sandbox hides .env", "Stop hook gets the agent's last message",
                 "cloud model refused", "non-local Ollama server refused", "context given to the agents"):
        line = next((l for l in report.splitlines() if step in l), "")
        assert line.startswith("| PASS"), f"{step}: {line or 'missing'}"
    return "acceptance script runs end to end (all key steps PASS on the simulation)"


def t_help(E):
    for script in ("dispatch_task.py", "delegate_local.py", "ask_local.py", "worktree.py", "savings.py", "models.py", "unload.py"):
        r = E.run(script, "--help")
        assert r.returncode == 0 and "Usage" in r.stdout, (script, r.stdout + r.stderr)
    r = E.run("dispatch_task.py")
    assert r.returncode == 2 and "required" in r.stderr and "--help" in r.stderr, r.stderr
    r = E.run("ask_local.py", "x", "--unknown")
    assert r.returncode == 2 and "unrecognized" in r.stderr, r.stderr
    return "help and usage errors"


def t_doctor(E):
    r = E.run("doctor.py")
    assert "Summary:" in r.stdout and "qwen3.6:35b-a3b: agent" in r.stdout, r.stdout + r.stderr
    r = E.run("doctor.py", "--quick")
    assert r.stdout.strip(), r.stderr
    big = E.run("doctor.py", "--suggest", env={"LOCAL_MEMORY_GB": "40"}).stdout
    small = E.run("doctor.py", "--suggest", env={"LOCAL_MEMORY_GB": "12"}).stdout
    assert "qwen3.6:35b-a3b" in big and "gpt-oss:120b" not in big, big
    assert "ministral-3:8b" in small and "qwen" not in small and "-cloud" not in small.replace("ending in -cloud", ""), small
    return "full and quick diagnostics, model suggestions by memory"


def t_install_settings(E):
    # --ollama-settings: keeps an existing server.json key, writes the macOS login agent (simulated).
    home = os.path.join(E.tmp, "install-home")
    os.makedirs(os.path.join(home, ".ollama"), exist_ok=True)
    with open(os.path.join(home, ".ollama", "server.json"), "w") as f:
        f.write('{"other": 1}')
    code = ("import sys, os, plistlib; sys.path.insert(0, %r); import ollama_settings as o; "
            "o.common.IS_MAC, o.common.IS_LINUX = True, False; o.quiet = lambda c: 0; "
            "assert o.applied() is False or True; o.set_cloud(True); assert not o.applied(); "
            "print('\\n'.join(o.apply())); assert o.applied(); "
            "print(plistlib.load(open(o.AGENT_PLIST, 'rb'))['ProgramArguments'][2])") % os.path.join(ROOT, "local", "scripts")
    r = subprocess.run([PY, "-c", code], env=dict(E.env, HOME=home, USERPROFILE=home), capture_output=True,
                       text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    conf = json.load(open(os.path.join(home, ".ollama", "server.json")))
    assert conf == {"other": 1, "disable_ollama_cloud": True}, conf
    script = r.stdout.strip().splitlines()[-1]
    for part in ("OLLAMA_NO_CLOUD 1", "OLLAMA_CONTEXT_LENGTH 65536", "OLLAMA_KEEP_ALIVE 30m"):
        assert "launchctl setenv " + part in script, script
    assert script.endswith("open -a Ollama") and "Login Items" in r.stdout, r.stdout
    # Changing a value: remembered, re-applied, checked; invalid values refused.
    code2 = ("import sys; sys.path.insert(0, %r); import ollama_settings as o; "
             "o.common.IS_MAC, o.common.IS_LINUX = True, False; o.quiet = lambda c: 0; o.memory_note = lambda c: ''; "
             "sys.argv = ['x'] + sys.argv[1:]; o.main()") % os.path.join(ROOT, "local", "scripts")

    def settings(*args):
        return subprocess.run([PY, "-c", code2] + list(args), env=dict(E.env, HOME=home, USERPROFILE=home),
                              capture_output=True, text=True, encoding="utf-8", timeout=60)
    r = settings("--context", "128k", "--keep-alive", "2h")
    assert r.returncode == 0 and "OLLAMA_CONTEXT_LENGTH=131072" in r.stdout, r.stdout + r.stderr
    plist = open(os.path.join(home, "Library", "LaunchAgents", "local.ollama.env.plist"), encoding="utf-8").read()
    assert "OLLAMA_CONTEXT_LENGTH 131072" in plist and "OLLAMA_KEEP_ALIVE 2h" in plist, plist
    r = settings("--check")
    assert r.returncode == 0, r.stdout + r.stderr
    r = settings("--show")
    assert "| OLLAMA_CONTEXT_LENGTH | 131072 | 131072 | 65536 |" in r.stdout, r.stdout + r.stderr
    r = subprocess.run([PY, "-c", "import sys; sys.path.insert(0, %r); import common; print(common.server_context())"
                        % os.path.join(ROOT, "local", "scripts")], env=dict(E.env, HOME=home, USERPROFILE=home),
                       capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == "131072", r.stdout + r.stderr          # the agents' context follows the choice
    for bad in (("--context", "16k"), ("--keep-alive", "soon")):
        r = settings(*bad)
        assert r.returncode == 2 and "ERROR" in r.stderr, (bad, r.stdout + r.stderr)
    # The cloud block is optional and reversible: lifted, the other keys of server.json stay; the skill
    # itself keeps refusing cloud models.
    r = settings("--allow-cloud")
    conf = json.load(open(os.path.join(home, ".ollama", "server.json")))
    plist = open(os.path.join(home, "Library", "LaunchAgents", "local.ollama.env.plist"), encoding="utf-8").read()
    assert conf == {"other": 1} and "OLLAMA_NO_CLOUD" not in plist and "no longer blocked" in r.stdout, r.stdout + plist
    assert settings("--check").returncode == 0
    r = settings("--block-cloud")
    conf = json.load(open(os.path.join(home, ".ollama", "server.json")))
    plist = open(os.path.join(home, "Library", "LaunchAgents", "local.ollama.env.plist"), encoding="utf-8").read()
    assert conf.get("disable_ollama_cloud") is True and "OLLAMA_NO_CLOUD 1" in plist, r.stdout + plist
    # A new user: nothing blocked on Ollama's side unless asked.
    home3 = os.path.join(E.tmp, "install-home-3")
    os.makedirs(home3, exist_ok=True)
    out = subprocess.run([PY, "-c", "import sys; sys.path.insert(0, %r); import ollama_settings as o; "
                          "print(o.cloud_choice(), [k for k, _ in o.settings()])" % os.path.join(ROOT, "local", "scripts")],
                         env=dict(E.env, HOME=home3, USERPROFILE=home3, XDG_DATA_HOME=os.path.join(home3, "data")),
                         capture_output=True, text=True, timeout=60).stdout
    assert out.startswith("False") and "OLLAMA_NO_CLOUD" not in out, out
    r = settings("--reset")
    assert "OLLAMA_CONTEXT_LENGTH=65536" in r.stdout and "OLLAMA_KEEP_ALIVE=30m" in r.stdout, r.stdout + r.stderr
    return ("Ollama settings: server.json merged, macOS login agent; --context/--keep-alive remembered, "
            "re-applied, checked; --reset")


CALIB_CASES = [
    ("R1", "7 seconds: DEFAULT_TIMEOUT = BASE_TIMEOUT * 2 + 1 (config.py:5).", (True, True)),
    ("R1", "**7** seconds — `config.py`, line 5", (True, True)),
    ("R1", "30 seconds, see legacy/old_client.py:4; the new one uses 7", (False, False)),
    ("R1", "3 seconds (config.py:7)", (False, False)),
    ("R2", "save_item, record_sale, delete_item", (True, None)),
    ("R2", "save_item, record_sale, delete_item, load_item", (False, None)),
    ("R3", "No. low_stock_items uses a strict < 5.", (True, None)),
    ("R3", "**Yes**, 5 is the threshold", (False, None)),
    ("R4", "There is no such function in the code.", (True, None)),
    ("R4", "send_low_stock_alert() in reports.py:8", (False, None)),
    ("R5", "validation.py:4, format [A-Z]{3}-\\d{4} e.g. ABC-1234", (True, True)),
    ("R5", "In the SKU checks, db.py:13", (False, False)),
    ("E1", "5 attempts (MAX_RETRIES=4, +1): http_client.py:12", (True, True)),
    ("E1", "2 attempts (legacy/old_client.py:9)", (False, False)),
    ("E1", "4 retries, so 5 tries at most. /tmp/x/reading/config.py:6", (True, True)),
    ("E3", "Nothing in the project sends emails.", (True, None)),
]

CALIB_SKIP = r"""
import json, sys
sys.argv = ["calibrate.py", "--quick"] + sys.argv[1:]
sys.path.insert(0, {scripts!r})
import calibrate as c
digest = open({digest_file!r}).read().strip()
c.common.installed = lambda: {{"m:1": {{"digest": digest}}}}
c.models_to_test = lambda: [("m:1", True)]
c.speed_probe = lambda m: ("50", "500")
c.run_task = lambda m, t, b: (print("RAN", t["id"]) or ("✅ 1st attempt", 0.1, 10))
c.run_reading = lambda m, a, b: (["| R1 | reading | ✅ answer | 0.1 min | |"], 1, 1)
c.measure_overhead = lambda m, b: 9000
c.main()
"""


def t_calibrate(E):
    sys.path.insert(0, os.path.join(ROOT, "local", "scripts"))
    import calibrate
    questions = {q["id"]: q for q in calibrate.READ_QUESTIONS}
    for qid, text, expected in CALIB_CASES:
        got = calibrate.score_answer(questions[qid], text)
        assert got == expected, (qid, text, got, expected)
    # Already calibrated and unchanged → skipped; changed digest, --force or a full run after a quick one → rerun.
    digest_file = os.path.join(E.tmp, "digest.txt")
    script = os.path.join(E.tmp, "calib_skip.py")
    open(script, "w").write(CALIB_SKIP.format(scripts=os.path.join(ROOT, "local", "scripts"), digest_file=digest_file))
    env = dict(E.env, XDG_DATA_HOME=os.path.join(E.tmp, "calib-data"))

    def calib(*args):
        r = subprocess.run([PY, script] + list(args), env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout
    open(digest_file, "w").write("sha256:aaa")
    assert "RAN" in calib()
    out = calib()
    assert "RAN" not in out and "skipped" in out and "Reading: 1/1" in out, out
    assert "RAN" in calib("--force")
    open(digest_file, "w").write("sha256:bbb")
    assert "RAN" in calib()
    return f"reading scorer on {len(CALIB_CASES)} answers; unchanged models skipped, --force and new versions rerun"


TEXTSTATS_SOLUTION = '''import re
from collections import Counter


def words(text):
    return re.findall(r"[a-z]+(?:'[a-z]+)?", text.lower())


def word_counts(text):
    return dict(Counter(words(text)))


def top_words(text, n=3):
    return sorted(word_counts(text).items(), key=lambda x: (-x[1], x[0]))[:n]


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\\s+", text) if s.strip()]


def mean_sentence_length(text):
    s = sentences(text)
    return round(sum(len(words(x)) for x in s) / len(s), 2) if s else 0.0


def excerpt(text, max_words=5):
    w = text.split()
    return " ".join(w[:max_words]) + ("\\u2026" if len(w) > max_words else "")


def report(text):
    return {"words": len(words(text)), "sentences": len(sentences(text)),
            "mean_sentence_length": mean_sentence_length(text), "top": top_words(text)}
'''

DETERMINANT = '''    if any(len(row) != len(m) for row in m):
        raise ValueError("not square")
    if len(m) == 1:
        return m[0][0]
    return sum((-1) ** j * m[0][j] * determinant([r[:j] + r[j + 1:] for r in m[1:]]) for j in range(len(m)))'''


def t_calibrate_tasks(E):
    """The calibration's code tasks are sound on this platform: each check fails on the starting files and
    passes with a reference solution; task 6 catches a rewritten working function; --only filters."""
    sys.argv, saved = ["calibrate.py"], sys.argv
    sys.path.insert(0, os.path.join(ROOT, "local", "scripts"))
    import importlib
    import calibrate
    importlib.reload(calibrate)
    sys.argv = saved
    tasks = {t["id"]: t for t in calibrate.TASKS}
    assert {"5-module", "6-continue"} <= set(tasks), list(tasks)

    def setup(task):
        repo = os.path.join(E.tmp, "ct-" + task["id"])
        shutil.rmtree(repo, ignore_errors=True)
        for rel, content in task["files"].items():
            path = os.path.join(repo, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        return repo

    def check(task, repo):
        return subprocess.run(task["check"], shell=True, cwd=repo, capture_output=True, text=True,
                              timeout=120).returncode == 0
    t5 = tasks["5-module"]
    repo = setup(t5)
    assert not check(t5, repo)
    open(os.path.join(repo, "textstats.py"), "w", encoding="utf-8").write(TEXTSTATS_SOLUTION)
    assert check(t5, repo), "reference solution of 5-module"
    t6 = tasks["6-continue"]
    repo = setup(t6)
    assert not check(t6, repo)
    path = os.path.join(repo, "matrix_utils.py")
    src = open(path, encoding="utf-8").read().replace("for j in range(len(a))]", "for j in range(len(b[0]))]")
    src = src.replace("    raise NotImplementedError", DETERMINANT)
    open(path, "w", encoding="utf-8").write(src)
    assert check(t6, repo), "reference solution of 6-continue"
    open(path, "w", encoding="utf-8").write(src.replace('"""n x n identity matrix."""', '"""Identity."""'))
    assert not check(t6, repo), "a rewritten working function must fail task 6"
    code = ("import sys; sys.argv = ['calibrate.py', '--only=5,6-continue']; sys.path.insert(0, %r); "
            "import calibrate; print([t['id'] for t in calibrate.TASKS])") % os.path.join(ROOT, "local", "scripts")
    out = subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=60).stdout
    assert "['5-module', '6-continue']" in out, out
    # The behaviour notes read from a task report.
    rep = os.path.join(E.tmp, "ct-notes", ".git", "local-agents")
    os.makedirs(rep, exist_ok=True)
    open(os.path.join(rep, "x.report.txt"), "w").write("Loop: m: calc.py edited 25 times (many tiny edits?)\n")
    note = calibrate.behaviour_notes(os.path.join(E.tmp, "ct-notes"), "x", [{"status": "stopped: step limit reached"}])
    assert note == " · tiny edits, step limit", note
    return "tasks 5 (module from tests) and 6 (continue partial work): fail at start, pass with a reference, " \
           "rewrite caught; --only; behaviour notes"


def t_savings(E):
    # End of task: the estimate is in the report and in the ledger; a resume only adds the difference.
    big = "".join(f"def helper_{i}(x):\n    return x * {i}\n\n" for i in range(600))
    repo = E.repo("sv", {"calc.py": CALC_BAD + big, "check.py": CHECK})
    E.scenario_set([{"tools": [["Read", {"file_path": os.path.join(E.tmp, "sv", "calc.py")}]],
                     "write": {"calc.py": CALC_OK + big}, "text": "add fixed."}])
    r = E.run("dispatch_task.py", "sv1", "Fix add() in calc.py.", "--write", "calc.py", "--check", f"{PY} check.py",
              cwd=repo)
    assert r.returncode == 0 and "Estimated Claude tokens saved by this task: ≈ " in r.stdout, r.stdout + r.stderr
    ledger = os.path.join(E.home, ".local", "share", "local-agents", "savings.jsonl")
    rows = [json.loads(l) for l in open(ledger, encoding="utf-8")]
    task = [x for x in rows if x["kind"] == "task" and x["id"] == "sv1"]
    assert len(task) == 1 and task[0]["in"] > 0 and task[0]["cwd"] == os.path.realpath(repo), rows
    # Conversation: a fake Claude Code transcript; ledger rows placed in the windows of its prompts.
    now = time.time()
    stamp = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))  # noqa: E731
    prompts = [(now - 400, "first prompt"), (now - 300, "second prompt"), (now - 200, "third prompt"),
               (now - 100, "fourth prompt"),
               (now - 5, "<command-name>/local</command-name><command-args>saved-tokens</command-args>")]
    tr = os.path.join(E.home, ".claude", "projects", "-proj", "abc.jsonl")
    os.makedirs(os.path.dirname(tr), exist_ok=True)
    with open(tr, "w", encoding="utf-8") as f:
        for t, text in prompts:
            f.write(json.dumps({"type": "user", "cwd": repo, "timestamp": stamp(t),
                                "message": {"role": "user", "content": text}}) + "\n")
            f.write(json.dumps({"type": "user", "cwd": repo, "timestamp": stamp(t + 1), "message": {
                "role": "user", "content": [{"type": "tool_result", "content": "x"}]}}) + "\n")
    with open(ledger, "w", encoding="utf-8") as f:
        for t, kind, saved in ((now - 350, "task", 1000), (now - 250, "question", 200), (now - 150, "task", 3000),
                               (now - 10000, "task", 99999)):                 # the last one: before the conversation
            f.write(json.dumps({"ts": t, "cwd": os.path.realpath(repo), "kind": kind, "id": "x", "model": "m",
                                "result": "passed", "in": saved, "out": 10}) + "\n")
        f.write(json.dumps({"ts": now - 150, "cwd": "/elsewhere", "kind": "task", "in": 77777, "out": 0}) + "\n")
    r = E.run("savings.py", "--conversation", cwd=repo)
    out = r.stdout
    assert r.returncode == 0 and "3 task(s)" not in out and "2 task(s), 1 question(s)" in out, out + r.stderr
    line = lambda prefix: next((l for l in out.splitlines() if prefix in l), "")  # noqa: E731
    assert "input: saved ~4,200" in line("Delegations:") and "99,999" not in out and "77,777" not in out, out
    last3 = out.split("Last 3 prompts:")[1]
    assert "first prompt" not in last3 and "saved-tokens" not in last3, out
    assert '"second prompt": 1 question(s) → ≈ 250 tokens saved in input equivalent (input: saved ~200' in last3, out
    assert '"third prompt": 1 task(s) → ≈ 3,050 tokens saved' in last3, out
    assert '"fourth prompt": no delegation.' in last3, out
    # The skill's own cost: /local typed, without its text in the transcript → the size of SKILL.md.
    skill_tokens = round(os.path.getsize(os.path.join(ROOT, "local", "SKILL.md")) / 3.5)
    assert f"1 time(s), ~{skill_tokens:,} input tokens (a cost)" in out, out
    assert f"input: saved ~{4200 - skill_tokens:,}" in line("Net savings:"), out
    assert "Input equivalent = input + 5 × output" in out, out
    # Its text injected in the transcript (7,000 chars = 2,000 tokens, during the third prompt): counted exactly.
    with open(tr, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "isMeta": True, "cwd": repo, "timestamp": stamp(now - 199), "message": {
            "role": "user", "content": [{"type": "text", "text": "Base directory for this skill: /h/.claude/skills/local\n"
                                         + "x" * (7000 - 55)}]}}) + "\n")
    out = E.run("savings.py", "--conversation", cwd=repo).stdout
    assert "1 time(s), ~2,000 input tokens (a cost)" in out and "input: saved ~2,200" in line("Net savings:"), out
    assert "input: saved ~1,000" in line('"third prompt"') and "skill cost ~2,000 included" in out, out
    # Measured: the delegations' recorded estimated cost replaced by what the transcript shows Claude read
    # (here a 3,500-char report = 1,000 tokens) around them; the commands of other tools are not counted.
    with open(ledger, "w", encoding="utf-8") as f:
        for t, kind, saved in ((now - 350, "task", 1000), (now - 250, "question", 200), (now - 150, "task", 3000)):
            f.write(json.dumps({"ts": t, "cwd": os.path.realpath(repo), "kind": kind, "id": "x", "model": "m",
                                "result": "passed", "in": saved, "out": 10, "cost_in": 500, "cost_out": 100}) + "\n")
    with open(tr, "a", encoding="utf-8") as f:
        for tid, cmd, body in (("u1", "python3 ~/.claude/skills/local/scripts/dispatch_task.py t1 'Fix add()'", "r" * 3500),
                               ("u2", "ls -la", "z" * 9000),
                               ("u3", "sed -n 1,9p ~/src/claude-local-agents/local/scripts/dispatch_task.py", "w" * 9000)):
            f.write(json.dumps({"type": "assistant", "cwd": repo, "timestamp": stamp(now - 140), "message": {
                "role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": "Bash",
                                                  "input": {"command": cmd}}]}}) + "\n")
            f.write(json.dumps({"type": "user", "cwd": repo, "timestamp": stamp(now - 139), "message": {
                "role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": body}]}}) + "\n")
    out = E.run("savings.py", "--conversation", cwd=repo).stdout
    assert "Measured in this conversation: ~1,000 input tokens read" in out and "estimate: ~1,500" in out, out
    assert "input: saved ~2,700" in line("Net savings, measured:"), out          # 4,200 + 1,500 - 1,000 - 2,000 (skill)
    # A background task's notification is not a prompt of the user.
    n_prompts = re.search(r"(\d+) prompt\(s\)", out).group(1)
    with open(tr, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "cwd": repo, "timestamp": stamp(now - 100), "message": {
            "role": "user", "content": "<task-notification> <task-id>b1</task-id> done</task-notification>"}}) + "\n")
    out = E.run("savings.py", "--conversation", cwd=repo).stdout
    assert "task-notification" not in out and f"{n_prompts} prompt(s)" in out, out
    # A task next to the user's own untracked file: that file is not counted as written by the task.
    repo2 = E.repo("sv2", {"calc.py": CALC_BAD, "check.py": CHECK})
    open(os.path.join(repo2, "notebook.ipynb"), "w").write("{}" * 50000)
    E.scenario_set([{"write": {"calc.py": CALC_OK}, "text": "add fixed."}])
    r = E.run("dispatch_task.py", "sv2", "Fix add() in calc.py.", "--write", "calc.py", "--check", f"{PY} check.py",
              cwd=repo2)
    est = [json.loads(l) for l in open(os.path.join(repo2, ".git", "local-agents", "sv2.stats.jsonl"))
           if '"estimate"' in l][-1]
    assert r.returncode == 0 and est["added"] < 2000 and est["diff"] < 3000, (est, r.stdout)
    assert "Small task:" in r.stdout, r.stdout
    return ("end-of-task estimate + ledger (user's untracked files left out, small tasks flagged); "
            "/local saved-tokens: conversation, last 3 prompts, skill cost, measured net")


def t_models(E):
    md = os.path.join(E.home, ".claude", "local-agents", "models.md")
    before = open(md, encoding="utf-8").read()
    try:
        r = E.run("models.py")
        out = r.stdout
        assert r.returncode == 0 and "| gemma3:12b | no |" in out and "Not listed yet: gemma3:12b" in out, out + r.stderr
        assert "nomic-embed" not in out and "glm-4.7" not in out, out
        r = E.run("models.py", "add", "glm-4.7:cloud", "gemma3:12b", "nomic-embed-text")
        assert "cloud model, never added" in r.stdout and "gemma3:12b added: questions" in r.stdout, r.stdout + r.stderr
        text = open(md, encoding="utf-8").read()
        assert "## gemma3:12b\n- Role: questions" in text and text.index("## gemma3:12b") < text.index("## Selection") \
            if "## Selection" in text else "## gemma3:12b" in text, text
        assert "DEFAULT" not in text.split("## gemma3:12b")[1].split("\n")[0], text     # a DEFAULT already exists
        r = E.run("models.py", "add", "gemma3:12b")
        assert "already in models.md" in r.stdout, r.stdout
        # /local size: the general preference only, never a model's own line.
        r = E.run("models.py", "size", "small")
        assert r.returncode == 0 and "Task size: small" in r.stdout and "gemma3:12b" in r.stdout, r.stdout + r.stderr
        r = E.run("models.py", "size", "Large")
        text = open(md, encoding="utf-8").read()
        prefs = text.split("## Preferences")[1].split("\n## ")[0]
        assert prefs.count("- Task size:") == 1 and "- Task size: large" in prefs, text
        assert "- Task size: tiny" in text.split("## gemma3:12b")[1], text          # its own line untouched
        r = E.run("models.py", "size")
        assert "General size: large" in r.stdout and "| gemma3:12b | tiny | tiny (its own) |" in r.stdout, r.stdout
        for size in ("tiny", "small", "medium", "large", "xlarge", "35%"):       # every size, both ways
            for flag in ([], ["--force"], ["--no-force"]):
                r = E.run("models.py", "size", size, *flag)
                assert r.returncode == 0, (size, flag, r.stdout + r.stderr)
                assert "- Task size: tiny" in open(md, encoding="utf-8").read().split("## gemma3:12b")[1], (size, flag)
        r = E.run("models.py", "size", "small", "--force")
        r = E.run("models.py", "size")
        assert "FORCED for every model: small" in r.stdout and "| gemma3:12b | tiny | small (forced) |" in r.stdout, r.stdout
        E.run("models.py", "size", "--no-force")
        r = E.run("models.py", "size", "huge")
        assert r.returncode == 2 and "tiny, small, medium" in r.stderr, r.stdout + r.stderr
    finally:
        open(md, "w", encoding="utf-8").write(before)
    # A new user: models.md created from the template, the first model with tool calling becomes DEFAULT.
    home2 = os.path.join(E.tmp, "home-models")
    os.makedirs(home2, exist_ok=True)
    r = E.run("models.py", "add", "gemma3:12b", "devstral-small-2:24b", env={"HOME": home2, "USERPROFILE": home2})
    text = open(os.path.join(home2, ".claude", "local-agents", "models.md"), encoding="utf-8").read()
    assert "## devstral-small-2:24b — DEFAULT" in text and "## gemma3:12b\n" in text, r.stdout + r.stderr + text
    assert "- Task size: medium" in text.split("## devstral-small-2:24b")[1].split("##")[0], text
    assert "- Task size: tiny" in text.split("## gemma3:12b")[1].split("##")[0], text
    assert text.index("## devstral-small-2:24b") < text.index("## Selection rules"), text
    # Preferences of the template carry their options in a note: read without it, and kept when rewritten.
    env2 = {"HOME": home2, "USERPROFILE": home2}
    E.run("models.py", "size", "small", env=env2)
    r = E.run("models.py", "size", env=env2)
    text = open(os.path.join(home2, ".claude", "local-agents", "models.md"), encoding="utf-8").read()
    assert "General size: small" in r.stdout and "- Task size: small  (tiny · small" in text, r.stdout + text
    return "screens installed models (no cloud, no embeddings) and adds the chosen ones to models.md"


def dispatch_size(E, model):
    code = ("import sys, types; sys.path.insert(0, %r); import dispatch_task; "
            "print(dispatch_task.task_size(types.SimpleNamespace(size=''), %r)[0])") % (os.path.join(ROOT, "local", "scripts"), model)
    return subprocess.run([PY, "-c", code], env=E.env, capture_output=True, text=True, timeout=60).stdout.strip()


def t_task_size(E):
    big = "".join(f"def f{i}(x):\n    return x + {i}  # padding padding padding padding\n\n" for i in range(1300))
    repo = E.repo("sz", {"calc.py": CALC_BAD + big, "check.py": CHECK})
    E.scenario_set([{"write": {"calc.py": CALC_OK + big}, "text": "add fixed."}])
    n = len(E.calls())
    r = E.run("dispatch_task.py", "sz1", "Fix add() in calc.py.", "--write", "calc.py", "--check", f"{PY} check.py",
              cwd=repo)
    assert r.returncode == 6 and "too big" in r.stdout and "Split it" in r.stdout, r.stdout + r.stderr
    assert "calc.py alone" in r.stdout and "does not fit" in r.stdout, r.stdout
    assert len(E.calls()) == n, "no agent may run for a task that is too big"
    r = E.run("dispatch_task.py", "sz2", "Fix add() in calc.py.", "--write", "calc.py", "--check", f"{PY} check.py",
              "--size", "xlarge", cwd=repo)
    assert r.returncode == 0 and "passed" in r.stdout.splitlines()[0], r.stdout + r.stderr
    subprocess.run(["git", "checkout", "-q", "--", "."], cwd=repo)
    # The context fills up during the work: no in-session fixes, no other model; split it.
    md = os.path.join(E.home, ".claude", "local-agents", "models.md")
    before = open(md, encoding="utf-8").read()            # restored at the end (the size is learned below)
    repo2 = E.repo("sz3", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"text": "API Error: 400 prompt is too long: 70210 tokens > 65536 maximum"}])
    n = len(E.calls())
    r = E.run("dispatch_task.py", "sz3", "Fix add() in calc.py.", "--keep-order", "--models", "qwen3.6:35b-a3b devstral-small-2:24b",
              "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo2)
    assert r.returncode == 6 and "context full" in r.stdout, r.stdout + r.stderr
    assert len(E.calls()) == n + 1, E.calls()[n:]
    # The model's task size is learned: one step down at once after its context filled up.
    try:
        assert "Task size of qwen3.6:35b-a3b: medium → small" in r.stdout, r.stdout
        assert "- Task size: small" in open(md, encoding="utf-8").read().split("## qwen3.6:35b-a3b")[1].split("##")[0]
        # ...and one step up after RAISE_AFTER passes close to the limit; a percentage is left alone.
        code = ("import sys; sys.path.insert(0, %r); import common; "
                "print([common.adjust_task_size('qwen3.6:35b-a3b', 'small', 'passed', f) for f in (0.7, 0.3, 0.8, 0.9)]); "
                "print(common.adjust_task_size('qwen3.6:35b-a3b', '35%%', 'context full'))") % os.path.join(ROOT, "local", "scripts")
        out = subprocess.run([PY, "-c", code], env=E.env, capture_output=True, text=True, timeout=60).stdout
        assert out.count("small → medium") == 1 and out.strip().endswith("\n") is False, out
        assert "- Task size: medium" in open(md, encoding="utf-8").read().split("## qwen3.6:35b-a3b")[1].split("##")[0]
        # A forced size wins over the model's own; --size wins over everything; no learning then.
        r = E.run("models.py", "size", "tiny", "--force")
        assert "forced to tiny" in r.stdout, r.stdout + r.stderr
        assert dispatch_size(E, "qwen3.6:35b-a3b") == "tiny"
        r = E.run("models.py", "size", "--no-force")
        assert "no longer forced" in r.stdout and dispatch_size(E, "qwen3.6:35b-a3b") == "medium", r.stdout
        # <size> --no-force: forced size lifted, and the general size set for the models without their own.
        E.run("models.py", "size", "tiny", "--force")
        r = E.run("models.py", "size", "large", "--no-force")
        assert "no longer forced" in r.stdout and "Task size: large" in r.stdout, r.stdout + r.stderr
        assert dispatch_size(E, "qwen3.6:35b-a3b") == "medium" and dispatch_size(E, "gpt-oss:20b") == "large"
    finally:
        open(md, "w", encoding="utf-8").write(before)
    sys.path.insert(0, os.path.join(ROOT, "local", "scripts"))
    import types
    import dispatch_task
    plan = dispatch_task.split_plan({"a.py": 9000, "b.py": 8000, "c.py": 3000, "d.py": 2000}, "x" * 400, 26000)
    assert plan[0].startswith("Suggested split: 2 tasks") and "[a.py, c.py, d.py] · [b.py]" in plan[0], plan
    for raw, expected in (("tiny", ("tiny", 0.20)), ("1", ("tiny", 0.20)), ("xlarge", ("xlarge", 0.65)),
                          ("medium", ("medium", 0.40)), ("35%", ("35%", 0.35)), ("medium (default)", ("medium", 0.40)),
                          ("Small", ("small", 0.30)), ("9", ("medium", 0.40)), ("95%", ("medium", 0.40))):
        got = dispatch_task.task_size(types.SimpleNamespace(size=raw))
        assert got == expected, (raw, got)
    real = dispatch_task.common.model_setting      # the model's own "Task size" wins over the preference
    try:
        dispatch_task.common.model_setting = lambda m, n, d="": "small" if m == "small-model:1b" else d
        assert dispatch_task.task_size(types.SimpleNamespace(size=""), "small-model:1b") == ("small", 0.30)
        assert dispatch_task.task_size(types.SimpleNamespace(size="large"), "small-model:1b") == ("large", 0.50)
    finally:
        dispatch_task.common.model_setting = real
    return ("too big: refused before running, or stopped when the context fills up (exit 6); size learned per "
            "model, forced, lifted")


def t_unload(E):
    LOADED.update({"qwen3.6:35b-a3b": 28, "devstral-small-2:24b": 17})
    run_dir = os.path.join(E.home, ".claude", "local-agents", "running")
    os.makedirs(run_dir, exist_ok=True)
    busy = os.path.join(run_dir, "busy.json")
    with open(busy, "w") as f:                       # a running agent (this process) uses devstral
        json.dump({"pid": os.getpid(), "model": "devstral-small-2:24b", "weights": 15, "need": 17}, f)
    try:
        r = E.run("unload.py", "--list")
        assert "devstral-small-2:24b: 17.0 GB — in use by 1" in r.stdout, r.stdout + r.stderr
        r = E.run("unload.py")
        assert "✔ qwen3.6:35b-a3b unloaded" in r.stdout and "devstral-small-2:24b: kept" in r.stdout, r.stdout + r.stderr
        assert list(LOADED) == ["devstral-small-2:24b"], LOADED
        r = E.run("unload.py", "--force")
        assert "✔ devstral-small-2:24b unloaded" in r.stdout and not LOADED, r.stdout + r.stderr
        r = E.run("unload.py")
        assert "already free" in r.stdout, r.stdout
    finally:
        os.remove(busy)
        LOADED.clear()
        LOADED["qwen3.6:35b-a3b"] = 28
    return "unloads loaded models, keeps those in use unless --force"


def t_progress(E):
    # Live progress in the task's shell: task, attempts, agent actions, failed check fixed in session,
    # next model; the report still follows (and is saved to its file).
    repo = E.repo("pg", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "qwen3.6:35b-a3b", "text": "I could not fix it."},
                    {"model": "devstral-small-2:24b", "if": "The check failed",
                     "write": {"calc.py": CALC_OK}, "text": "Fixed."},
                    {"model": "devstral-small-2:24b", "write": {"calc.py": CALC_BAD + "# try\n"}, "text": "Done."}])
    r = E.run("dispatch_task.py", "pg1", "Fix add() in calc.py.", "--keep-order", "--models", "qwen3.6:35b-a3b devstral-small-2:24b",
              "--write", "calc.py", "--check", f"{PY} check.py", "--retries", "2", cwd=repo, env={"LOCAL_PROGRESS": "on"})
    out = r.stdout
    for piece in ("▶ Task pg1: qwen3.6:35b-a3b → devstral-small-2:24b · task size medium",
                  "▶ attempt 1 · qwen3.6:35b-a3b", "agent finished: done",
                  "→ devstral-small-2:24b starts over with its summary", "  · writes calc.py",
                  "→ the agent fixes it in the same session (round 1/2)", "✓ checks passed", "■ passed"):
        assert piece in out, (piece, out)
    lines = out.splitlines()
    assert all(l.startswith("[") for l in lines[:lines.index("")]), out           # progress first, then report
    report = open(os.path.join(repo, ".git", "local-agents", "pg1.report.txt"), encoding="utf-8").read()
    assert report.startswith("== Task pg1: passed") and "[00:" not in report, report   # the report file stays clean
    r = E.run("dispatch_task.py", "pg2", "x", "--write", "calc.py", cwd=E.repo("pg2", {"calc.py": CALC_OK}),
              env={"LOCAL_PROGRESS": "off"})
    assert "▶" not in r.stdout, r.stdout
    return "live progress: task, attempts, agent actions, in-session fixes, next model; off with LOCAL_PROGRESS=off"


def t_failure_handling(E):
    # Lessons from a real session: readable check output, no wasted sessions, a chain that keeps going.
    sys.path.insert(0, os.path.join(ROOT, "local", "scripts"))
    import common
    # 1. The check output keeps the project's frames and the error, drops library warnings.
    noisy = ("tests/test_m.py:12: in test_hist\n    h = m.value_hist(v)\nmetrics.py:148: in value_hist\n"
             "    counts, edges = np.histogram(d, bins='auto')\n/v/.venv/lib/python3.12/site-packages/numpy/h.py:780: in histogram\n"
             "    x = f()\nE   ValueError: autodetected range of [nan, nan] is not finite\n"
             + "\n".join("/v/.venv/lib/python3.12/site-packages/pyparsing/core.py:1: PyparsingDeprecationWarning: x"
                         for _ in range(80))
             + "\nFAILED tests/test_m.py::test_hist - ValueError\n===== 1 failed, 6 passed in 2.31s =====")
    digest = common.check_digest(noisy, 15)
    assert "metrics.py:148" in digest and "E   ValueError" in digest and "Deprecation" not in digest, digest
    assert "1 failed, 6 passed" in digest and len(digest.splitlines()) <= 16, digest
    assert common.project_frames(noisy.splitlines())[-1] == "metrics.py:148"
    # 2. pytest checks get readable options (the command's own options still win).
    repo = E.repo("fh1", {"a.py": "x = 1\n"})
    sys.path.insert(0, os.path.join(ROOT, "local", "scripts"))
    from check_run import run_check
    ok, text = run_check(repo, PY + ' -c "import os; print(os.environ[\'PYTEST_ADDOPTS\'])" # pytest')
    assert "-p no:warnings" in text and "--tb=short" in text, text
    ok, text = run_check(repo, PY + ' -c "import os; print(os.environ[\'PYTEST_ADDOPTS\'])" # pytest --tb=long')
    assert "--tb=short" not in text, text
    # 3. A session that ran out of steps: the next model takes over (no new session of the same model).
    repo = E.repo("fh3", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "qwen3.6:35b-a3b", "subtype": "error_max_turns", "text": "stopped"},
                    {"model": "devstral-small-2:24b", "write": {"calc.py": CALC_OK}, "text": "Fixed."}])
    n = len(E.calls())
    r = E.run("dispatch_task.py", "fh3", "Fix add().", "--keep-order", "--models", "qwen3.6:35b-a3b devstral-small-2:24b",
              "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo)
    models = [c["model"] for c in E.calls()[n:] if c["turn"] == 0]
    assert r.returncode == 0 and models == ["qwen3.6:35b-a3b", "devstral-small-2:24b"], (models, r.stdout)
    # 4. Endless output: next model at once.
    repo = E.repo("fh4", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "qwen3.6:35b-a3b", "subtype": "error_during_execution",
                     "text": "API Error: Claude's response exceeded the 8192 output token maximum."},
                    {"model": "devstral-small-2:24b", "write": {"calc.py": CALC_OK}, "text": "Fixed."}])
    n = len(E.calls())
    r = E.run("dispatch_task.py", "fh4", "Fix add().", "--keep-order", "--models", "qwen3.6:35b-a3b devstral-small-2:24b",
              "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo)
    models = [c["model"] for c in E.calls()[n:] if c["turn"] == 0]
    assert r.returncode == 0 and models == ["qwen3.6:35b-a3b", "devstral-small-2:24b"], (models, r.stdout)
    assert "endless output" in r.stdout, r.stdout
    # 5. Actions are logged; re-reading the same file again and again is reported.
    repo = E.repo("fh5", {"calc.py": CALC_BAD, "check.py": CHECK})
    target = os.path.join(E.tmp, "fh5", "calc.py")
    E.scenario_set([{"tools": [["Read", {"file_path": target}]] * 6, "write": {"calc.py": CALC_OK}, "text": "ok"}])
    r = E.run("dispatch_task.py", "fh5", "Fix add().", "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo)
    actions = open(os.path.join(repo, ".git", "local-agents", "fh5.actions.log"), encoding="utf-8").read()
    assert actions.count("reads calc.py") == 6 and "writes calc.py" in actions, actions
    assert "Loop: " in r.stdout and "calc.py read 6 times" in r.stdout, r.stdout
    # 6. A process left behind by a command is stopped when the command ends (it would undo a stash).
    late = os.path.join(E.tmp, "late.txt")
    common.run(["sh", "-c", f"(sleep 1; echo x > {late}) >/dev/null 2>&1 </dev/null & echo started"], timeout=30)
    time.sleep(2)
    assert not os.path.exists(late), "a child of the command kept running"
    # 7. A stash whose files come back: one stash only, and the reason is told.
    repo = E.repo("fh7", {"m.py": "a = 1\n"})
    path = os.path.join(repo, "m.py")
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            open(path, "w").write(f"a = {time.time()}\n")
            time.sleep(0.2)
    open(path, "w").write("a = 2\n")
    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        created, left, why = common.stash_detail(repo, "test")
    finally:
        stop.set()
        t.join()
    assert created and left == ["m.py"] and "still writing" in why, (created, left, why)
    assert len(git(repo, "stash", "list").splitlines()) == 1
    return "check digest, pytest options, step limit / endless output → next model, actions log, loops, stash"


def t_session_feedback(E):
    # 1. The user's untracked files (outside --write) are tolerated and never touched, even when a model
    #    fails and its work is set aside; an untracked file inside --write stops the task.
    repo = E.repo("sf1", {"calc.py": CALC_BAD, "check.py": CHECK})
    with open(os.path.join(repo, "notes.ipynb"), "w") as f:
        f.write('{"cells": []}\n')
    E.scenario_set([{"model": "qwen3.6:35b-a3b", "write": {"calc.py": CALC_BAD + "# nope\n"}, "text": "tried"},
                    {"model": "devstral-small-2:24b", "write": {"calc.py": CALC_OK}, "text": "fixed"}])
    r = E.run("dispatch_task.py", "sf1", "Fix add().", "--models", "qwen3.6:35b-a3b devstral-small-2:24b",
              "--write", "calc.py", "--check", f"{PY} check.py", "--keep-order", cwd=repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert open(os.path.join(repo, "notes.ipynb")).read() == '{"cells": []}\n', "untracked file touched"
    assert "notes.ipynb" not in git(repo, "stash", "show", "--include-untracked", "--name-only", "stash@{0}")
    assert "notes.ipynb" not in r.stdout.split("== Changed files ==")[1], r.stdout
    repo2 = E.repo("sf1b", {"calc.py": CALC_BAD})
    open(os.path.join(repo2, "helper.py"), "w").write("x = 1\n")
    r = E.run("dispatch_task.py", "sf1b", "Fix add().", "--write", "calc.py helper.py", cwd=repo2)
    assert r.returncode != 0 and "untracked files inside the task's scope" in r.stderr, r.stderr
    # 2. Baseline check: failures that were already there are told apart; a check that already passes is flagged.
    assert "Before the task the check failed; now it passes." in open(
        os.path.join(repo, ".git", "local-agents", "sf1.report.txt")).read()
    repo3 = E.repo("sf2", {"calc.py": CALC_OK, "check.py": CHECK})
    E.scenario_set([{"write": {"calc.py": CALC_OK + "# docs\n"}, "text": "done"}])
    r = E.run("dispatch_task.py", "sf2", "Document add().", "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo3)
    assert "the check already passed: it may not test the requested change" in r.stdout, r.stdout
    # 3. The script puts first the model with the clearly better record on this kind of task.
    mem = os.path.join(E.home, ".claude", "local-agents", "memory.json")
    data = json.load(open(mem)) if os.path.exists(mem) else {}
    data.setdefault("qwen3.6:35b-a3b", {})["kinds"] = {"changes, checked": [0, 3]}
    data.setdefault("devstral-small-2:24b", {})["kinds"] = {"changes, checked": [3, 3]}
    json.dump(data, open(mem, "w"))
    repo4 = E.repo("sf3", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"write": {"calc.py": CALC_OK}, "text": "fixed"}])
    n = len(E.calls())
    r = E.run("dispatch_task.py", "sf3", "Fix add().", "--models", "qwen3.6:35b-a3b devstral-small-2:24b",
              "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo4)
    assert E.calls()[n]["model"] == "devstral-small-2:24b" and "order changed: devstral-small-2:24b first" in r.stdout, r.stdout
    after = json.load(open(mem))["devstral-small-2:24b"]["kinds"]["changes, checked"]
    assert after == [4, 4], after                                     # the outcome is recorded
    repo5 = E.repo("sf3b", {"calc.py": CALC_BAD, "check.py": CHECK})
    n = len(E.calls())
    E.run("dispatch_task.py", "sf3b", "Fix add().", "--models", "qwen3.6:35b-a3b devstral-small-2:24b",
          "--write", "calc.py", "--check", f"{PY} check.py", "--keep-order", cwd=repo5)
    assert E.calls()[n]["model"] == "qwen3.6:35b-a3b"
    # 4. What Claude finishes by hand after a failed task counts as a cost; success rate per model.
    repo6 = E.repo("sf4", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"write": {"calc.py": CALC_BAD + "# still wrong\n"}, "text": "tried"}])
    r = E.run("dispatch_task.py", "sf4", "Fix add().", "--models", "qwen3.6:35b-a3b", "--no-fallback", "--write", "calc.py",
              "--check", f"{PY} check.py", "--retries", "0", cwd=repo6)
    assert r.returncode == 1, r.stdout
    open(os.path.join(repo6, "calc.py"), "w").write(CALC_OK + "".join(f"# fixed by hand {i}\n" for i in range(40)))
    r = E.run("savings.py", cwd=repo6)
    assert "Finished by hand after a failed task: 1 (sf4)" in r.stdout, r.stdout + r.stderr
    assert "Models: qwen3.6:35b-a3b 0/1 passed" in r.stdout, r.stdout
    fixes = [json.loads(l) for l in open(os.path.join(E.home, ".local", "share", "local-agents", "savings.jsonl"))
             if '"manual fix"' in l]
    assert len(fixes) == 1 and fixes[0]["out"] < 0 and fixes[0]["id"] == "sf4", fixes
    E.run("savings.py", cwd=repo6)                                   # counted once only
    assert len([l for l in open(os.path.join(E.home, ".local", "share", "local-agents", "savings.jsonl"))
                if '"manual fix"' in l]) == 1
    return "untracked files tolerated, baseline check, model reordering by record, manual fixes, success rates"


def t_learning(E):
    scripts = os.path.join(ROOT, "local", "scripts")
    # 1. Scope: a new untracked file the agent never touched is the user's (created while the task ran).
    repo = E.repo("ln1", {"calc.py": CALC_BAD, "other.py": "x = 1\n"})
    with open(os.path.join(repo, "notes.md"), "w") as f:
        f.write("mine\n")
    with open(os.path.join(repo, "other.py"), "a") as f:
        f.write("y = 2\n")
    sec = os.path.join(E.tmp, "ln1-sec.log")
    with open(sec, "w") as f:
        f.write(json.dumps({"decision": "ALLOWED (rule)", "tool": "Edit", "detail": "calc.py"}) + "\n")
    code = ("import sys, json; sys.path.insert(0, %r); import common; "
            "print(json.dumps(common.out_of_scope(sys.argv[1], 'calc.py', sys.argv[2])))") % scripts
    out = lambda: json.loads(subprocess.run([PY, "-c", code, repo, sec], env=E.env, capture_output=True,  # noqa: E731
                                            text=True, timeout=60).stdout)
    assert out() == ["other.py"], out()                # tracked change: always the agent's to answer for
    code2 = ("import sys; sys.path.insert(0, %r); import dispatch_task as d; "
             "print(d.changed_summary(sys.argv[1], 'calc.py', sys.argv[2]))") % scripts
    summ = subprocess.run([PY, "-c", code2, repo, sec], env=E.env, capture_output=True, text=True, timeout=60)
    assert "other.py" in summ.stdout and "notes.md" not in summ.stdout, summ.stdout + summ.stderr  # report too
    with open(sec, "a") as f:
        f.write(json.dumps({"decision": "ALLOWED (rule)", "tool": "Bash", "detail": "echo x > notes.md"}) + "\n")
    assert out() == ["notes.md", "other.py"], out()    # named by one of its commands: the agent's
    # 2. Lessons and the learned output room, in an isolated home.
    home = os.path.join(E.tmp, "ln-home")
    os.makedirs(home, exist_ok=True)
    code = ("import sys; sys.path.insert(0, %r); import common as c; m = 'qwen3.6:35b-a3b'; "
            "c.record_attempt(m, 'output limit', False); assert 'one WHOLE function per Edit' in c.lesson_note(m); "
            "assert c.raise_output_reserve(m) == ''; c.record_attempt(m, 'output limit', False, lessons=['output limit']); "
            "assert '8192 → 16384' in c.raise_output_reserve(m), 'raise'; assert c.agent_output_reserve(0, m) == 16384; "
            "assert c.agent_output_reserve(0, 'other') == 8192; c.record_attempt(m, None, True, 120); "
            "c.record_attempt(m, 'output limit', False, lessons=['output limit']); "
            "l = '\\n'.join(c.learned_lines([m])); print(l); "
            "assert 'passed up to 120 lines' in l and 'output limit ×3' in l and 'lessons not enough' in l, l; "
            "c.record_attempt(m, 'churn', False); c.record_attempt(m, 'churn', True, lessons=['churn']); "
            "c.record_attempt(m, 'churn', False, lessons=['churn']); l = '\\n'.join(c.learned_lines([m])); "
            "assert 'output limit, churn' in l, l") % scripts
    r = subprocess.run([PY, "-c", code], env=dict(E.env, HOME=home, USERPROFILE=home), capture_output=True,
                       text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    # 3. End to end: an endless output is learned, and the next task's instruction carries the lesson.
    home2 = os.path.join(E.tmp, "ln-home2")
    shutil.copytree(E.home, home2)
    env = dict(HOME=home2, USERPROFILE=home2, XDG_DATA_HOME=os.path.join(home2, ".local", "share"))
    repo = E.repo("ln3", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "qwen3.6:35b-a3b", "subtype": "error_during_execution",
                     "text": "API Error: Claude's response exceeded the 8192 output token maximum."},
                    {"model": "devstral-small-2:24b", "write": {"calc.py": CALC_OK}, "text": "Fixed."}])
    r = E.run("dispatch_task.py", "ln3", "Fix add().", "--keep-order", "--models", "qwen3.6:35b-a3b devstral-small-2:24b",
              "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo, env=env)
    assert "Learned: qwen3.6:35b-a3b failed by output limit" in r.stdout, r.stdout
    repo = E.repo("ln4", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_OK}, "text": "Fixed."}])
    n = len(E.calls())
    r = E.run("dispatch_task.py", "ln4", "Fix add().", "--models", "qwen3.6:35b-a3b", "--write", "calc.py",
              "--check", f"{PY} check.py", cwd=repo, env=env)
    first = [c for c in E.calls()[n:] if c["turn"] == 0][0]
    assert "one WHOLE function per Edit" in first["prompt"] and "passed" in r.stdout.splitlines()[0], r.stdout
    # 4. Best attempt: a model that made progress is continued, not thrown away, and its work is put back
    #    when the next model does worse.
    tests = ("import unittest, calc\nclass T(unittest.TestCase):\n"
             "    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n"
             "    def test_sub(self):\n        self.assertEqual(calc.sub(5, 3), 2)\n")
    bad = "def add(a, b):\n    return a - b\n\ndef sub(a, b):\n    return a + b\n"
    half = "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a + b\n"
    repo = E.repo("ln5", {"calc.py": bad, "test_calc.py": tests})
    E.scenario_set([{"model": "qwen3.6:35b-a3b", "write": {"calc.py": half}, "text": "Half done."},
                    {"model": "devstral-small-2:24b", "write": {"calc.py": "def add(a, b:\n"}, "text": "Oops."}])
    n = len(E.calls())
    r = E.run("dispatch_task.py", "ln5", "Fix add() and sub().", "--keep-order", "--retries", "0",
              "--models", "qwen3.6:35b-a3b devstral-small-2:24b", "--write", "calc.py",
              "--check", f"{PY} -m unittest test_calc", cwd=repo, env=env)
    second = [c for c in E.calls()[n:] if c["turn"] == 0][1]
    assert "Continue from it" in second["prompt"] and "1 test(s) pass now (0 before it)" in second["prompt"], \
        second["prompt"]
    assert open(os.path.join(repo, "calc.py")).read() == half, r.stdout
    assert "Best attempt put back in the files: qwen3.6:35b-a3b's, 1 test(s) passing" in r.stdout, r.stdout
    assert "[lessons] devstral-small-2:24b" not in open(os.path.join(repo, ".git", "local-agents", "ln5.log")).read()
    # 5. Reading different parts of a file is normal work, not going in circles.
    repo = E.repo("ln6", {"calc.py": CALC_BAD, "check.py": CHECK})
    target = os.path.join(E.tmp, "ln6", "calc.py")
    E.scenario_set([{"tools": [["Read", {"file_path": target, "offset": o}] for o in (1, 50, 100, 150, 200)],
                     "write": {"calc.py": CALC_OK}, "text": "ok"}])
    r = E.run("dispatch_task.py", "ln6", "Fix add().", "--write", "calc.py", "--check", f"{PY} check.py",
              cwd=repo, env=env)
    assert "passed" in r.stdout.splitlines()[0] and "Loop:" not in r.stdout, r.stdout
    assert "[lessons] qwen3.6:35b-a3b: output limit" in open(os.path.join(repo, ".git", "local-agents", "ln6.log")).read()
    # 6. A pass reached through many tiny edits is learned too (churn): no size increase, a lesson next time.
    repo = E.repo("ln7", {"calc.py": CALC_BAD, "check.py": CHECK})
    target = os.path.join(E.tmp, "ln7", "calc.py")
    E.scenario_set([{"model": "devstral-small-2:24b", "tools": [["Edit", {"file_path": target, "old_string": "a",
                     "new_string": "a"}]] * 22, "write": {"calc.py": CALC_OK}, "text": "ok"}])
    r = E.run("dispatch_task.py", "ln7", "Fix add().", "--models", "devstral-small-2:24b", "--write", "calc.py",
              "--check", f"{PY} check.py", cwd=repo, env=env)
    assert "passed" in r.stdout.splitlines()[0] and "calc.py edited 23 times" in r.stdout, r.stdout
    assert "Learned: devstral-small-2:24b passed, but wastefully: churn" in r.stdout, r.stdout
    repo = E.repo("ln8", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_OK}, "text": "Fixed."}])
    n = len(E.calls())
    E.run("dispatch_task.py", "ln8", "Fix add().", "--models", "devstral-small-2:24b", "--write", "calc.py",
          "--check", f"{PY} check.py", cwd=repo, env=env)
    first = [c for c in E.calls()[n:] if c["turn"] == 0][0]
    assert "no series of one-line edits" in first["prompt"], first["prompt"]
    # 7. The same search again and again, inside the virtualenv: a loop, and wandering in the environment.
    repo = E.repo("ln9", {"calc.py": CALC_BAD, "check.py": CHECK, ".venv/bin/activate": "# easy\n"})
    venv = os.path.join(E.tmp, "ln9", ".venv", "bin", "activate")
    E.scenario_set([{"model": "gpt-oss:20b", "tools": [["Grep", {"pattern": "easy", "path": venv}]] * 6,
                     "write": {"calc.py": CALC_OK}, "text": "ok"}])
    r = E.run("dispatch_task.py", "ln9", "Fix add().", "--models", "gpt-oss:20b", "--write", "calc.py",
              "--check", f"{PY} check.py", cwd=repo, env=env)
    assert "read 6 times" in r.stdout and "looked into the environment" in r.stdout, r.stdout
    assert "Learned: gpt-oss:20b passed, but wastefully: wander" in r.stdout, r.stdout
    return ("user files left alone, lessons from failure modes, learned output room, lesson in the next "
            "instruction, best attempt continued and put back, no false loop on reads of different parts, "
            "churn learned from a wasteful pass, repeated searches and wandering in the environment")


def t_brief_report(E):
    # A pass checked by tests: a short report with the changed files and the new public signatures;
    # no agent answer, no "Not good enough?" line, no security line when nothing happened.
    repo = E.repo("br1", {"calc.py": CALC_BAD, "check.py": CHECK})
    code = (CALC_OK.replace("add(a, b)", "add(a: int, b: int) -> int") + "\n\ndef mul(a, b):\n    return a * b\n\n\n"
            "def _helper():\n    pass\n" + "".join(f"\n\ndef f{i}():\n    pass\n" for i in range(12)))
    E.scenario_set([{"model": "*", "write": {"calc.py": code}, "text": "I fixed add and added mul.\nAll good."}])
    r = E.run("dispatch_task.py", "br1", "Fix add() and add mul().", "--write", "calc.py", "--check", f"{PY} check.py",
              cwd=repo)
    out = r.stdout
    assert out.splitlines()[0].startswith("== Task br1: passed"), out
    assert "== Changed files == calc.py (+" in out and "new: def mul(a, b)" in out and "_helper" not in out, out
    assert "changed: def add(a: int, b: int) -> int" in out and "… and 2 more" in out, out
    assert "Before the task the check failed; now it passes." in out, out
    for gone in ("Agent's final answer", "Not good enough?", "Check OK: a look", "== Security"):
        assert gone not in out, (gone, out)
    # A failure keeps the details.
    repo = E.repo("br2", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_BAD.replace("a - b", "a * b")}, "text": "Done."}])
    r = E.run("dispatch_task.py", "br2", "Fix add().", "--retries", "0", "--write", "calc.py", "--check",
              f"{PY} check.py", cwd=repo)
    assert "Agent's final answer" in r.stdout and "Not good enough?" in r.stdout, r.stdout
    return "short report on a checked pass (changed files, new public signatures), full report otherwise"


def t_citations(E):
    # An exploration's file:line citations are checked against the files: Claude rereads only the doubtful.
    repo = E.repo("ct", {"calc.py": "import os\n\n\ndef add(a, b):\n    return a + b\n",
                         "pkg/util.py": "X = 1\n\n\ndef helper():\n    return X\n"})
    answer = ("`add(a, b)` is defined at calc.py:4 and `helper()` at util.py:4.\n"
              "`missing_fn` is used at calc.py:2.\n"
              "Config in settings.py:3, and `add` again at calc.py:90.\n"
              "[agent] qwen3.6:35b-a3b | done")
    E.scenario_set([{"model": "*", "text": answer}])
    r = E.run("delegate_local.py", "Where are add and helper?", "--readonly", cwd=repo)
    out = r.stdout
    assert r.returncode == 0 and "Citations: 5 checked, 3 doubtful" in out, out
    assert "calc.py:2: missing_fn not near line 2" in out and "settings.py:3: no such file" in out, out
    assert "calc.py:90: calc.py has only 5 lines" in out and "util.py:4" not in out.split("Citations:")[1], out
    E.scenario_set([{"model": "*", "text": "`add(a, b)` is at calc.py:4-5; `helper` at pkg/util.py:4."}])
    r = E.run("delegate_local.py", "Where?", "--readonly", cwd=repo)
    assert "Citations: 2 checked against the files, all consistent" in r.stdout, r.stdout
    # "line 4" / "lines 4-5": the file is the last one named before.
    E.scenario_set([{"model": "*", "text": "In `calc.py`, `add` is defined at line 4 (body lines 4-5).\n"
                                            "`helper` returns `X` (line 5); `missing_fn` at line 1."}])
    r = E.run("delegate_local.py", "Where?", "--readonly", cwd=repo)
    assert "Citations: 4 checked, 2 doubtful" in r.stdout and "calc.py:1: missing_fn" in r.stdout, r.stdout
    return ("exploration citations: bare file names found, wrong lines, missing files and unrelated lines "
            "flagged, \"line N\" tied to the file named before")


def t_no_learn(E):
    # Calibration runs (LOCAL_NO_LEARN=1) are exercises: nothing is learned from them, real tasks still learn.
    state = os.path.join(E.home, ".claude", "local-agents")
    mem, md = os.path.join(state, "memory.json"), os.path.join(state, "models.md")

    def snap():          # what is learned; the measures of the machine (size_gb, ctx…) may be recorded
        data = json.load(open(mem)) if os.path.exists(mem) else {}
        learned = {m: {k: v for k, v in e.items() if k not in ("size_gb", "ctx", "agent_ctx", "overhead")}
                   for m, e in data.items() if isinstance(e, dict)}
        return [{m: e for m, e in learned.items() if e}, open(md).read() if os.path.exists(md) else None]
    before = snap()
    for i, env in enumerate(({"LOCAL_NO_LEARN": "1"}, {})):
        repo = E.repo(f"nl{i}", {"calc.py": CALC_BAD, "check.py": CHECK})
        E.scenario_set([{"model": "*", "write": {"calc.py": CALC_OK}, "text": "fixed"}])
        r = E.run("dispatch_task.py", f"nl{i}", "Fix add().", "--write", "calc.py", "--check", f"{PY} check.py",
                  cwd=repo, env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        if env:
            assert snap() == before, ("a calibration run changed what was learned", snap(), before)
    assert snap()[0] != before[0], "a real task learned nothing"
    return "calibration runs learn nothing (memory.json, task size), real tasks still do"


def t_fallback_recheck(E):
    sys.path.insert(0, os.path.join(ROOT, "local", "scripts"))
    import common
    import delegate_local
    # 1. One model given: one fallback is added (it fits the task), and takes over when the first fails.
    repo = E.repo("fb1", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "qwen3.6:35b-a3b", "text": "Could not fix it."},
                    {"model": "*", "write": {"calc.py": CALC_OK}, "text": "Fixed."}])
    n = len(E.calls())
    r = E.run("dispatch_task.py", "fb1", "Fix add().", "--models", "qwen3.6:35b-a3b", "--retries", "0",
              "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo)
    models = [c["model"] for c in E.calls()[n:] if c["turn"] == 0]
    assert r.returncode == 0 and len(models) == 2 and models[0] == "qwen3.6:35b-a3b", (models, r.stdout)
    assert "Fallback: " + models[1] in r.stdout, r.stdout
    # 2. After a failure by size (step limit), a model with a smaller task size is not tried.
    md = os.path.join(E.home, ".claude", "local-agents", "models.md")
    saved_md = open(md, encoding="utf-8").read()
    try:
        open(md, "w", encoding="utf-8").write(saved_md.replace("## gpt-oss:20b\n", "## gpt-oss:20b\n- Task size: tiny\n"))
        repo = E.repo("fb2", {"calc.py": CALC_BAD, "check.py": CHECK})
        E.scenario_set([{"model": "qwen3.6:35b-a3b", "subtype": "error_max_turns", "text": "stopped"},
                        {"model": "*", "write": {"calc.py": CALC_OK}, "text": "Fixed."}])
        n = len(E.calls())
        r = E.run("dispatch_task.py", "fb2", "Fix add().", "--keep-order", "--models", "qwen3.6:35b-a3b gpt-oss:20b",
                  "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo)
        models = [c["model"] for c in E.calls()[n:] if c["turn"] == 0]
        assert r.returncode != 0 and models == ["qwen3.6:35b-a3b"], (models, r.stdout)
        assert "gpt-oss:20b not tried" in r.stdout and "split the task" in r.stdout, r.stdout
    finally:
        open(md, "w", encoding="utf-8").write(saved_md)
    # 3. A session cut off before its final answer: a summary, never the raw stream events.
    out = "\n".join([json.dumps({"type": "system", "subtype": "thinking_tokens", "session_id": "s"}),
                     json.dumps({"type": "assistant", "message": {"content": [
                         {"type": "tool_use", "name": "Write", "input": {"file_path": "build.py"}}]}}),
                     'ystem","subtype":"thinking_tokens","session_id":"s"}'])
    text = delegate_local.stream_summary(out)
    assert "Files written: build.py" in text and "thinking_tokens" not in text, text
    if not common.IS_MAC:        # /tmp is a private tmpfs under bubblewrap: the write is not refused there
        return "one fallback added and used; no smaller model after a failure by size; cut session summarized"
    # 4. A hidden check writing to a fixed /tmp path: CHECK IMPOSSIBLE, not counted against the model;
    #    the fixed check ($LOCAL_TMP) is rerun alone with --recheck, and the pass is recorded.
    repo = E.repo("fb3", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_OK}, "text": "Fixed."}])
    bad = f"{PY} -c \"import calc; open('/tmp/local-tests-fb3.json', 'w').write(str(calc.add(2, 3)))\""
    good = f"{PY} -c \"import calc, os; open(os.path.join(os.environ['LOCAL_TMP'], 'x'), 'w').write(str(calc.add(2, 3)))\""
    r = E.run("dispatch_task.py", "fb3", "Fix add().", "--models", "qwen3.6:35b-a3b", "--no-fallback", "--retries", "0",
              "--write", "calc.py", "--check", f"{PY} check.py", "--hidden-check", bad, cwd=repo)
    assert r.returncode != 0 and "impossible (check could not run)" in r.stdout.splitlines()[0], r.stdout
    assert "writes outside the project (/tmp/local-tests-fb3.json)" in r.stdout and "--recheck" in r.stdout, r.stdout
    assert common.model_outcomes([{"tried": [["m", "impossible (check could not run)"]]}]) == {}
    n = len(E.calls())
    r = E.run("dispatch_task.py", "fb3", "--recheck", "--check", f"{PY} check.py", "--hidden-check", good, cwd=repo)
    assert r.returncode == 0 and "recheck" in r.stdout.splitlines()[0] and "passed" in r.stdout.splitlines()[0], r.stdout
    assert len(E.calls()) == n, "--recheck ran an agent"
    rows = [json.loads(l) for l in open(os.path.join(E.home, ".local", "share", "local-agents", "savings.jsonl"))]
    assert [x["result"] for x in rows if x.get("id") == "fb3"][-1] == "passed", rows
    r = E.run("savings.py", cwd=repo)
    assert "qwen3.6:35b-a3b 1/1 passed" in r.stdout, r.stdout
    return ("one fallback added and used; no smaller model after a failure by size; cut session summarized; "
            "check writing to /tmp → CHECK IMPOSSIBLE (not the model's fault), --recheck records the pass")


def t_user_text_and_typed(E):
    # 1. --user-text: the user's prompt (a /local command here) is appended verbatim, line breaks kept.
    repo = E.repo("ut1", {"calc.py": CALC_BAD, "check.py": CHECK})
    spec = "Calculation spec:\n- add(2, 3) == 5\n- add(1, 1) == 2"
    tr = os.path.join(E.home, ".claude", "projects", "-ut1", "conv.jsonl")
    os.makedirs(os.path.dirname(tr), exist_ok=True)
    with open(tr, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "cwd": repo, "timestamp": "2026-01-01T10:00:00.000Z", "message": {
            "role": "user", "content": f"<command-name>/local</command-name>\n<command-args>{spec}</command-args>"}}) + "\n")
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_OK}, "text": "Fixed."}])
    n = len(E.calls())
    r = E.run("dispatch_task.py", "ut1", "Implement the spec below in calc.py.", "--user-text", "calculation  SPEC",
              "--models", "qwen3.6:35b-a3b", "--no-fallback", "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo)
    assert r.returncode == 0, r.stdout + r.stderr
    prompt = E.calls()[n]["prompt"]
    assert "The user's request, verbatim" in prompt and spec in prompt and "<command-args>" not in prompt, prompt
    est = [json.loads(l) for l in open(os.path.join(repo, ".git", "local-agents", "ut1.stats.jsonl")) if '"estimate"' in l][-1]
    assert est["prompt"] == len("Implement the spec below in calc.py."), est    # the user's text is not Claude's output
    r = E.run("dispatch_task.py", "ut2", "Do it.", "--user-text", "nothing like this", "--write", "calc.py", cwd=repo)
    assert r.returncode == 2 and "no prompt of the user starts with" in r.stderr, r.stderr
    # 2. What the agent did not type (a figure, an executed notebook produced by its script) is neither
    #    counted as lines it passed nor as code saved.
    repo = E.repo("ut3", {"calc.py": CALC_BAD, "check.py": CHECK})
    png = "\x89PNG\r\n\x1a\n\x00" + "\n".join("x" * 50 for _ in range(400))
    nb = json.dumps({"cells": [{"cell_type": "code", "source": ["print(1)\n"] * 300}]}, indent=1)
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_OK},
                     "raw_write": {"out/fig.png": png, "out/report.ipynb": nb}, "text": "Fixed."}])
    r = E.run("dispatch_task.py", "ut3", "Fix add().", "--models", "qwen3.6:35b-a3b", "--no-fallback",
              "--write", "calc.py out/", "--check", f"{PY} check.py", cwd=repo)
    assert r.returncode == 0, r.stdout + r.stderr
    mem = json.load(open(os.path.join(E.home, ".claude", "local-agents", "memory.json"), encoding="utf-8"))
    assert mem["qwen3.6:35b-a3b"]["ok_lines"][-1] == 1, mem["qwen3.6:35b-a3b"]     # the one line of calc.py changed
    est = [json.loads(l) for l in open(os.path.join(repo, ".git", "local-agents", "ut3.stats.jsonl"))
           if '"estimate"' in l][-1]
    assert est["added"] < 200, est
    # 3. A data file the agent read whole counts as its first lines only (Claude would have run head).
    repo = E.repo("ut4", {"calc.py": CALC_BAD, "check.py": CHECK, "data.csv": "a,b\n" + "1,2\n" * 5000})
    E.scenario_set([{"tools": [["Read", {"file_path": os.path.join(E.tmp, "ut4", "data.csv"), "limit": 20}]],
                     "write": {"calc.py": CALC_OK}, "text": "Fixed."}])
    r = E.run("dispatch_task.py", "ut4", "Fix add().", "--models", "qwen3.6:35b-a3b", "--no-fallback",
              "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo)
    est = [json.loads(l) for l in open(os.path.join(repo, ".git", "local-agents", "ut4.stats.jsonl"))
           if '"estimate"' in l][-1]
    assert r.returncode == 0 and 0 < est["read"] < 200, (est, r.stdout)
    return "user's prompt appended verbatim (not found → error); figures and notebooks the agent's code produced not counted"


def t_data_reads(E):
    big = "a,b\n" + "1,2\n" * 9000
    root = E.repo("dr", {"data/big.csv": big, "data/small.json": "{}", "data/x.db": "", "src/a.py": ""})
    env = dict(E.env, LOCAL_PROJECT_ROOT=root, LOCAL_SECURITY_LOG=os.path.join(E.tmp, "sec_dr.log"))
    j = lambda *p: os.path.join(root, *p)  # noqa: E731
    cases = [
        ("Read", {"file_path": j("data/big.csv")}, {}, False),                 # big data file: whole read refused
        ("Read", {"file_path": j("data/big.csv"), "limit": 20}, {}, True),     # its head is fine
        ("Read", {"file_path": j("data/big.csv")}, {"LOCAL_DATA_READ_KB": "100"}, True),
        ("Read", {"file_path": j("data/small.json")}, {}, True),
        ("Read", {"file_path": j("data/x.db")}, {}, False),                    # binary data
        ("Read", {"file_path": j("data/small.json")}, {"LOCAL_NO_READ": "data/"}, False),
        ("Grep", {"pattern": "1", "path": j("data")}, {"LOCAL_NO_READ": "data/"}, False),
        ("Read", {"file_path": j("src/a.py")}, {"LOCAL_NO_READ": "data/"}, True),
    ]
    bad = []
    for tool, inp, extra, expected in cases:
        r = subprocess.run([PY, os.path.join(S, "guard.py")], input=json.dumps({"tool_name": tool, "tool_input": inp}),
                           env=dict(env, **extra), cwd=root, capture_output=True, text=True, encoding="utf-8")
        if (r.returncode == 0) != expected:
            bad.append(f"{tool} {inp} {extra} → {'allowed' if r.returncode == 0 else 'blocked'}: {r.stderr.strip()[:120]}")
    assert not bad, "\n".join(bad)
    return f"{len(cases)} cases"


def t_take_over_passed(E):
    repo = E.repo("to", {"calc.py": CALC_OK})
    E.scenario_set([{"model": "*", "write": {"new.py": "X = 1\n"}}])
    r = E.run("dispatch_task.py", "a1", "Create new.py.", "--write", "new.py", cwd=repo)
    assert r.returncode == 0, r.stdout + r.stderr
    E.scenario_set([{"model": "*", "write": {"new.py": "X = 1\nY = 2\n"}}])
    r = E.run("dispatch_task.py", "a2", "Add Y to new.py.", "--write", "new.py", cwd=repo)
    assert r.returncode == 0 and "STOP" not in r.stdout + r.stderr, r.stdout + r.stderr
    assert "result of task a1" in git(repo, "log", "--format=%s", "-1"), git(repo, "log", "--format=%s")
    open(os.path.join(repo, "new.py"), "a").write("# by hand\n")               # changed since: not taken over
    r = E.run("dispatch_task.py", "a3", "Add Z.", "--write", "new.py", cwd=repo)
    assert r.returncode != 0 and "STOP" in r.stdout + r.stderr, r.stdout + r.stderr
    return "a passed task's new file is committed by the next task; a file changed since still stops"


def t_queue(E):
    repo = E.repo("q", {"calc.py": CALC_OK})
    E.scenario_set([{"model": "*", "write": {"one.py": "A = 1\n"}}])
    plan = os.path.join(E.tmp, "plan.json")
    json.dump({"common": ["--retries", "0"], "tasks": [
        {"id": "q1", "instruction": "Create one.py.", "args": ["--write", "one.py"]},
        {"id": "q2", "instruction": "Edit one.py.", "args": ["--write", "one.py", "--check", f"{PY} -c 'raise SystemExit(1)'"]},
        {"id": "q3", "instruction": "Never run.", "args": ["--write", "one.py"]}]}, open(plan, "w"))
    r = E.run("dispatch_queue.py", plan, cwd=repo)
    out = r.stdout
    assert r.returncode != 0 and "Stopped at task q2" in out and "not run: q3" in out, out + r.stderr
    assert "q2.report.txt" in out.splitlines()[-1], out
    assert not os.path.exists(os.path.join(repo, ".git", "local-agents", "q3.report.txt"))
    return "tasks run in order; the queue stops at the first failure and names its report"


def t_run_notebook(E):
    py = os.environ.get("LOCAL_TEST_NB_PYTHON", PY)     # an interpreter with nbformat, nbclient, ipykernel
    if subprocess.run([py, "-c", "import nbformat, nbclient, ipykernel"], capture_output=True).returncode:
        return "skipped (no nbclient/ipykernel; set LOCAL_TEST_NB_PYTHON)"
    sys.path.insert(0, S)
    import common  # noqa: E402
    if not common.sandbox_kind():
        return "skipped (no sandbox)"
    cell = lambda src: {"cell_type": "code", "metadata": {}, "source": src, "outputs": [],  # noqa: E731
                        "execution_count": None}
    nb = {"nbformat": 4, "nbformat_minor": 5, "metadata": {"kernelspec": {"name": "python3", "display_name": "py"}},
          "cells": [cell("print(6 * 7)"), cell("open('out.txt', 'w').write('x')")]}
    repo = E.repo("nb", {"n.ipynb": json.dumps(nb)})
    cmd = f"{py} {os.path.join(S, 'run_notebook.py')} n.ipynb"
    argv, _ = common.sandboxed_argv(repo, cmd, writable=True)
    r = subprocess.run(argv, cwd=repo, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0 and "2/2 code cells executed, 0 error(s)" in r.stdout, r.stdout + r.stderr[-500:]
    assert "42" in json.dumps(json.load(open(os.path.join(repo, "n.ipynb")))["cells"][0]["outputs"])
    argv, _ = common.sandboxed_argv(repo, cmd)                   # read-only project: the cell's write fails
    r = subprocess.run(argv, cwd=repo, capture_output=True, text=True, timeout=180)
    assert r.returncode == 1, r.stdout + r.stderr[-500:]
    return "a kernel runs inside the checks' sandbox (ipc transport)"


def t_manual_fix_bounds(E):
    repo = E.repo("mf", {"calc.py": CALC_BAD, "check.py": CHECK})
    fail_check = ["--retries", "0", "--no-fallback", "--check", f"{PY} check.py"]
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_BAD + "# try\n"}}])
    r = E.run("dispatch_task.py", "m1", "Fix add().", "--write", "calc.py out.ipynb", *fail_check, cwd=repo)
    assert r.returncode != 0, r.stdout
    git(repo, "stash", "pop")
    open(os.path.join(repo, "out.ipynb"), "w").write("{" + "x" * 50000 + "}")   # an executed notebook: generated
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "manual")
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_OK + "# " + "y" * 3000 + "\n"}}])   # a LOCAL task's work
    r = E.run("dispatch_task.py", "m2", "Fix add().", "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo)
    assert r.returncode == 0, r.stdout + r.stderr
    out = E.run("savings.py", cwd=repo).stdout
    assert "Finished by hand" not in out, out                       # nothing typed by Claude
    git(repo, "commit", "-qam", "m2")
    E.scenario_set([{"model": "*", "write": {"calc.py": CALC_BAD + "# again\n"}}])
    E.run("dispatch_task.py", "m3", "Fix add().", "--write", "calc.py", *fail_check, cwd=repo)
    git(repo, "stash", "pop")
    open(os.path.join(repo, "calc.py"), "w").write(CALC_OK + "# fixed by hand\n")
    out = E.run("savings.py", cwd=repo).stdout
    assert "Finished by hand after a failed task: 1 (m3)" in out, out
    return "work of a later local task and generated files are not counted as Claude's manual fix"


def t_install_claude_md(E):
    # --claude-md adds the hint once (an older wording is replaced); --uninstall removes it, user text intact.
    home = os.path.join(E.tmp, "install-home-md")
    md = os.path.join(home, ".claude", "CLAUDE.md")
    os.makedirs(os.path.dirname(md), exist_ok=True)
    mine = "# My rules\nBe brief.\n"
    with open(md, "w", encoding="utf-8") as f:
        f.write(mine + "\n<!-- skill /local -->\nAn older wording\nof the hint.\n\n## After\nKeep me.\n")
    env = dict(E.env, HOME=home, USERPROFILE=home, XDG_DATA_HOME=os.path.join(home, "data"))

    def install(*args):
        r = subprocess.run([PY, os.path.join(ROOT, "install.py")] + list(args), env=env, capture_output=True,
                           text=True, encoding="utf-8", timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout, open(md, encoding="utf-8").read()
    out, text = install("--claude-md")
    assert text.count("<!-- skill /local -->") == 1 and "older wording" not in text and "Keep me." in text, text
    assert text.startswith(mine) and "updated in" in out, out + text
    out, again = install("--claude-md")
    assert again == text and "already mentions" in out, out + again
    assert os.path.isfile(os.path.join(home, ".claude", "skills", "local", "SKILL.md"))
    out, text = install("--uninstall")
    assert "<!-- skill /local -->" not in text and "/local skill" not in text, text
    assert text.startswith(mine) and "## After\nKeep me." in text and "removed from" in out, out + text
    assert not os.path.exists(os.path.join(home, ".claude", "skills", "local")), out
    return "--claude-md adds or updates its hint once; --uninstall removes it and leaves the user's text"


def t_force_models(E):
    # /local --force-model: exactly the user's models, in order: no fallback, none skipped by size.
    md = os.path.join(E.home, ".claude", "local-agents", "models.md")
    saved_md = open(md, encoding="utf-8").read()
    repo = E.repo("fm1", {"calc.py": CALC_BAD, "check.py": CHECK})
    E.scenario_set([{"model": "*", "text": "Could not fix it."}])
    n = len(E.calls())
    r = E.run("dispatch_task.py", "fm1", "Fix add().", "--force-models", "20b", "--retries", "0",
              "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo)
    models = [c["model"] for c in E.calls()[n:] if c["turn"] == 0]
    assert r.returncode != 0 and models == ["gpt-oss:20b"], (models, r.stdout)
    assert "forced by the user (gpt-oss:20b)" in r.stdout and "Fallback:" not in r.stdout, r.stdout
    try:
        open(md, "w", encoding="utf-8").write(saved_md.replace("## gpt-oss:20b\n", "## gpt-oss:20b\n- Task size: tiny\n"))
        repo = E.repo("fm2", {"calc.py": CALC_BAD, "check.py": CHECK})
        E.scenario_set([{"model": "qwen3.6:35b-a3b", "subtype": "error_max_turns", "text": "stopped"},
                        {"model": "*", "write": {"calc.py": CALC_OK}, "text": "Fixed."}])
        n = len(E.calls())
        r = E.run("dispatch_task.py", "fm2", "Fix add().", "--force-models", "a3b, gpt-oss:20b",
                  "--write", "calc.py", "--check", f"{PY} check.py", cwd=repo)
        models = [c["model"] for c in E.calls()[n:] if c["turn"] == 0]
        assert r.returncode == 0 and models == ["qwen3.6:35b-a3b", "gpt-oss:20b"], (models, r.stdout)
    finally:
        open(md, "w", encoding="utf-8").write(saved_md)
    # Names: short parts of exactly one installed name; ambiguous, unknown, "-" lists and cloud refused.
    r = E.run("models.py", "resolve", "a3b,devstral")
    assert r.returncode == 0 and r.stdout.split() == ["qwen3.6:35b-a3b", "devstral-small-2:24b"], r.stdout + r.stderr
    for spec, err in (("3", "several"), ("a3b-devstral", "commas, not '-'"), ("glm-4.7:cloud", "cloud")):
        r = E.run("models.py", "resolve", spec)
        assert r.returncode == 1 and err in r.stderr, (spec, r.stdout + r.stderr)
    repo, n = E.repo("fm3", {"calc.py": CALC_BAD}), len(E.calls())
    r = E.run("dispatch_task.py", "fm3", "Fix add().", "--force-models", "nope", "--write", "calc.py", cwd=repo)
    assert r.returncode == 2 and "--force-models" in r.stderr + r.stdout and len(E.calls()) == n, r.stdout + r.stderr
    return "--force-models: the user's models only, in order, none skipped; short names resolved, bad ones refused"


TESTS = [t_guard, t_no_learn, t_ask_local, t_dispatch_success, t_dispatch_chain, t_unknown_tool, t_review, t_reject, t_research,
         t_manual_changes, t_blocked_actions, t_sandbox_down, t_scope_tamper, t_review_verdicts, t_zero_tests, t_hidden_check, t_stop_hook, t_hooks_not_running, t_check_integrity, t_no_sandbox, t_worktree_editable, t_signal, t_capacity, t_parallel, t_acceptance_script, t_help, t_doctor,
         t_install_settings, t_install_claude_md, t_calibrate, t_calibrate_tasks, t_savings, t_models,
         t_task_size, t_unload, t_progress,
         t_failure_handling, t_session_feedback, t_learning, t_brief_report, t_citations, t_fallback_recheck, t_force_models, t_user_text_and_typed,
         t_data_reads, t_take_over_passed, t_queue, t_run_notebook, t_manual_fix_bounds]


def main():
    if os.name == "nt":
        print("These tests run on Linux and macOS (on Windows: WSL).")
        sys.exit(2)
    E = Env()
    failed = 0
    try:
        for t in TESTS:
            if PATTERN and PATTERN not in t.__name__:
                continue
            start = time.time()
            try:
                detail = t(E)
                print(f"✔ {t.__name__[2:]:<18} {time.time() - start:5.1f} s  {detail}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"✘ {t.__name__[2:]:<18} {time.time() - start:5.1f} s  {type(e).__name__}: {e}")
    finally:
        E.close()
    print(f"\n{'All tests passed.' if not failed else f'{failed} test(s) failed.'}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
