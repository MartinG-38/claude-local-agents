#!/usr/bin/env python3
"""Simple question to a local Ollama model: instruction + optional content -> answer.
The model has no access to any file. The cheapest option for Claude, which only reads the answer.
Works with any local chat model (tools not required). macOS, Linux, Windows.

Usage: ask_local.py "instruction" --file big.log [options]
       command 2>&1 | ask_local.py "instruction" --stdin [options]
  --file F         content to analyze (repeatable)
  --stdin          read the content from standard input until the end (output of a command)
  --model M        model (default: the one in models.md)                        LOCAL_MODEL
  --think V        off | on | low | medium | high (default off; adapted to the model)   LOCAL_THINK
  --temp T         temperature, e.g. 0.2                                        LOCAL_TEMP
  --ctx N          context size (default: sized to the content, up to the model's maximum)  LOCAL_CTX
  --max-tokens N   maximum answer length                                        LOCAL_MAX_TOKENS
  --keep-alive D   keep the model loaded after the call (default 30m; 0 = unload)  LOCAL_KEEP_ALIVE
Statistics (speed, duration) are written to stderr.
Content too large for the model's context is refused (exit code 3) rather than silently truncated:
split it, or let an agent explore it (delegate_local.py --readonly).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402


def fail(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def main():
    common.utf8_stdio()
    env = os.environ.get
    ap = common.Parser(__doc__)
    ap.add_argument("prompt")
    ap.add_argument("--model", default=env("LOCAL_MODEL", ""))
    ap.add_argument("--think", default=env("LOCAL_THINK", "off"))
    ap.add_argument("--temp", default=env("LOCAL_TEMP", ""))
    ap.add_argument("--ctx", default=env("LOCAL_CTX", ""))
    ap.add_argument("--max-tokens", default=env("LOCAL_MAX_TOKENS", ""))
    ap.add_argument("--keep-alive", default=env("LOCAL_KEEP_ALIVE", "30m"))
    ap.add_argument("--file", action="append", default=[])
    ap.add_argument("--stdin", action="store_true")
    o = ap.parse_args()
    if not o.prompt.strip():
        fail('usage: cat file | ask_local.py "instruction" --stdin', 2)

    options = {}
    for raw, key, conv in ((o.temp, "temperature", float), (o.ctx, "num_ctx", int), (o.max_tokens, "num_predict", int)):
        if str(raw).strip():
            try:
                options[key] = conv(str(raw).replace(",", "."))
            except ValueError:
                fail(f"invalid value for {key}: {raw!r}", 2)
    if o.think.lower() not in ("off", "on", "low", "medium", "high", "false", "true"):
        fail("--think must be off, on, low, medium or high.", 2)

    try:
        model = common.default_model(o.model)
        p = common.check_model(model, need_tools=False)
        think, note = common.think_param(p, {"false": "off", "true": "on"}.get(o.think.lower(), o.think))
    except common.ModelError as e:
        fail(str(e))

    parts = []
    for path in o.file:
        try:
            parts.append(f"### {path}\n" + open(path, encoding="utf-8", errors="replace").read())
        except OSError:
            fail(f"cannot read {path}.", 2)
    if o.stdin:
        parts.append(common.read_stdin_all())          # waits for the end of the command, even a slow one
    elif not o.file:
        parts.append(common.read_stdin_if_any(3))      # pipe without --stdin: only if it arrives quickly
    content = "\n\n".join(p for p in parts if p.strip())
    prompt = o.prompt
    if content.strip():   # the instruction is repeated after the content, where the model reads it last
        prompt += (f"\n\n--- CONTENT ---\n{content}\n--- END OF CONTENT ---\n\n"
                   f"Reminder of the instruction: {o.prompt}\n"
                   "Answer only from the content above. If the answer is not in it, write \"not in the text\" "
                   "for that point: no guesses, no \"probably\".")
        n_lines = content.count("\n") + 1
        if n_lines < 100 and len(content) < 8000:
            print(f"(note: short content, {n_lines} lines: reading it directly costs about as much as this "
                  "question and its answer)", file=sys.stderr)
    # Ollama's default context is small: size it to the content, so that nothing is cut silently.
    est = common.local_tokens(prompt)
    need = est + int(options.get("num_predict", 2048)) + 512
    max_ctx = int(p.get("context") or 131072)
    if "num_ctx" not in options:
        # The agents' context when it is enough (no reload of a model an agent may be using), else larger.
        usual = common.agent_context(model)
        options["num_ctx"] = usual if need <= usual else next(
            (b for b in (131072, 262144) if b >= need), need)
        options["num_ctx"] = min(options["num_ctx"], max_ctx)
    if need > options["num_ctx"]:
        fail(f"content too large: ~{need} tokens needed, {options['num_ctx']} available for {model}. "
             "Split it (one file or one section at a time), or use delegate_local.py --readonly.", 3)
    try:
        res, note2 = common.chat(model, prompt, think=think, options=options, keep_alive=o.keep_alive)
    except Exception as e:  # noqa: BLE001
        fail(f"model call failed: {e}")

    answer = (res.get("message") or {}).get("content", "").strip()
    print(answer)
    read = res.get("prompt_eval_count", 0)
    if read and (read < 0.5 * est or read + 64 >= options["num_ctx"]):
        print(f"WARNING: the model read ~{read} tokens of ~{est}, in a context of {options['num_ctx']}: the "
              "content was probably truncated; do not trust this answer, split the content.", file=sys.stderr)
    common.record_loaded()
    for n in (note, note2):
        if n:
            print(f"(note: {n})", file=sys.stderr)

    ev, evd = res.get("eval_count", 0), res.get("eval_duration", 0)
    pe, ped = res.get("prompt_eval_count", 0), res.get("prompt_eval_duration", 0)
    print(f"\n[stats] model={model} think={o.think} options={options or 'default'} | "
          f"prompt {pe} tokens ({f'{pe / (ped / 1e9):.0f}' if ped else '?'} tok/s) | "
          f"answer {ev} tokens ({f'{ev / (evd / 1e9):.1f}' if evd else '?'} tok/s) | "
          f"total {res.get('total_duration', 0) / 1e9:.1f} s", file=sys.stderr)

    # Volumes for the token-savings estimate (savings.py), when inside a git repository.
    gitdir = common.git_common_dir(os.getcwd())
    if gitdir:
        try:
            os.makedirs(os.path.join(gitdir, "local-agents"), exist_ok=True)
            with open(os.path.join(gitdir, "local-agents", "questions.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "ask", "model": model, "prompt": len(o.prompt),
                                    "content": len(content), "answer": len(answer)}) + "\n")
        except OSError:
            pass
    cost = common.question_cost(len(o.prompt), len(answer))
    common.ledger_add("question", "", model, "answered", *common.question_savings(len(o.prompt), len(content), len(answer)),
                      cost_in=cost[0], cost_out=cost[1])


if __name__ == "__main__":
    main()
