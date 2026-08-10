#!/usr/bin/env python3
"""Build the "code health" panel published alongside the Doxygen API docs.

The panel answers one question: how manageable is this codebase, in numbers that
mean the same thing on darktable and on Ansel. It is generated identically in both
repositories so the two published sites can be read side by side.

Inputs, all optional except the first — a missing tool degrades its own section to
"not available" instead of failing the build:

  doc/api/sqlite3/doxygen_sqlite3.db   Doxygen's own symbol table (GENERATE_SQLITE3),
                                       produced by a fast first Doxygen pass. Gives
                                       symbols per file and the include graph.
  lizard                               cyclomatic complexity (CCN) per function.
  cppcheck                             static analysis without needing a build.
  <clang-tidy report>.json/.txt        clang-tidy findings, when a separate job that
                                       can produce compile_commands.json has run.

Outputs:

  doc/code-health.md          a Doxygen page (picked up by INPUT, themed, searchable)
  doc/code-health.json        the same numbers, machine-readable, for cross-repo diffing

Usage:
  python3 tools/code_health.py --project darktable --source-dir src \\
      [--db doc/api/sqlite3/doxygen_sqlite3.db] [--clang-tidy-log FILE]
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict

# Directories that are vendored third-party code, excluded everywhere so the numbers
# describe the code this repository actually authors. Kept in step with the
# sonar.exclusions line in .sonarcloud.properties.
EXCLUDED_DIR_PARTS = (
    "/external/",
    "/tests/integration/",
    "/doxygen-awesome-css/",
)

SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".h", ".hpp", ".m", ".mm")


def is_excluded(path):
    p = "/" + path.replace(os.sep, "/").lstrip("/")
    return any(part in p for part in EXCLUDED_DIR_PARTS)


def run(cmd, **kw):
    """Run a command, returning (ok, stdout). Never raises on a non-zero exit."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           errors="replace", check=False, **kw)
        return r.returncode == 0, r.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


# --------------------------------------------------------------------------- symbols


def collect_symbols(db_path):
    """Symbols per file, from Doxygen's SQLite output.

    memberdef.kind is Doxygen's own vocabulary: 'function', 'variable', 'typedef',
    'macro definition', 'enumeration'. Note it is 'macro definition', not 'define'.
    """
    if not db_path or not os.path.exists(db_path):
        return None
    con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    try:
        rows = con.execute(
            """
            SELECT p.name AS path, m.kind AS kind, COUNT(*) AS n
            FROM memberdef m JOIN path p ON p.rowid = m.file_id
            GROUP BY p.name, m.kind
            """
        ).fetchall()
    except sqlite3.Error as exc:
        sys.stderr.write("code_health: symbol query failed: %s\n" % exc)
        return None
    finally:
        con.close()

    per_file = defaultdict(Counter)
    for path, kind, n in rows:
        if is_excluded(path):
            continue
        per_file[path][kind] += n

    out = []
    for path, kinds in per_file.items():
        out.append(
            {
                "file": path,
                "total": sum(kinds.values()),
                "functions": kinds.get("function", 0),
                "variables": kinds.get("variable", 0),
                "typedefs": kinds.get("typedef", 0),
                "macros": kinds.get("macro definition", 0),
                "enums": kinds.get("enumeration", 0),
            }
        )
    out.sort(key=lambda r: (-r["total"], r["file"]))
    return out


def collect_includers(db_path):
    """How many files include each header, directly.

    This is the number behind the "included by" graphs: the fan-in of a header, and
    the single clearest measure of how entangled a codebase's headers are.
    """
    if not db_path or not os.path.exists(db_path):
        return None
    con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        if "includes" not in tables:
            return None
        # Doxygen's `includes` table links a including file to an included one. Column
        # names have moved between versions, so resolve them rather than assume.
        cols = {r[1] for r in con.execute("PRAGMA table_info(includes)")}
        src = "src_id" if "src_id" in cols else ("including_id" if "including_id" in cols else None)
        dst = "dst_id" if "dst_id" in cols else ("included_id" if "included_id" in cols else None)
        if not src or not dst:
            return None
        # path.local distinguishes files belonging to this tree (1) from system headers
        # resolved outside it (0). Counting <stdlib.h>'s fan-in says nothing about how
        # entangled this codebase is, so restrict both ends to local files.
        rows = con.execute(
            "SELECT ps.name, pd.name FROM includes i "
            "JOIN path ps ON ps.rowid = i.%s JOIN path pd ON pd.rowid = i.%s "
            "WHERE ps.local = 1 AND pd.local = 1" % (src, dst)
        ).fetchall()
    except sqlite3.Error as exc:
        sys.stderr.write("code_health: include query failed: %s\n" % exc)
        return None
    finally:
        con.close()

    fan_in = Counter()
    for including, included in rows:
        if is_excluded(including) or is_excluded(included):
            continue
        fan_in[included] += 1
    out = [{"file": f, "included_by": n} for f, n in fan_in.most_common()]
    return out


