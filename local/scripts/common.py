#!/usr/bin/env python3
"""Shared helpers for the /local skill: system (macOS, Linux, Windows), processes, sandbox,
local Ollama models and their capabilities. Python 3.8+; no external dependencies.

Command line:
  common.py default            -> name of the default model
  common.py hidden-dir <name>  -> folder for held-out tests (dispatch_task.py --hidden-check)
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------- system
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
OS_NAME = "Windows" if IS_WIN else ("macOS" if IS_MAC else ("Linux" if IS_LINUX else sys.platform))

HOME = os.path.expanduser("~")
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPTS)
STATE_DIR = os.path.join(HOME, ".claude", "local-agents")          # models.md (read by Claude)
MODELS_MD = os.path.join(STATE_DIR, "models.md")
TEMPLATE = os.path.join(SKILL_DIR, "models.template.md")
PY = sys.executable or "python3"


def data_dir():
    """Working folder outside ~/.claude (calibration creates test repositories there)."""
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share")
    return os.path.join(base, "local-agents")


def hidden_dir(name=""):
    """Folder for held-out tests (dispatch_task.py --hidden-check): outside every project, readable by
    the checks, unreadable by the agents."""
    path = os.path.join(data_dir(), "hidden", name) if name else os.path.join(data_dir(), "hidden")
    os.makedirs(path, exist_ok=True)
    return path


def utf8_stdio():
    """UTF-8 output everywhere (the Windows console is not UTF-8 by default)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


class _Help(argparse.Action):
    def __init__(self, option_strings, dest, **kw):
        super().__init__(option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, nargs=0)

    def __call__(self, parser, namespace, values, option_string=None):
        print((parser.doc or "").strip())
        parser.exit(0)


class Parser(argparse.ArgumentParser):
    """ArgumentParser whose -h / --help prints the script's documentation (its docstring)."""

    def __init__(self, doc, **kw):
        super().__init__(add_help=False, allow_abbrev=False, **kw)
        self.doc = doc
        self.add_argument("-h", "--help", action=_Help)

    def error(self, message):
        self.exit(2, f"ERROR: {message}\nHelp: {self.prog} --help\n")


def fwd(path):
    """Path with forward slashes: accepted by Python, bash and cmd, including on Windows."""
    return path.replace("\\", "/")


def quote(arg):
    return '"' + fwd(arg) + '"'


# --------------------------------------------------------------------------- processes
def kill_tree(proc):
    """Stops a process and everything it started."""
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def run(cmd, cwd=None, env=None, input_text=None, timeout=None, merge_stderr=False):
    """Runs a command (list) with stdin closed or provided, and an optional time limit.
    Returns (code, stdout, stderr, timed_out)."""
    kwargs = dict(cwd=cwd, env=env, stdout=subprocess.PIPE,
                  stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
                  stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                  text=True, encoding="utf-8", errors="replace")
    if IS_WIN:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd if isinstance(cmd, str) else cmd[0]}", False
    try:
        out, err = proc.communicate(input=input_text, timeout=timeout)
        _reap_group(proc)
        return proc.returncode, out or "", err or "", False
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        out, err = proc.communicate()
        return 124, out or "", err or "", True
    except BaseException:          # interrupted (signal, Ctrl+C): never leave an orphan behind
        kill_tree(proc)
        raise


def _reap_group(proc):
    """The command ended: stop whatever it left running in its process group (a child still writing
    files would undo a later git stash). POSIX only; the group is the command's own session."""
    if IS_WIN:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def run_stream(cmd, cwd=None, env=None, input_text=None, timeout=None, on_line=None):
    """Like run(), but calls on_line(line) for each line of stdout as it arrives (live progress); on_line
    returning True stops the command. Returns (code, stdout, stderr, timed_out)."""
    import queue
    kwargs = dict(cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                  text=True, encoding="utf-8", errors="replace", bufsize=1)
    if IS_WIN:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd if isinstance(cmd, str) else cmd[0]}", False
    lines, err_parts, q = [], [], queue.Queue()

    def feed():
        try:
            if input_text is not None:
                proc.stdin.write(input_text)
            proc.stdin.close()
        except (OSError, ValueError, AttributeError):
            pass

    def read_out():
        for line in proc.stdout:
            q.put(line)
        q.put(None)

    def read_err():
        err_parts.append(proc.stderr.read())

    threads = [threading.Thread(target=f, daemon=True) for f in (feed, read_out, read_err)]
    for t in threads:
        t.start()
    deadline = time.time() + timeout if timeout else None
    timed_out = False
    try:
        while True:
            try:
                line = q.get(timeout=1)
            except queue.Empty:
                if deadline and time.time() > deadline:
                    timed_out = True
                    kill_tree(proc)
                    break
                continue
            if line is None:
                break
            lines.append(line)
            if on_line:
                try:
                    stop = on_line(line)
                except Exception:  # noqa: BLE001   (progress must never break the run)
                    stop = False
                if stop:
                    kill_tree(proc)
                    break
        proc.wait(timeout=30)
        _reap_group(proc)
    except BaseException:          # interrupted (signal, Ctrl+C): never leave an orphan behind
        kill_tree(proc)
        raise
    for t in threads[1:]:
        t.join(timeout=5)
    return (124 if timed_out else proc.returncode), "".join(lines), "".join(err_parts), timed_out


_T0 = time.time()


def progress_on():
    return os.environ.get("LOCAL_PROGRESS", "on").lower() not in ("off", "0", "no", "false")


def progress(msg):
    """One line of live progress (shown in the background shell of a task): [mm:ss] message."""
    if not progress_on():
        return
    secs = int(time.time() - _T0)
    try:
        print(f"[{secs // 60:02d}:{secs % 60:02d}] {msg}", flush=True)
    except (OSError, ValueError):
        pass


def git(args, cwd):
    code, out, _, _ = run(["git", "-c", "core.fsmonitor=false"] + args, cwd=cwd, timeout=60)
    return out if code == 0 else ""


# Untracked files the user already had when a task started (e.g. a notebook in progress): left alone by
# the task (never stashed, never "out of scope") as long as they stay unchanged. {path: sha256}
BASELINE = {}


def file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def untracked(root):
    return sorted(p for p in git(["ls-files", "--others", "--exclude-standard", "-z"], root).split("\0") if p)


def load_baseline(path):
    global BASELINE
    try:
        BASELINE = json.load(open(path, encoding="utf-8")) if path else {}
    except (OSError, ValueError):
        BASELINE = {}
    return BASELINE


def changed_paths(root):
    """Paths changed in the working tree since the last commit (modified, deleted, new; ignored files
    excluded), relative to root, with forward slashes. The user's untracked files present when the task
    started (BASELINE) are left out while unchanged."""
    out = set()
    for args in (["diff", "--name-only", "-z"], ["diff", "--name-only", "-z", "--cached"],
                 ["ls-files", "--others", "--exclude-standard", "-z"]):
        out.update(p for p in git(args, root).split("\0") if p)
    if BASELINE:
        out = {p for p in out if not (p in BASELINE and file_hash(os.path.join(root, p)) == BASELINE[p])}
    return sorted(out)


def normalize_write(write, top, cwd):
    """--write paths -> paths relative to the project root, with "/" ("./a.py", absolute paths and
    paths given from a subfolder all work). Folders keep their trailing "/"."""
    out = []
    for w in (write or "").split():
        trail = w.endswith(("/", "\\"))
        p = os.path.expanduser(w)
        p = p if os.path.isabs(p) else os.path.join(cwd, p)
        try:
            rel = os.path.relpath(os.path.normpath(p), top).replace(os.sep, "/")
        except ValueError:          # another drive on Windows
            rel = w
        out.append(rel + ("/" if trail and rel not in (".", "") else ""))
    return " ".join(out)


def in_write_scope(rel, write_paths):
    """True if the project-relative path is covered by the --write paths (file, folder or glob)."""
    rel = rel.replace("\\", "/")
    for w in (x.replace("\\", "/").rstrip("/") for x in write_paths):
        if w in ("", ".") or rel == w or rel.startswith(w + "/") or fnmatch.fnmatch(rel, w):
            return True
    return False


def agent_touched(rel, security_log):
    """True if an allowed action of the agent (security log) names this path: Write/Edit on it, or a
    command mentioning it. Without a readable log, assume it did (the safe side for scope checks)."""
    try:
        rows = [json.loads(l) for l in open(security_log, encoding="utf-8") if l.strip()]
    except (OSError, ValueError, TypeError):
        return True
    name = os.path.basename(rel)
    return any(str(r.get("decision", "")).startswith("ALLOWED") and r.get("tool") != "Read"
               and (r.get("detail") == rel or name in str(r.get("detail", ""))) for r in rows)


WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def typed_files(root, security_log):
    """Paths (relative to root) the agent wrote itself with Write/Edit: what Claude would have had to type.
    Files produced by running code (an executed notebook, figures, data) are not in it. None if the log
    cannot be read."""
    try:
        rows = [json.loads(l) for l in open(security_log, encoding="utf-8") if l.strip()]
    except (OSError, ValueError, TypeError):
        return None
    out = set()
    for r in rows:
        if str(r.get("decision", "")).startswith("ALLOWED") and r.get("tool") in WRITE_TOOLS and r.get("detail"):
            p = str(r["detail"])
            p = os.path.relpath(p, root) if os.path.isabs(p) else p
            out.add(os.path.normpath(p).replace("\\", "/"))
    return out


def is_text_source(path):
    """False for binary files (a NUL byte at the start) and notebooks (.ipynb: generated by a script in this
    skill, never typed by the agent): they do not count as code written."""
    if path.endswith(".ipynb"):
        return False
    try:
        with open(path, "rb") as f:
            return b"\0" not in f.read(8192)
    except OSError:
        return False


def out_of_scope(root, write, security_log=None):
    """Changed paths not covered by the --write paths (empty when --write is not set). New untracked
    files the agent never touched (security log) are the user's, created while the task ran: left out."""
    paths = (write or "").split()
    if not paths:
        return []
    stray = [p for p in changed_paths(root) if not in_write_scope(p, paths)]
    if security_log is None:
        security_log = os.environ.get("LOCAL_SECURITY_LOG") or None
    if stray and security_log:
        tracked = set(p for p in git(["ls-files", "-z", "--"] + stray, root).split("\0") if p)
        stray = [p for p in stray if p in tracked or agent_touched(p, security_log)]
    return stray


