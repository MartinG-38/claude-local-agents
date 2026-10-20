#!/usr/bin/env python3
"""Guard for local agents (Claude Code PreToolUse hook). Uses no Claude tokens.

Checks every action of the local agent before it runs:
  - files: only inside the project, never secrets or .git/, writes limited to
    LOCAL_WRITE_PATHS if set;
  - commands: denylist (refused), allowlist (accepted), gray zone -> local security expert
    (an Ollama model, free). Without a clear answer from the expert: refused.
Exit 2 = action blocked, the reason is sent back to the agent. Every decision is logged.

Variables: LOCAL_PROJECT_ROOT, LOCAL_WRITE_PATHS, LOCAL_NO_READ, LOCAL_DATA_READ_KB, LOCAL_SECURITY_LOG,
           LOCAL_REVIEW_MODEL, OLLAMA_HOST_URL.
"""
import datetime
import fnmatch
import glob
import json
import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

ROOT = os.path.realpath(os.environ.get("LOCAL_PROJECT_ROOT") or os.getcwd())
WRITE_PATHS = os.environ.get("LOCAL_WRITE_PATHS", "").split()
READONLY = os.environ.get("LOCAL_READONLY") == "1"
NO_READ = os.environ.get("LOCAL_NO_READ", "").split()
DATA_READ_KB = int(os.environ.get("LOCAL_DATA_READ_KB") or 16)
# Data files: a full read of a big one fills the agent's context and its time limit for nothing (the
# instruction gives their structure). Binary ones are never useful to Read.
DATA_EXT = {".csv", ".tsv", ".jsonl", ".ndjson", ".json", ".xml", ".log", ".dat"}
BINARY_DATA_EXT = {".db", ".sqlite", ".sqlite3", ".parquet", ".feather", ".xlsx", ".xls", ".pkl", ".npy", ".npz", ".h5"}
LOG = os.environ.get("LOCAL_SECURITY_LOG", "")
REVIEW_MODEL = os.environ.get("LOCAL_REVIEW_MODEL") or os.environ.get("LOCAL_MODEL", "")
REVIEW_TIMEOUT = 90  # seconds; the hook is declared with a longer timeout

SECRET_PATTERNS = common.SECRET_PATTERNS

# Start of a command (after ; && || | ( $( or a backquote, and after wrappers such as env, nohup, xargs…):
# system commands are only refused there, so that `grep -rn 'open(' src` or `ls docs/source` still work.
CMD = r"(?:^|[;&|(`]|\$\()\s*(?:(?:sudo|env|nohup|time|nice|command|builtin|exec|xargs)(?:\s+-\S+)*\s+|\w+=\S*\s+)*"

# Always refused (even if the expert would say yes).
DENY = [
    (r"[<>]\(", "process substitution"),
    (r"\bsudo\b|\bsu\b|\bdoas\b", "privilege escalation"),
    (r"\brm\s+(-[a-zA-Z]*[rf]|--recursive|--force)", "recursive or forced deletion"),
    (r"\b(curl|wget|ssh|scp|sftp|rsync|nc|ncat|netcat|telnet|ftp)\b", "network access"),
    (r"\bgit\s+(push|pull|fetch|clone|remote|reset|clean|checkout|restore|rebase|filter-branch|config)\b",
     "git command reserved for Claude"),
    (r"\b(pip3?|uv|poetry|conda|npm|pnpm|yarn|brew|gem|cargo)\s+(install|add|remove|uninstall)\b",
     "package installation"),
    (r"\b(chmod|chown|chflags|xattr)\b", "permission change"),
    (CMD + r"(dd|mkfs|diskutil|shutdown|reboot|halt|kill|killall|pkill|launchctl|crontab|osascript|open|defaults|"
           r"security|systemctl|systemd-run|loginctl|docker|podman)\b", "system command"),
    (CMD + r"(eval|exec|source)\b|(^|[;&|]\s*)\.\s", "indirect execution"),
    (r"base64\s+(-d|--decode)|\|\s*(sh|bash|zsh|python3?)\b", "execution of piped code"),
    (r"\bollama\b|\bclaude\b", "model control reserved for Claude"),
    (r">\s*/dev/(?!null)|/etc/|/System/|/Library/|/usr/", "system area"),
    (r"(^|\s|=|['\"])~|\$\{?HOME\b|\$\{?(USERPROFILE|APPDATA|LOCALAPPDATA)\b|\.\./", "path outside the project"),
    (r"(^|[\s/'\"=])\.git(/|\s|$)", "git history reserved for Claude"),
    (r"\bfind\b.*\s-(delete|exec|execdir|ok|okdir|fprint|fprint0|fprintf|fls)\b", "destructive find"),
    (r"--output\b|\s--pre(\s|=)|\btee\b", "indirect file write"),
    (r"(?i)\b(Remove-Item|Invoke-WebRequest|Invoke-RestMethod|iwr|irm|Start-Process|Set-ExecutionPolicy|"
     r"Stop-Process|New-Service|Set-ItemProperty|reg\s+(add|delete)|del\s+/[sq]|rd\s+/s|rmdir\s+/s)\b",
     "destructive or network Windows command"),
]