# --------------------------------------------------------------------------- lizard


def collect_ccn(source_dir):
    """Per-function cyclomatic complexity, via lizard.

    The distribution matters more than the total: a codebase's maintenance cost lives
    in its tail, not its mean, so the thresholds below are reported as counts.
    """
    if not shutil.which("lizard"):
        return None
    cmd = ["lizard", "--csv", "-l", "c", "-l", "cpp"]
    for part in ("*/external/*", "*/tests/integration/*", "*/doxygen-awesome-css/*"):
        cmd += ["-x", part]
    cmd.append(source_dir)
    ok, out = run(cmd)
    if not out.strip():
        sys.stderr.write("code_health: lizard produced no output\n")
        return None

    funcs = []
    for line in out.splitlines():
        # nloc,ccn,token,param,length,location,file,name,long_name,start,end
        parts = line.split(",")
        if len(parts) < 8:
            continue
        try:
            nloc, ccn, _tok, params, length = (int(parts[i]) for i in range(5))
        except ValueError:
            continue  # header row
        path, name = parts[6], parts[7]
        if is_excluded(path):
            continue
        funcs.append(
            {"file": path, "name": name, "ccn": ccn, "nloc": nloc,
             "params": params, "length": length}
        )
    if not funcs:
        return None

    ccns = sorted(f["ccn"] for f in funcs)
    nlocs = [f["nloc"] for f in funcs]

    def pct(p):
        if not ccns:
            return 0
        idx = min(len(ccns) - 1, max(0, int(round((p / 100.0) * (len(ccns) - 1)))))
        return ccns[idx]

    worst = sorted(funcs, key=lambda f: (-f["ccn"], -f["nloc"]))[:40]
    longest = sorted(funcs, key=lambda f: -f["nloc"])[:20]
    return {
        "functions": len(funcs),
        "ccn_total": sum(ccns),
        "ccn_mean": round(sum(ccns) / float(len(ccns)), 2),
        "ccn_median": pct(50),
        "ccn_p90": pct(90),
        "ccn_p99": pct(99),
        "ccn_max": ccns[-1],
        "nloc_total": sum(nlocs),
        "nloc_mean": round(sum(nlocs) / float(len(nlocs)), 1),
        "over_15": sum(1 for c in ccns if c > 15),
        "over_25": sum(1 for c in ccns if c > 25),
        "over_50": sum(1 for c in ccns if c > 50),
        "over_100": sum(1 for c in ccns if c > 100),
        "long_over_100_lines": sum(1 for n in nlocs if n > 100),
        "long_over_300_lines": sum(1 for n in nlocs if n > 300),
        "params_over_7": sum(1 for f in funcs if f["params"] > 7),
        "worst": worst,
        "longest": longest,
    }


# --------------------------------------------------------------------------- cppcheck


