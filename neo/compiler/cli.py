"""neoc: compile ``.neo.py`` files to plain ``.py`` files."""

from __future__ import annotations

import argparse
import io
import sys
import tokenize
import unicodedata
from pathlib import Path

import neo
from neo.compiler import CompileResult, compile_source

SUFFIX = ".neo.py"


def _sources(names: list[str], parser: argparse.ArgumentParser) -> list[Path]:
    found = []
    for name in names:
        path = Path(name)
        if path.is_dir():
            found += sorted(path.rglob(f"*{SUFFIX}"))
        elif path.name.endswith(SUFFIX) and path.is_file():
            found.append(path)
        elif not path.exists():
            parser.error(f"{name}: no such file or directory")
        else:
            parser.error(f"{name}: expected a {SUFFIX} file")
    return found


def _caret(text: str, col: int) -> str:
    # Wide (e.g. CJK) characters take two terminal columns; tabs are kept as tabs.
    pad = ("\t" if c == "\t" else "  " if unicodedata.east_asian_width(c) in "WF" else " " for c in text[:col])
    return "".join(pad) + "^"


def _report(result: CompileResult, path: Path, source: str, explain: bool, out) -> None:
    lines = source.splitlines()
    for diagnostic in result.diagnostics:
        print(diagnostic.format(str(path)), file=sys.stderr)
        if 0 < diagnostic.line <= len(lines):
            text = lines[diagnostic.line - 1]
            print(f"    {text}", file=sys.stderr)
            print(f"    {_caret(text, diagnostic.col)}", file=sys.stderr)
    if explain:
        for explanation in result.explanations:
            print(explanation.format(str(path)), file=out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neoc", description="Compile Python Neo (.neo.py) files to plain Python.")
    parser.add_argument("sources", nargs="+", metavar="SOURCE", help=f"{SUFFIX} files or directories containing them")
    parser.add_argument("-o", "--output", help="output file for a single source; '-' writes to stdout")
    parser.add_argument("--explain", action="store_true", help="list every rewrite neoc made")
    parser.add_argument("--version", action="version", version=f"neoc {neo.__version__}")
    args = parser.parse_args(argv)

    paths = _sources(args.sources, parser)
    if args.output and len(paths) != 1:
        parser.error("-o/--output needs exactly one source file")

    failed = False
    for path in paths:
        raw = path.read_bytes()
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        source = raw.decode(encoding)
        result = compile_source(source, str(path))
        to_stdout = args.output == "-"
        _report(result, path, source, args.explain, sys.stderr if to_stdout else sys.stdout)
        if not result.ok:
            failed = True
            continue
        if to_stdout:
            sys.stdout.write(result.code)
        else:
            target = Path(args.output) if args.output else path.with_name(path.name[: -len(SUFFIX)] + ".py")
            target.write_text(result.code, encoding=encoding, newline="")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
