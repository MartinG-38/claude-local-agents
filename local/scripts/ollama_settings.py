#!/usr/bin/env python3
"""Ollama settings for the agents, applied for good (they survive a restart). Idempotent.

  ollama_settings.py                   applies the chosen settings (then restart Ollama)
  ollama_settings.py --show            chosen values, what is set on this system, what Ollama really uses
  ollama_settings.py --context 128k    changes the context length (32k minimum for an agent), and applies
  ollama_settings.py --keep-alive 1h   changes how long a model stays loaded (5m, 30m, 2h, -1 = forever)
  ollama_settings.py --reset           back to the defaults (64k context, 30m)
  ollama_settings.py --block-cloud     optional: also blocks Ollama's cloud on Ollama's side, for every use
                                       of Ollama (~/.ollama/server.json and OLLAMA_NO_CLOUD=1)
  ollama_settings.py --allow-cloud     lifts that block (the skill itself still never uses the cloud)
  ollama_settings.py --check           exit code 0 if the chosen settings are applied, 1 otherwise

The skill never uses Ollama's cloud whatever you choose: its scripts refuse cloud models and remote
servers. Blocking the cloud on Ollama's side is an extra, optional lock for everything else using
Ollama on this machine (the app, other tools). The chosen values are kept in the skill's data folder,
so the automatic check at the start of a session applies them instead of the defaults. How they are
applied:
  macOS    a login agent (~/Library/LaunchAgents/local.ollama.env.plist) sets the variables, then
           opens Ollama (launchctl setenv alone is lost at each restart)
  Windows  user variables (setx)
  Linux    needs sudo: prints the lines to add to the ollama service
"""
import json
import os
import plistlib
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

DEFAULTS = {"OLLAMA_CONTEXT_LENGTH": "65536", "OLLAMA_KEEP_ALIVE": "30m"}
MIN_CONTEXT = 32768
SERVER_JSON = os.path.join(common.HOME, ".ollama", "server.json")
AGENT_LABEL = "local.ollama.env"
AGENT_PLIST = os.path.join(common.HOME, "Library", "LaunchAgents", AGENT_LABEL + ".plist")
SCRIPT = os.path.abspath(__file__)


def config_path():
    return os.path.join(common.data_dir(), "ollama-settings.json")


def chosen():
    """Chosen values (defaults, overridden by the saved choices)."""
    values = dict(DEFAULTS)
    try:
        saved = json.load(open(config_path(), encoding="utf-8"))
        values.update({k: str(v) for k, v in saved.items() if k in DEFAULTS})
    except Exception:  # noqa: BLE001
        pass
    return values


def cloud_choice():
    """True if Ollama's cloud is to be blocked on Ollama's side: the saved choice, else whether it is
    already blocked (an existing block is never lifted without being asked)."""
    try:
        saved = json.load(open(config_path(), encoding="utf-8"))
        if "block_cloud" in saved:
            return bool(saved["block_cloud"])
    except Exception:  # noqa: BLE001
        pass
    return cloud_blocked_in_file()


def set_cloud(block):
    try:
        saved = json.load(open(config_path(), encoding="utf-8"))
    except Exception:  # noqa: BLE001
        saved = {}
    saved["block_cloud"] = bool(block)
    os.makedirs(common.data_dir(), exist_ok=True)
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(saved, f, indent=1)


def settings():
    """(name, value) pairs to set: the cloud block if chosen, the context, the keep-alive."""
    values = chosen()
    return ((("OLLAMA_NO_CLOUD", "1"),) if cloud_choice() else ()) + (
        ("OLLAMA_CONTEXT_LENGTH", values["OLLAMA_CONTEXT_LENGTH"]), ("OLLAMA_KEEP_ALIVE", values["OLLAMA_KEEP_ALIVE"]))


def save(values):
    try:
        values = dict(json.load(open(config_path(), encoding="utf-8")), **values)   # keeps the cloud choice
    except Exception:  # noqa: BLE001
        pass
    os.makedirs(common.data_dir(), exist_ok=True)
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(values, f, indent=1)


def parse_context(raw):
    m = re.fullmatch(r"\s*(\d+)\s*([kK])?\s*", raw or "")
    if not m:
        raise ValueError(f"'{raw}': give a number of tokens, e.g. 65536 or 64k.")
    n = int(m.group(1)) * (1024 if m.group(2) else 1)
    if n < MIN_CONTEXT:
        raise ValueError(f"{n} tokens: too small for an agent (the instructions and tools alone take a lot); "
                         f"{MIN_CONTEXT} minimum, 65536 recommended.")
    if n > 1048576:
        raise ValueError(f"{n} tokens: more than any local model supports.")
    return str(n)


