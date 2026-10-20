#!/usr/bin/env python3
"""Calibration of the local models, per model: speed, code tasks, and reading (answers checked
automatically: exactness, file:line citations, no invention).

Usage: calibrate.py [--quick] [--force] [model ...]
  no model : those of ~/.claude/local-agents/models.md that are installed
  --quick  : code tasks 1 and 2 only (the reading tasks always run: they are short)
  --only=5,6 : only these code tasks (ids or numbers), to try a model on them; nothing is recorded as
             a calibration. Tasks: 1 skeleton, 2 bug, 3 tests, 4 docstrings, 5 module from tests (real size,
             signatures found only in the tests), 6 continue someone else's partial work without rewriting it
  --force  : recalibrates models already calibrated (by default they are skipped while the model
             itself, its quantization included, and this calibration suite are unchanged)
Variables: LOCAL_TIMEOUT (minutes per attempt, default 15), LOCAL_RETRIES (default 1)

Works in a data folder outside your projects and outside ~/.claude (unreadable for the agents):
~/.local/share/local-agents/calibration/ (macOS, Linux) or %LOCALAPPDATA%\\local-agents\\calibration\\
(Windows). Models without tool calling get the speed and the reading of provided content only.
Writes results.md and prints a compact table. Uses no Claude tokens.
Typical duration: 15 to 40 min per model on a recent machine with enough memory (about 5 more for reading).
"""
import datetime
import json
import os
import re
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PY = common.quote(common.PY)   # "python3" does not exist on Windows: real path of Python
QUICK = "--quick" in sys.argv
FORCE = "--force" in sys.argv
SUITE = 3          # increase when the tasks or the scoring change: models are then recalibrated
ONLY = [x for a in sys.argv[1:] if a.startswith("--only=") for x in a[7:].split(",") if x]
ARGS = [a for a in sys.argv[1:] if not a.startswith("-")]
if "-h" in sys.argv or "--help" in sys.argv:
    print(__doc__.strip())
    sys.exit(0)


def d(s):
    return textwrap.dedent(s).lstrip("\n")


# ---------------------------------------------------------------------------- tasks
GEOMETRY = d('''
    import math


    def circle_area(radius):
        if radius < 0:
            raise ValueError("negative radius")
        return math.pi * radius ** 2


    def distance(a, b):
        return math.hypot(b[0] - a[0], b[1] - a[1])


    def perimeter(points):
        if len(points) < 3:
            raise ValueError("at least 3 points")
        return sum(distance(points[i], points[(i + 1) % len(points)]) for i in range(len(points)))


    def centroid(points):
        n = len(points)
        return (sum(p[0] for p in points) / n, sum(p[1] for p in points) / n)
''')

CART = d('''
    class Cart:
        def __init__(self):
            self.items = {}  # name -> [unit price, quantity]

        def add(self, name, price, quantity=1):
            if price < 0 or quantity <= 0:
                raise ValueError("invalid price or quantity")
            if name in self.items:
                self.items[name][1] += quantity
            else:
                self.items[name] = [price, quantity]

        def remove(self, name):
            if name not in self.items:
                raise KeyError(name)
            del self.items[name]

        def total(self, discount=0.0):
            if not 0 <= discount <= 1:
                raise ValueError("discount between 0 and 1")
            gross = sum(p * q for p, q in self.items.values())
            return round(gross * (1 - discount), 2)
''')

CHECK_TESTS = d('''
    """Checks that tests/test_cart.py passes and catches real bugs (mutants)."""
    import ast, os, shutil, subprocess, sys, tempfile

    def run(cwd):
        return subprocess.run([sys.executable, "-m", "unittest", "tests.test_cart", "-q"],
                              cwd=cwd, capture_output=True, text=True)

    if not os.path.exists("tests/test_cart.py"):
        sys.exit("tests/test_cart.py missing")
    tree = ast.parse(open("tests/test_cart.py").read())
    n = sum(1 for x in ast.walk(tree) if isinstance(x, ast.FunctionDef) and x.name.startswith("test"))
    if n < 5:
        sys.exit(f"only {n} tests (5 expected)")
    r = run(".")
    if r.returncode != 0:
        print(r.stderr[-1500:]); sys.exit("the tests fail on the correct code")
    src = open("cart.py").read()
    mutants = {
        "discount ignored": src.replace("gross * (1 - discount)", "gross"),
        "quantities not added up": src.replace("self.items[name][1] += quantity",
                                               "self.items[name] = [price, quantity]"),
    }
    for name, code in mutants.items():
        tmp = tempfile.mkdtemp()
        shutil.copytree("tests", os.path.join(tmp, "tests"))
        open(os.path.join(tmp, "cart.py"), "w").write(code)
        if run(tmp).returncode == 0:
            sys.exit(f"the tests do not catch the bug \\"{name}\\"")
    print(f"OK: {n} tests, mutants caught")
''')

