#!/usr/bin/env python3
"""Runs a task's check command (tests, linter), in a sandbox. Uses no Claude tokens.

The tested code was written by a local agent, so it is confined: macOS (sandbox-exec) or Linux
(bubblewrap, package "bubblewrap"): no network, the project READ-ONLY by default (the checked code
can neither hide files from git, nor delete ignored files such as .env, nor rewrite .git), writes
only in a scratch folder (caches, reports), secrets, the user's other processes and the held-out
tests out of reach, environment variables that may hold secrets removed.
Without a sandbox (Windows, Linux without bubblewrap) the check is refused unless
LOCAL_ALLOW_UNSANDBOXED=1 (then the report says "[no sandbox]").
With --writable, the project's secret files stay read-only and files created by the check that git
ignores are listed in the report.
The evidence protects against accidents (early exit, no test collected), not against code written to
fool the test runner: that is the job of the held-out tests and of Claude's diff review.

A check only passes with positive evidence: exit code 0, and for pytest a test report showing at
least one test and no failure; for unittest "Ran N tests" (N > 0) and "OK". A check that runs no
test, or stops before reporting, fails. A command that cannot run (exit 126/127) or that tries to
write into the read-only project, or outside the project and its scratch folder, is reported as
"CHECK IMPOSSIBLE", not as a failure of the code. Temporary files: $LOCAL_TMP (= $TMPDIR, used by
Python's tempfile), a scratch folder emptied after the check; a fixed path such as /tmp/x is refused.

Usage: check_run.py <project root> "<command>" [--writable]
Variables: LOCAL_CHECK_TIMEOUT (seconds, default 300), LOCAL_STATS_FILE (optional)
Exit code: 0 if the check passes, 1 otherwise.
"""
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

READ_ONLY_MARKERS = re.compile(r"Read-only file system|\[Errno 30\]|EROFS")
# macOS: sandbox-exec denies a write with EPERM ("Operation not permitted"), not EROFS.
if common.IS_MAC:
    READ_ONLY_MARKERS = re.compile(READ_ONLY_MARKERS.pattern + r"|\[Errno 1\] Operation not permitted|EPERM: operation not permitted")
# The path in "PermissionError: [Errno 1] Operation not permitted: '/tmp/x.json'" and the like.
DENIED_PATH = re.compile(r"(?:Operation not permitted|Read-only file system|EROFS|EPERM)[^'\"\n]{0,40}['\"](/[^'\"\n]+)['\"]")


def _denied_outside(out, root):
    """The first refused path outside the project, if the output names one."""
    for path in DENIED_PATH.findall(out):
        real = os.path.realpath(path)
        if real != root and not real.startswith(root.rstrip(os.sep) + os.sep):
            return path
    return ""


def _pytest_evidence(xml_path):
    """(tests, failures + errors) from a JUnit XML report, or None if there is no usable report."""
    try:
        root = ET.parse(xml_path).getroot()
    except (OSError, ET.ParseError):
        return None
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    tests = sum(int(x.get("tests", 0)) for x in suites)
    bad = sum(int(x.get("failures", 0)) + int(x.get("errors", 0)) for x in suites)
    return tests, bad


def _ignored(root):
    out = common.git(["ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"], root)
    return {p for p in out.split("\0") if p}