# Allowed without review: commands that read or test without rewriting files
# (every segment of the command must start like this). Tools that reformat
# (black, isort, ruff format, ruff --fix) go through the expert.
PY = r"(\.?venv[\\/](bin|Scripts)[\\/])?python(3|\.exe)?"
ALLOW = [
    rf"({PY}\s+-m\s+)?pytest(\s|$)", rf"{PY}\s+-m\s+(unittest|doctest|mypy)(\s|$)",
    rf"({PY}\s+-m\s+)?ruff\s+check(?!.*--fix)(\s|$)", rf"({PY}\s+-m\s+)?(mypy|flake8|pylint)(\s|$)",
    r"(ls|pwd|wc|head|tail|cat|echo|grep|rg|find|diff)(\s|$)",
    r"git\s+(status|diff|log|show)(\s|$)", r"(true|false)$",
]

REVIEW_PROMPT = """You are a security expert. An automated coding agent wants to run this shell command
in the project {root} ({os_name}). It is only allowed to read and test the project's code:
no network, no change outside the project, no installation, nothing irreversible.

Command: {cmd}

Answer on a single line, exactly:
ALLOW
or
DENY: <short reason>
When in doubt, DENY."""


def log(decision, tool, detail, reason=""):
    if not LOG:
        return
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": datetime.datetime.now().isoformat(timespec="seconds"),
                                "decision": decision, "tool": tool, "detail": detail[:300],
                                "reason": reason}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def block(tool, detail, reason):
    log("DENIED", tool, detail, reason)
    print(f"Action blocked by security: {reason}. Stay within the scope of the task.", file=sys.stderr)
    sys.exit(2)


def allow(tool, detail, how="rule"):
    log(f"ALLOWED ({how})", tool, detail)
    sys.exit(0)


def inside_root(path):
    """(inside the project?, relative path with "/"). Handles Windows paths (C:\\..., another drive)."""
    p = os.path.realpath(os.path.join(ROOT, os.path.expanduser(path)))
    cmp_p, cmp_root = (os.path.normcase(p), os.path.normcase(ROOT))
    ok = cmp_p == cmp_root or cmp_p.startswith(cmp_root.rstrip(os.sep) + os.sep)
    try:
        rel = os.path.relpath(p, ROOT)
    except ValueError:  # another drive on Windows
        return False, p
    return ok, rel.replace(os.sep, "/")


def is_secret(rel):
    name = os.path.basename(rel)
    return any(fnmatch.fnmatch(name, pat) for pat in SECRET_PATTERNS)


def check_data_read(tool, path, inp):
    """Reads of data files: none under --no-read, binary ones never, big text ones only their head."""
    ok, rel = inside_root(path)
    if not ok:
        return
    if NO_READ and common.in_write_scope(rel, NO_READ):
        block(tool, rel, "the instruction already gives what you need from these files (--no-read): "
                         "do not open them, work from the instruction")
    if tool != "Read":
        return
    ext = os.path.splitext(rel)[1].lower()
    if ext in BINARY_DATA_EXT:
        block(tool, rel, "binary data file: Read cannot show it usefully; work from the instruction")
    try:
        kb = os.path.getsize(os.path.join(ROOT, rel)) / 1024
    except OSError:
        return
    limit = inp.get("limit")
    if ext in DATA_EXT and kb > DATA_READ_KB and not (isinstance(limit, int) and 0 < limit <= 40):
        block(tool, rel, f"data file of {kb:.0f} KB: read only its first lines (Read with limit=20), "
                         "the instruction gives its structure")


def check_file(tool, path, write):
    if write and READONLY:
        block(tool, path, "read-only exploration: no writes")
    ok, rel = inside_root(path)
    if not ok:
        block(tool, path, "file outside the project")
    if is_secret(rel):
        block(tool, rel, "sensitive file (secrets, keys)")
    parts = rel.lower().split("/")
    if ".git" in parts or (write and ".claude" in parts):
        block(tool, rel, "reserved folder (.git / .claude)")
    if write and WRITE_PATHS:
        if not common.in_write_scope(rel, WRITE_PATHS):
            block(tool, rel, f"writes limited to: {' '.join(WRITE_PATHS)}")
    allow(tool, rel)


def ask_reviewer(cmd):
    if not REVIEW_MODEL or common.is_cloud(REVIEW_MODEL):
        return False, "no local security expert available"
    if not common.is_local_host():
        return False, "Ollama server is not local"
    try:
        p = common.profile(REVIEW_MODEL)
        think, _ = common.think_param(p, "on")
        res, _ = common.chat(REVIEW_MODEL, REVIEW_PROMPT.format(root=ROOT, cmd=cmd, os_name=common.OS_NAME), think=think,
                             options={"temperature": 0}, timeout=REVIEW_TIMEOUT)
        answer = (res.get("message") or {}).get("content", "").strip()
    except Exception as e:  # noqa: BLE001
        return False, f"security expert unreachable ({type(e).__name__})"
    first = answer.splitlines()[0].strip() if answer else ""
    if first.upper().startswith("ALLOW"):
        return True, ""
    if first.upper().startswith("DENY"):
        return False, "security expert: " + first.split(":", 1)[-1].strip()
    return False, "unclear answer from the security expert"