CHECK_DOCSTRINGS = d('''
    """Checks the docstrings of geometry.py, with the code unchanged."""
    import ast, sys
    ORIGINAL = {original!r}

    def without_docstrings(tree):
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.Module)) and node.body and \\
               isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant) \\
               and isinstance(node.body[0].value.value, str):
                node.body = node.body[1:] or [ast.Pass()]
        return ast.dump(tree)

    tree = ast.parse(open("geometry.py").read())
    for f in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        doc = ast.get_docstring(f) or ""
        if len(doc) < 25:
            sys.exit(f"docstring missing or too short: {{f.name}}")
    if without_docstrings(tree) != without_docstrings(ast.parse(ORIGINAL)):
        sys.exit("the code was changed (only docstrings were requested)")
    print("OK: docstrings present, code unchanged")
''').format(original=GEOMETRY)

UNITTEST = f"{PY} -m unittest discover -s tests -q"

# Task 5: a module of real size (as in real tasks), written from a skeleton AND from tests alone (excerpt,
# report: their signature and default value appear only in the tests).
TEXTSTATS = d('''
    """Simple statistics on an English text."""
    import re


    def words(text: str) -> list:
        """Lowercase words of the text, in order: letters, with an inner apostrophe ("don't")."""
        raise NotImplementedError


    def word_counts(text: str) -> dict:
        """{word: number of occurrences}, the words as returned by words()."""
        raise NotImplementedError


    def top_words(text: str, n: int = 3) -> list:
        """The n most frequent words as (word, count): decreasing count, ties in alphabetical order."""
        raise NotImplementedError


    def sentences(text: str) -> list:
        """The sentences of the text: split at the whitespace that follows . ! or ?; stripped, empty ones dropped."""
        raise NotImplementedError


    def mean_sentence_length(text: str) -> float:
        """Average number of words per sentence, rounded to 2 decimals; 0.0 when there is no sentence."""
        raise NotImplementedError
''')
TEXTSTATS_TESTS = d('''
    import unittest
    from textstats import words, word_counts, top_words, sentences, mean_sentence_length, excerpt, report

    TEXT = "The cat sat. The cat ran away! Did the dog see the cat? Don't ask."


    class T(unittest.TestCase):
        def test_words(self):
            self.assertEqual(words("Don't PANIC, it's fine."), ["don't", "panic", "it's", "fine"])
            self.assertEqual(words("  ...  "), [])

        def test_counts(self):
            c = word_counts(TEXT)
            self.assertEqual((c["the"], c["cat"], c["don't"]), (4, 3, 1))

        def test_top(self):
            self.assertEqual(top_words(TEXT), [("the", 4), ("cat", 3), ("ask", 1)])
            self.assertEqual(top_words(TEXT, 1), [("the", 4)])

        def test_sentences(self):
            self.assertEqual(sentences(TEXT)[1], "The cat ran away!")
            self.assertEqual(sentences("Wait... what?  Yes!"), ["Wait...", "what?", "Yes!"])
            self.assertEqual(sentences("   "), [])

        def test_mean_length(self):
            self.assertEqual(mean_sentence_length(TEXT), 3.75)
            self.assertEqual(mean_sentence_length(""), 0.0)

        def test_excerpt(self):
            self.assertEqual(excerpt("One two three four five six"), "One two three four five…")
            self.assertEqual(excerpt("One two three"), "One two three")
            self.assertEqual(excerpt("a b c d", 2), "a b…")
            self.assertEqual(excerpt("a b c d", max_words=4), "a b c d")

        def test_report(self):
            r = report(TEXT)
            self.assertEqual(r, {"words": 15, "sentences": 4, "mean_sentence_length": 3.75,
                                 "top": [("the", 4), ("cat", 3), ("ask", 1)]})
            self.assertEqual(report("")["sentences"], 0)
''')