def run_check(root, command, stats_file=None, timeout=None, project_on_path=False, writable=False,
              mask_hidden=True, strict_tmp=False):
    """Returns (ok, report text). project_on_path: the project root is put on PYTHONPATH (held-out
    tests stored outside the project can then import it). writable: the checked code may write in
    the project (.git and virtual environments stay read-only)."""
    if timeout is None:
        try:
            timeout = int(os.environ.get("LOCAL_CHECK_TIMEOUT", "300"))
        except ValueError:
            timeout = 300
    if not common.sandbox_kind() and os.environ.get("LOCAL_ALLOW_UNSANDBOXED") != "1":
        how = ('install the "bubblewrap" package' if common.IS_LINUX else
               "use WSL2" if common.IS_WIN else "no sandbox is available on this system")
        return False, (f"CHECK IMPOSSIBLE — checks never run unconfined by default: {how}, or set "
                       "LOCAL_ALLOW_UNSANDBOXED=1 to accept running the checked code without a sandbox.")
    work = tempfile.mkdtemp(prefix="local-check-")
    try:
        tokens = shlex.split(command, posix=not common.IS_WIN)
    except ValueError:
        tokens = command.split()
    extra = [t if os.path.isdir(t) else os.path.dirname(t) for t in tokens if os.path.isabs(t) and os.path.exists(t)]
    argv, sandbox = common.sandboxed_argv(root, command, writable=writable, work=work, extra_read=extra,
                                          mask_hidden=mask_hidden, strict_tmp=strict_tmp)
    env = common.scrub_env(os.environ)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    for k in ("LOCAL_HIDDEN_CHECK_FILE", "LOCAL_STOP_LOG"):
        env.pop(k, None)
    # Caches and reports go to the scratch folder (the project may be read-only).
    for key, sub in (("PYTHONPYCACHEPREFIX", "pyc"), ("XDG_CACHE_HOME", "cache"), ("COVERAGE_FILE", "coverage/.coverage"),
                     ("MYPY_CACHE_DIR", "mypy"), ("RUFF_CACHE_DIR", "ruff"), ("GOCACHE", "go-build"),
                     ("npm_config_cache", "npm"), ("CARGO_TARGET_DIR", "cargo-target"), ("TMPDIR", "tmp")):
        path = os.path.join(work, sub)
        os.makedirs(os.path.dirname(path) if key == "COVERAGE_FILE" else path, exist_ok=True)
        env[key] = path
    env["LOCAL_TMP"] = env["TMPDIR"]          # documented name for the check's own temporary files
    first = ([root] if project_on_path else []) + common.worktree_pythonpath(root)
    if first:
        env["PYTHONPATH"] = os.pathsep.join(first + [p for p in [env.get("PYTHONPATH", "")] if p])
    ignored_before = _ignored(root) if writable else set()
    addopts = (env.get("PYTEST_ADDOPTS", "") + " -p no:cacheprovider").strip()
    junit = os.path.join(work, "pytest-report.xml")
    uses_pytest = bool(re.search(r"\bpytest\b", command)) and "--junitxml" not in command and "--junit-xml" not in command
    if uses_pytest:
        addopts += f" --junitxml={common.quote(junit) if ' ' in junit else junit}"
        # Readable failures for the agent: no warnings summary, short tracebacks (the command's own
        # options, read after these, still win).
        if "no:warnings" not in command and not re.search(r"(^|\s)-W\b|--disable-warnings", command):
            addopts += " -p no:warnings"
        if "--tb" not in command:
            addopts += " --tb=short"
    env["PYTEST_ADDOPTS"] = addopts
    start = time.time()
    code, out, _, timed_out = common.run(argv, cwd=root, env=env, timeout=timeout, merge_stderr=True)
    lines = out.strip().splitlines()
    ok = code == 0 and not timed_out
    impossible = ""
    if not ok and lines and lines[0].startswith(("bwrap:", "sandbox-exec:")):
        impossible = f"the sandbox did not start ({lines[0][:150]}). Run /local doctor; draw no conclusion about the code."
    elif code in (126, 127) and not timed_out:
        impossible = (f"the check command could not run (exit code {code}: command not found or not executable). "
                      f"Fix the --check command (use the project's interpreter, e.g. .venv/bin/python -m pytest).")
    elif not ok and READ_ONLY_MARKERS.search(out) and _denied_outside(out, root):
        impossible = (f"the check writes outside the project ({_denied_outside(out, root)}), which is refused during "
                      "checks. Write temporary files to $LOCAL_TMP (or $TMPDIR / tempfile): a scratch folder "
                      "emptied after the check. Then rerun only the checks: dispatch_task.py <id> --recheck "
                      "(same --check / --hidden-check).")
    elif not ok and not writable and READ_ONLY_MARKERS.search(out):
        impossible = ("the check tries to write into the project, which is read-only during checks. Make the tests "
                      "write to $LOCAL_TMP (a scratch folder), or rerun with --check-writable.")
    note = ""
    if ok and uses_pytest:
        evidence = _pytest_evidence(junit)
        if evidence is None:
            ok, note = False, " (pytest stopped before writing its report: interrupted run?)"
        elif evidence[0] == 0:
            ok, note = False, " (no test was run: wrong path or filter?)"
        elif evidence[1]:
            ok, note = False, f" ({evidence[1]} failing test(s) despite exit code 0)"
    elif ok and re.search(r"-m\s+unittest\b", command):
        ran = re.findall(r"^Ran (\d+) tests? in", out, re.M)
        if not ran or int(ran[-1]) == 0:
            ok, note = False, " (no test was run: wrong path or filter?)"
        elif not re.search(r"^OK\b", out, re.M):
            ok, note = False, " (unittest did not report OK)"
    elif ok and re.search(r"^Ran 0 tests\b|\bno tests ran\b|\bcollected 0 items\b", out, re.M | re.I):
        ok, note = False, " (no test was run: wrong path or filter?)"
    shutil.rmtree(work, ignore_errors=True)
    if writable:     # files git does not see, created by the checked code: shown, never silent
        new = sorted(_ignored(root) - ignored_before)
        if new:
            note += f" [the check created files ignored by git: {', '.join(new[:5])}{'…' if len(new) > 5 else ''}]"
    if impossible:
        text = f"CHECK IMPOSSIBLE — {impossible}\n" + "\n".join(lines[-15:])
        if stats_file:
            with open(stats_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "check", "ok": False, "code": code, "impossible": True}) + "\n")
        return False, text
    head = (f"CHECK {'OK' if ok else 'FAILED'}"
            f"{f' ({timeout} s limit reached)' if timed_out else ''}{note} — {command}"
            f"{'' if sandbox else ' [no sandbox]'}")
    text = head + "\n" + ("\n".join(lines[-12:]) if ok else common.check_digest(out, 40))
    if stats_file:
        with open(stats_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "check", "ok": ok, "code": code, "sandbox": sandbox,
                                "duration_s": round(time.time() - start)}) + "\n")
    return ok, text


def main():
    common.utf8_stdio()
    args = [a for a in sys.argv[1:] if a != "--writable"]
    if len(args) < 2 or "-h" in args or "--help" in args:
        print(__doc__.strip())
        sys.exit(0 if ("-h" in args or "--help" in args) else 2)
    ok, text = run_check(os.path.realpath(args[0]), args[1], os.environ.get("LOCAL_STATS_FILE"),
                         writable="--writable" in sys.argv)
    print(text)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