def parse_keep_alive(raw):
    raw = (raw or "").strip()
    if not re.fullmatch(r"-1|\d+(s|m|h)?", raw):
        raise ValueError(f"'{raw}': e.g. 5m, 30m, 2h, or -1 (kept loaded until Ollama stops).")
    return raw


def quiet(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).returncode
    except (OSError, subprocess.SubprocessError):
        return -1


def cloud_blocked_in_file():
    try:
        return json.load(open(SERVER_JSON, encoding="utf-8")).get("disable_ollama_cloud") is True
    except Exception:  # noqa: BLE001
        return False


def block_cloud():
    data = {}
    if os.path.exists(SERVER_JSON):
        try:
            data = json.load(open(SERVER_JSON, encoding="utf-8"))
        except ValueError:
            return f"⚠ {SERVER_JSON} is not valid JSON: add \"disable_ollama_cloud\": true to it yourself."
    if data.get("disable_ollama_cloud") is True:
        return "✔ Ollama's cloud is already disabled."
    data["disable_ollama_cloud"] = True
    os.makedirs(os.path.dirname(SERVER_JSON), exist_ok=True)
    with open(SERVER_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return f"✔ Ollama's cloud disabled ({SERVER_JSON}). Restart Ollama to apply it."


def unblock_cloud():
    if not os.path.exists(SERVER_JSON):
        return "✔ Ollama's cloud is not blocked on Ollama's side (the skill still never uses it)."
    try:
        data = json.load(open(SERVER_JSON, encoding="utf-8"))
    except ValueError:
        return f"⚠ {SERVER_JSON} is not valid JSON: remove \"disable_ollama_cloud\" from it yourself."
    if "disable_ollama_cloud" not in data:
        return "✔ Ollama's cloud is not blocked on Ollama's side (the skill still never uses it)."
    data.pop("disable_ollama_cloud")
    with open(SERVER_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return (f"✔ Ollama's cloud no longer blocked on Ollama's side ({SERVER_JSON}); the skill still never uses "
            "it. Restart Ollama to apply it.")


def user_variable(name):
    """Persistent user variable (Windows registry)."""
    out = common.run(["reg", "query", r"HKCU\Environment", "/v", name], timeout=10)[1]
    m = re.search(rf"{name}\s+REG_\w+\s+(\S+)", out)
    return m.group(1) if m else ""


def agent_script():
    return "; ".join(f"launchctl setenv {k} {v}" for k, v in settings()) + "; open -a Ollama"


def installed_values():
    """What is set for good on this system: {name: value} (macOS: read from the login agent)."""
    if common.IS_MAC:
        try:
            script = plistlib.load(open(AGENT_PLIST, "rb"))["ProgramArguments"][2]
        except Exception:  # noqa: BLE001
            return {}
        return dict(re.findall(r"launchctl setenv (\w+) (\S+?);", script))
    if common.IS_WIN:
        names = ("OLLAMA_NO_CLOUD", "OLLAMA_CONTEXT_LENGTH", "OLLAMA_KEEP_ALIVE")
        return {k: user_variable(k) for k in names if user_variable(k)}
    return {}


def applied():
    """True if the chosen settings are applied for good on this system (Linux: cannot be done here)."""
    if cloud_choice() != cloud_blocked_in_file():
        return False
    if common.IS_MAC or common.IS_WIN:
        now = installed_values()
        if not cloud_choice() and "OLLAMA_NO_CLOUD" in now:
            return False
        return all(now.get(k) == v for k, v in settings())
    return True


def apply():
    """Applies the chosen settings. Returns the lines to print."""
    pairs = settings()
    lines = [block_cloud() if cloud_choice() else unblock_cloud()]
    if common.IS_MAC:
        os.makedirs(os.path.dirname(AGENT_PLIST), exist_ok=True)
        with open(AGENT_PLIST, "wb") as f:
            plistlib.dump({"Label": AGENT_LABEL, "ProgramArguments": ["/bin/sh", "-c", agent_script()],
                           "RunAtLoad": True}, f)
        for k, v in pairs:                                      # for the next start of Ollama, right now
            quiet(["launchctl", "setenv", k, v])
        if not cloud_choice():
            quiet(["launchctl", "unsetenv", "OLLAMA_NO_CLOUD"])
        domain = f"gui/{os.getuid()}"
        quiet(["launchctl", "bootout", domain, AGENT_PLIST])    # reload if already installed
        loaded = quiet(["launchctl", "bootstrap", domain, AGENT_PLIST]) == 0
        lines.append(f"✔ Login agent {AGENT_PLIST}" + ("" if loaded else " (written; active at next login)")
                     + ": at each login it sets " + ", ".join(f"{k}={v}" for k, v in pairs)
                     + ", then opens Ollama.")
        lines.append("ℹ TO DO: quit Ollama (menu bar icon > Quit) and reopen it so that it uses these values. "
                     "Once: remove Ollama from System Settings > General > Login Items (\"Open at Login\") so "
                     "that it starts after the variables. Leave the app's Context length and Cloud settings "
                     "alone; keep \"Expose Ollama to the network\" off.")
        lines.append(f"  To undo: launchctl bootout {domain} \"{AGENT_PLIST}\" && rm \"{AGENT_PLIST}\"")
    elif common.IS_WIN:
        failed = [k for k, v in pairs if quiet(["setx", k, v]) != 0]
        if not cloud_choice() and user_variable("OLLAMA_NO_CLOUD"):
            quiet(["reg", "delete", r"HKCU\Environment", "/v", "OLLAMA_NO_CLOUD", "/f"])
        lines.append("⚠ setx failed for " + ", ".join(failed) if failed else
                     "✔ User variables set: " + ", ".join(f"{k}={v}" for k, v in pairs) + ".")
        lines.append("ℹ TO DO: quit Ollama (taskbar icon > Quit) and reopen it to apply them.")
    else:
        env = "\n".join(f'Environment="{k}={v}"' for k, v in pairs)
        lines.append("ℹ Linux (Ollama as a systemd service), needs sudo: run  sudo systemctl edit ollama  "
                     f"and add (or replace):\n[Service]\n{env}\nthen: sudo systemctl restart ollama")
    return lines


def memory_note(context):
    """What the default model would need with this context, against the usable memory."""
    try:
        model = common.default_model()
        weights, need, _ = common.model_memory(model, ctx=int(context))
        usable = common.machine()["usable_gb"]
    except Exception:  # noqa: BLE001
        return ""
    verdict = "fits" if need <= usable else "does NOT fit: slow (partly on the CPU) or refused"
    return (f"With {int(context) // 1024}k, {model} needs ~{need:.0f} GB "
            f"(weights {weights:.0f} GB) of ~{usable:.0f} GB usable: {verdict}.")


def show():
    values = chosen()
    now = installed_values()
    lines = ["| Setting | Chosen | Set on this system | Default |", "|---|---|---|---|"]
    lines.append(f"| Cloud blocked on Ollama's side (optional) | {'yes' if cloud_choice() else 'no'} | "
                 f"{'yes' if cloud_blocked_in_file() else 'no'} (server.json) | no |")
    for k, v in settings():
        if k != "OLLAMA_NO_CLOUD":
            lines.append(f"| {k} | {v} | {now.get(k, '—')} | {DEFAULTS[k]} |")
    try:
        loaded = [(m.get("name"), m.get("context_length")) for m in common.api("/api/ps").get("models", [])]
    except Exception:  # noqa: BLE001
        loaded = None
    if loaded is None:
        lines.append("Ollama does not answer: start it to see the values it really uses.")
    elif loaded:
        lines.append("Loaded now: " + ", ".join(f"{n} with a {c} context" for n, c in loaded)
                     + ("" if all(str(c) == values["OLLAMA_CONTEXT_LENGTH"] for _, c in loaded)
                        else " (differs from the chosen value: restart Ollama)"))
    note = memory_note(values["OLLAMA_CONTEXT_LENGTH"])
    if note:
        lines.append(note)
    lines.append("" if applied() else "Not applied yet: run ollama_settings.py (no option).")
    return [l for l in lines if l]


def main():
    common.utf8_stdio()
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__.strip())
        return
    if "--check" in args:
        ok = applied()
        print("Ollama settings applied." if ok else "Ollama settings not applied for good.")
        sys.exit(0 if ok else 1)
    if "--show" in args:
        print("\n".join(show()))
        print("The skill itself never uses Ollama's cloud (cloud models and remote servers are refused).")
        return
    if "--block-cloud" in args or "--allow-cloud" in args:
        set_cloud("--block-cloud" in args)
    values = chosen()
    try:
        if "--reset" in args:
            values = dict(DEFAULTS)
        for opt, key, parse in (("--context", "OLLAMA_CONTEXT_LENGTH", parse_context),
                                ("--keep-alive", "OLLAMA_KEEP_ALIVE", parse_keep_alive)):
            if opt in args:
                i = args.index(opt)
                if i + 1 >= len(args):
                    raise ValueError(f"{opt} needs a value.")
                values[key] = parse(args[i + 1])
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    if values != chosen() or "--reset" in args:
        if values["OLLAMA_CONTEXT_LENGTH"] != chosen()["OLLAMA_CONTEXT_LENGTH"]:
            common.forget_agent_contexts()
        save(values)
        print("Chosen: " + ", ".join(f"{k}={v}" for k, v in values.items()) + ".")
        note = memory_note(values["OLLAMA_CONTEXT_LENGTH"])
        if note:
            print(note)
    print("\n".join(apply()))


if __name__ == "__main__":
    main()