# Task 6: continue someone else's partial work (what dispatch_task asks of the next model when the
# previous one made progress): fix and complete, without rewriting the functions that already work.
MATRIX_KEPT = d('''
    def transpose(m):
        """Transpose of a matrix given as a list of rows."""
        return [list(row) for row in zip(*m)]


    def identity(n):
        """n x n identity matrix."""
        return [[1 if i == j else 0 for j in range(n)] for i in range(n)]


    def trace(m):
        """Sum of the diagonal of a square matrix; ValueError if it is not square."""
        if any(len(row) != len(m) for row in m):
            raise ValueError("not square")
        return sum(m[i][i] for i in range(len(m)))
''')
MATRIX = MATRIX_KEPT + d('''


    def matmul(a, b):
        """Product a·b of two matrices; ValueError if their sizes do not match."""
        if len(a[0]) != len(b):
            raise ValueError("sizes do not match")
        return [[sum(a[i][k] * b[k][j] for k in range(len(b))) for j in range(len(a))] for i in range(len(a))]


    def determinant(m):
        """Determinant of a square matrix (expansion along the first row); ValueError if it is not square."""
        raise NotImplementedError
''')
MATRIX_TESTS = d('''
    import unittest
    from matrix_utils import transpose, identity, trace, matmul, determinant


    class T(unittest.TestCase):
        def test_done(self):
            self.assertEqual(transpose([[1, 2, 3]]), [[1], [2], [3]])
            self.assertEqual(identity(2), [[1, 0], [0, 1]])
            self.assertEqual(trace([[1, 2], [3, 4]]), 5)

        def test_matmul(self):
            self.assertEqual(matmul([[1, 2], [3, 4]], [[5, 6], [7, 8]]), [[19, 22], [43, 50]])
            self.assertEqual(matmul([[1, 2]], [[1, 0, 2], [0, 1, 3]]), [[1, 2, 8]])
            with self.assertRaises(ValueError):
                matmul([[1, 2]], [[1, 2]])

        def test_determinant(self):
            self.assertEqual(determinant([[5]]), 5)
            self.assertEqual(determinant([[1, 2], [3, 4]]), -2)
            self.assertEqual(determinant([[2, 0, 1], [1, 3, 2], [1, 1, 2]]), 6)
            self.assertEqual(determinant(identity(4)), 1)
            with self.assertRaises(ValueError):
                determinant([[1, 2]])
''')
CHECK_KEPT = d('''
    """The functions that already worked must come out unchanged (docstrings included)."""
    import ast
    import sys

    ORIGINAL = %r


    def functions(src):
        return {n.name: ast.dump(n) for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)}


    before, now = functions(ORIGINAL), functions(open("matrix_utils.py", encoding="utf-8").read())
    changed = [name for name in before if now.get(name) != before[name]]
    if changed:
        print("Functions that already worked were rewritten:", ", ".join(changed))
        sys.exit(1)
    print("Working functions kept.")
''') % MATRIX_KEPT

TASKS = [
    {
        "id": "1-skeleton", "type": "fill in a skeleton",
        "write": "text_utils.py", "check": UNITTEST,
        "files": {
            "text_utils.py": d('''
                def slugify(text: str) -> str:
                    """Turns a text into a URL identifier.

                    - lowercase, accents removed (é -> e, ç -> c);
                    - every non-alphanumeric character becomes a hyphen;
                    - no repeated hyphens, none at the start or the end.
                    Example: "  Crème Brûlée à Paris !! " -> "creme-brulee-a-paris"
                    """
                    raise NotImplementedError
            '''),
            "tests/test_text_utils.py": d('''
                import unittest
                from text_utils import slugify

                class T(unittest.TestCase):
                    def test_example(self):
                        self.assertEqual(slugify("  Crème Brûlée à Paris !! "), "creme-brulee-a-paris")
                    def test_simple(self):
                        self.assertEqual(slugify("Hello World"), "hello-world")
                    def test_punctuation(self):
                        self.assertEqual(slugify("It's a café, déjà--2026 !"), "it-s-a-cafe-deja-2026")
                    def test_empty(self):
                        self.assertEqual(slugify("  !!  "), "")
            '''),
        },
        "prompt": "Implement the slugify function in text_utils.py according to its docstring. "
                  "The tests are in tests/test_text_utils.py (do not modify them). "
                  "Use only the standard library.",
    },
    {
        "id": "2-bug", "type": "fix a bug",
        "write": "stats.py", "check": UNITTEST,
        "files": {
            "stats.py": d('''
                def moving_average(values, k):
                    """Averages of the sliding windows of size k.

                    Example: moving_average([1, 2, 3, 4], 2) -> [1.5, 2.5, 3.5].
                    ValueError if k <= 0 or k > len(values).
                    """
                    if k <= 0 or k > len(values):
                        raise ValueError("invalid k")
                    return [sum(values[i:i + k]) / k for i in range(len(values) - k)]
            '''),
            "tests/test_stats.py": d('''
                import unittest
                from stats import moving_average

                class T(unittest.TestCase):
                    def test_example(self):
                        self.assertEqual(moving_average([1, 2, 3, 4], 2), [1.5, 2.5, 3.5])
                    def test_whole_window(self):
                        self.assertEqual(moving_average([2, 4], 2), [3.0])
                    def test_error(self):
                        with self.assertRaises(ValueError):
                            moving_average([1], 2)
            '''),
        },
        "prompt": "The tests in tests/test_stats.py fail. Find and fix the bug in stats.py, "
                  "without modifying the tests.",
    },
    {
        "id": "3-tests", "type": "write tests",
        "write": "tests/test_cart.py", "check": f"{PY} checks/check_tests.py",
        "files": {"cart.py": CART, "tests/__init__.py": "", "checks/check_tests.py": CHECK_TESTS},
        "prompt": "Write tests/test_cart.py: at least 5 unittest tests for the Cart class in cart.py "
                  "(adding, adding up quantities of the same item, removing, total with discount, errors). "
                  "Do not modify cart.py. The tests must pass and catch real bugs.",
    },
    {
        "id": "4-docstrings", "type": "document code",
        "write": "geometry.py", "check": f"{PY} checks/check_docstrings.py && " + UNITTEST,
        "files": {
            "geometry.py": GEOMETRY,
            "checks/check_docstrings.py": CHECK_DOCSTRINGS,
            "tests/test_geometry.py": d('''
                import unittest
                from geometry import circle_area, perimeter, centroid

                class T(unittest.TestCase):
                    def test_area(self):
                        self.assertAlmostEqual(circle_area(1), 3.14159, places=4)
                    def test_perimeter(self):
                        self.assertAlmostEqual(perimeter([(0, 0), (3, 0), (3, 4)]), 12)
                    def test_centroid(self):
                        self.assertEqual(centroid([(0, 0), (2, 0), (2, 2), (0, 2)]), (1, 1))
            '''),
        },
        "prompt": "Add a docstring to every function in geometry.py (what it does, its parameters, its "
                  "return value, the errors it raises). Change nothing else in the code.",
    },
    {
        "id": "5-module", "type": "module from tests",
        "write": "textstats.py", "check": UNITTEST,
        "files": {"textstats.py": TEXTSTATS, "tests/test_textstats.py": TEXTSTATS_TESTS},
        "prompt": "Implement the module textstats.py so that the tests in tests/test_textstats.py pass: fill in "
                  "the functions of the skeleton, and add the functions the tests use that are missing (read "
                  "the tests for their exact signature and behaviour). Standard library only. Do not modify "
                  "the tests.",
    },
    {
        "id": "6-continue", "type": "continue partial work",
        "write": "matrix_utils.py", "check": f"{PY} checks/check_kept.py && " + UNITTEST,
        "files": {"matrix_utils.py": MATRIX, "tests/test_matrix.py": MATRIX_TESTS, "checks/check_kept.py": CHECK_KEPT},
        "prompt": "Another agent started this task and stopped: in matrix_utils.py, transpose, identity and "
                  "trace are done and their tests pass. Continue its work: make all the tests in "
                  "tests/test_matrix.py pass (matmul still has a bug, determinant is missing). Keep what works: "
                  "change nothing in the functions that already pass, do not rewrite the file.",
    },
]

