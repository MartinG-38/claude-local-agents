#!/usr/bin/env python3
"""Executes a Jupyter notebook in place, also inside the checks' sandbox. Uses no Claude tokens.

Run it with the PROJECT's interpreter (it needs nbformat, nbclient and ipykernel there):
  .venv/bin/python ~/.claude/skills/local/scripts/run_notebook.py outputs/report.ipynb [options]
  --out F        write the executed notebook to F instead of in place
  --cwd D        working directory of the kernel (default: the current one, i.e. the project root)
  --timeout N    seconds per cell (default 600)
The kernel talks over Unix sockets in the temp folder ("ipc" transport): the sandbox of the checks
forbids TCP, even on localhost. Prints one line per failing cell and a summary; exit 1 on error.
A notebook builder script then only BUILDS the notebook; a check runs it with this script:
  --check ".venv/bin/python build_x.py && .venv/bin/python $S/run_notebook.py outputs/x.ipynb && …"
"""
import argparse
import os
import shutil
import sys
import tempfile


def main():
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument("notebook")
    ap.add_argument("--out", default="")
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--timeout", type=int, default=600)
    o = ap.parse_args()
    try:
        import nbformat
        from jupyter_client.manager import KernelManager
        from nbclient import NotebookClient
        from nbclient.exceptions import CellExecutionError
    except ImportError as e:
        print(f"ERROR: {e.name} is missing from this interpreter ({sys.executable}); "
              "install nbformat, nbclient and ipykernel in the project's environment.", file=sys.stderr)
        sys.exit(2)
    nb = nbformat.read(o.notebook, as_version=4)
    for c in nb.cells:                       # stale results must not count as executed
        if c.cell_type == "code":
            c.execution_count, c.outputs = None, []
    if os.name == "nt":                      # no ipc transport on Windows (and no sandbox there)
        km, sockdir = None, None
    else:
        sockdir = tempfile.mkdtemp(prefix="nbk")
        km = KernelManager(kernel_name="python3", transport="ipc", ip=os.path.join(sockdir, "kernel"))
    client = NotebookClient(nb, km=km, timeout=o.timeout, kernel_name="python3",
                            resources={"metadata": {"path": o.cwd}})
    error = ""
    try:
        client.execute()
    except CellExecutionError as e:
        error = str(e).strip().splitlines()[-1][:300] if str(e).strip() else "cell error"
    finally:
        if sockdir:
            shutil.rmtree(sockdir, ignore_errors=True)
    try:
        nbformat.write(nb, o.out or o.notebook)
    except OSError as e:
        print(f"ERROR: cannot write {o.out or o.notebook} ({e.strerror}): a check needs --check-writable, "
              "or --out to a file in $LOCAL_TMP.", file=sys.stderr)
        sys.exit(1)
    code = [c for c in nb.cells if c.cell_type == "code"]
    failing = [i for i, c in enumerate(nb.cells) if c.cell_type == "code"
               and any(out.get("output_type") == "error" for out in c.get("outputs", []))]
    for i in failing:
        out = next(x for x in nb.cells[i].outputs if x.get("output_type") == "error")
        print(f"cell {i}: {out.get('ename')}: {str(out.get('evalue'))[:200]}")
    ran = sum(1 for c in code if c.get("execution_count"))
    print(f"{o.notebook}: {ran}/{len(code)} code cells executed, {len(failing)} error(s)"
          + (f" — {error}" if error and not failing else ""))
    sys.exit(1 if failing or error else 0)


if __name__ == "__main__":
    main()