def brace_expand(word, limit=64):
    m = re.search(r"\{([^{}]*,[^{}]*)\}", word)
    if not m:
        return [word]
    out = []
    for alt in m.group(1).split(","):
        out += brace_expand(word[:m.start()] + alt + word[m.end():], limit)
        if len(out) >= limit:
            break
    return out[:limit]


def check_bash(cmd):
    flat = " ".join(cmd.split())
    for pattern, reason in DENY:
        if re.search(pattern, flat):
            block("Bash", flat, reason)
    # Words as bash will really see them: $'…' decoded, quotes removed (.gi''t), braces expanded
    # ({.e,}nv): every resulting path is checked.
    decoded = re.sub(r"\$'((?:[^'\\]|\\.)*)'",
                     lambda m: "'" + m.group(1).encode("utf-8").decode("unicode_escape", "replace").replace("'", "") + "'",
                     flat)
    try:
        words = shlex.split(decoded)
    except ValueError:
        words = decoded.split()
    tokens = [t for w in words for t in brace_expand(w)]
    for tok in tokens:
        if ".git" in tok.replace("\\", "/").lower().split("/"):
            block("Bash", flat, "git history reserved for Claude")
        if any(is_secret(piece) for piece in re.split(r"[=:]", tok) if piece):      # also HEAD:.env, --x=.env
            block("Bash", flat, "sensitive file (secrets, keys)")
        if re.search(r"[*?\[]", tok):          # globs: check what they really match (.en? -> .env)
            pattern = tok if os.path.isabs(tok) else os.path.join(ROOT, tok)
            for match in glob.glob(pattern, recursive=True)[:500]:
                ok, rel = inside_root(match)
                if not ok or is_secret(rel) or ".git" in rel.lower().split("/"):
                    block("Bash", flat, f"pattern matching a forbidden file ({tok})")
        if re.match(r"^[A-Za-z]:[\\/]", tok) and not inside_root(tok)[0]:   # C:\... or C:/...
            block("Bash", flat, f"path outside the project ({tok})")
        for piece in re.split(r"=|(?<![A-Za-z]):", tok):
            if piece.startswith("/") and not piece.startswith(("/tmp", "/private/tmp", "/dev/null")):
                if not inside_root(piece)[0]:
                    block("Bash", flat, f"path outside the project ({piece})")
    # Allowlist: every segment must be recognized, with no command substitution
    # and no redirection to a file (a write always goes through the expert).
    segments = [s.strip() for s in re.split(r"&&|\|\||;|\||\n", flat) if s.strip()]
    cleaned = re.sub(r"\d?>&\d|\d?>\s*/dev/null", "", flat)
    redirect = re.search(r">", cleaned)
    # Redirection target = file write: same rules as the Write tool.
    for target in re.findall(r">>?\s*([^\s;&|]+)", cleaned):
        target = target.strip("'\"")
        ok, rel = inside_root(target)
        parts = rel.lower().split("/")
        if not ok or is_secret(rel) or ".git" in parts or ".claude" in parts:
            block("Bash", flat, f"redirection to a forbidden file ({target})")
        if WRITE_PATHS and not common.in_write_scope(rel, WRITE_PATHS):
            block("Bash", flat, f"redirection outside the writable scope ({target})")
    if "$(" not in flat and "`" not in flat and not redirect and segments and all(
        any(re.match(p, s) for p in ALLOW) for s in segments
    ):
        allow("Bash", flat)
    ok, reason = ask_reviewer(flat)
    if ok:
        allow("Bash", flat, "security expert")
    block("Bash", flat, reason)


def main():
    common.utf8_stdio()
    try:
        data = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        block("?", "", "unreadable hook input")
    tool = data.get("tool_name", "")
    inp = data.get("tool_input", {}) or {}
    if tool in ("Bash", "PowerShell"):
        check_bash(inp.get("command", ""))
    elif tool in ("Edit", "Write", "MultiEdit"):
        check_file(tool, inp.get("file_path", ""), write=True)
    elif tool == "NotebookEdit":
        check_file(tool, inp.get("notebook_path", ""), write=True)
    elif tool == "Read":
        check_data_read(tool, inp.get("file_path", ""), inp)
        check_file(tool, inp.get("file_path", ""), write=False)
    elif tool in ("Glob", "Grep"):
        pattern = inp.get("pattern", "") if tool == "Glob" else inp.get("glob", "") or ""
        if ".." in pattern or pattern.startswith(("/", "~")):
            block(tool, pattern, "search pattern outside the project")
        if tool == "Grep":
            check_data_read(tool, inp.get("path") or ".", inp)
        check_file(tool, inp.get("path") or ".", write=False)
    else:
        block(tool, "", f"tool {tool} not allowed for local agents")


if __name__ == "__main__":
    main()