# ---------------------------------------------------------------------------- reading
# A small project with known answers. Distractor: legacy/old_client.py (no longer used, other values).
READ_FILES = {
    "config.py": d('''
        """Settings of the inventory service."""
        import os

        BASE_TIMEOUT = 3
        DEFAULT_TIMEOUT = BASE_TIMEOUT * 2 + 1
        MAX_RETRIES = int(os.environ.get("INV_RETRIES", "4"))
        LOW_STOCK = 5
        CURRENCY = "EUR"
    '''),
    "http_client.py": d('''
        """Minimal HTTP client with retries."""
        import time
        import urllib.request

        import config


        def fetch(url, timeout=None):
            """Downloads url and returns the body as text."""
            timeout = timeout or config.DEFAULT_TIMEOUT
            last_error = None
            for attempt in range(config.MAX_RETRIES + 1):
                try:
                    with urllib.request.urlopen(url, timeout=timeout) as response:
                        return response.read().decode("utf-8")
                except OSError as error:
                    last_error = error
                    time.sleep(0.5 * (attempt + 1))
            raise ConnectionError(f"{url}: {last_error}")
    '''),
    "validation.py": d('''
        """Checks on user input."""
        import re

        SKU_PATTERN = re.compile(r"^[A-Z]{3}-\\d{4}$")


        def validate_sku(sku):
            """Raises ValueError if sku is not like ABC-1234."""
            if not SKU_PATTERN.match(sku):
                raise ValueError(f"invalid SKU: {sku!r}")
            return sku


        def validate_quantity(quantity):
            if not isinstance(quantity, int) or quantity < 0:
                raise ValueError(f"invalid quantity: {quantity!r}")
            return quantity
    '''),
    "db.py": d('''
        """Storage of items in SQLite."""
        import sqlite3

        from validation import validate_quantity, validate_sku


        class Inventory:
            def __init__(self, path=":memory:"):
                self.conn = sqlite3.connect(path)
                self.conn.execute("CREATE TABLE IF NOT EXISTS items (sku TEXT PRIMARY KEY, name TEXT, qty INTEGER)")

            def save_item(self, sku, name, qty):
                validate_sku(sku)
                validate_quantity(qty)
                self.conn.execute("INSERT OR REPLACE INTO items VALUES (?, ?, ?)", (sku, name, qty))

            def load_item(self, sku):
                row = self.conn.execute("SELECT sku, name, qty FROM items WHERE sku = ?", (sku,)).fetchone()
                return None if row is None else {"sku": row[0], "name": row[1], "qty": row[2]}

            def list_items(self):
                rows = self.conn.execute("SELECT sku, name, qty FROM items")
                return [{"sku": s, "name": n, "qty": q} for s, n, q in rows]

            def record_sale(self, sku, sold):
                self.conn.execute("UPDATE items SET qty = qty - ? WHERE sku = ?", (sold, sku))

            def delete_item(self, sku):
                self.conn.execute("DELETE FROM items WHERE sku = ?", (sku,))
    '''),
    "reports.py": d('''
        """Reports on the stock."""
        import config


        def low_stock_items(inventory):
            """Items whose quantity is below the threshold."""
            return [item for item in inventory.list_items() if item["qty"] < config.LOW_STOCK]


        def stock_value(inventory, prices):
            """Total value of the stock, in config.CURRENCY."""
            return sum(item["qty"] * prices.get(item["sku"], 0) for item in inventory.list_items())
    '''),
    "legacy/old_client.py": d('''
        """Old client, kept for reference only: no longer imported anywhere."""
        import urllib.request

        TIMEOUT = 30
        RETRIES = 2


        def fetch(url):
            for _ in range(RETRIES):
                try:
                    return urllib.request.urlopen(url, timeout=TIMEOUT).read()
                except OSError:
                    pass
            return None
    '''),
}