def _stash_count(root):
    return len([l for l in git(["stash", "list"], root).splitlines() if l.strip()])


def stash_detail(root, label, paths=None):
    """git stash push -u (only these paths if given, taken literally), ONE stash per call: retried only
    if no stash could be created (parallel worktrees share the stash). Returns (created, left, why):
    left = paths still changed afterwards, why = what the leftovers look like (for the report)."""
    env = dict(os.environ, GIT_LITERAL_PATHSPECS="1")
    created = False
    if paths is None and BASELINE:          # never the user's own untracked files: only the task's changes
        paths = changed_paths(root)
        if not paths:
            return False, [], ""
    for attempt in range(3):
        before = _stash_count(root)
        run(["git", "-c", "core.fsmonitor=false", "stash", "push", "-u", "-m", label]
            + (["--"] + list(paths) if paths else []), cwd=root, env=env, timeout=60)
        created = _stash_count(root) > before
        if created:
            break
        time.sleep(1 + attempt)
    if created:
        time.sleep(1)                   # anything still writing shows up now, not after the next model started
    now = changed_paths(root)
    left = now if paths is None else [p for p in paths if p in now]
    why = ""
    if left:
        stamps = {p: _mtime(os.path.join(root, p)) for p in left}
        time.sleep(2)
        if any(_mtime(os.path.join(root, p)) != t for p, t in stamps.items()):
            why = "modified again after the stash: a process is still writing them"
        elif not run(["git", "diff", "--quiet", "--ignore-all-space", "--ignore-cr-at-eol", "--"] + left,
                     cwd=root, timeout=60)[0]:
            why = "identical to the last commit apart from whitespace or line endings (git filters, autocrlf?)"
        else:
            why = "still different from the last commit after the stash"
    return created, left, why


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def stash(root, label, paths=None):
    """git stash push -u (see stash_detail). True when nothing of it is left in the working tree."""
    return not stash_detail(root, label, paths)[1]


def set_aside_paths(root, paths, label):
    """Moves these changes to a git stash (recoverable, nothing is deleted). Returns True on success."""
    return stash(root, label, paths)


def worktree_fingerprint(root):
    """Fingerprint of the uncommitted work (diff + new files): tells whether anything changed since."""
    h = hashlib.sha256()
    h.update(git(["diff", "--binary"], root).encode("utf-8", "replace"))
    h.update(git(["diff", "--binary", "--cached"], root).encode("utf-8", "replace"))
    for rel in sorted(p for p in git(["ls-files", "--others", "--exclude-standard", "-z"], root).split("\0") if p):
        h.update(rel.encode("utf-8", "replace"))
        try:
            with open(os.path.join(root, rel), "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    return h.hexdigest()


# ---------------------------------------------------------------------------- check output for the agent
_FRAME = re.compile(r"([\w./\\-]+\.(?:py|pyx|ipynb|js|jsx|ts|tsx|go|rs|java|kt|rb|php|c|cc|cpp|h|hpp|cs|swift)):(\d+)")
_LIBRARY = re.compile(r"site-packages|dist-packages|[\\/]\.?venv[\\/]|[\\/]lib[\\/]python\d|node_modules|"
                      r"[\\/]usr[\\/]lib|<frozen|importlib")
_NOISE = re.compile(r"(Deprecation|Pending|Future|Runtime|User|Resource)Warning|warnings\.warn|-- Docs: https://docs\.pytest")
_KEY = re.compile(r"^\s*E\s|^(FAILED|ERROR)\b|^\S*(Error|Exception)\b.*:|^=+ .*(failed|passed|error).* =+$|"
                  r"^Traceback|^\s*assert\b|\bAssertionError\b", re.I)


def passed_count(text):
    """How many tests passed, from a check's output (pytest summary, unittest "Ran N tests"); 0 when the
    tests could not even be collected or imported; None when the output says nothing about it."""
    text = text or ""
    if re.search(r"ERROR collecting|errors? during collection|ImportError|SyntaxError", text) and \
            not re.search(r"\d+ passed", text):
        return 0
    m = re.search(r"(\d+) passed", text)
    if m:
        return int(m.group(1))
    m = re.search(r"^Ran (\d+) tests?", text, re.M)
    if m:
        bad = sum(int(x) for x in re.findall(r"(?:failures|errors)=(\d+)", text))
        return max(0, int(m.group(1)) - bad)
    return 0 if re.search(r"\d+ failed|\d+ errors?\b", text) else None


_CITE = re.compile(r"([\w./-]+\.[A-Za-z]\w{0,4}):(\d+(?:\s*[-–]\s*\d+)?(?:\s*,\s*\d+(?:\s*[-–]\s*\d+)?)*)")
_LINE_WORD = re.compile(r"\b[Ll]ines?\s+(\d+(?:\s*[-–]\s*\d+)?(?:\s*(?:,|and)\s*\d+(?:\s*[-–]\s*\d+)?)*)")
_FILE = re.compile(r"[\w./-]+\.[A-Za-z]\w{0,4}")
_TICKED = re.compile(r"`([^`]+)`")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _claim_names(line):
    """Identifiers a claim is about: those in `backticks` of the line (calls, keys, names), not file names."""
    names = []
    for tick in _TICKED.findall(line):
        if _CITE.search(tick) or re.fullmatch(r"[\w./-]+\.\w{1,5}", tick):
            continue
        names += [n for n in _IDENT.findall(tick) if n not in ("self", "None", "True", "False", "return")]
    return names


def check_citations(text, root, near=3):
    """Checks the file:line citations of an exploration's answer against the files: the file exists (a bare
    file name is looked up in the repository), the line exists, and a name the claim is about appears within
    `near` lines. Returns (number checked, doubtful [(citation, reason)]): Claude reads only the doubtful."""
    lines = text.splitlines()
    tracked = [p for p in git(["ls-files", "-z"], root).split("\0") if p]
    cache, checked, doubtful = {}, 0, []
    current = None          # the last file named, for "line 37" / "lines 89-175" citations

    def files_of(ref):
        return [ref] if os.path.isfile(os.path.join(root, ref)) else \
            [p for p in tracked if p == ref or p.endswith("/" + ref)]

    def content(path):
        if path not in cache:
            try:
                cache[path] = open(os.path.join(root, path), encoding="utf-8", errors="replace").read().splitlines()
            except OSError:
                cache[path] = None
        return cache[path]
    for i, line in enumerate(lines):
        names = _claim_names(line)
        back = i
        while not names and back > 0 and i - back < 3:      # "Called at: f.py:28": the name is just above
            back -= 1
            names = _claim_names(lines[back])
        cites = [(m.start(), m.group(1).lstrip("./"), m.group(2), m.end()) for m in _CITE.finditer(line)]
        files = [(m.start(), m.group(0).lstrip("./")) for m in _FILE.finditer(line) if files_of(m.group(0).lstrip("./"))]
        for m in _LINE_WORD.finditer(line):
            before = [f for pos, f in files if pos < m.start()]
            ref = before[-1] if before else current
            if ref:
                cites.append((m.start(), ref, m.group(1), m.end()))
        if files:
            current = files[-1][1]
        prev_end = 0
        for start, ref, spans, end in sorted(cites):
            # The names of the clause just before the citation, else those of the whole line.
            clause = _claim_names(line[prev_end:start])
            prev_end = end
            names_here = clause or names
            candidates = files_of(ref)
            for part in re.split(r"\s*(?:,|and)\s*", spans):
                nums = [int(x) for x in re.findall(r"\d+", part)]     # "28" or a range "28-40"
                lo, hi = nums[0], nums[-1]
                checked += 1
                cite = f"{ref}:{part}"
                if not candidates:
                    doubtful.append((cite, "no such file in the repository"))
                    continue
                reasons = []
                for path in candidates[:3]:
                    body = content(path) or []
                    if lo > len(body):
                        reasons.append(f"{path} has only {len(body)} lines")
                        continue
                    window = "\n".join(body[max(0, lo - 1 - near):hi + near])
                    if names_here and not any(re.search(rf"\b{re.escape(n)}\b", window) for n in names_here):
                        reasons.append(f"{', '.join(dict.fromkeys(names_here[:3]))} not near line {lo} of {path}")
                        continue
                    reasons = []
                    break
                if reasons:
                    doubtful.append((cite, reasons[0]))
    return checked, doubtful


def citations_note(text, root):
    """The line printed after an exploration: what the citation check found."""
    checked, doubtful = check_citations(text, root)
    if not checked:
        return "Citations: none to check (ask for file:line to verify claims cheaply)."
    if not doubtful:
        return f"Citations: {checked} checked against the files, all consistent: no need to reread them."
    return (f"Citations: {checked} checked, {len(doubtful)} doubtful (read only these):\n"
            + "\n".join(f"  - {c}: {why}" for c, why in doubtful[:8])
            + (f"\n  … and {len(doubtful) - 8} more" if len(doubtful) > 8 else ""))


def failing_tests(text):
    """Names of the failing tests in a check's output (pytest "FAILED x::y", unittest "FAIL: test (mod)")."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^(?:FAILED|ERROR) (\S+::\S+)", line) or re.match(r"^(?:FAIL|ERROR): (\S+ \([\w.]+\))", line)
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def project_frames(lines):
    """file:line references into the project's own files (not libraries), in order, without repeats."""
    out = []
    for line in lines:
        for m in _FRAME.finditer(line):
            ref = f"{m.group(1)}:{m.group(2)}"
            if not _LIBRARY.search(m.group(1)) and ref not in out:
                out.append(ref)
    return out


def check_digest(text, limit=40):
    """The useful part of a failed check's output, at most `limit` lines: error lines (E ...), frames that
    point into the project (not libraries), the summary; library warnings dropped. The decisive line of a
    long traceback is usually one of the project's frames, which a plain tail would cut."""
    lines = [l for l in text.splitlines() if l.strip()]
    keep = set()
    for i, line in enumerate(lines):
        if _NOISE.search(line) and not line.lstrip().startswith("E "):
            continue
        frame = any(not _LIBRARY.search(m.group(1)) for m in _FRAME.finditer(line))
        if frame or _KEY.search(line):
            keep.add(i)
            if frame and i + 1 < len(lines) and not _FRAME.search(lines[i + 1]):
                keep.add(i + 1)                     # the code line shown under a frame
    chosen = sorted(keep)[-limit:]
    for i in range(len(lines) - 1, -1, -1):        # room left: the end of the output, warnings excluded
        if len(chosen) >= limit:
            break
        if i not in chosen and not _NOISE.search(lines[i]):
            chosen.append(i)
    chosen = sorted(set(chosen))
    out, prev = [], -1
    for i in chosen:
        if i > prev + 1:
            out.append(f"[… {i - prev - 1} line(s) omitted]")
        out.append(lines[i])
        prev = i
    return "\n".join(out)


def error_signature(text):
    """What identifies a failure (error lines and project frames), to notice the same error twice."""
    lines = text.splitlines()
    errors = [l.strip() for l in lines if l.lstrip().startswith("E ") or re.match(r"^\S*(Error|Exception)\b.*:", l)]
    return hashlib.sha256("\n".join(errors[-6:] + project_frames(lines)[-3:]).encode("utf-8", "replace")).hexdigest()


HIDDEN_FEEDBACK = ("An additional check that you cannot see failed. Your implementation is probably "
                   "tailored to the visible tests or misses a case of the instruction: re-read the "
                   "instruction and handle the general case (no special-casing of test values).")


# ---------------------------------------------------------------------------- token savings
# Rough estimate (~3.5 characters per token). Claude alone: read what the local model read (input) and
# write what it wrote (output). With delegation: write the instruction (output), read the answer, report
# and diff (input). A delegation that failed counts as a loss (Claude had to take over).
CHARS_PER_TOKEN = 3.5
OUTPUT_WEIGHT = 5          # usual output/input price ratio of Claude models
TASK_SUCCESS = ("passed", "approved by review", "unverified")


def tok(chars):
    return round(chars / CHARS_PER_TOKEN)


def local_tokens(text):
    """Tokens a local model will see in text, on the safe side. Digits cost one token each (Qwen, Llama 3),
    so logs full of timestamps and ids hold ~1.8 characters per token, not ~3.3 like code or prose."""
    digits = sum(c.isdigit() for c in text)
    punct = len(re.findall(r"[^\w\s]", text))
    return int(digits + 0.6 * punct + (len(text) - digits - punct) / 3.5)


# Fixed input cost of each call that Claude would not have alone: the tool call and its framing, the
# end-of-task notification, the commands that show the report and the diff.
TASK_FIXED_IN = 300
QUESTION_FIXED_IN = 100
# Below this saving (input equivalent), a task was too small to be worth delegating.
SMALL_TASK = 3000


def task_savings(estimates, result):
    """(input, output) tokens saved by a code task from its "estimate" rows (all attempts, resumes
    included), or None if not measured."""
    if not estimates:
        return None
    cost_in = tok(sum(e["report"] + e["diff"] for e in estimates)) + TASK_FIXED_IN * len(estimates)
    cost_out = tok(sum(e["prompt"] for e in estimates))
    if result in TASK_SUCCESS:
        return tok(estimates[-1]["read"]) - cost_in, tok(estimates[-1]["added"]) - cost_out
    return -cost_in, -cost_out


def task_cost(estimates):
    """(input, output) Claude tokens a code task is expected to cost Claude: instruction written, reports
    and diffs read, fixed cost per call. savings.py compares it with what the transcript shows."""
    return (tok(sum(e["report"] + e["diff"] for e in estimates)) + TASK_FIXED_IN * len(estimates),
            tok(sum(e["prompt"] for e in estimates))) if estimates else (0, 0)


def question_cost(prompt_chars, answer_chars):
    return tok(answer_chars) + QUESTION_FIXED_IN, tok(prompt_chars)


def question_savings(prompt_chars, content_chars, answer_chars):
    """Question to a local model: Claude only writes the question and reads the answer."""
    return tok(content_chars) - tok(answer_chars) - QUESTION_FIXED_IN, tok(answer_chars) - tok(prompt_chars)


def ledger_path():
    return os.path.join(data_dir(), "savings.jsonl")


def ledger_add(kind, ident, model, result, saved_in, saved_out, cwd=None, **extra):
    """One row per delegation in a ledger shared by all projects (for estimates per conversation).
    Not for calibration or acceptance runs (LOCAL_NO_LEDGER=1): they save nothing."""
    if os.environ.get("LOCAL_NO_LEDGER") == "1":
        return
    try:
        os.makedirs(data_dir(), exist_ok=True)
        with open(ledger_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": round(time.time(), 1), "cwd": os.path.realpath(cwd or os.getcwd()),
                                "kind": kind, "id": ident, "model": model, "result": result,
                                "in": saved_in, "out": saved_out, **extra}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def worktree_tree(root):
    """git tree id of the whole working tree (tracked and untracked files, ignored excluded), without
    touching the index or the files: what a task left, to measure later what Claude changed by hand."""
    fd, index = tempfile.mkstemp(prefix="local-index-")
    os.close(fd)
    os.remove(index)
    env = dict(os.environ, GIT_INDEX_FILE=index)
    try:
        run(["git", "read-tree", "HEAD"], cwd=root, env=env, timeout=60)
        run(["git", "add", "-A"], cwd=root, env=env, timeout=120)
        return run(["git", "write-tree"], cwd=root, env=env, timeout=60)[1].strip()
    finally:
        try:
            os.remove(index)
        except OSError:
            pass


GENERATED_EXT = {".ipynb", ".csv", ".tsv", ".json", ".jsonl", ".xml", ".png", ".jpg", ".jpeg", ".svg", ".pdf",
                 ".html", ".parquet", ".db", ".sqlite", ".pkl", ".npy", ".npz", ".log", ".txt"}


def _generated(rel):
    """A file a script produces (data, figures, executed notebooks…), not code Claude would type."""
    return os.path.splitext(rel)[1].lower() in GENERATED_EXT


def record_manual_fixes(root):
    """For each task of this repository that did not pass, count once what Claude changed by hand in its
    files afterwards (a cost of the delegation): read the changed files (input), write the added lines
    (output). Idempotent: one ledger row per task. Returns the rows added."""
    gitdir = git_common_dir(root)
    if not gitdir:
        return []
    done = {(r.get("cwd"), r.get("id")) for r in ledger_rows() if r.get("kind") == "manual fix"}
    added, now_tree = [], None
    starts = [(r["t"], r["tree"]) for path in glob_stats(os.path.join(gitdir, "local-agents"))
              for r in _jsonl(path) if r.get("type") == "start" and r.get("t") and r.get("tree")]
    for path in glob_stats(os.path.join(gitdir, "local-agents")):
        task_id = os.path.basename(path)[: -len(".stats.jsonl")]
        rows = [r for r in _jsonl(path) if r.get("type") == "task"]
        last = rows[-1] if rows else {}
        if not last.get("end_tree") or last.get("result") in TASK_SUCCESS or last.get("result") == "needs information":
            continue
        where = os.path.realpath(last.get("root") or root)
        if (where, task_id) in done:
            continue
        # Claude's manual work ends where the next local task starts: what that task wrote is not Claude's.
        later = sorted(st for st in starts if last.get("t") and st[0] > last["t"])
        if later:
            until = later[0][1]
        else:
            now_tree = now_tree or worktree_tree(where)
            until = now_tree
        paths = (last.get("write") or "").split()
        diff = git(["diff", last["end_tree"], until, "--"] + paths, where) if paths else ""
        if not diff.strip():
            continue                     # not touched (yet): counted the first time it is
        added_chars, files, keep = 0, [], False
        for l in diff.splitlines():
            if l.startswith("+++ "):
                f = l[6:] if l.startswith("+++ b/") else ""
                keep = bool(f) and not _generated(f)   # data, figures, executed notebooks: produced, not typed
                if keep:
                    files.append(f)
            elif keep and l.startswith("+") and len(l) < 1000:
                added_chars += len(l) - 1
        if not files:
            continue
        read_chars = sum(len(open(os.path.join(where, f), encoding="utf-8", errors="replace").read())
                         for f in files if os.path.isfile(os.path.join(where, f)))
        row = {"kind": "manual fix", "id": task_id, "result": last.get("result"),
               "in": -tok(read_chars), "out": -tok(added_chars)}
        ledger_add("manual fix", task_id, "", last.get("result", ""), row["in"], row["out"], cwd=where)
        added.append(row)
    return added


def glob_stats(folder):
    try:
        return sorted(os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".stats.jsonl"))
    except OSError:
        return []