def collect_cppcheck(source_dir, jobs):
    """cppcheck findings by severity and by rule id.

    cppcheck is used rather than clang-tidy for the always-on panel because it needs
    no compile_commands.json, so it runs in the docs job on both repositories under
    identical conditions. clang-tidy findings, which need a configured build tree,
    are folded in from --clang-tidy-log when a job that can produce one has run.
    """
    if not shutil.which("cppcheck"):
        return None
    cmd = [
        "cppcheck", "--quiet", "--enable=all", "--inline-suppr",
        "--suppress=missingInclude", "--suppress=missingIncludeSystem",
        "--suppress=unmatchedSuppression", "--suppress=checkersReport",
        "--template={severity}|{id}|{file}",
        "-j", str(jobs),
        "-i", os.path.join(source_dir, "external"),
        "-i", os.path.join(source_dir, "tests", "integration"),
        source_dir,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write("code_health: cppcheck failed: %s\n" % exc)
        return None

    by_sev, by_id = Counter(), Counter()
    for line in r.stderr.splitlines():          # cppcheck reports on stderr
        parts = line.split("|")
        if len(parts) < 3:
            continue
        sev, rule, path = parts[0], parts[1], parts[2]
        if is_excluded(path):
            continue
        by_sev[sev] += 1
        by_id[rule] += 1
    if not by_sev:
        return None
    return {
        "total": sum(by_sev.values()),
        "by_severity": dict(by_sev.most_common()),
        "top_rules": by_id.most_common(25),
    }


# --------------------------------------------------------------------------- clang-tidy


CLANG_TIDY_LINE = re.compile(
    r"^(?P<file>[^:\s]+):\d+:\d+:\s+(?P<sev>warning|error):"
    r"\s+.*\[(?P<check>[a-zA-Z0-9_.\-,]+)\]\s*$"
)


def collect_clang_tidy(log_path):
    """Aggregate a clang-tidy run's console log by check name.

    Deliberately parses the log rather than running clang-tidy: producing
    compile_commands.json needs a configured build tree and the project's full
    dependency set, which does not belong in the docs job.
    """
    if not log_path or not os.path.exists(log_path):
        return None
    by_check, by_sev, files = Counter(), Counter(), set()
    seen = set()
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = CLANG_TIDY_LINE.match(line.strip())
            if not m:
                continue
            path = m.group("file")
            if is_excluded(path):
                continue
            # clang-tidy repeats a finding once per translation unit that includes
            # the header it lives in; dedupe on the whole location+check.
            key = line.strip()
            if key in seen:
                continue
            checks = m.group("check").split(",")
            # clang-tidy reports the flags a GCC-oriented build passes that clang does
            # not know as findings. They are about build flags, not about the code, and
            # would otherwise dominate the tally.
            if checks[0] == "clang-diagnostic-unknown-warning-option":
                continue
            seen.add(key)
            # A finding tagged [bugprone-reserved-identifier,cert-dcl37-c,cert-dcl51-cpp]
            # is ONE finding reported under a check and its aliases. Count the primary
            # name only, or every aliased check inflates the table three-fold.
            by_check[checks[0]] += 1
            by_sev[m.group("sev")] += 1
            files.add(path)
    if not by_check:
        return None
    return {
        "total": sum(by_sev.values()),
        "files_with_findings": len(files),
        "by_severity": dict(by_sev.most_common()),
        "top_checks": by_check.most_common(25),
    }


# --------------------------------------------------------------------------- cloc


def collect_cloc(source_dir):
    if not shutil.which("cloc"):
        return None
    ok, out = run([
        "cloc", "--quiet", "--json",
        "--not-match-d", "(external|integration|doxygen-awesome-css)",
        source_dir,
    ])
    if not ok or not out.strip():
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    data.pop("header", None)
    summary = data.pop("SUM", None)
    langs = sorted(
        ({"language": k, **v} for k, v in data.items()),
        key=lambda d: -d.get("code", 0),
    )[:12]
    return {"sum": summary, "languages": langs}


# --------------------------------------------------------------------------- report


def md_table(headers, rows, aligns=None):
    if not rows:
        return "_no data_\n"
    aligns = aligns or ["---"] * len(headers)
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join(aligns) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out) + "\n"