def line_of(path, text):
    """1-based line of the first line of READ_FILES[path] containing text."""
    for i, line in enumerate(READ_FILES[path].splitlines(), 1):
        if text in line:
            return i
    raise ValueError(f"{text!r} not in {path}")


def lines_of(path, *texts, margin=1):
    out = set()
    for t in texts:
        n = line_of(path, t)
        out.update(range(n - margin, n + margin + 1))
    return {(path, n) for n in out}


NEGATION = re.compile(r"\b(no|none|not|nothing|nowhere|doesn'?t|does not|isn'?t|there is no)\b", re.I)
CITATION = re.compile(r"([\w./-]+\.py)\W{0,3}(?:lines?\s*|L)?(\d+)", re.I)

# kind: "value" (expected word/number; the distractor's value must not come first), "set" (all the
# expected names, none of the forbidden ones), "yesno" (first word), "none" (nothing to find: says so).
# cite: the acceptable (file, line) pairs, or None when no citation is asked.
READ_QUESTIONS = [
    {"id": "R1", "mode": "given", "kind": "value", "expect": "7", "distractor": "30",
     "q": "What timeout, in seconds, does fetch() use when no timeout is passed? Give the number and the "
          "file:line where the value is defined.",
     "cite": lines_of("config.py", "DEFAULT_TIMEOUT =") | lines_of("http_client.py", "timeout or config")},
    {"id": "R2", "mode": "given", "kind": "set", "expect": ["save_item", "record_sale", "delete_item"],
     "forbid": ["load_item", "list_items"],
     "q": "Which methods of Inventory insert, update or delete rows? List their names only.", "cite": None},
    {"id": "R3", "mode": "given", "kind": "yesno", "expect": "no",
     "q": "Is an item with exactly 5 units in stock returned by low_stock_items()? Start your answer with "
          "yes or no, then explain in one sentence.", "cite": None},
    {"id": "R4", "mode": "given", "kind": "none",
     "q": "Which function sends an email alert when the stock is low? Give its file:line.", "cite": None},
    {"id": "R5", "mode": "given", "kind": "value", "expect": r"\[A-Z\]\{3\}|ABC-1234|AAA-0000|(?:3|three) (?:capital|upper)[- ]?(?:case )?letters",
     "q": "Where is the format of a SKU checked, and what format is expected? Answer with file:line.",
     "cite": lines_of("validation.py", "SKU_PATTERN = ", "if not SKU_PATTERN", "def validate_sku")},
    {"id": "E1", "mode": "explore", "kind": "value", "expect": "5", "distractor": "2",
     "q": "When INV_RETRIES is not set, how many times at most does fetch() try to download a URL before "
          "giving up? Give the number and the file:line where it is decided.",
     "cite": lines_of("http_client.py", "range(config.MAX_RETRIES") | lines_of("config.py", "MAX_RETRIES =")},
    {"id": "E2", "mode": "explore", "kind": "value", "expect": "7", "distractor": "30",
     "q": "What default timeout, in seconds, does the fetch() function actually used by the service apply? "
          "Give the number and the file:line.",
     "cite": lines_of("config.py", "DEFAULT_TIMEOUT =") | lines_of("http_client.py", "timeout or config")},
    {"id": "E3", "mode": "explore", "kind": "none",
     "q": "Which function sends an email alert when the stock is low? Give its file:line.", "cite": None},
]


