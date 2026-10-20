#!/usr/bin/env python3
"""Unloads the local models Ollama keeps in memory, to free it (they reload by themselves when needed).

Usage:
  unload.py              unloads every loaded model, except those a running local agent is using
  unload.py <model> ...  unloads only these models
  unload.py --force      also the models in use (their agents keep working, but wait for a reload)
  unload.py --list       what is loaded, and what is in use

Nothing is deleted: the models stay installed. Only Ollama's memory is freed.
"""
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

GB = 2 ** 30


def loaded():
    """{model: GB in memory} as Ollama reports it."""
    out = {}
    for m in common.api("/api/ps").get("models", []):
        name = m.get("name") or m.get("model") or ""
        if name:
            out[common.normalize(name)] = (m.get("size_vram") or m.get("size") or 0) / GB
    return out


def in_use():
    """{model: number of running local agents using it}."""
    count = {}
    for e in common._running():
        key = common.normalize(e.get("model", ""))
        count[key] = count.get(key, 0) + 1
    return count


def unload(name):
    """Asks Ollama to unload a model at once (keep_alive 0). True if the request was accepted."""
    try:
        common.api("/api/generate", {"model": name, "keep_alive": 0}, timeout=60)
        return True
    except Exception:  # noqa: BLE001
        pass
    exe = shutil.which("ollama")          # embedding models and older servers: the command line
    return bool(exe) and common.run([exe, "stop", name], timeout=60)[0] == 0


def main():
    common.utf8_stdio()
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__.strip())
        return
    try:
        now = loaded()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"ERROR: Ollama does not answer ({e}). Nothing to unload.")
    busy = in_use()
    if "--list" in args:
        if not now:
            print("No model loaded.")
        for name, gb in sorted(now.items()):
            print(f"{name}: {gb:.1f} GB" + (f" — in use by {busy[name]} running agent(s)" if name in busy else ""))
        return
    force = "--force" in args
    wanted = [common.normalize(a) for a in args if not a.startswith("-")]
    targets = [n for n in (wanted or sorted(now)) if n in now]
    for n in wanted:
        if n not in now:
            print(f"· {n}: not loaded.")
    if not targets:
        print("No model to unload: memory is already free." if not now else "Nothing to unload.")
        return
    done, freed = [], 0.0
    for name in targets:
        if name in busy and not force:
            print(f"· {name}: kept, {busy[name]} running agent(s) use it (--force to unload it anyway).")
            continue
        if unload(name):
            done.append(name)
            freed += now[name]
        else:
            print(f"✘ {name}: Ollama refused to unload it.")
    deadline = time.time() + 15            # Ollama unloads in the background: wait for it
    while done and time.time() < deadline:
        try:
            if not set(done) & set(loaded()):
                break
        except Exception:  # noqa: BLE001
            break
        time.sleep(0.5)
    left = set(done) & set(loaded()) if done else set()
    for name in done:
        print(f"✔ {name} unloaded ({now[name]:.1f} GB)" if name not in left else f"… {name}: still unloading")
    if done:
        print(f"About {freed:.0f} GB freed. The models stay installed and reload at the next use "
              "(a few seconds to a minute).")


if __name__ == "__main__":
    main()
