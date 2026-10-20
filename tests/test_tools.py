#!/usr/bin/env python3
"""Spec tests of profile_data.py and make_notebook.py (stdlib only). Usage: python3 tests/test_tools.py [profile|notebook]"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

S = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "local", "scripts")
PY = sys.executable


def run(script, *args, cwd=None):
    r = subprocess.run([PY, os.path.join(S, script)] + list(args), cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    return r.returncode, r.stdout, r.stderr


def test_profile():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "a.csv"), "w", encoding="utf-8") as f:
        f.write("id,shop,qty,when,name\n")
        for i in range(1, 301):
            f.write(f"{i},{i % 4 + 1},{i / 10:.3f},2024-03-{i % 28 + 1:02d} 10:00:00,Zoë\n")
    with open(os.path.join(d, "b.csv"), "w", encoding="utf-8") as f:   # overlaps a.csv on id 291..300
        f.write("id,shop,qty,when,name\n")
        for i in range(291, 351):
            f.write(f"{i},2,1.5,2024-09-01 08:00:00,AnaÃ¯s\n")                    # mojibake
    with open(os.path.join(d, "c.csv"), "w", encoding="latin-1") as f:
        f.write("N° id;Magasin;Poids (kg);Date\n")
        for i in range(1, 51):
            f.write(f"{i};MAG-0{i % 4 + 1};{i},5;0{i % 9 + 1}/02/2024\n")
        f.write("7;MAG-02;7,5;08/02/2024\n")                                        # duplicate key 7
    json.dump({"coop": "X", "categories": [{"nom": "Fruits", "produits": [{"id": 1, "prix_centimes": 250}]}]},
              open(os.path.join(d, "cat.json"), "w", encoding="utf-8"))
    with open(os.path.join(d, "m.xml"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0"?><magasins><magasin id="1" region="Nord"><ville>Lille</ville></magasin>'
                '<magasin id="2" region="Sud"><ville>Nîmes</ville></magasin></magasins>')
    con = sqlite3.connect(os.path.join(d, "v.db"))
    con.execute("CREATE TABLE sales (tx INTEGER, prod INTEGER, qty_g INTEGER)")
    con.executemany("INSERT INTO sales VALUES (?,?,?)", [(i, 99 if i < 3 else i % 18 + 1, 1000 + i) for i in range(40)])
    con.commit()
    con.close()

    code, out, err = run("profile_data.py", d, "--key", "id")
    assert code == 0, err
    sections = {}
    cur = None
    for line in out.splitlines():
        if line.startswith("== "):
            cur = line[3:].split()[0]
            sections[cur] = ""
        elif cur:
            sections[cur] += line + "\n"
    assert set(sections) >= {"a.csv", "b.csv", "c.csv", "cat.json", "m.xml", "v.db"}, list(sections)
    a, b, c = sections["a.csv"], sections["b.csv"], sections["c.csv"]
    assert "encoding: utf-8" in a and "sep: ','" in a and "rows: 300" in a, a
    assert "encoding: latin-1" in c and "sep: ';'" in c and "decimal: ','" in c and "rows: 51" in c, c
    assert "qty: float" in a and "when: datetime" in a and "id: int" in a, a
    assert "min 1" in a and "max 300" in a, a                                     # numeric range of id
    assert "mojibake" in b and "mojibake" not in a, b
    assert "duplicates of N° id: 1" in c, c                                      # --key matches a column containing it
    assert "MAG-0" in c, c                                                       # sample values of a text column
    assert "keys: coop, categories" in sections["cat.json"], sections["cat.json"]
    assert "categories[]" in sections["cat.json"] and "produits[]" in sections["cat.json"]
    assert "prix_centimes" in sections["cat.json"]
    x = sections["m.xml"]
    assert "root: magasins" in x and "magasin ×2" in x and "@id" in x and "@region" in x and "ville" in x, x
    v = sections["v.db"]
    assert "table sales" in v and "rows: 40" in v and "qty_g" in v and "max 1039" in v, v
    assert "overlap a.csv / b.csv on id: 10" in out, out                        # shared keys between files
    assert len(out.splitlines()) < 150, len(out.splitlines())                   # compact
    code, out, err = run("profile_data.py", os.path.join(d, "a.csv"), "--rows", "1")
    assert code == 0 and "== a.csv" in out and "b.csv" not in out, out + err
    print("profile: ok")


MODULE = '''"""Doc of the module."""
import json
from pathlib import Path

import math

DOSSIER = Path("data")
SCHEMA = ["a", "b"]


def _helper(x):
    return x + 1


def load(dossier=DOSSIER):
    """Load."""
    return _helper(1)


class Thing:
    pass


if __name__ == "__main__":
    print(load())
'''

SPEC = '''%% md
# Title

%% imports
%% include intro
%% getsource _helper, load
%% code
x = load()
assert x == 2
%% md
## Part 2
%% getsource Thing
'''


def test_notebook():
    d = tempfile.mkdtemp()
    open(os.path.join(d, "mod.py"), "w", encoding="utf-8").write(MODULE)
    open(os.path.join(d, "spec.md"), "w", encoding="utf-8").write(SPEC)
    open(os.path.join(d, "texts.md"), "w", encoding="utf-8").write(
        "<!-- intro -->\nSome **intro** text.\n\n<!-- other -->\nOther.\n")
    out = os.path.join(d, "out", "nb.ipynb")
    code, so, err = run("make_notebook.py", "spec.md", "-o", out, "--module", "mod.py", "--texts", "texts.md", cwd=d)
    assert code == 0, so + err
    nb = json.load(open(out, encoding="utf-8"))
    assert nb["nbformat"] == 4 and nb["nbformat_minor"] >= 5 and "kernelspec" in nb["metadata"]
    cells = nb["cells"]
    src = ["".join(c["source"]) if isinstance(c["source"], list) else c["source"] for c in cells]
    kinds = [c["cell_type"] for c in cells]
    assert kinds == ["markdown", "code", "markdown", "code", "code", "markdown", "code"], kinds
    assert src[0].strip() == "# Title"
    imp = src[1]
    assert "import json" in imp and "from pathlib import Path" in imp and "import math" in imp
    assert 'DOSSIER = Path("data")' in imp and "SCHEMA" in imp
    assert "def " not in imp and "__main__" not in imp and "Doc of the module" not in imp
    assert src[2].strip() == "Some **intro** text."
    assert src[3].startswith("def _helper(x):") and "def load(dossier=DOSSIER):" in src[3] and '"""Load."""' in src[3]
    assert "class Thing" not in src[3]
    assert src[4].strip() == "x = load()\nassert x == 2"
    assert src[6].strip() == "class Thing:\n    pass"
    for c in cells:
        assert c.get("id") and isinstance(c["id"], str) and c.get("metadata") == {}
        if c["cell_type"] == "code":
            assert c["outputs"] == [] and c["execution_count"] is None
    # errors: unknown function, unknown text key
    open(os.path.join(d, "bad.md"), "w", encoding="utf-8").write("%% getsource nope\n")
    code, so, err = run("make_notebook.py", "bad.md", "-o", out, "--module", "mod.py", cwd=d)
    assert code != 0 and "nope" in so + err
    open(os.path.join(d, "bad2.md"), "w", encoding="utf-8").write("%% include missing\n")
    code, so, err = run("make_notebook.py", "bad2.md", "-o", out, "--texts", "texts.md", cwd=d)
    assert code != 0 and "missing" in so + err
    print("notebook: ok")


if __name__ == "__main__":
    which = sys.argv[1:] or ["profile", "notebook"]
    if "profile" in which:
        test_profile()
    if "notebook" in which:
        test_notebook()