def score_answer(question, text):
    """(answer correct?, citation verdict: True, False or None if not asked). Pure function."""
    t = text.strip()
    kind = question["kind"]
    if kind == "value":
        v = re.sub(r"\blines?\s*\d+(?:\s*[-–]\s*\d+)?", " ", CITATION.sub(" ", t), flags=re.I)  # not answers
        m = re.search(rf"(?<![\w.-])(?:{question['expect']})(?![\w-])", v)
        bad = question.get("distractor")
        b = re.search(rf"(?<![\w.:-]){bad}(?![\w])", v) if bad else None
        ok = bool(m) and (not b or m.start() < b.start())
    elif kind == "set":
        names = set(re.findall(r"\w+", t))
        ok = all(n in names for n in question["expect"]) and not any(n in names for n in question["forbid"])
    elif kind == "yesno":
        first = re.findall(r"[A-Za-z]+", t)[:1]
        ok = bool(first) and first[0].lower() == question["expect"]
    else:   # "none": says that nothing does it, and cites no line as the answer
        ok = bool(NEGATION.search(t[:300])) and not re.search(r"def \w*(mail|alert)", t)
    cite = None
    if question["cite"]:
        found = {(os.path.basename(f) if os.path.basename(f) in ("config.py", "http_client.py", "validation.py",
                  "db.py", "reports.py") else f.lstrip("./"), int(n)) for f, n in CITATION.findall(t)}
        cite = bool(found & question["cite"])
    return ok, cite


def numbered_project():
    parts = []
    for path, content in READ_FILES.items():
        body = "\n".join(f"{i:>3}| {line}" for i, line in enumerate(content.splitlines(), 1))
        parts.append(f"### file: {path}\n{body}")
    return "\n\n".join(parts)


