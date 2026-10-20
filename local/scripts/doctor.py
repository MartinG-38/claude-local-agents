#!/usr/bin/env python3
"""Diagnostics for the /local skill (macOS, Linux, Windows). Uses no Claude tokens.

Usage:
  doctor.py             full diagnostics (installation), with each model's capabilities
  doctor.py --quick     quick check at the start of a session: a single line if all is well
  doctor.py --capacity  machine memory and the footprint of each model (to choose models)
  doctor.py --suggest   models suggested for this machine's memory (proposals: install what you like)
"""
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import ollama_settings  # noqa: E402

QUICK = "--quick" in sys.argv
PY = "python" if common.IS_WIN else "python3"
if "-h" in sys.argv or "--help" in sys.argv:
    print(__doc__.strip())
    sys.exit(0)


def capacity_lines():
    """Compact table: usable memory, footprint of each model in models.md, possible combinations."""
    m = common.machine()
    try:
        inst = common.installed()
    except common.ModelError as e:
        return [f"ERROR: {e}"]
    common.record_loaded()
    names = [common.normalize(n) for n, _ in common.models_md_entries() if common.normalize(n) in inst] or \
        [n for n, e in inst.items() if e.get("size") and "embed" not in n.lower()]
    usable = m["usable_gb"]
    lines = [f"Memory usable by the models: ~{usable:.0f} GB ({m['kind']}; RAM {m['ram_gb']:.0f} GB).",
             f"Estimates for a {common.server_context() // 1024}k context:", "",
             "| Model | Weights | Need of one agent | Simultaneous agents of this model |", "|---|---|---|---|"]
    mem = {}
    for n in names:
        w, need, source = common.model_memory(n, inst)
        mem[n] = (w, need)
        kv = need - w
        same = int((usable - w) // kv) if kv > 0 and usable >= need else 0
        fit = "does not fit (slow or impossible)" if need > usable else ("4+" if same >= 4 else f"{max(1, same)}")
        lines.append(f"| {n} | {w:.0f} GB | {need:.0f} GB ({source}) | {fit} |")
    pairs = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if mem[a][1] + mem[b][1] <= usable:
                pairs.append(f"{a} + {b}")
    lines += ["", "Two different models at the same time: " + (", ".join(pairs) if pairs else
              "no combination fits → run the tasks one after the other (the memory guard makes them wait)."),
              "Same model in parallel: the server must accept several requests (OLLAMA_NUM_PARALLEL ≥ 2), "
              "otherwise they are processed one after the other.",
              "\"measured\" = recorded during a use; \"estimated\"/\"approximate\" become measurements with use.",
              "Rule: never pick a smaller model to parallelize if quality would suffer."]
    return lines


if "--capacity" in sys.argv:
    common.utf8_stdio()
    print("\n".join(capacity_lines()))
    sys.exit(0)
if "--suggest" in sys.argv:
    common.utf8_stdio()
    print("\n".join(common.suggestion_lines()))
    sys.exit(0)
results = []  # (level, message); OK, WARNING, ERROR


def add(level, msg):
    results.append((level, msg))


def server_env(name):
    """Environment variable of the Ollama server, depending on the system."""
    if os.environ.get(name):
        return os.environ[name]
    if common.IS_MAC and shutil.which("launchctl"):
        return common.run(["launchctl", "getenv", name], timeout=10)[1].strip()
    if common.IS_LINUX and shutil.which("systemctl"):
        out = common.run(["systemctl", "show", "ollama", "--property=Environment"], timeout=10)[1]
        m = re.search(rf"\b{name}=(\S+)", out)
        return m.group(1) if m else ""
    if common.IS_WIN:
        out = common.run(["reg", "query", r"HKCU\Environment", "/v", name], timeout=10)[1]
        m = re.search(rf"{name}\s+REG_\w+\s+(\S+)", out)
        return m.group(1) if m else ""
    return ""


def how_to_set(name, value):
    if common.IS_MAC or common.IS_WIN:
        return f"{PY} {ollama_settings.SCRIPT} (kept after a restart), then restart Ollama"
    return f"sudo systemctl edit ollama (Environment=\"{name}={value}\"), then sudo systemctl restart ollama"


# --- System and tools -------------------------------------------------------
if sys.version_info < (3, 8):
    add("ERROR", f"Python {sys.version.split()[0]}: 3.8 or newer required.")
for tool, why in (("claude", "Claude Code"), ("git", "task tracking")):
    if not shutil.which(tool):
        add("ERROR", f"{tool} not found ({why}).")
if not QUICK:
    add("OK", f"System: {common.OS_NAME}, Python {sys.version.split()[0]}.")
    gb = common.ram_gb()
    if gb:
        add("OK", f"RAM: {gb:.0f} GB.")

if shutil.which("claude"):
    _, out, err, _ = common.run(["claude", "--version"], timeout=30)
    if not QUICK and out.strip():
        add("OK", f"Claude Code {out.split()[0]}.")
    helptext = "".join(common.run(["claude", "--help"], timeout=30)[1:3])
    for flag in ("--tools", "--strict-mcp-config", "--permission-mode"):
        if helptext and flag not in helptext:
            add("ERROR", f"Claude Code too old ({flag} option missing): claude update.")
    if helptext and "--restricted" not in helptext:
        add("WARNING", "Claude Code without --restricted mode: claude update to isolate the agents better.")

# --- Sandboxes --------------------------------------------------------------
if not QUICK:
    kind = common.sandbox_kind()
    if kind:
        ok, detail = common.sandbox_selftest()          # real test on this machine
        if ok:
            add("OK", f"Sandbox for checks: {kind} — tested: {detail}.")
        else:
            add("ERROR", f"Sandbox {kind} not working: {detail}. Checks are not safe.")
    elif common.IS_LINUX:
        add("ERROR", "bubblewrap missing: checks are refused until you install the \"bubblewrap\" package "
                     "(or set LOCAL_ALLOW_UNSANDBOXED=1 to accept running them unconfined).")
    elif common.IS_WIN:
        add("WARNING", "Windows: no sandbox; checks are refused unless LOCAL_ALLOW_UNSANDBOXED=1 (WSL2 recommended). "
                       "No sandbox either for the agents' shell (Claude Code then "
                       "refuses it: agents work without a shell). To get one: WSL2.")

# --- Local only -------------------------------------------------------------
if not common.is_local_host():
    add("ERROR", f"OLLAMA_HOST_URL={common.HOST} is not local.")
try:
    conf = json.load(open(os.path.join(common.HOME, ".ollama", "server.json"), encoding="utf-8"))
except Exception:  # noqa: BLE001
    conf = {}
if (common.IS_MAC or common.IS_WIN) and not ollama_settings.applied():
    add("WARNING", "Ollama settings not applied for good (context, keep-alive, cloud block if chosen): "
                   f"{PY} {ollama_settings.SCRIPT}")
if not QUICK:     # the skill refuses the cloud anyway; blocking it on Ollama's side is optional
    if conf.get("disable_ollama_cloud") is True or server_env("OLLAMA_NO_CLOUD") == "1":
        add("OK", "Ollama's cloud also blocked on Ollama's side (the skill never uses it anyway).")
    else:
        add("OK", "The skill refuses Ollama's cloud models and remote servers. Optional: also block the "
                  "cloud on Ollama's side, for every use of Ollama: /local settings block-cloud.")

# --- Server and models ------------------------------------------------------
inst = {}
try:
    ver = common.api("/api/version").get("version", "?")
    inst = common.installed()
    v = tuple(int(x) for x in re.findall(r"\d+", ver)[:3]) or (0,)
    if v < (0, 14):
        add("ERROR", f"Ollama {ver}: too old for Claude Code (Anthropic-compatible API since 0.14): update it.")
    elif common.IS_MAC and v < (0, 40):
        add("WARNING", f"Ollama {ver}: update it (≥ 0.40 runs models on MLX by default on Apple Silicon, much faster).")
    elif not QUICK:
        add("OK", f"Ollama {ver}.")
except Exception:  # noqa: BLE001
    add("ERROR", f"Ollama does not answer on {common.HOST}: start the Ollama app or 'ollama serve'.")

if True:  # models listed in models.md
    listed = [n for n, _ in common.models_md_entries()]
    if not listed:
        add("WARNING", "No model listed in models.md yet: /local models adds installed ones (models.py), "
                       "doctor.py --suggest proposes some to install for this machine.")
    if not os.path.exists(common.MODELS_MD) and not QUICK:
        add("WARNING", f"{common.MODELS_MD} missing: it will be created from the template on first use.")
    for name in listed:
        if common.is_cloud(name):
            add("ERROR", f"models.md contains the cloud model {name}: remove it.")
            continue
        if not inst:
            continue
        if common.normalize(name) not in inst:
            alt = common.suggest(name, inst)
            add("WARNING", f"{name} listed in models.md but not found: "
                + (f"installed as {', '.join(alt)} (fix the heading in models.md)." if alt
                   else f"ollama pull {name}"))
            continue
        if QUICK:
            continue
        try:
            p = common.profile(name)
        except common.ModelError as e:
            add("WARNING", str(e))
            continue
        think = ("no" if p["think_values"] in ([False], []) or (p["think_values"] is None and not p["thinking_cap"])
                 else ("levels " + "/".join(v for v in p["think_values"] if isinstance(v, str))
                       if p["think_values"] and any(isinstance(v, str) for v in p["think_values"]) else "yes"))
        ctx = f"{p['context'] // 1024}k" if p["context"] else "?"
        if not p["chat"] or p["embedding_only"]:
            add("ERROR", f"{name}: not a chat model (embeddings?): remove it from models.md.")
        elif not p["tools"]:
            add("WARNING", f"{name}: no tool calling → simple questions only (no agent). "
                           f"Reasoning: {think}, max context {ctx}.")
        elif p["context"] and p["context"] < common.AGENT_GOOD_CTX:
            add("WARNING", f"{name}: agent possible but max context {ctx} (< 64k) → small tasks. "
                           f"Reasoning: {think}.")
        else:
            add("OK", f"{name}: agent ✓ (tools), reasoning: {think}, max context {ctx}.")
    if inst:
        try:
            add("OK", f"Default model: {common.default_model()}.")
        except common.ModelError as e:
            add("ERROR", str(e))

# --- Server context and keep-alive ------------------------------------------
ctx = server_env("OLLAMA_CONTEXT_LENGTH")
loaded = []
try:
    loaded = [int(m["context_length"]) for m in common.api("/api/ps").get("models", []) if m.get("context_length")]
except Exception:  # noqa: BLE001
    pass
best = max(loaded) if loaded else (int(ctx) if ctx.isdigit() else None)
want = int(ollama_settings.chosen()["OLLAMA_CONTEXT_LENGTH"])     # 65536 unless the user chose another value
if best is None:
    if not QUICK:
        add("WARNING", f"Ollama context cannot be checked: make sure it is {want} "
                       "(" + how_to_set("OLLAMA_CONTEXT_LENGTH", str(want)) + ").")
elif best < 32768:
    add("ERROR", f"Ollama context = {best}: too small for an agent ({how_to_set('OLLAMA_CONTEXT_LENGTH', str(want))}).")
elif best < want:
    add("WARNING", f"Ollama context = {best}, {want} chosen ({how_to_set('OLLAMA_CONTEXT_LENGTH', str(want))}).")
elif not QUICK:
    add("OK", f"Ollama context: {best}.")
if not QUICK:
    ka = server_env("OLLAMA_KEEP_ALIVE")
    add("OK" if ka else "WARNING", f"Keep-alive: {ka}." if ka else
        "Default keep-alive (5 min). Recommended during work sessions: "
        + how_to_set("OLLAMA_KEEP_ALIVE", "30m") + ".")

# --- Capacity (full diagnostics only) ---------------------------------------
if not QUICK and inst:
    cap = capacity_lines()
    add("OK", cap[0])
    for line in cap[4:]:
        if line.startswith("| ") and "does not fit" in line:
            add("WARNING", "Model too big for the usable memory: " + line.strip("| ").split(" |")[0])

# --- Compact output ---------------------------------------------------------
common.utf8_stdio()
problems = [r for r in results if r[0] != "OK"]
if QUICK and not problems:
    default = next((m for lvl, m in results if m.startswith("Default model")), "")
    print(f"doctor: all set. {default}".strip())
    try:        # what the scripts learned from real tasks: Claude sizes and words its tasks from it
        first = common.default_model()
        learned = common.learned_lines([first] + common.agent_models("auto", exclude=first))
    except Exception:  # noqa: BLE001
        learned = []
    if learned:
        print("Learned from real tasks:\n" + "\n".join(learned))
    try:        # stands for models.md at the start of a session (Claude reads the file only when needed)
        digest = common.models_md_digest()
    except Exception:  # noqa: BLE001
        digest = []
    if digest:
        print("models.md:\n" + "\n".join("  " + l for l in digest))
else:
    icons = {"OK": "✔", "WARNING": "⚠", "ERROR": "✘"}
    for level, msg in (problems if QUICK else results):
        print(f"{icons[level]} {msg}")
    if not QUICK:
        print(f"\nSummary: {sum(r[0] == 'ERROR' for r in results)} error(s), "
              f"{sum(r[0] == 'WARNING' for r in results)} warning(s).")
sys.exit(1 if any(r[0] == "ERROR" for r in results) else 0)
