#!/usr/bin/env python3
"""Installs or updates the /local skill for Claude Code (macOS, Linux, Windows). Python 3.8+.

  python3 install.py                 installs or updates the skill
  python3 install.py --ollama-settings
                                     also: Ollama settings for the agents that survive a restart
                                     (64k context, models kept 30 min): macOS a login agent,
                                     Windows user variables, Linux instructions (the skill also
                                     does it by itself at the first session)
  python3 install.py --block-cloud   optional: also blocks Ollama's cloud on Ollama's side, for every
                                     use of Ollama (~/.ollama/server.json, plus OLLAMA_NO_CLOUD=1 with
                                     --ollama-settings). The skill never uses the cloud either way.
  python3 install.py --claude-md     also: asks Claude, in ~/.claude/CLAUDE.md, to think of /local
  python3 install.py --uninstall     removes the skill (backed up; models.md is kept) and the
                                     --claude-md instruction; prints how to remove the Ollama settings

On Windows, replace python3 with python (or py).
Never modifies ~/.claude/local-agents/models.md if it exists (your models and lessons learned).
The previous version of the skill is backed up outside the skills folder, never deleted.
"""
import datetime
import os
import shutil
import sys

if sys.version_info < (3, 8):
    sys.exit("Python 3.8 or newer is required.")

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "local")
sys.path.insert(0, os.path.join(SRC, "scripts"))
import common  # noqa: E402
import ollama_settings  # noqa: E402

DEST = os.path.join(common.HOME, ".claude", "skills", "local")
CLAUDE_MD = os.path.join(common.HOME, ".claude", "CLAUDE.md")
MARK = "<!-- skill /local -->"
CLAUDE_MD_BLOCK = f"""
{MARK}
To save my tokens, use the /local skill whenever a task requires reading a lot (code exploration,
large files, logs, test output) or writing code in volume (the thresholds are in the skill); smaller
tasks are cheaper done directly.
"""


def backup_existing():
    if not os.path.exists(DEST):
        return ""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(common.data_dir(), "backups", f"local-{stamp}")
    target, n = base, 1
    while os.path.exists(target):
        n += 1
        target = f"{base}-{n}"
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.move(DEST, target)
    return target


def strip_claude_md(text):
    """Text without the block this installer added: the marker line and the lines after it up to the
    first blank line (an older wording of the block goes too), plus the blank line before it."""
    if MARK not in text:
        return text
    lines = text.split("\n")
    start = next(i for i, line in enumerate(lines) if line.strip() == MARK)
    end = start + 1
    while end < len(lines) and lines[end].strip():
        end += 1
    if start > 0 and not lines[start - 1].strip():
        start -= 1
    return "\n".join(lines[:start] + lines[end:])


def add_claude_md():
    text = open(CLAUDE_MD, encoding="utf-8").read() if os.path.exists(CLAUDE_MD) else ""
    new = strip_claude_md(text).rstrip("\n") + ("\n" if text.strip() else "") + CLAUDE_MD_BLOCK
    if new == text:
        return "✔ CLAUDE.md already mentions /local."
    os.makedirs(os.path.dirname(CLAUDE_MD), exist_ok=True)
    with open(CLAUDE_MD, "w", encoding="utf-8") as f:
        f.write(new)
    return f"✔ Instruction {'updated in' if MARK in text else 'added to'} {CLAUDE_MD}."


def remove_claude_md():
    if not os.path.exists(CLAUDE_MD):
        return ""
    text = open(CLAUDE_MD, encoding="utf-8").read()
    if MARK not in text:
        return ""
    with open(CLAUDE_MD, "w", encoding="utf-8") as f:
        f.write(strip_claude_md(text))
    return f"✔ /local instruction removed from {CLAUDE_MD}."


def main():
    common.utf8_stdio()
    args = sys.argv[1:]
    helps = ("-h", "--help")
    unknown = [a for a in args if a not in ("--block-cloud", "--ollama-settings", "--claude-md", "--uninstall") + helps]
    if unknown or any(a in helps for a in args):
        if unknown:
            print(f"Unknown option(s): {' '.join(unknown)}\n")
        print(__doc__.strip())
        sys.exit(2 if unknown else 0)

    if "--uninstall" in args:
        saved = backup_existing()
        print(f"Skill removed (backed up to {saved})." if saved else "The skill was not installed.")
        removed = remove_claude_md()
        if removed:
            print(removed)
        print(f"Kept: {common.MODELS_MD} and your logs in the repositories (.git/local-agents).")
        print("The Ollama settings stay (see the README, \"Uninstall\", to remove them).")
        return

    saved = backup_existing()
    shutil.copytree(SRC, DEST, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print(f"✔ Skill {'updated' if saved else 'installed'}: {DEST}")
    if saved:
        print(f"  (previous version backed up to {saved})")
    if not os.path.exists(common.MODELS_MD):
        os.makedirs(common.STATE_DIR, exist_ok=True)
        shutil.copy(common.TEMPLATE.replace(common.SKILL_DIR, DEST), common.MODELS_MD)
        print(f"✔ Model list created: {common.MODELS_MD} (adapt it to your models)")
    else:
        print(f"✔ Model list kept: {common.MODELS_MD}")
    if "--block-cloud" in args:
        ollama_settings.set_cloud(True)
    if "--ollama-settings" in args:
        print("\n".join(ollama_settings.apply()))
    elif "--block-cloud" in args:
        print(ollama_settings.block_cloud())
    if "--claude-md" in args:
        print(add_claude_md())

    for name in ("claude", "git", "ollama"):
        if not shutil.which(name):
            print(f"⚠ \"{name}\" not found in the PATH.")
    if common.IS_LINUX and not shutil.which("bwrap"):
        print("⚠ bubblewrap missing: install the \"bubblewrap\" package (sandbox for the checks).")
    if common.IS_WIN:
        print("ℹ Windows: checks run without a sandbox and agents have no shell (use WSL2 to get one).")
    if "--ollama-settings" not in args and not ollama_settings.applied():
        print("ℹ To set Ollama for the agents (64k context, kept loaded), once: "
              "python3 install.py --ollama-settings")
    print("\nThen: ollama pull <model> (proposals for this machine: python3 local/scripts/doctor.py --suggest), "
          "and in Claude Code: /local models (adds your installed models to models.md), /local doctor, "
          "/local calibrate --quick.")


if __name__ == "__main__":
    main()
