#!/usr/bin/env python3
"""Build a Jupyter notebook (.ipynb, nbformat 4.5) from a small text spec.

Usage: make_notebook.py SPEC -o OUT.ipynb [--module M.py] [--texts T.md] [--run] [--python PY]
  SPEC   : text file with blocks introduced by "%% name" lines
  -o OUT : output notebook path (parent folder created if needed)
  --module M.py  : Python module for %% imports and %% getsource
  --texts T.md   : markdown file for %% include (blocks marked <!-- KEY -->)
  --run          : after building, execute with run_notebook.py
  --python PY    : interpreter for --run (default: sys.executable)
"""
import argparse
import ast
import json
import os
import re
import sys


def parse_args():
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument("spec")
    ap.add_argument("-o", required=True, dest="out")
    ap.add_argument("--module", default="")
    ap.add_argument("--texts", default="")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--python", default=sys.executable)
    return ap.parse_args()


def _strip_cell(text):
    """Strip leading and trailing blank lines, keep a trailing newline."""
    lines = text.splitlines()
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    end = len(lines)
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end]) + "\n" if start < end else ""


def _split_spec(text):
    """Yield (tag, body) for each %% block."""
    blocks = []
    current_tag = None
    current_lines = []
    for line in text.splitlines():
        m = re.match(r"^%% (.+)$", line)
        if m:
            if current_tag is not None:
                blocks.append((current_tag, "\n".join(current_lines)))
            current_tag = m.group(1)
            current_lines = []
        else:
            current_lines.append(line)
    if current_tag is not None:
        blocks.append((current_tag, "\n".join(current_lines)))
    return blocks


def _extract_imports(module_path):
    """Extract top-level statements that are not def/class/if __name__."""
    src = open(module_path, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.splitlines()

    # Find module docstring end
    doc_end = 0
    if (tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)):
        doc_end = tree.body[0].end_lineno  # 1-based

    # Find if __name__ block start
    main_start = None
    for node in tree.body:
        if (isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "__name__"):
            main_start = node.lineno
            break

    result = []
    for node in tree.body:
        # Skip module docstring
        if node is tree.body[0] and doc_end > 0 and node.end_lineno == doc_end:
            continue
        # Skip def/class
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        # Skip if __name__ block
        if main_start is not None and node.lineno >= main_start:
            continue
        # Include this statement (imports, assignments, etc.)
        start = node.lineno - 1  # 0-based
        end = node.end_lineno    # 1-based, exclusive in slice
        result.append("\n".join(lines[start:end]))

    return "\n".join(result) + "\n" if result else ""


def _getsource(module_path, names):
    """Get source of named top-level functions/classes, with decorators."""
    src = open(module_path, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.splitlines()

    name_list = [n.strip() for n in names.split(",")]
    found = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # Include decorators: get the start of the first decorator
            start = node.lineno - 1  # 0-based
            if node.decorator_list:
                start = node.decorator_list[0].lineno - 1
            end = node.end_lineno  # 1-based
            found[node.name] = "\n".join(lines[start:end])

    parts = []
    for name in name_list:
        if name not in found:
            print(f"ERROR: unknown function or class '{name}' in module", file=sys.stderr)
            sys.exit(1)
        parts.append(found[name])

    return "\n\n".join(parts) + "\n"


def _extract_texts(texts_path, key):
    """Extract block marked by <!-- KEY --> from the texts file."""
    content = open(texts_path, encoding="utf-8").read()
    marker = f"<!-- {key} -->"
    # Find the start marker
    idx = content.find(marker)
    if idx == -1:
        print(f"ERROR: unknown text key '{key}'", file=sys.stderr)
        sys.exit(1)
    # Content starts after the marker line
    start = idx + len(marker)
    rest = content[start:]
    # Find next marker
    lines = rest.splitlines()
    end = len(lines)
    for i, line in enumerate(lines):
        if re.search(r"<!-- \w+ -->", line):
            end = i
            break
    return "\n".join(lines[:end])


def _make_cell(cell_type, source, cell_id):
    cell = {
        "cell_type": cell_type,
        "metadata": {},
        "id": cell_id,
        "source": source.splitlines(keepends=True) if source else [],
    }
    if cell_type == "code":
        cell["outputs"] = []
        cell["execution_count"] = None
    return cell


def main():
    args = parse_args()

    spec_text = open(args.spec, encoding="utf-8").read()
    blocks = _split_spec(spec_text)

    cells = []
    counter = [0]

    def next_id():
        counter[0] += 1
        return f"c{counter[0]:03d}"

    for tag, body in blocks:
        if tag == "md":
            text = _strip_cell(body)
            cells.append(_make_cell("markdown", text, next_id()))
        elif tag == "code":
            text = _strip_cell(body)
            cells.append(_make_cell("code", text, next_id()))
        elif tag == "imports":
            if not args.module:
                print("ERROR: %% imports requires --module", file=sys.stderr)
                sys.exit(1)
            text = _strip_cell(_extract_imports(args.module))
            cells.append(_make_cell("code", text, next_id()))
        elif tag.startswith("getsource "):
            names = tag[len("getsource "):]
            if not args.module:
                print(f"ERROR: %% getsource requires --module", file=sys.stderr)
                sys.exit(1)
            text = _strip_cell(_getsource(args.module, names))
            cells.append(_make_cell("code", text, next_id()))
        elif tag.startswith("include "):
            key = tag[len("include "):]
            if not args.texts:
                print(f"ERROR: %% include requires --texts", file=sys.stderr)
                sys.exit(1)
            text = _strip_cell(_extract_texts(args.texts, key))
            cells.append(_make_cell("markdown", text, next_id()))
        else:
            print(f"ERROR: unknown spec command '{tag}'", file=sys.stderr)
            sys.exit(1)

    nb = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {
                "name": "python",
            },
        },
        "cells": cells,
    }

    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(nb, f, indent=1, ensure_ascii=False)
        f.write("\n")

    if args.run:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        run_nb = os.path.join(script_dir, "run_notebook.py")
        r = os.system(f"{args.python} {run_nb} {args.out}")
        sys.exit(r >> 8 if os.name != "nt" else r)


if __name__ == "__main__":
    main()
