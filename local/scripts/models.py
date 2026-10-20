#!/usr/bin/env python3
"""Screens the models installed in Ollama and adds the ones you choose to models.md.

Usage:
  models.py                       table of the installed local models (tool calling, context, reasoning,
                                  fits in memory, already listed); in a terminal, asks which to add
  models.py add <model> [...] [--default <model>]
                                  adds these models to ~/.claude/local-agents/models.md (one block each)
  models.py resolve "20b,gemma"   exact names of the installed models the user named (/local --force-model):
                                  exact names, or a part of exactly one installed name; commas or spaces
  models.py size                  shows the task size in force (/local size)
  models.py size <size>           sets it: tiny, small, medium, large or xlarge (or a percentage, e.g. 35%);
                                  models with their own size (learned) keep it
  models.py size <size> --force   imposes it on every model, their own sizes included (kept, not erased)
  models.py size --no-force       back to each model's own size
  models.py size <size> --no-force
                                  both: lifts the forced size and sets the size of the models without their own

Cloud models and embedding models are never added. Each added block gets a role guessed from the
model's capabilities; adjust it if you like. If models.md has no DEFAULT yet, the first added model
with tool calling becomes it (or the one given with --default).
"""
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402


def fail(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def think_label(p):
    values = p["think_values"]
    if values in ([False], []) or (values is None and not p["thinking_cap"]):
        return "no"
    levels = [v for v in values or [] if isinstance(v, str)]
    return "levels " + "/".join(levels) if levels else "yes"


def screen():
    """Rows: dict per installed local model (embeddings and cloud models left out)."""
    try:
        inst = common.installed()
    except common.ModelError as e:
        fail(f"ERROR: {e}")
    listed = {common.normalize(n) for n, _ in common.models_md_entries()}
    usable = common.machine()["usable_gb"]
    rows = []
    for name in sorted(inst):
        try:
            p = common.profile(name)
        except common.ModelError:
            continue
        if p["cloud"] or p["embedding_only"] or not p["chat"]:
            continue
        weights, need, _ = common.model_memory(name, inst)
        rows.append({"name": name, "tools": p["tools"], "context": p["context"], "think": think_label(p),
                     "weights": weights, "fits": need <= usable, "need": need, "listed": name in listed})
    return rows, usable


def role(row):
    if not row["tools"]:
        return "questions and summaries only (no tool calling: cannot work as an agent)."
    if row["context"] and row["context"] < common.AGENT_GOOD_CTX:
        return "small agent tasks (maximum context below 64k); questions and summaries."
    if not row["fits"]:
        return "agent, but too big for this machine's memory (slow): only if nothing else will do."
    return "agent: code tasks, explorations, summaries (to refine after /local calibrate)."


def block(row, default):
    size = common.guess_task_size(row["weights"], row["context"], row["tools"])
    return (f"## {row['name']}{' — DEFAULT' if default else ''}\n- Role: {role(row)}\n"
            f"- Task size: {size}\n- Observed: —\n- Settings: —\n")


def add(names, default=None):
    rows, _ = screen()
    by_name = {r["name"]: r for r in rows}
    if not os.path.exists(common.MODELS_MD):
        os.makedirs(common.STATE_DIR, exist_ok=True)
        shutil.copy(common.TEMPLATE, common.MODELS_MD)
    text = open(common.MODELS_MD, encoding="utf-8").read()
    has_default = any(d for _, d in common.models_md_entries())
    chosen, notes = [], []
    for raw in names:
        name = common.normalize(raw)
        if common.is_cloud(raw):
            notes.append(f"✘ {raw}: cloud model, never added.")
        elif name not in by_name:
            alt = common.suggest(raw, {r: {} for r in by_name})
            notes.append(f"✘ {raw}: not an installed local chat model"
                         + (f" (did you mean {', '.join(alt)}?)" if alt else " (ollama list shows the names)."))
        elif by_name[name]["listed"] or name in [r["name"] for r in chosen]:
            notes.append(f"· {name}: already in models.md.")
        else:
            chosen.append(by_name[name])
    if not chosen:
        print("\n".join(notes) or "Nothing to add.")
        return 1 if any(n.startswith("✘") for n in notes) else 0
    want_default = common.normalize(default) if default else None
    if not has_default and not want_default:
        want_default = next((r["name"] for r in chosen if r["tools"]), None)
    if want_default and has_default:
        notes.append(f"· models.md already has a DEFAULT model: {want_default} is not marked DEFAULT "
                     "(move the word by hand to change it).")
        want_default = None
    blocks = "\n".join(block(r, r["name"] == want_default) for r in chosen)
    m = re.search(r"^## Selection rules", text, re.M)
    text = (text[:m.start()] + blocks + "\n" + text[m.start():]) if m else text.rstrip("\n") + "\n\n" + blocks
    with open(common.MODELS_MD, "w", encoding="utf-8") as f:
        f.write(text)
    for r in chosen:
        notes.append(f"✔ {r['name']} added" + (" as DEFAULT" if r["name"] == want_default else "")
                     + f": {role(r)}")
    print("\n".join(notes))
    print(f"File: {common.MODELS_MD}. Next: /local calibrate --quick (models already calibrated and unchanged are skipped).")
    return 0


def write_preference(key, value):
    """Writes (value) or removes (None) the line "- <key>: <value>" of the Preferences section."""
    if not os.path.exists(common.MODELS_MD):
        os.makedirs(common.STATE_DIR, exist_ok=True)
        shutil.copy(common.TEMPLATE, common.MODELS_MD)
    text = open(common.MODELS_MD, encoding="utf-8").read()
    if not re.search(r"^## Preferences\s*$", text, re.M):
        text = text.rstrip("\n") + "\n\n## Preferences\n"
    sec = re.search(r"^## Preferences\s*$\n((?:(?!^## ).*\n?)*)", text, re.M)     # never a model's own line
    body = sec.group(1)
    pat = rf"^-\s*{re.escape(key)}\s*:.*\n?"
    if value is None:
        body = re.sub(pat, "", body, count=1, flags=re.M | re.I)
    elif re.search(pat, body, re.M | re.I):
        note = re.search(r"^-\s*" + re.escape(key) + r"\s*:.*?(\s+\(.*\))\s*$", body, re.M | re.I)   # kept
        line = f"- {key}: {value}{note.group(1) if note else ''}\n"
        body = re.sub(pat, lambda _: line, body, count=1, flags=re.M | re.I)
    else:
        body = f"- {key}: {value}\n" + body
    with open(common.MODELS_MD, "w", encoding="utf-8") as f:
        f.write(text[:sec.start(1)] + body + text[sec.end(1):])


def unforce():
    if not common.preference("Task size forced"):
        print("No forced task size: each model already uses its own size (or the general one).")
        return
    write_preference("Task size forced", None)
    print("✔ Task size no longer forced: each model uses its own size again (or the general one).")


def set_size(value, force=False):
    """Writes the general "Task size" preference in models.md (section Preferences), or the forced one."""
    parsed = common.parse_task_size(value)
    if not parsed:
        fail(f"ERROR: '{value}': tiny, small, medium, large or xlarge (or a percentage from 10% to 80%).", 2)
    value = parsed[0]
    if force:
        write_preference("Task size forced", value)
        print(f"✔ Task size forced to {value} for every model ({parsed[1]:.0%} of the context before any work), "
              "their own sizes included (kept: /local size --no-force goes back to them).")
        return
    write_preference("Task size", value)
    print(f"✔ Task size: {value} ({parsed[1]:.0%} of the context before any work). It applies from the next task.")
    own = [n for n, _ in common.models_md_entries() if common.model_setting(n, "Task size")]
    if own:
        print("  These models keep their own (learned) size: " + ", ".join(own)
              + ". To impose it on them too: /local size " + value + " --force")
    if common.preference("Task size forced"):
        print(f"  Note: a forced size ({common.preference('Task size forced')}) still applies to every model: "
              "/local size --no-force to lift it.")


def show_size():
    """Table: for each model, its own (learned) size, and the size in force for the next tasks."""
    forced = common.preference("Task size forced")
    pref = common.preference("Task size")
    general = (common.parse_task_size(pref) or (common.DEFAULT_SIZE,))[0] if pref else common.DEFAULT_SIZE
    forced_size = (common.parse_task_size(forced) or ("",))[0] if forced else ""
    print(f"General size: {general}" + ("" if pref else " (default)")
          + (f"   ·   FORCED for every model: {forced_size} (/local size --no-force to lift it)" if forced_size else ""))
    rows = []
    for name, _ in common.models_md_entries():
        own = common.model_setting(name, "Task size")
        own_size = (common.parse_task_size(own) or (own,))[0] if own else ""
        now = forced_size or own_size or general
        why = "forced" if forced_size else ("its own" if own_size else "general")
        rows.append(f"| {name} | {own_size or '—'} | {now} ({why}) |")
    if rows:
        print("\n| Model | Own size (learned, kept while forced) | In force now |\n|---|---|---|")
        print("\n".join(rows))
    print("\nSizes: tiny 20 %, small 30 %, medium 40 %, large 50 %, xlarge 65 % of the usable context before any "
          "work. /local size <size> [--force | --no-force]")


def main():
    common.utf8_stdio()
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__.strip())
        return
    if args and args[0] == "size":
        rest = [a for a in args[1:] if a not in ("--force", "--no-force")]
        if "--no-force" in args:
            unforce()
            if rest:                     # and the size for the models without their own
                set_size(" ".join(rest))
        elif not rest:
            show_size()
        else:
            set_size(" ".join(rest), force="--force" in args)
        return
    if args and args[0] == "resolve":
        try:
            print(" ".join(common.resolve_models(" ".join(args[1:]))))
        except common.ModelError as e:
            fail(f"ERROR: {e}")
        return
    if args and args[0] == "add":
        rest = args[1:]
        default = None
        if "--default" in rest:
            i = rest.index("--default")
            if i + 1 >= len(rest):
                fail("Usage: models.py add <model> [...] [--default <model>]", 2)
            default = rest[i + 1]
            del rest[i:i + 2]
            if common.normalize(default) not in [common.normalize(x) for x in rest]:
                rest.append(default)
        if not rest:
            fail("Usage: models.py add <model> [...] [--default <model>]", 2)
        sys.exit(add(rest, default))
    if args:
        fail(__doc__.strip(), 2)

    rows, usable = screen()
    if not rows:
        print("No local chat model installed in Ollama. doctor.py --suggest proposes some for this machine.")
        return
    print(f"Installed local models (memory usable by the models: ~{usable:.0f} GB):\n")
    print("| # | Model | Tool calling (agent) | Max context | Reasoning | Weights | Fits in memory | In models.md |")
    print("|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        ctx = f"{r['context'] // 1024}k" if r["context"] else "?"
        print(f"| {i} | {r['name']} | {'yes' if r['tools'] else 'no'} | {ctx} | {r['think']} | "
              f"{r['weights']:.0f} GB | {'yes' if r['fits'] else 'no (~%.0f GB needed)' % r['need']} | "
              f"{'yes' if r['listed'] else '—'} |")
    new = [r for r in rows if not r["listed"]]
    if not new:
        print("\nAll installed models are already in models.md.")
        return
    print("\nNot listed yet: " + ", ".join(r["name"] for r in new))
    if not sys.stdin.isatty():
        print("To add some: models.py add <model> [...] [--default <model>]")
        return
    answer = input("Numbers of the models to add (e.g. 1 3), Enter for none: ").strip()
    picked = []
    for tok in re.findall(r"\d+", answer):
        if 1 <= int(tok) <= len(rows):
            picked.append(rows[int(tok) - 1]["name"])
    if not picked:
        print("Nothing added.")
        return
    default = None
    if not any(d for _, d in common.models_md_entries()) and len(picked) > 1:
        d = input(f"Default model (number among {', '.join(picked)}; Enter = the first with tool calling): ").strip()
        if d.isdigit() and 1 <= int(d) <= len(rows):
            default = rows[int(d) - 1]["name"]
    sys.exit(add(picked, default))


if __name__ == "__main__":
    main()