def _jsonl(path):
    out = []
    try:
        for line in open(path, encoding="utf-8"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def counts_for_model(result):
    """False for outcomes that say nothing about the model: a check that could not run, a security hook
    that did not run, an unusable model or Ollama, a task too big for one agent (split it)."""
    return not str(result).startswith(("impossible", "too big"))


def model_outcomes(rows):
    """{model: [passed, tried]} from task rows carrying "tried": [[model, result], ...]."""
    out = {}
    for r in rows:
        for model, result in r.get("tried") or []:
            if not counts_for_model(result):
                continue
            entry = out.setdefault(model, [0, 0])
            entry[1] += 1
            entry[0] += result in TASK_SUCCESS
    return out


def outcomes_line(rows):
    stats = model_outcomes(rows)
    if not stats:
        return ""
    return "Models: " + " · ".join(f"{m} {p}/{n} passed" for m, (p, n) in
                                   sorted(stats.items(), key=lambda x: (-x[1][1], x[0])))


def ledger_rows():
    rows = []
    try:
        for line in open(ledger_path(), encoding="utf-8"):
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return rows


SAVINGS_FORMULA = (f"Input equivalent = input + {OUTPUT_WEIGHT} × output (an output token costs ~{OUTPUT_WEIGHT}× "
                   "an input token); \"cost\" = Claude spent more than alone, \"saved\" = less.")


def savings_line(saved_in, saved_out):
    """"≈ 155,459 tokens saved in input equivalent (input: cost ~22,166 · output: saved ~35,525)"."""
    word = lambda x: "saved" if x >= 0 else "cost"  # noqa: E731
    total = saved_in + OUTPUT_WEIGHT * saved_out
    return (f"≈ {abs(total):,} tokens {'saved' if total >= 0 else 'lost'} in input equivalent "
            f"(input: {word(saved_in)} ~{abs(saved_in):,} · output: {word(saved_out)} ~{abs(saved_out):,})")


def git_common_dir(cwd):
    """.git folder of the MAIN repository (also from a worktree): logs are centralized there."""
    d = git(["rev-parse", "--git-common-dir"], cwd).strip()
    return os.path.realpath(os.path.join(cwd, d)) if d else ""


def main_root(cwd):
    """Root of the main repository (not of the worktree)."""
    d = git_common_dir(cwd)
    return os.path.dirname(d) if d else ""


def worktree_path(main, task_id):
    """Worktree of a parallel task: outside the project and outside ~/.claude."""
    import hashlib
    key = os.path.basename(main) + "-" + hashlib.sha1(main.encode("utf-8")).hexdigest()[:8]
    return os.path.join(data_dir(), "worktrees", key, task_id)


def read_stdin_all():
    """Explicitly reads stdin to the end (--stdin option, task "-")."""
    return "" if sys.stdin is None else sys.stdin.read()


def read_stdin_if_any(first_wait=3.0):
    """Reads stdin only if data really arrives (without blocking on an inherited input).
    Also works on Windows, where select() does not work on pipes."""
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    chunks, started, done = [], threading.Event(), threading.Event()

    def reader():
        try:
            while True:
                block = sys.stdin.read(65536)
                started.set()
                if not block:
                    break
                chunks.append(block)
        except Exception:  # noqa: BLE001
            pass
        finally:
            started.set()
            done.set()

    threading.Thread(target=reader, daemon=True).start()
    if not started.wait(first_wait):
        return ""          # nothing arrives: inherited input, ignored
    done.wait()
    return "".join(chunks)


# --------------------------------------------------------------------------- sandbox
SECRET_DIRS = [".ssh", ".aws", ".gnupg", ".claude", ".config/gcloud", ".kube", ".docker", ".config/gh",
               ".azure", ".password-store", "Library/Keychains"]
SECRET_FILES = [".git-credentials", ".netrc", ".npmrc", ".pypirc", ".pgpass"]
ENV_DIRS = [".venv", "venv"]          # protected from the checked code when the project is writable


def _mac_profile(writable, mask_hidden, strict_tmp):
    """sandbox-exec profile. The last matching rule wins."""
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        # Unix sockets inside the temp/work folders only (a Jupyter kernel over the "ipc" transport, see
        # run_notebook.py); the session's sockets (ssh-agent, launchd, docker…) stay unreachable.
        '(allow network* (local unix-socket (subpath (param "TMP")) (subpath (param "WORK"))) '
        '(remote unix-socket (subpath (param "TMP")) (subpath (param "WORK"))))',
        '(deny process-exec (literal "/usr/bin/osascript") (literal "/usr/bin/open") '
        '(literal "/usr/bin/security") (literal "/bin/launchctl"))',
        "(deny appleevent-send)",
        "(deny file-write*)",
        '(allow file-write* (subpath (param "WORK")) (literal "/dev/null") (literal "/dev/tty") '
        '(literal "/dev/dtracehelper") (regex #"^/dev/fd/"))',
    ]
    if not strict_tmp:
        lines.append('(allow file-write* (subpath (param "TMP")) (subpath "/private/tmp") '
                     '(subpath "/private/var/folders"))')
    # The project rule comes AFTER the temp-folder allowances (a project may live under /var/folders),
    # and .git / virtual environments last: the last matching rule wins.
    lines.append('(allow file-write* (subpath (param "ROOT")))' if writable
                 else '(deny file-write* (subpath (param "ROOT")))')
    lines.append("(deny file-write* " + " ".join(
        f'(subpath (string-append (param "ROOT") "/{d}"))' for d in [".git"] + ENV_DIRS) + ")")
    reads = ([f'(subpath (string-append (param "HOME") "/{d}"))' for d in SECRET_DIRS]
             + [f'(literal (string-append (param "HOME") "/{f}"))' for f in SECRET_FILES])
    if mask_hidden:
        reads.append('(subpath (param "HIDDEN"))')
    lines.append("(deny file-read* " + " ".join(reads) + ")")
    return "\n".join(lines) + "\n"


