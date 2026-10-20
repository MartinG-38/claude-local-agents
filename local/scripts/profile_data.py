#!/usr/bin/env python3
"""Deterministic data profiler (no model). Compact text output lets Claude learn
what to normalize in data files without reading them.

Usage:
  profile_data.py PATH... [--key COL] [--rows N]

PATH = files or folders (folders: their data files, sorted, not recursive).
--key COL  column name fragment for duplicate / overlap checks
--rows N   number of sample rows to show for SQLite (default 3)
"""
import argparse
import collections
import csv
import io
import json
import os
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET

# ── mojibake patterns (UTF-8 bytes decoded as latin-1) ──────────────────
MOJIBAKE_RE = re.compile(r'[ÃÂ][\x80-\xbf]')

# ── helpers ──────────────────────────────────────────────────────────────

def _try_read(path):
    """Return (text, encoding) trying utf-8 then latin-1."""
    for enc in ("utf-8", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read(), enc
        except (UnicodeDecodeError, ValueError):
            continue
    with open(path, "r", encoding="latin-1") as f:
        return f.read(), "latin-1"


def _kb(path):
    return max(1, round(os.path.getsize(path) / 1024))


def _detect_sep(text):
    """Separator giving the same number (> 0) of fields on the first lines, the most fields first;
    "," last on a tie (it is also the decimal mark of "1,5")."""
    lines = [l for l in text.splitlines()[:30] if l.strip()]
    best, best_n = ",", 0
    for sep in (";", "\t", "|", ","):
        counts = {len(next(csv.reader([l], delimiter=sep))) for l in lines}
        if len(counts) == 1 and counts.pop() > max(best_n, 1):
            best, best_n = sep, len(next(csv.reader([lines[0]], delimiter=sep)))
    return best


def _has_decimal_comma(rows, sep):
    """Check if numeric-looking values use comma as decimal."""
    for row in rows[:50]:
        for val in row:
            if re.match(r'^-?\d{1,4},\d{1,6}$', val):
                return True
    return False


def _infer_type(values):
    """Return type string for a list of non-empty string values."""
    int_re = re.compile(r'^-?\d+$')
    float_re = re.compile(r'^-?\d+\.?\d*([eE][+-]?\d+)?$')
    datetime_re = re.compile(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}')
    date_re = re.compile(r'^\d{4}-\d{2}-\d{2}$')
    date2_re = re.compile(r'^\d{1,2}/\d{1,2}/\d{2,4}$')

    n = len(values)
    if n == 0:
        return "text"
    int_count = 0
    float_count = 0
    dt_count = 0
    date_count = 0
    for v in values:
        if int_re.match(v):
            int_count += 1
        elif float_re.match(v):
            float_count += 1
        elif datetime_re.match(v):
            dt_count += 1
        elif date_re.match(v) or date2_re.match(v):
            date_count += 1

    threshold = max(2, int(n * 0.6))
    if int_count >= threshold:
        return "int"
    if float_count >= threshold:
        return "float"
    if dt_count >= threshold:
        return "datetime"
    if date_count >= threshold:
        return "date"
    return "text"


def _parse_float(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _parse_int(v):
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def profile_csv(path, key_col_name, nrows_sample):
    text, enc = _try_read(path)
    lines_out = []
    lines_out.append(f"encoding: {enc}")
    sep = _detect_sep(text)
    lines_out.append(f"sep: '{sep}'")

    reader = csv.reader(io.StringIO(text), delimiter=sep)
    raw_rows = list(reader)
    if not raw_rows:
        lines_out.append("rows: 0")
        return lines_out

    header = raw_rows[0]
    data = raw_rows[1:]
    has_dec = _has_decimal_comma(data, sep)
    if has_dec:
        lines_out.append("decimal: ','")
    lines_out.append(f"rows: {len(data)}")

    # Build column data
    cols = {h: [] for h in header}
    for row in data:
        for i, h in enumerate(header):
            if i < len(row):
                cols[h].append(row[i])
            else:
                cols[h].append("")

    # Check for mojibake
    has_mojibake = False
    for vals in cols.values():
        for v in vals:
            if MOJIBAKE_RE.search(v):
                has_mojibake = True
                break
        if has_mojibake:
            break
    if has_mojibake:
        lines_out.append("WARNING: mojibake detected")

    # Per-column stats
    for h in header:
        vals = cols[h]
        non_empty = [v for v in vals if v.strip()]
        missing = len(vals) - len(non_empty)
        unique = len(set(non_empty))
        t = _infer_type([v.replace(",", ".") for v in non_empty] if has_dec else non_empty)

        parts = [f"  {h}: {t}"]
        if missing:
            parts.append(f"missing {missing}")
        parts.append(f"unique {unique}")

        if t in ("int", "float"):
            nums = []
            for v in non_empty:
                clean = v.replace(",", ".") if has_dec else v
                n = _parse_float(clean)
                if n is not None:
                    nums.append(n)
            if nums:
                mn = min(nums)
                mx = max(nums)
                if t == "int":
                    parts.append(f"min {int(mn)} max {int(mx)}")
                else:
                    parts.append(f"min {mn:g} max {mx:g}")

        if t == "text" and unique <= 5:
            samples = list(collections.OrderedDict.fromkeys(non_empty))[:3]
            parts.append(f"sample {'; '.join(samples)}")
        elif t == "text" and unique > 5:
            samples = list(collections.OrderedDict.fromkeys(non_empty))[:3]
            parts.append(f"sample {'; '.join(samples)}")

        lines_out.append(" ".join(parts))

    # Duplicate check for --key
    if key_col_name:
        for h in header:
            if key_col_name.lower() in h.lower():
                non_empty = [v for v in cols[h] if v.strip()]
                counts = collections.Counter(non_empty)
                dups = sum(c - 1 for c in counts.values() if c > 1)
                if dups:
                    lines_out.append(f"duplicates of {h}: {dups}")

    return lines_out


def profile_json(path):
    text, _ = _try_read(path)
    obj = json.loads(text)
    lines_out = []
    if isinstance(obj, dict):
        lines_out.append(f"keys: {', '.join(obj.keys())}")
        _json_skeleton(obj, lines_out, depth=0)
    elif isinstance(obj, list):
        lines_out.append(f"array: {len(obj)} items")
        if obj and isinstance(obj[0], dict):
            lines_out.append(f"  first item keys: {', '.join(obj[0].keys())}")
    return lines_out


def _json_skeleton(obj, lines, depth=0, max_depth=4):
    if depth >= max_depth:
        return
    indent = "  " * (depth + 1)
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, list):
                lines.append(f"{indent}{k}[] ({len(v)} items)")
                if v and isinstance(v[0], dict):
                    lines.append(f"{indent}  keys: {', '.join(v[0].keys())}")
                    _json_skeleton(v[0], lines, depth + 2, max_depth)
            elif isinstance(v, dict):
                lines.append(f"{indent}{k}: {{")
                _json_skeleton(v, lines, depth + 1, max_depth)
    elif isinstance(obj, list):
        pass  # handled by caller


def profile_xml(path):
    tree = ET.parse(path)
    root = tree.getroot()
    lines_out = []
    lines_out.append(f"root: {root.tag}")

    children = {}
    for child in root:
        tag = child.tag
        if tag not in children:
            children[tag] = {"count": 0, "attrs": set(), "subtags": set()}
        children[tag]["count"] += 1
        for attr in child.attrib:
            children[tag]["attrs"].add(attr)
        for sub in child:
            children[tag]["subtags"].add(sub.tag)

    for tag, info in children.items():
        parts = [f"  {tag} ×{info['count']}"]
        if info["attrs"]:
            parts.append(" ".join(f"@{a}" for a in sorted(info["attrs"])))
        if info["subtags"]:
            parts.append(" ".join(sorted(info["subtags"])))
        lines_out.append(" ".join(parts))

    return lines_out


def profile_sqlite(path, key_col_name, nrows_sample):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    lines_out = []
    for tname in tables:
        lines_out.append(f"table {tname}")
        cur.execute(f"SELECT sql FROM sqlite_master WHERE name='{tname}'")
        sql = cur.fetchone()[0] or ""
        lines_out.append(f"  CREATE: {sql}")
        cur.execute(f"SELECT COUNT(*) FROM [{tname}]")
        rowcount = cur.fetchone()[0]
        lines_out.append(f"  rows: {rowcount}")

        cur.execute(f"PRAGMA table_info([{tname}])")
        columns = cur.fetchall()
        for col in columns:
            cname = col[1]
            cur.execute(f"SELECT MIN([{cname}]), MAX([{cname}]) FROM [{tname}]")
            mn, mx = cur.fetchone()
            parts = [f"  {cname}"]
            if mn is not None:
                parts.append(f"min {mn} max {mx}")
            lines_out.append(" ".join(parts))

        if nrows_sample and rowcount > 0:
            cur.execute(f"SELECT * FROM [{tname}] LIMIT {nrows_sample}")
            rows = cur.fetchall()
            cols = [d[1] for d in columns]
            lines_out.append(f"  sample ({len(rows)} rows):")
            for row in rows:
                lines_out.append(f"    {list(zip(cols, row))}")

    conn.close()
    return lines_out


# ── main ─────────────────────────────────────────────────────────────────

def _collect_paths(paths_arg):
    result = []
    for p in paths_arg:
        if os.path.isdir(p):
            data_ext = {".csv", ".tsv", ".txt", ".json", ".xml", ".db", ".sqlite", ".sqlite3"}
            entries = sorted(os.listdir(p))
            for e in entries:
                full = os.path.join(p, e)
                if os.path.isfile(full) and os.path.splitext(e)[1].lower() in data_ext:
                    result.append(full)
        else:
            result.append(p)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip())
    ap.add_argument("paths", nargs="+", help="files or folders to profile")
    ap.add_argument("--key", default=None, help="column name fragment for duplicate/overlap checks")
    ap.add_argument("--rows", type=int, default=3, help="sample rows for SQLite")
    args = ap.parse_args()

    paths = _collect_paths(args.paths)
    all_key_data = {}  # filename -> set of key values

    for path in paths:
        name = os.path.basename(path)
        ext = os.path.splitext(name)[1].lower()
        size_kb = _kb(path)
        print(f"== {name} ({size_kb} KB)")

        if ext in (".csv", ".tsv", ".txt"):
            lines = profile_csv(path, args.key, args.rows)
            if args.key:
                # Collect key values for overlap
                text, _ = _try_read(path)
                sep = _detect_sep(text)
                reader = csv.reader(io.StringIO(text), delimiter=sep)
                header = next(reader, [])
                key_col = None
                for h in header:
                    if args.key.lower() in h.lower():
                        key_col = h
                        break
                if key_col:
                    idx = header.index(key_col)
                    vals = set()
                    for row in reader:
                        if idx < len(row):
                            vals.add(row[idx])
                    all_key_data[name] = vals
            for l in lines:
                print(l)

        elif ext == ".json":
            lines = profile_json(path)
            for l in lines:
                print(l)

        elif ext == ".xml":
            lines = profile_xml(path)
            for l in lines:
                print(l)

        elif ext in (".db", ".sqlite", ".sqlite3"):
            lines = profile_sqlite(path, args.key, args.rows)
            for l in lines:
                print(l)

    # Overlap report
    if args.key and len(all_key_data) >= 2:
        names = sorted(all_key_data.keys())
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                n1, n2 = names[i], names[j]
                common = all_key_data[n1] & all_key_data[n2]
                if common:
                    print(f"overlap {n1} / {n2} on {args.key}: {len(common)}")


if __name__ == "__main__":
    main()