def build_markdown(project, data):
    L = []
    A = L.append
    A("Code health {#code_health}")
    A("============")
    A("")
    A("Generated with `tools/code_health.py` during the documentation build. The same")
    A("script, with the same thresholds and the same third-party exclusions, runs in the")
    A("Ansel repository and in the frozen darktable 5.0 reference tree, so the two")
    A("published panels can be read side by side.")
    A("")
    A("Vendored code is excluded throughout (`src/external/`, the integration-test data,")
    A("the Doxygen theme), matching `sonar.exclusions` in `.sonarcloud.properties`. Every")
    A("number below therefore describes code this repository actually authors.")
    A("")

    # ---- size
    cloc = data.get("cloc")
    A("[TOC]")
    A("")
    A("Size {#ch_size}")
    A("----")
    A("")
    if cloc and cloc.get("sum"):
        s = cloc["sum"]
        A(md_table(
            ["Measure", "Value"],
            [["Files", "{:,}".format(int(s.get("nFiles", 0)))],
             ["Lines of code", "{:,}".format(int(s.get("code", 0)))],
             ["Comment lines", "{:,}".format(int(s.get("comment", 0)))],
             ["Blank lines", "{:,}".format(int(s.get("blank", 0)))],
             ["Comment ratio", "{:.1f} %".format(
                 100.0 * s.get("comment", 0) / max(1, s.get("code", 0) + s.get("comment", 0)))]],
            ["---", "--:"]))
        A("")
        A(md_table(
            ["Language", "Files", "Code", "Comment"],
            [[l["language"], "{:,}".format(l.get("nFiles", 0)),
              "{:,}".format(l.get("code", 0)), "{:,}".format(l.get("comment", 0))]
             for l in cloc["languages"]],
            ["---", "--:", "--:", "--:"]))
    else:
        A("_cloc not available._")
    A("")

    # ---- complexity
    ccn = data.get("ccn")
    A("Cyclomatic complexity {#ch_ccn}")
    A("---------------------")
    A("")
    if ccn:
        A("Per-function CCN, measured by `lizard`. The mean is the least interesting number")
        A("here: maintenance cost lives in the tail, so the counts above each threshold are")
        A("what to compare.")
        A("")
        A(md_table(
            ["Measure", "Value"],
            [["Functions", "{:,}".format(ccn["functions"])],
             ["Total CCN", "{:,}".format(ccn["ccn_total"])],
             ["Mean CCN", ccn["ccn_mean"]],
             ["Median CCN", ccn["ccn_median"]],
             ["90th percentile", ccn["ccn_p90"]],
             ["99th percentile", ccn["ccn_p99"]],
             ["Maximum CCN", ccn["ccn_max"]],
             ["Mean function length (NLOC)", ccn["nloc_mean"]]],
            ["---", "--:"]))
        A("")
        total = float(max(1, ccn["functions"]))
        A(md_table(
            ["Threshold", "Functions", "Share"],
            [["CCN > 15 (worth refactoring)", "{:,}".format(ccn["over_15"]),
              "{:.1f} %".format(100 * ccn["over_15"] / total)],
             ["CCN > 25 (hard to test)", "{:,}".format(ccn["over_25"]),
              "{:.1f} %".format(100 * ccn["over_25"] / total)],
             ["CCN > 50", "{:,}".format(ccn["over_50"]),
              "{:.1f} %".format(100 * ccn["over_50"] / total)],
             ["CCN > 100", "{:,}".format(ccn["over_100"]),
              "{:.1f} %".format(100 * ccn["over_100"] / total)],
             ["Longer than 100 lines", "{:,}".format(ccn["long_over_100_lines"]),
              "{:.1f} %".format(100 * ccn["long_over_100_lines"] / total)],
             ["Longer than 300 lines", "{:,}".format(ccn["long_over_300_lines"]),
              "{:.1f} %".format(100 * ccn["long_over_300_lines"] / total)],
             ["More than 7 parameters", "{:,}".format(ccn["params_over_7"]),
              "{:.1f} %".format(100 * ccn["params_over_7"] / total)]],
            ["---", "--:", "--:"]))
        A("")
        A("### Most complex functions {#ch_ccn_worst}")
        A("")
        A(md_table(
            ["CCN", "NLOC", "Params", "Function", "File"],
            [[f["ccn"], f["nloc"], f["params"], "`%s`" % f["name"], f["file"]]
             for f in ccn["worst"]],
            ["--:", "--:", "--:", "---", "---"]))
        A("")
        A("### Longest functions {#ch_ccn_longest}")
        A("")
        A(md_table(
            ["NLOC", "CCN", "Function", "File"],
            [[f["nloc"], f["ccn"], "`%s`" % f["name"], f["file"]]
             for f in ccn["longest"]],
            ["--:", "--:", "---", "---"]))
    else:
        A("_lizard not available._")
    A("")

    # ---- coupling
    inc = data.get("includers")
    A("Header coupling {#ch_coupling}")
    A("---------------")
    A("")
    if inc:
        A("Direct fan-in: how many files include each header. This is the number behind the")
        A('"included by" graphs on each file page, and the clearest single measure of how')
        A("entangled the headers are. A header near the top of this table cannot be changed")
        A("without rebuilding, and re-reviewing, most of the codebase.")
        A("")
        A(md_table(
            ["Included by", "Header"],
            [[r["included_by"], r["file"]] for r in inc[:40]],
            ["--:", "---"]))
    else:
        A("_include data not available (needs Doxygen's SQLite output)._")
    A("")

    # ---- symbols
    sym = data.get("symbols")
    A("Symbols per file {#ch_symbols}")
    A("----------------")
    A("")
    if sym:
        tot = sum(r["total"] for r in sym)
        A("From Doxygen's own symbol table. A file with a very large symbol count is doing")
        A("more than one job; a header with one is an interface.")
        A("")
        A(md_table(
            ["Measure", "Value"],
            [["Files with symbols", "{:,}".format(len(sym))],
             ["Symbols total", "{:,}".format(tot)],
             ["Mean per file", "{:.1f}".format(tot / float(max(1, len(sym))))],
             ["Files with > 100 symbols", "{:,}".format(sum(1 for r in sym if r["total"] > 100))],
             ["Files with > 50 symbols", "{:,}".format(sum(1 for r in sym if r["total"] > 50))]],
            ["---", "--:"]))
        A("")
        A("### Largest interfaces {#ch_symbols_top}")
        A("")
        A(md_table(
            ["Symbols", "Functions", "Variables", "Typedefs", "Macros", "Enums", "File"],
            [[r["total"], r["functions"], r["variables"], r["typedefs"],
              r["macros"], r["enums"], r["file"]] for r in sym[:60]],
            ["--:", "--:", "--:", "--:", "--:", "--:", "---"]))
        A("")
        A("The complete per-file table is in `code-health.json`, published next to this page.")
    else:
        A("_symbol data not available (needs Doxygen's SQLite output)._")
    A("")

    # ---- static analysis
    A("Static analysis {#ch_static}")
    A("---------------")
    A("")
    cpp = data.get("cppcheck")
    A("### cppcheck {#ch_cppcheck}")
    A("")
    if cpp:
        A(md_table(["Severity", "Findings"],
                   [[k, "{:,}".format(v)] for k, v in cpp["by_severity"].items()],
                   ["---", "--:"]))
        A("")
        A(md_table(["Findings", "Rule"],
                   [["{:,}".format(n), "`%s`" % r] for r, n in cpp["top_rules"]],
                   ["--:", "---"]))
    else:
        A("_cppcheck not available._")
    A("")
    ct = data.get("clang_tidy")
    A("### clang-tidy {#ch_clang_tidy}")
    A("")
    if ct:
        A(md_table(["Measure", "Value"],
                   [["Findings", "{:,}".format(ct["total"])],
                    ["Files with findings", "{:,}".format(ct["files_with_findings"])]],
                   ["---", "--:"]))
        A("")
        A(md_table(["Findings", "Check"],
                   [["{:,}".format(n), "`%s`" % c] for c, n in ct["top_checks"]],
                   ["--:", "---"]))
    else:
        A("_No clang-tidy report was supplied to this build. clang-tidy needs a configured")
        A("build tree (`compile_commands.json`), which the documentation job does not")
        A("produce; the separate code-health workflow supplies it when it has run._")
    A("")
    A("Elsewhere {#ch_elsewhere}")
    A("---------")
    A("")
    A("SonarCloud carries the findings this panel does not: rule-level issues, duplication,")
    A("cognitive complexity and technical debt, with the same third-party exclusions.")
    A("")
    A("- darktable 5.0: <https://sonarcloud.io/project/overview?id=aurelienpierreeng_darktable-5>")
    A("- Ansel: <https://sonarcloud.io/project/overview?id=aurelienpierreeng_ansel>")
    A("")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default="project")
    ap.add_argument("--source-dir", default="src")
    ap.add_argument("--db", default="doc/api/sqlite3/doxygen_sqlite3.db")
    ap.add_argument("--clang-tidy-log", default=None)
    ap.add_argument("--out-md", default="doc/code-health.md")
    ap.add_argument("--out-json", default="doc/code-health.json")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2)))
    ap.add_argument("--skip-cppcheck", action="store_true")
    args = ap.parse_args()

    data = {"project": args.project}

    def step(name, fn):
        sys.stderr.write("code_health: %s ... " % name)
        sys.stderr.flush()
        try:
            v = fn()
        except Exception as exc:                      # never fail the docs build
            sys.stderr.write("failed (%s)\n" % exc)
            return None
        sys.stderr.write("ok\n" if v else "unavailable\n")
        return v

    data["cloc"] = step("cloc", lambda: collect_cloc(args.source_dir))
    data["ccn"] = step("lizard", lambda: collect_ccn(args.source_dir))
    data["symbols"] = step("symbols", lambda: collect_symbols(args.db))
    data["includers"] = step("includes", lambda: collect_includers(args.db))
    if not args.skip_cppcheck:
        data["cppcheck"] = step("cppcheck",
                                lambda: collect_cppcheck(args.source_dir, args.jobs))
    data["clang_tidy"] = step("clang-tidy", lambda: collect_clang_tidy(args.clang_tidy_log))

    for path, payload in ((args.out_json, json.dumps(data, indent=1, sort_keys=True)),
                          (args.out_md, build_markdown(args.project, data))):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(payload)
        sys.stderr.write("code_health: wrote %s\n" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