def sandbox_kind():
    if IS_MAC and os.path.exists("/usr/bin/sandbox-exec"):
        return "macOS (sandbox-exec)"
    if IS_LINUX and shutil.which("bwrap"):
        return "Linux (bubblewrap)"
    return ""


_SECRET_ENV = re.compile(r"(?i)(token|secret|passw|credential|api[_-]?key|private[_-]?key|auth|cookie|session|"
                         r"^aws_|^azure_|^gcp_|^google_application|^gh_|^github_|^gitlab_|^anthropic_|^openai_|"
                         r"^hf_|^hugging|^npm_config__auth|^docker_|^vault_|^sentry_|^stripe_|^slack_)")
_KEEP_ENV = {"MAX_THINKING_TOKENS"}


# "RESEARCH: <question>" line of an agent, also when the model formats it in Markdown
# ("**RESEARCH:** …", "- RESEARCH: …", "> `RESEARCH`: …").
RESEARCH_RE = re.compile(r"^[ \t>*_`#-]*RESEARCH[*_` \t]*:[*_` \t]*(\S.*?)[*_` \t]*$", re.M | re.I)

# The agent's context is full (error from Claude Code or Ollama): more turns cannot help.
CONTEXT_FULL = re.compile(r"prompt is too long|context (?:window|length) (?:exceeded|limit)|exceeds? the (?:maximum )?"
                          r"context|maximum context length|too many tokens|input length .{0,40}exceed", re.I)

SECRET_PATTERNS = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "*.keychain*", ".netrc",
    ".npmrc", ".pypirc", ".git-credentials", "credentials", "credentials.json", "credentials.y*ml", "credentials.toml",
    "credentials.ini", "secrets", "secrets.json", "secrets.y*ml", "secrets.toml", "secrets.ini", "*secret*.json",
    "*.secrets", "*.tfvars", "*.tfstate",
]


def is_secret_name(name):
    return any(fnmatch.fnmatch(name, p) for p in SECRET_PATTERNS)


def project_secrets(root, max_depth=6):
    """Secret files of the project (.env, keys…), as absolute paths, without descending into large
    generated folders. Used to make them unreadable at the OS level for the agents' shell."""
    skip = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", "dist", "build", ".mypy_cache", "target"}
    out = []
    base = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip and dirpath.count(os.sep) - base < max_depth]
        out += [os.path.join(dirpath, f) for f in filenames if is_secret_name(f)]
        if len(out) > 200:
            break
    return out


def worktree_pythonpath(root):
    """In a task's worktree, the linked virtual environment may contain an editable install pointing
    to the MAIN repository: put the worktree's own code first on PYTHONPATH."""
    if not os.path.islink(os.path.join(root, ".venv")) and not os.path.islink(os.path.join(root, "venv")):
        return []
    return [p for p in (os.path.join(root, "src"), root) if os.path.isdir(p)]


def scrub_env(env):
    """Copy of the environment without the variables that may hold secrets (tokens, keys, cloud and
    API credentials, session sockets): local agents and checked code never need them."""
    keep = _KEEP_ENV | set(os.environ.get("LOCAL_KEEP_ENV", "").split())     # e.g. a dummy key the tests read
    return {k: v for k, v in env.items() if k in keep or not _SECRET_ENV.search(k)}


def shell_argv(command):
    """POSIX: argv list. Windows: a ready-made command line string (cmd.exe needs its own quoting:
    a list would be re-quoted by Python and break quoted paths)."""
    return f'cmd /d /s /c "{command}"' if IS_WIN else ["/bin/sh", "-c", command]