def run_reading(model, agent_ok, base):
    """Rows of the results table for the reading questions."""
    repo = os.path.join(base, re.sub(r"[^A-Za-z0-9._-]", "_", model), "reading")
    for rel, content in READ_FILES.items():
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    git(["init", "-q"], repo)
    git(["add", "-A"], repo)
    git(["commit", "-qm", "start"], repo)
    content = numbered_project()
    env = dict(os.environ, LOCAL_THINK="off", LOCAL_TEMP="0.2")
    rows, points, total, log = [], 0, 0, []
    for q in READ_QUESTIONS:
        if q["mode"] == "explore" and not agent_ok:
            continue
        start = datetime.datetime.now()
        if q["mode"] == "given":
            _, out, err, _ = common.run([common.PY, "-I", os.path.join(HERE, "ask_local.py"),
                                         q["q"] + " Answer in at most 4 lines; the line numbers are shown "
                                         "before each line of code.", "--stdin", "--model", model],
                                        env=env, input_text=content, timeout=900)
            kind = "reading (content given)"
        else:
            _, out, err, _ = common.run([common.PY, "-I", os.path.join(HERE, "delegate_local.py"), q["q"],
                                         "--readonly", "--model", model, "--timeout", "10", "--max-turns", "20"],
                                        cwd=repo, env=env, timeout=900)
            kind = "exploration (agent)"
        ok, cite = score_answer(q, out)
        got = int(ok) + (int(bool(cite)) if cite is not None else 0)
        points += got
        total += 1 + (1 if cite is not None else 0)
        verdict = ("✅" if ok else "❌") + (" answer" if q["kind"] != "none" else " no invention")
        if cite is not None:
            verdict += " · " + ("✅" if cite else "❌") + " file:line"
        minutes = (datetime.datetime.now() - start).total_seconds() / 60
        rows.append(f"| {q['id']} | {kind} | {verdict} | {minutes:.1f} min | |")
        log.append(f"## {q['id']}: {q['q']}\n{out.strip() or err.strip()[-500:]}\n")
    with open(os.path.join(repo, "..", "reading-answers.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(log))
    return rows, points, total


if QUICK:
    TASKS = TASKS[:2]
if ONLY:
    TASKS = [t for t in TASKS if t["id"] in ONLY or t["id"].split("-")[0] in ONLY]


# ---------------------------------------------------------------------------- helpers
def models_to_test():
    """(model, can be an agent?) for each model requested or listed in models.md."""
    try:
        inst = common.installed()
    except common.ModelError as e:
        sys.exit(f"ERROR: {e}")
    wanted = ARGS or [n for n, _ in common.models_md_entries()]
    out = []
    for m in wanted:
        if common.is_cloud(m) or common.normalize(m) not in inst:
            alt = [] if common.is_cloud(m) else common.suggest(m, inst)
            print(f"(skipped: {m} " + (f"not found, installed as {', '.join(alt)})" if alt
                                       else "is not installed or not local)"))
            continue
        try:
            p = common.profile(m)
        except common.ModelError:
            continue
        if p["embedding_only"] or not p["chat"]:
            print(f"(skipped: {m} is not a chat model)")
            continue
        out.append((m, p["tools"] and (not p["context"] or p["context"] >= common.AGENT_MIN_CTX)))
    if not out:
        sys.exit("No local model to calibrate.")
    return out


def speed_probe(model):
    env = dict(os.environ, LOCAL_MODEL=model, LOCAL_THINK="off", LOCAL_TEMP="0.2")
    _, _, err, _ = common.run([common.PY, "-I", os.path.join(HERE, "ask_local.py"),
                               "Write a Python function that merges two sorted lists into a sorted list, "
                               "with its docstring. Answer with the code only."], env=env, timeout=900)
    m = re.search(r"prompt \d+ tokens \(([\d?]+) tok/s\) \| answer \d+ tokens \(([\d.?]+) tok/s\)", err)
    return (m.group(2), m.group(1)) if m else ("?", "?")


def git(args, repo):
    common.run(["git", "-c", "user.email=calibration@local", "-c", "user.name=calibration"] + args, cwd=repo, timeout=60)


def run_task(model, task, base):
    repo = os.path.join(base, re.sub(r"[^A-Za-z0-9._-]", "_", model), task["id"])
    os.makedirs(repo, exist_ok=True)
    for rel, content in task["files"].items():
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    git(["init", "-q"], repo)
    git(["add", "-A"], repo)
    git(["commit", "-qm", "start"], repo)
    common.run([common.PY, "-I", os.path.join(HERE, "dispatch_task.py"), task["id"], "-",
                "--models", model, "--write", task["write"], "--check", task["check"],
                "--retries", os.environ.get("LOCAL_RETRIES", "1"), "--timeout", os.environ.get("LOCAL_TIMEOUT", "15")],
               cwd=repo, input_text=task["prompt"])
    stats_path = os.path.join(repo, ".git", "local-agents", f"{task['id']}.stats.jsonl")
    agents, final = [], {}
    if os.path.exists(stats_path):
        for line in open(stats_path, encoding="utf-8"):
            try:
                x = json.loads(line)
            except ValueError:
                continue
            if x.get("type") == "agent":
                agents.append(x)
            elif x.get("type") == "task":
                final = x
    ok = final.get("result") == "passed"
    fixes = max(0, final.get("attempts", len(agents)) - 1) + final.get("in_session_fixes", 0)
    verdict = ("✅ 1st attempt" if fixes == 0 else f"✅ after {fixes} fix(es)") if ok else "❌ failed"
    if str(final.get("result", "")).startswith("impossible"):
        verdict = "❌ agent error"
    if any("min limit" in a.get("status", "") for a in agents):
        verdict += " (time limit)"
    minutes = sum(a.get("duration_s", 0) for a in agents) / 60
    verdict += behaviour_notes(repo, task["id"], agents)
    return verdict, minutes, sum(a.get("tokens_out", 0) for a in agents)


def behaviour_notes(repo, task_id, agents):
    """How the model worked, beyond the verdict (the same signals real tasks learn from): many tiny edits,
    looking into the environment, going in circles, running out of steps."""
    try:
        text = open(os.path.join(repo, ".git", "local-agents", f"{task_id}.report.txt"), encoding="utf-8").read()
    except OSError:
        text = ""
    seen = [label for label, pattern in (("tiny edits", r"edited \d+ times"), ("looked into .venv", r"looked into the environment"),
                                         ("going in circles", r"read \d+ times"))
            if re.search(pattern, text)]
    if any("step limit" in a.get("status", "") for a in agents):
        seen.append("step limit")
    return (" · " + ", ".join(seen)) if seen else ""


# ---------------------------------------------------------------------------- main
def measure_overhead(model, base):
    """Tokens Claude Code's own instructions take in an agent's context, measured with a one-turn task."""
    repo = os.path.join(base, re.sub(r"[^A-Za-z0-9._-]", "_", model), "overhead")
    os.makedirs(repo, exist_ok=True)
    with open(os.path.join(repo, "README.md"), "w", encoding="utf-8") as f:
        f.write("probe\n")
    git(["init", "-q"], repo)
    git(["add", "-A"], repo)
    git(["commit", "-qm", "start"], repo)
    stats = os.path.join(repo, "stats.jsonl")
    common.run([common.PY, "-I", os.path.join(HERE, "delegate_local.py"), "Reply with the single word OK. Use no tool.",
                "--model", model, "--timeout", "5", "--max-turns", "2"],
               cwd=repo, env=dict(os.environ, LOCAL_STATS_FILE=stats), timeout=600)
    rows = [json.loads(l) for l in open(stats, encoding="utf-8")] if os.path.exists(stats) else []
    tokens = rows[-1].get("tokens_in", 0) if rows else 0
    if rows and rows[-1].get("turns") == 1 and 2000 < tokens < 60000:
        common.record_overhead(model, tokens)
        return tokens
    return None


def suggest_size(model, inst, verdicts):
    """(size, reason): first guess from the model's size and context, adjusted by the code tasks."""
    weights = ((inst.get(common.normalize(model)) or {}).get("size") or 0) / 2**30
    try:
        ctx = common.profile(model)["context"]
    except common.ModelError:
        ctx = 0
    size = common.guess_task_size(weights, ctx)
    failed = sum(v.startswith("❌") for v in verdicts)
    fixed = sum("fix" in v for v in verdicts)
    if failed:
        return common.size_step(size, -1), f"{failed} code task(s) failed"
    if verdicts and not fixed and len(verdicts) >= 4:
        bigger = common.size_step(size, +1)
        if bigger == "xlarge" and common.server_context() < 131072:
            bigger = "large"
        return bigger, "all code tasks passed at the first attempt"
    return size, f"{weights:.0f} GB model" + (f", {fixed} task(s) needed fixes" if fixed else "")


def index_path():
    return os.path.join(common.data_dir(), "calibration", "index.json")


def load_index():
    try:
        return json.load(open(index_path(), encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def main():
    common.utf8_stdio()
    os.environ["LOCAL_NO_LEDGER"] = "1"      # tests, not real savings
    os.environ["LOCAL_NO_LEARN"] = "1"       # exercises: nothing learned from them
    models = models_to_test()
    inst = common.installed()
    index = load_index()
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    base = os.path.join(common.data_dir(), "calibration", stamp)
    os.makedirs(base, exist_ok=True)
    lines = [f"# Calibration of {stamp.replace('_', ' ')} ({common.OS_NAME})", ""]
    for model, agent_ok in models:
        key = common.normalize(model)
        digest = (inst.get(key) or {}).get("digest", "")
        prev = index.get(key) or {}
        if (not FORCE and not ONLY and digest and prev.get("digest") == digest and prev.get("suite") == SUITE
                and (QUICK or prev.get("level") == "full")):
            print(f"== {model}: already calibrated on {prev.get('date', '?')} and unchanged: skipped "
                  "(--force to redo).", flush=True)
            lines += prev.get("section", []) + [f"(calibrated on {prev.get('date', '?')}; model unchanged)", ""]
            continue
        print(f"== {model}: speed measurement...", flush=True)
        gen, pre = speed_probe(model)
        section = [f"## {model}", f"Speed: generation {gen} tok/s, prompt reading {pre} tok/s", ""]
        lessons = common.lessons_of(model)
        if lessons and agent_ok:      # learned from real tasks: part of its instructions, so part of the result
            section[-1:-1] = [f"Lessons from real tasks in its instructions: {', '.join(lessons)}"]
        section += ["| Task | Type | Result | Duration | Tokens written |", "|---|---|---|---|---|"]
        verdicts = []
        if agent_ok:
            for task in TASKS:
                print(f"== {model}: task {task['id']}...", flush=True)
                verdict, minutes, out = run_task(model, task, base)
                verdicts.append(verdict)
                section.append(f"| {task['id']} | {task['type']} | {verdict} | {minutes:.1f} min | {out} |")
        print(f"== {model}: reading questions...", flush=True)
        rows, points, total = run_reading(model, agent_ok, base)
        section += rows
        section += ["", f"Reading: {points}/{total} points (answer correct, file:line correct, nothing "
                        "invented when there is nothing to find)."]
        if agent_ok:
            overhead = measure_overhead(model, base)
            cost = "Claude Code's own instructions: " + (f"{overhead:,} tokens (measured)." if overhead else "not measured.")
            if ONLY:        # a few tasks only: too little to size the model's tasks
                section.append("Partial run (--only): no task size suggested, not recorded as a calibration. " + cost)
            else:
                size, why = suggest_size(model, inst, verdicts)
                section.append(f"Suggested task size: {size} ({why}). " + cost)
        else:
            section.append("No tool calling (or context too small): code tasks and exploration not tested; "
                           "questions on provided content only.")
        section.append("")
        lines += section
        if digest and not ONLY:
            index[key] = {"digest": digest, "suite": SUITE, "level": "quick" if QUICK else "full",
                          "date": stamp.replace("_", " "), "section": section}
            with open(index_path(), "w", encoding="utf-8") as f:
                json.dump(index, f, indent=1, ensure_ascii=False)
    lines.append(f"Working folders (diffs, logs, reading answers): {base}")
    report = "\n".join(lines)
    with open(os.path.join(base, "results.md"), "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print("\n" + report)


if __name__ == "__main__":
    main()