def _inside(path, folder):
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def sandboxed_argv(root, command, writable=False, work=None, extra_read=(), mask_hidden=True,
                   strict_tmp=False):
    """Confined shell command for checks: no network, the project READ-ONLY unless writable=True
    (then .git, gitlinks and virtual environments stay read-only), writes allowed in `work` (and the
    temp folders unless strict_tmp), secrets and (if mask_hidden) held-out tests unreadable, no access
    to the user's other processes or session services. Returns (argv, sandbox description or "")."""
    root = os.path.realpath(root)
    home = os.path.realpath(HOME)
    work = os.path.realpath(work) if work else None
    extra_read = list(extra_read) + [os.path.realpath(os.path.join(root, d)) for d in ENV_DIRS + ["node_modules"]
                                     if os.path.islink(os.path.join(root, d))]   # environments linked into a worktree
    if IS_MAC and os.path.exists("/usr/bin/sandbox-exec"):
        params = {"ROOT": root, "TMP": os.path.realpath(tempfile.gettempdir()), "HOME": home,
                  "WORK": work or os.path.realpath(tempfile.gettempdir()),
                  "HIDDEN": os.path.realpath(hidden_dir())}
        argv = ["/usr/bin/sandbox-exec"]
        for k, v in params.items():
            argv += ["-D", f"{k}={v}"]
        profile = _mac_profile(writable, mask_hidden, strict_tmp)
        if writable:      # secret files of the project: neither rewritten nor deleted
            lits = " ".join('(literal "%s")' % p.replace('"', '') for p in project_secrets(root))
            if lits:
                profile += f"(deny file-write* {lits})\n"
        return argv + ["-p", profile] + shell_argv(command), sandbox_kind()
    if IS_LINUX and shutil.which("bwrap"):
        argv = ["bwrap", "--die-with-parent", "--new-session", "--unshare-net", "--unshare-pid", "--unshare-ipc",
                "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
        for d in ("/run", "/var/run"):       # session bus, docker socket… : unreachable
            if os.path.isdir(d) and not os.path.islink(d) and not _inside(root, d):
                argv += ["--tmpfs", d]
        for d in SECRET_DIRS:
            p = os.path.join(home, d)
            if os.path.isdir(p) and not _inside(root, p):
                argv += ["--tmpfs", p]
        for f in SECRET_FILES:
            p = os.path.join(home, f)
            if os.path.isfile(p):
                argv += ["--ro-bind", "/dev/null", p]
        hidden = os.path.realpath(hidden_dir())
        if mask_hidden and not _inside(root, hidden):
            argv += ["--tmpfs", hidden]
        argv += ["--bind" if writable else "--ro-bind", root, root]
        if writable:
            protect = [os.path.join(root, ".git")] + [os.path.join(root, d) for d in ENV_DIRS
                                                       if not os.path.islink(os.path.join(root, d))]
            for line in git(["ls-files", "-s"], root).splitlines():          # submodules (gitlinks)
                if line.startswith("160000 ") and "\t" in line:
                    protect.append(os.path.join(root, line.split("\t", 1)[1], ".git"))
            protect += project_secrets(root)           # .env & co. cannot be deleted or rewritten
            for p in protect:
                if os.path.lexists(p):            # .git may be a FILE (worktree, submodule)
                    argv += ["--ro-bind", p, p]
        if work:
            argv += ["--bind", work, work]
        for p in extra_read:             # e.g. held-out tests under /tmp, hidden by the tmpfs above
            p = os.path.realpath(p)
            if p.startswith("/tmp/") and os.path.exists(p) and not _inside(p, root):
                argv += ["--ro-bind", p, p]
        return argv + ["--chdir", root] + shell_argv(command), sandbox_kind()
    return shell_argv(command), ""


def sandbox_selftest():
    """Tests the check sandbox on THIS machine, in both modes. Returns (ok, detail).
    (None, ...) if there is no sandbox."""
    if not sandbox_kind():
        return None, "no sandbox on this system"
    os.makedirs(data_dir(), exist_ok=True)
    proj = tempfile.mkdtemp(prefix="selftest-", dir=data_dir())       # like a real project: not under /tmp
    work = tempfile.mkdtemp(prefix="local-agents-selftest-work-")
    os.makedirs(os.path.join(proj, ".git"))
    os.makedirs(os.path.join(proj, ".venv"))
    secret = os.path.join(hidden_dir(), ".sandbox-probe")      # stands for secrets and held-out tests
    with open(secret, "w") as f:
        f.write("secret")
    py = PY.replace("'", "")
    probes = (f"(echo x > inside.txt) 2>/dev/null && echo WROTE_PROJECT; "
              f"(echo x > '{work}/w.txt') 2>/dev/null || echo NO_WORK; "
              f"(echo x > .git/probe) 2>/dev/null && echo WROTE_GIT; "
              f"(echo x > .venv/probe) 2>/dev/null && echo WROTE_VENV; "
              f"cat '{secret}' >/dev/null 2>&1 && echo READ_HIDDEN; "
              f"'{py}' -c \"import socket; socket.create_connection(('1.1.1.1', 80), timeout=3)\" "
              f">/dev/null 2>&1 && echo NETWORK; true")
    problems = []
    for writable in (False, True):
        argv, _ = sandboxed_argv(proj, probes, writable=writable, work=work)
        code, out, err, _ = run(argv, cwd=proj, timeout=60, merge_stderr=True)
        mode = "writable mode" if writable else "read-only mode"
        if not out.strip() and code != 0:
            problems.append(f"{mode}: the sandbox does not start ({(err or out).strip()[:80]})")
            continue
        if ("WROTE_PROJECT" in out) != writable:
            problems.append(f"{mode}: project {'NOT writable' if writable else 'writable'}")
        for marker, what in (("NO_WORK", "scratch folder not writable"), ("WROTE_GIT", "writing to .git NOT blocked"),
                             ("WROTE_VENV", "writing to .venv NOT blocked"),
                             ("READ_HIDDEN", "held-out tests/secrets readable"), ("NETWORK", "network NOT blocked")):
            if marker in out:
                problems.append(f"{mode}: {what}")
    shutil.rmtree(proj, ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)
    try:
        os.remove(secret)
    except OSError:
        pass
    return (not problems), ("; ".join(problems) or
                            "network, .git, .venv, secrets and held-out tests blocked; project read-only "
                            "(writable on request)")


# --------------------------------------------------------------------------- machine capacity
def ram_gb():
    try:
        if IS_MAC:
            return int(run(["sysctl", "-n", "hw.memsize"], timeout=10)[1].strip()) / 2**30
        if IS_LINUX:
            for line in open("/proc/meminfo"):
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 2**20
        if IS_WIN:
            import ctypes

            class _MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("m", ctypes.c_ulong), ("t", ctypes.c_ulonglong),
                            ("a", ctypes.c_ulonglong), ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                            ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("e", ctypes.c_ulonglong)]
            st = _MS()
            st.l = ctypes.sizeof(_MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.t / 2**30
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def machine():
    """Memory usable by the models: {'ram_gb', 'gpu_gb', 'usable_gb', 'kind'}.
    LOCAL_MEMORY_GB forces the usable value if the estimate is wrong."""
    ram = ram_gb()
    gpu, kind, usable = 0.0, "", 0.0
    if IS_MAC:
        kind = "unified memory (Apple Silicon)"
        limit = run(["sysctl", "-n", "iogpu.wired_limit_mb"], timeout=10)[1].strip()
        usable = int(limit) / 1024 if limit.isdigit() and int(limit) > 0 else ram * (0.75 if ram >= 36 else 0.66)
    else:
        if shutil.which("nvidia-smi"):
            out = run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"], timeout=15)[1]
            gpu = sum(float(x) for x in re.findall(r"\d+(?:\.\d+)?", out)) / 1024
        if gpu:
            kind, usable = "NVIDIA GPU", gpu * 0.95
        else:
            kind, usable = "RAM (CPU inference: slow)", ram * 0.6
    forced = os.environ.get("LOCAL_MEMORY_GB", "")
    if re.fullmatch(r"\d+(\.\d+)?", forced):
        usable, kind = float(forced), kind + ", forced value"
    return {"ram_gb": ram, "gpu_gb": gpu, "usable_gb": usable, "kind": kind}


# Suggested models by memory usable by the models (tags checked on ollama.com in October 2026; models
# change fast: calibrate before trusting a choice). (minimum usable GB, model, weights GB, role);
# minimum ≈ weights + room for a 64k context. Users install whatever they like: these are proposals.
SUGGESTIONS = [
    (10, "ministral-3:8b", 6, "questions and summaries; agent tasks unreliable at this size"),
    (15, "ministral-3:14b", 9, "questions, simple agent tasks (Mistral)"),
    (22, "gpt-oss:20b", 14, "agent, fast; reasoning by levels"),
    (23, "devstral-small-2:24b", 15, "agent for code (Mistral)"),
    (26, "qwen3.8:27b", 18, "agent, strong on code"),
    (32, "qwen3.6:35b-a3b", 24, "agent, fast (mixture of experts, ~3B active parameters)"),
    (40, "qwen3.8:27b-q8_0", 30, "the same 27b, more precise quantization"),
    (75, "gpt-oss:120b", 65, "large model, for the hardest tasks"),
]
# Apple Silicon: MLX builds, much faster with Ollama ≥ 0.40.
MLX_TAGS = {"qwen3.8:27b": "qwen3.8:27b-nvfp4", "qwen3.6:35b-a3b": "qwen3.6:35b-a3b-coding-nvfp4",
            "qwen3.8:27b-q8_0": "qwen3.8:27b-mxfp8"}


def suggestion_lines():
    m = machine()
    usable = m["usable_gb"]
    fit = [x for x in SUGGESTIONS if x[0] <= usable]
    lines = [f"Memory usable by the models: ~{usable:.0f} GB ({m['kind']}).",
             "Suggestions (October 2026, approximate sizes; any local model with tool calling works):"]
    if not fit:
        lines.append("  Too little memory for a useful local agent: delegate only questions to a very small "
                     "model, or rely on Claude alone.")
    try:
        inst = installed()
    except ModelError:
        inst = {}
    for _, name, weights, role in sorted(fit, key=lambda x: -x[2])[:4]:
        tag = MLX_TAGS.get(name, name) if IS_MAC else name
        mark = " (installed)" if normalize(tag) in inst or normalize(name) in inst else ""
        lines.append(f"  - {tag}: ~{weights} GB — {role}{mark}")
    lines.append("Install: ollama pull <model> (never a tag ending in -cloud), then /local models (or "
                 "models.py add <model>) to list it in models.md, then /local calibrate --quick.")
    listed = {normalize(n) for n, _ in models_md_entries()}
    extra = [n for n in inst if n not in listed and "embed" not in n]
    if extra:
        lines.append("Already installed, not listed in models.md: " + ", ".join(sorted(extra)[:8]))
    return lines


def agent_context(model):
    """Context window the Ollama server really gives this model: measured during an agent run, else
    the configured value (server_context)."""
    meas = _measured().get(normalize(model)) or {}
    if meas.get("agent_ctx"):
        return int(meas["agent_ctx"])
    return server_context()


def server_context(default=65536):
    """OLLAMA_CONTEXT_LENGTH as seen by this process, else the value chosen with ollama_settings.py,
    else the default."""
    v = os.environ.get("OLLAMA_CONTEXT_LENGTH", "")
    if v.isdigit():
        return int(v)
    try:
        v = str(json.load(open(os.path.join(data_dir(), "ollama-settings.json"), encoding="utf-8"))
                .get("OLLAMA_CONTEXT_LENGTH", ""))
    except Exception:  # noqa: BLE001
        v = ""
    return int(v) if v.isdigit() else default


MEM_FILE = os.path.join(STATE_DIR, "memory.json")


def _measured():
    try:
        data = json.load(open(MEM_FILE, encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


THINK_BUDGETS = {"off": 0, "low": 4000, "on": 16000, "medium": 16000, "high": 32000}
ANSWER_TOKENS = 8192      # room for one answer of an agent (a file of a few hundred lines), thinking excluded
AGENT_OVERHEAD = 10000    # Claude Code's instructions and tool definitions, until measured (calibrate.py)


def agent_output_reserve(thinking_budget=0, model=None):
    """Output tokens an agent may produce per answer (CLAUDE_CODE_MAX_OUTPUT_TOKENS). Claude Code keeps
    this much of the context free, so it must stay small: its default for unknown models (32000)
    would leave only half of a 64k context before it compacts the conversation. A model that hit the
    limit even after the lesson "small edits" gets more (learned, raise_output_reserve)."""
    learned = int(((_measured().get(normalize(model)) or {}).get("answer_tokens") or 0)) if model else 0
    return max(ANSWER_TOKENS, learned) + int(thinking_budget or 0)


def learning_off():
    """Calibration and acceptance runs (LOCAL_NO_LEARN=1): exercises, not real tasks. The models still get
    their lessons, but nothing is learned from these runs (memory.json, models.md "Task size")."""
    return os.environ.get("LOCAL_NO_LEARN") == "1"


def raise_output_reserve(model, ceiling=16384):
    """After a second "output limit" failure (the lesson did not suffice): double this model's room per
    answer, up to the ceiling (a quarter of a 64k context). Returns a line for the report."""
    if learning_off():
        return ""
    data = _measured()
    entry = data.setdefault(normalize(model), {})
    if int((entry.get("failures") or {}).get("output limit", 0)) < 2:
        return ""
    now = int(entry.get("answer_tokens") or ANSWER_TOKENS)
    if now >= ceiling:
        return f"Its output room is already {now} tokens: split its tasks instead."
    entry["answer_tokens"] = min(ceiling, now * 2)
    _save_memory(data)
    return f"Its output room per answer goes {now} → {entry['answer_tokens']} tokens."


def agent_overhead(model):
    """Tokens Claude Code's own instructions take in an agent's context: measured, else the estimate."""
    meas = _measured().get(normalize(model)) or {}
    return int(meas.get("agent_overhead") or AGENT_OVERHEAD)


def record_overhead(model, tokens):
    data = _measured()
    data.setdefault(normalize(model), {})["agent_overhead"] = int(tokens)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(MEM_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except OSError:
        pass


def guess_task_size(weights_gb, max_context, tools=True):
    """First "Task size" for a model, from its size (a proxy for its strength) and its context;
    calibration and real use refine it."""
    if not tools:
        return "tiny"
    size = "tiny" if weights_gb < 7 else ("small" if weights_gb < 12 else "medium")
    if max_context and max_context < AGENT_GOOD_CTX and size == "medium":
        size = "small"
    return size


def task_kind(root, write, checked):
    """A coarse kind of task, to learn which model does best at what: new code (the files to write do
    not exist yet) or changes to existing files, with or without an automatic check."""
    paths = [p for p in (write or "").split() if not p.endswith("/")]
    new = bool(paths) and not any(os.path.exists(os.path.join(root, p)) for p in paths)
    return ("new code" if new else "changes") + (", checked" if checked else ", unchecked")


def record_kind_outcomes(kind, tried):
    """Remembers, per model and kind of task, how many attempts passed (memory.json)."""
    if learning_off():
        return
    if not kind or not tried:
        return
    data = _measured()
    for model, result in tried:
        if not counts_for_model(result):
            continue
        entry = data.setdefault(normalize(model), {}).setdefault("kinds", {}).setdefault(kind, [0, 0])
        entry[1] += 1
        entry[0] += result in TASK_SUCCESS
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(MEM_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except OSError:
        pass


def reorder_models(chain, kind, min_tries=2, margin=0.34):
    """Puts first the model with the clearly best record on this kind of task, when the record is
    meaningful (min_tries attempts each). Returns (chain, note) — note empty if the order is kept."""
    if len(chain) < 2 or not kind:
        return chain, ""
    data = _measured()

    def record(m):
        return (data.get(normalize(m)) or {}).get("kinds", {}).get(kind) or [0, 0]

    first = record(chain[0])
    if first[1] < min_tries:
        return chain, ""
    rate = lambda r: r[0] / r[1]  # noqa: E731
    best = max((m for m in chain[1:] if record(m)[1] >= min_tries), key=lambda m: rate(record(m)), default=None)
    if best and rate(record(best)) - rate(first) >= margin:
        b = record(best)
        new = [best] + [m for m in chain if m != best]
        return new, (f"order changed: {best} first on \"{kind}\" tasks ({best} {b[0]}/{b[1]} passed, "
                     f"{chain[0]} {first[0]}/{first[1]})")
    return chain, ""


# Lessons: how a model failed before changes what it is told next time (costs no Claude tokens).
FAILURE_TIPS = {
    "output limit": "Write code in pieces: one WHOLE function per Edit (never a whole file at once, never "
                    "more than ~80 lines in a single Write or Edit).",
    "churn": "Write each function complete in a single Edit (10 to 60 lines), then move on: no series of "
             "one-line edits, no rewording of docstrings or comments.",
    "time limit": "Time is short: read only the files named in the task, implement, finish (the check then runs), fix "
                  "what it reports. "
                  "No exploration of other files, no inspection of the environment. Write early and in parts "
                  "(a first Write, then one Edit per function or section): what is written is kept if time runs out.",
    "step limit": "Before acting, plan in 3 to 5 numbered steps, then follow them in order. Never rerun a "
                  "command or reread a file without having changed something.",
    "wander": "Do not inspect the environment (.venv, the interpreter, installed packages): it is set up and "
              "the check runs the tests for you. Stay in the files named in the task.",
    "loop": "Read each file once and note the line numbers you need; do not reread a file you already read.",
    "tools": "Use only the tools you are given; never call another one. Without a shell you cannot run "
             "commands: finish, the check then runs by itself and you get its errors if it fails.",
}


def failure_mode(status, text, circles, runaway, churn="", wander=""):
    """Why an attempt failed or struggled, in the terms of FAILURE_TIPS (None: no lesson to draw). Also
    used on a pass that ran out of steps or churned: it worked, but wastefully."""
    status = status or ""
    if runaway:
        return "output limit"
    if "tool it does not have" in status:
        return "tools"
    if churn:
        return "churn"             # the cause of a step limit, when it happens: the more useful lesson
    if wander:
        return "wander"
    if "min limit" in status or "budget" in status:
        return "time limit"
    if "step limit" in status:
        return "step limit"
    if circles:
        return "loop"
    return None


def _save_memory(data):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(MEM_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except OSError:
        pass


def record_attempt(model, mode, passed, lines=0, lessons=()):
    """Learns from one model's attempt: its failure mode (counted), the size in lines of what it got
    right (last 8 passes), and whether the lessons it was given held (a lesson that failed twice more
    is marked weak: the report says so, Claude should then split rather than retry)."""
    if learning_off():
        return
    data = _measured()
    entry = data.setdefault(normalize(model), {})
    if mode:
        fails = entry.setdefault("failures", {})
        fails[mode] = int(fails.get(mode, 0)) + 1
    if passed and lines:
        entry["ok_lines"] = (entry.get("ok_lines", []) + [int(lines)])[-8:]
    for lesson in lessons:
        rec = entry.setdefault("lessons", {}).setdefault(lesson, [0, 0])
        rec[1] += 1
        rec[0] += bool(passed) and mode != lesson     # a pass with the same fault again: the lesson did not hold
    _save_memory(data)


def lesson_note(model):
    """Extra rules for this model, from its past failures: appended to its instruction."""
    fails = ((_measured().get(normalize(model)) or {}).get("failures") or {})
    tips = [FAILURE_TIPS[m] for m in FAILURE_TIPS if fails.get(m)]
    return ("\n\nLessons from your previous tasks (follow them):\n- " + "\n- ".join(tips)) if tips else ""


def lessons_of(model):
    fails = ((_measured().get(normalize(model)) or {}).get("failures") or {})
    return [m for m in FAILURE_TIPS if fails.get(m)]


def learned_lines(models):
    """One compact line per model of what the scripts learned (doctor --quick): passes by kind of task,
    size in lines of the tasks it passed, failure modes. Empty when nothing is known yet."""
    data, out = _measured(), []
    for m in models:
        e = data.get(normalize(m)) or {}
        kinds = ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in (e.get("kinds") or {}).items())
        ok = sorted(e.get("ok_lines") or [])
        lines = f"passed up to {ok[-1]} lines (median {ok[len(ok) // 2]})" if ok else ""
        fails = ", ".join(f"{k} ×{v}" for k, v in (e.get("failures") or {}).items())
        weak = [k for k, (p, n) in (e.get("lessons") or {}).items() if n >= 2 and p == 0]
        parts = [p for p in (kinds, lines, fails and "failed on: " + fails,
                             weak and "lessons not enough (split instead): " + ", ".join(weak)) if p]
        if parts:
            out.append(f"  {m}: " + " · ".join(parts))
    return out


def forget_agent_contexts():
    """The context setting changed: the contexts measured during agent runs no longer hold."""
    data = _measured()
    if any("agent_ctx" in v for v in data.values() if isinstance(v, dict)):
        for v in data.values():
            if isinstance(v, dict):
                v.pop("agent_ctx", None)
        try:
            with open(MEM_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
        except OSError:
            pass


def record_loaded(agent_model=""):
    """Records the memory actually used by loaded models (/api/ps: weights + cache + compute), so
    that later estimates for these models are measurements. Keeps the measurement made with the
    largest context. The context agents run with ("agent_ctx") is only taken from an agent run
    (agent_model), never from a one-off question with its own size. Silent on failure."""
    try:
        loaded = api("/api/ps").get("models", [])
    except Exception:  # noqa: BLE001
        return
    data = _measured()
    changed = False
    for m in loaded:
        name = normalize(m.get("name") or m.get("model") or "")
        size, ctx = m.get("size") or 0, m.get("context_length") or 0
        if name and size and ctx and not is_cloud(name, m):
            entry = dict(data.get(name) or {})
            if int(ctx) >= int(entry.get("ctx") or 0):
                entry.update({"size_gb": round(size / 2**30, 2), "ctx": int(ctx)})
            if agent_model and normalize(agent_model) == name:
                entry["agent_ctx"] = int(ctx)
            data[name] = entry
            changed = True
    if changed:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = f"{MEM_FILE}.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, MEM_FILE)
        except OSError:
            pass


def _kv_per_token(mi):
    """KV-cache bytes per token from the architecture (/api/show), in f16; None if unknown.
    An upper bound for sliding-window or partial-attention models (safe: we wait more, not less)."""
    arch = mi.get("general.architecture") or ""

    def g(key):
        return mi.get(f"{arch}.{key}")

    layers, kvh = g("block_count"), g("attention.head_count_kv")
    if not isinstance(layers, int) or layers <= 0:
        return None
    if isinstance(kvh, list) and kvh:
        heads = sum(x for x in kvh if isinstance(x, int))
    elif isinstance(kvh, int) and kvh > 0:
        heads = kvh * layers
        interval = g("full_attention_interval")
        if isinstance(interval, int) and interval > 1:
            heads /= interval
    else:
        return None
    kl, vl = g("attention.key_length"), g("attention.value_length")
    if not isinstance(kl, int) or not isinstance(vl, int):
        emb, nh = g("embedding_length"), g("attention.head_count")
        if not isinstance(emb, int) or not isinstance(nh, int) or nh <= 0:
            return None
        kl = vl = emb // nh
    return heads * (kl + vl) * 2


def model_memory(name, inst=None, ctx=None):
    """(weights GB, need of one agent GB, source) with this context: weights + KV cache + margin.
    Source: "measured" (recorded during a previous use), "estimated" (model architecture)
    or "approximate" (size only)."""
    inst = installed() if inst is None else inst
    key = normalize(name)
    weights = ((inst.get(key) or {}).get("size") or 0) / 2**30
    ctx = ctx or agent_context(name)          # the context the server really uses, not a client default
    meas = _measured().get(key) or {}
    if meas.get("ctx") and meas.get("size_gb", 0) > weights > 0:
        return weights, weights + (meas["size_gb"] - weights) * ctx / meas["ctx"] + 0.5, "measured"
    try:
        per_token = profile(name).get("kv_per_token")
    except ModelError:
        per_token = None
    if per_token:
        factor = {"q8_0": 0.5, "q4_0": 0.25}.get(os.environ.get("OLLAMA_KV_CACHE_TYPE", "").lower(), 1.0)
        return weights, weights + per_token * ctx * factor / 2**30 + 1.0 + 0.03 * weights, "estimated"
    return weights, weights + (ctx / 65536) * max(2.0, 0.25 * weights) + 1.0, "approximate"


RUN_DIR = os.path.join(STATE_DIR, "running")


def pid_alive(pid):
    try:
        if IS_WIN:
            out = run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], timeout=10)[1]
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


def _running():
    """Local agents currently running on this machine (registry shared by parallel tasks)."""
    out = []
    os.makedirs(RUN_DIR, exist_ok=True)
    for fn in os.listdir(RUN_DIR):
        path = os.path.join(RUN_DIR, fn)
        try:
            e = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if pid_alive(int(e.get("pid", 0))):
            out.append(e)
        else:
            try:
                os.remove(path)
            except OSError:
                pass
    return out


def _lock(path, stale=60):
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > stale:
                    os.remove(path)
                    continue
            except OSError:
                pass
            time.sleep(0.2)


def memory_load(entries):
    """Memory used by running agents: each model's weights once + their caches."""
    weights = {}
    kv = 0.0
    for e in entries:
        weights[e["model"]] = max(weights.get(e["model"], 0.0), e["weights"])
        kv += e["need"] - e["weights"]
    return sum(weights.values()) + kv


def acquire_memory(model, max_wait_s=7200, on_wait=None):
    """Reserves memory for an agent. If it is short (parallel agents), WAITS instead of switching to a
    smaller model: quality before speed. Returns (token, seconds waited); token None if the
    maximum wait is exceeded."""
    os.makedirs(RUN_DIR, exist_ok=True)
    try:
        weights, need, _ = model_memory(model)
    except ModelError:
        weights, need = 0.0, 0.0
    usable = machine()["usable_gb"] or float("inf")
    lock = os.path.join(RUN_DIR, ".lock")
    start, warned = time.time(), False
    while True:
        _lock(lock)
        try:
            entries = _running()
            total = memory_load(entries + [{"model": model, "weights": weights, "need": need}])
            if not entries or total <= usable:
                token = os.path.join(RUN_DIR, f"{os.getpid()}-{int(time.time() * 1000)}.json")
                with open(token, "w", encoding="utf-8") as f:
                    json.dump({"pid": os.getpid(), "model": model, "weights": weights, "need": need,
                               "since": time.time()}, f)
                return token, time.time() - start
        finally:
            try:
                os.remove(lock)
            except OSError:
                pass
        if time.time() - start > max_wait_s:
            return None, time.time() - start
        if on_wait and not warned:
            on_wait(total, usable, [e["model"] for e in entries])
            warned = True
        time.sleep(float(os.environ.get("LOCAL_WAIT_POLL", "10")))


def release_memory(token):
    if token:
        try:
            os.remove(token)
        except OSError:
            pass


# --------------------------------------------------------------------------- Ollama and models
def _default_host():
    """OLLAMA_HOST_URL, else the port of OLLAMA_HOST if it designates this machine, else the standard port."""
    if os.environ.get("OLLAMA_HOST_URL"):
        return os.environ["OLLAMA_HOST_URL"].rstrip("/")
    m = re.match(r"^(?:https?://)?(localhost|127\.0\.0\.1|0\.0\.0\.0)?:?(\d+)?$", os.environ.get("OLLAMA_HOST", ""))
    port = m.group(2) if m and m.group(2) else "11434"
    return f"http://127.0.0.1:{port}"


HOST = _default_host()
AGENT_MIN_CTX = 32768
AGENT_GOOD_CTX = 65536
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # localhost: never a proxy


class ModelError(Exception):
    pass


def is_local_host(host=None):
    host = host or HOST
    return host.startswith("http://localhost:") or host.startswith("http://127.0.0.1:")


def api(path, payload=None, timeout=10):
    if not is_local_host():
        raise ModelError(f"'{HOST}' is not local. Only localhost is allowed.")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(HOST + path, data=data, headers={"Content-Type": "application/json"})
    with _opener.open(req, timeout=timeout) as r:
        return json.load(r)


def normalize(name):
    name = (name or "").strip()
    return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"


def is_cloud(name, entry=None):
    tag = normalize(name).rsplit(":", 1)[-1].lower()
    if tag == "cloud" or tag.endswith("-cloud"):
        return True
    return bool(entry and (entry.get("remote_host") or entry.get("remote_model")))


def installed():
    try:
        tags = api("/api/tags")
    except ModelError:
        raise
    except Exception:  # noqa: BLE001
        raise ModelError(f"Ollama does not answer on {HOST}: start the Ollama app or 'ollama serve'.")
    out = {}
    for m in tags.get("models", []):
        name = m.get("name") or m.get("model") or ""
        if name and not is_cloud(name, m):
            out[normalize(name)] = m
    return out


def profile(name):
    try:
        info = api("/api/show", {"model": name}, timeout=20)
    except Exception:  # noqa: BLE001
        raise ModelError(f"cannot read the characteristics of '{name}' from Ollama.")
    caps = set(info.get("capabilities") or [])
    ctx = 0
    for key, val in (info.get("model_info") or {}).items():
        if key.endswith(".context_length") and isinstance(val, int):
            ctx = max(ctx, val)
    thinking = info.get("thinking")
    return {
        "name": name,
        "tools": "tools" in caps,
        "chat": ("completion" in caps) or not caps,
        "embedding_only": "embedding" in caps and "completion" not in caps,
        "think_values": thinking.get("values") if isinstance(thinking, dict) else None,
        "think_default": thinking.get("default") if isinstance(thinking, dict) else None,
        "thinking_cap": "thinking" in caps,
        "context": ctx,
        "family": (info.get("details") or {}).get("family", ""),
        "cloud": is_cloud(name, info),
        "kv_per_token": _kv_per_token(info.get("model_info") or {}),
    }


def think_param(p, requested):
    """LOCAL_THINK (off/on/low/medium/high) -> ("think" value accepted by THIS model, note).
    Value None = do not send the parameter."""
    requested = (requested or "off").lower()
    values = p["think_values"]
    if values is None:
        if not p["thinking_cap"]:
            return None, ("" if requested == "off" else f"{p['name']} does not support reasoning")
        return {"off": False, "on": True}.get(requested, requested), ""
    if values in ([False], []):
        return None, ("" if requested == "off" else f"{p['name']} does not support reasoning")
    levels = [v for v in values if isinstance(v, str)]
    if requested == "off":
        if False in values:
            return False, ""
        low = levels[0] if levels else None
        return low, (f"{p['name']} cannot turn reasoning off: minimum level \"{low}\"" if low else "")
    if requested == "on":
        if True in values:
            return True, ""
        return (p["think_default"] if p["think_default"] in levels else (levels[-1] if levels else None)), ""
    if requested in levels:
        return requested, ""
    if True in values:
        return True, f"levels not supported by {p['name']}: reasoning simply turned on"
    return p["think_default"], f"level \"{requested}\" unknown to {p['name']}: default level"


def models_md_entries():
    path = MODELS_MD if os.path.exists(MODELS_MD) else TEMPLATE
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return []
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)   # commented-out examples: ignored
    return [(m.group(1), "DEFAULT" in m.group(2).upper())
            for m in re.finditer(r"^##\s+`?([^\s`]+)`?(.*)$", text, re.M) if looks_like_model(m.group(1))]


def models_md_digest():
    """A few lines that stand for models.md at the start of a session (doctor --quick): the preference
    values (without their explanations), each model's role and own task size, and what must not be
    delegated. Claude reads the whole file only to plan, calibrate or update it."""
    path = MODELS_MD if os.path.exists(MODELS_MD) else TEMPLATE
    try:
        text = re.sub(r"<!--.*?-->", "", open(path, encoding="utf-8").read(), flags=re.S)
    except OSError:
        return []
    out = []
    sec = re.search(r"^## Preferences\s*$\n((?:(?!^## ).*\n?)*)", text, re.M)
    if sec:          # top-level lines only: the indented ones explain the options
        prefs = [f"{m.group(1).strip()} {re.sub(r'  +[(].*[)]$', '', m.group(2)).strip()}"
                 for m in re.finditer(r"^-\s*([^:\n]+):\s*(.+?)\s*$", sec.group(1), re.M)]
        if prefs:
            out.append("Preferences: " + " · ".join(prefs))
    for name, default in models_md_entries():
        role = model_setting(name, "Role")
        size = model_setting(name, "Task size")
        out.append(f"Model {name}{' (DEFAULT)' if default else ''}: " + (role or "no role given")
                   + (f" · task size {size}" if size else ""))
    dont = re.search(r"^## Do not delegate locally.*$\n((?:(?!^## ).*\n?)*)", text, re.M)
    items = [l.strip()[1:].strip() for l in (dont.group(1) if dont else "").splitlines()
             if l.strip().startswith("-") and l.strip() not in ("-", "- —", "- -")]
    if items:
        out.append("Do not delegate locally: " + " · ".join(items))
    return out


# Task size: tiny, small, medium, large or xlarge (or a percentage for a finer setting): share of the
# agent's usable context a task may fill BEFORE any work (instruction + files to read + Claude Code's
# own instructions). 1 to 5 are accepted as synonyms.
SIZE_SHARES = {"tiny": 0.20, "small": 0.30, "medium": 0.40, "large": 0.50, "xlarge": 0.65}
SIZE_ORDER = list(SIZE_SHARES)
DEFAULT_SIZE = "medium"


def parse_task_size(raw):
    """(label, share) for "small", "35%", "medium (default)", "2"…; None if not understood."""
    raw = (raw or "").strip().lower()
    m = re.match(r"(\d{1,2})\s*%", raw)
    if m:
        return (f"{m.group(1)}%", int(m.group(1)) / 100) if 10 <= int(m.group(1)) <= 80 else None
    word = (re.findall(r"[a-z0-9]+", raw) or [""])[0]
    if word.isdigit() and 1 <= int(word) <= len(SIZE_ORDER):
        word = SIZE_ORDER[int(word) - 1]
    return (word, SIZE_SHARES[word]) if word in SIZE_SHARES else None


def size_step(name, delta):
    """The size `delta` steps away from `name`, kept within tiny..xlarge."""
    i = SIZE_ORDER.index(name) if name in SIZE_ORDER else SIZE_ORDER.index(DEFAULT_SIZE)
    return SIZE_ORDER[max(0, min(len(SIZE_ORDER) - 1, i + delta))]


def set_model_setting(model, name, value):
    """Writes "- <name>: <value>" in the block of this model in models.md (replaced, or added after its
    Role line). False if the model has no block."""
    try:
        text = open(MODELS_MD, encoding="utf-8").read()
    except OSError:
        return False
    want = normalize(model)
    line = f"- {name}: {value}"
    for b in re.finditer(r"^##\s+`?([^\s`]+)`?.*$\n((?:(?!^## ).*\n?)*)", text, re.M):
        if normalize(b.group(1)) != want or "<!--" in text[:b.start()].rsplit("-->", 1)[-1]:
            continue
        body = b.group(2)
        pat = rf"^\s*-\s*{re.escape(name)}\s*:.*$"
        if re.search(pat, body, re.M | re.I):
            body = re.sub(pat, line, body, count=1, flags=re.M | re.I)
        else:
            m = re.search(r"^\s*-\s*Role\s*:.*\n", body, re.M | re.I)
            at = m.end() if m else 0
            body = body[:at] + line + "\n" + body[at:]
        try:
            with open(MODELS_MD, "w", encoding="utf-8") as f:
                f.write(text[:b.start(2)] + body + text[b.end(2):])
            return True
        except OSError:
            return False
    return False


RAISE_AFTER = 3        # tasks passed close to the limit before a model's task size goes up one step


def adjust_task_size(model, size, outcome, fill=0.0):
    """Learns each model's task size from real tasks. outcome: "context full" (one step down at once) or
    "passed" with fill = share of the size limit the task used (RAISE_AFTER passes above 60 % of it:
    one step up). Writes the model's own "Task size" line in models.md; returns a line for the report."""
    if learning_off():
        return ""
    if size not in SIZE_ORDER:
        return ""                                   # a percentage: set by hand, left alone
    data = _measured()
    entry = data.setdefault(normalize(model), {})
    new, why = size, ""
    if outcome == "context full":
        entry["size_streak"] = 0
        new, why = size_step(size, -1), "its context filled up during this task"
    elif outcome == "step limit":
        entry["size_streak"] = 0
        entry["step_limits"] = int(entry.get("step_limits", 0)) + 1
        if entry["step_limits"] >= 2:
            entry["step_limits"] = 0
            new, why = size_step(size, -1), "it ran out of steps on 2 tasks in a row"
    elif outcome == "passed":
        entry["step_limits"] = 0
    if outcome == "passed" and fill >= 0.6:
        entry["size_streak"] = int(entry.get("size_streak", 0)) + 1
        if entry["size_streak"] >= RAISE_AFTER:
            entry["size_streak"] = 0
            new = size_step(size, +1)
            if new == "xlarge" and agent_context(model) < 131072:
                new = size
            why = f"{RAISE_AFTER} tasks close to the limit passed"
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(MEM_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except OSError:
        pass
    if new != size and set_model_setting(model, "Task size", new):
        return f"Task size of {model}: {size} → {new} ({why}; models.md updated)."
    return ""


def model_setting(model, name, default=""):
    """Value of a line "- <name>: <value>" in the block of this model in models.md, else the default."""
    path = MODELS_MD if os.path.exists(MODELS_MD) else TEMPLATE
    try:
        text = re.sub(r"<!--.*?-->", "", open(path, encoding="utf-8").read(), flags=re.S)
    except OSError:
        return default
    want = normalize(model)
    for m in re.finditer(r"^##\s+`?([^\s`]+)`?.*$\n((?:(?!^## ).*\n?)*)", text, re.M):
        if normalize(m.group(1)) == want:
            v = re.search(rf"^\s*-\s*{re.escape(name)}\s*:\s*(.+?)\s*$", m.group(2), re.M | re.I)
            return v.group(1) if v else default
    return default


def preference(name, default=""):
    """Value of a models.md preference (line "- <name>: <value>"), else the default value."""
    path = MODELS_MD if os.path.exists(MODELS_MD) else TEMPLATE
    try:
        text = re.sub(r"<!--.*?-->", "", open(path, encoding="utf-8").read(), flags=re.S)
    except OSError:
        return default
    sec = re.search(r"^## Preferences\s*$\n((?:(?!^## ).*\n?)*)", text, re.M)   # not a model's own line
    m = re.search(rf"^\s*-\s*{re.escape(name)}\s*:\s*(.+?)\s*$", sec.group(1) if sec else text, re.M | re.I)
    return re.sub(r"\s+\(.*\)$", "", m.group(1)) if m else default       # without the "  (options…)" note


def agent_models(spec, exclude=""):
    """Local models usable as agents, according to "auto" (models.md order), "none" or a list."""
    spec = (spec or "auto").strip()
    if spec.lower() in ("none", "no", "0", "-"):
        return []
    inst = installed()
    names = [n for n, _ in models_md_entries()] if spec.lower() == "auto" else spec.replace(",", " ").split()
    out = []
    for n in names:
        key = normalize(n)
        if key == normalize(exclude) or key in [normalize(x) for x in out] or is_cloud(n) or key not in inst:
            continue
        try:
            p = profile(n)
        except ModelError:
            continue
        if p["tools"] and p["chat"] and (not p["context"] or p["context"] >= AGENT_MIN_CTX):
            out.append(n)
    return out


def looks_like_model(name):
    """Tells a model name ("gpt-oss:20b", "hf.co/Org/Model:Q4_K_M") from a section title
    ("Preferences"): a model name has a tag (":") or a namespace ("/")."""
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*(:[A-Za-z0-9._-]+)?", name or "")
                and (":" in name or "/" in name))


def suggest(name, inst=None):
    """Installed models with the same base name, to fix a missing or wrong tag."""
    base = normalize(name).rsplit(":", 1)[0]
    inst = installed() if inst is None else inst
    return [n for n in inst if n.rsplit(":", 1)[0] == base]


def resolve_models(spec, inst=None):
    """The models a user named ("20b,gemma" or exact names), in their order. Each name is an installed
    local model's exact name (":latest" optional) or a part of exactly one installed name. Commas or
    spaces separate them, never "-" (model names contain it). Raises ModelError."""
    inst = installed() if inst is None else inst
    out = []
    for tok in re.split(r"[,\s]+", (spec or "").strip()):
        if not tok:
            continue
        if is_cloud(tok):
            raise ModelError(f"'{tok}' is a cloud model, which is forbidden. Use a local model.")
        hit = normalize(tok) if normalize(tok) in inst else None
        if not hit:
            cands = [n for n in inst if tok.lower() in n.lower()]
            if len(cands) > 1:
                raise ModelError(f"'{tok}' matches several installed models ({', '.join(cands)}): be more precise.")
            if not cands:
                hint = " Separate models with commas, not '-'." if "-" in tok else ""
                raise ModelError(f"No installed local model matches '{tok}'.{hint} "
                                 f"Installed: {', '.join(inst) or 'none'}.")
            hit = cands[0]
        if hit not in out:
            out.append(hit)
    if not out:
        raise ModelError("No model given.")
    return out


def default_model(explicit=None):
    """Explicit model, else LOCAL_MODEL, else "DEFAULT" in models.md, else the first listed and
    installed one, else the first installed local model that supports tools."""
    name = (explicit or os.environ.get("LOCAL_MODEL", "")).strip()
    if name:
        return name
    inst = installed()
    entries = [(n, d) for n, d in models_md_entries() if normalize(n) in inst and not is_cloud(n)]
    for n, is_default in entries:
        if is_default:
            return n
    if entries:
        return entries[0][0]
    for n in inst:
        try:
            p = profile(n)
        except ModelError:
            continue
        if p["tools"] and p["chat"]:
            return n
    raise ModelError("no usable local model: install one (doctor.py --suggest proposes some for this machine) "
                     "and add it to ~/.claude/local-agents/models.md.")


def check_model(name, need_tools=False):
    if is_cloud(name):
        raise ModelError(f"'{name}' is a cloud model, which is forbidden. Use a local model.")
    inst = installed()
    if normalize(name) not in inst:
        alt = suggest(name, inst)
        if alt:
            raise ModelError(f"'{name}' not found; installed as: {', '.join(alt)} (specify the tag).")
        raise ModelError(f"model '{name}' is not installed. Run: ollama pull {name}")
    p = profile(name)
    if p["cloud"]:
        raise ModelError(f"'{name}' is a remote (cloud) model, which is forbidden.")
    if p["embedding_only"] or not p["chat"]:
        raise ModelError(f"'{name}' is not a chat model (an embedding model?).")
    if need_tools and not p["tools"]:
        raise ModelError(f"'{name}' does not support tool calling: it cannot be an agent. "
                         "Use it for simple questions (ask_local.py) or pick another model.")
    return p


def chat(model, prompt, think=None, options=None, keep_alive=None, timeout=1800):
    """/api/chat call; if the model rejects the "think" parameter (400), retries without it."""
    payload = {"model": model, "stream": False, "messages": [{"role": "user", "content": prompt}]}
    if think is not None:
        payload["think"] = think
    if options:
        payload["options"] = options
    if keep_alive:
        payload["keep_alive"] = keep_alive
    try:
        return api("/api/chat", payload, timeout=timeout), ""
    except urllib.error.HTTPError as e:
        if e.code == 400 and "think" in payload:
            payload.pop("think")
            return api("/api/chat", payload, timeout=timeout), "answered without the reasoning setting (rejected by the model)"
        raise


if __name__ == "__main__":
    utf8_stdio()
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "default":
            print(default_model())
        elif len(sys.argv) > 2 and sys.argv[1] == "hidden-dir":
            print(hidden_dir(re.sub(r"[^A-Za-z0-9._-]", "-", sys.argv[2])))
        else:
            print(__doc__)
    except ModelError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
