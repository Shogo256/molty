"""Step 1: valid Python passes through unchanged, and neoc writes .py files."""

import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import HEADER, NeoTestCase, compile_neo

from neo.compiler.cli import main

PLAIN = '''\
#!/usr/bin/env python3
"""Plain Python: every Neo keyword is an ordinary name here."""
import asyncio

val = 1                 # 'val' as a variable
var = val + 1
task = [var]
compute = task[0]
scope = {"compute": compute}

def f(x: int, *args, y: str = "s", **kw) -> int:
    return x  # trailing comment

class C:
    def m(self, val: int = val):
        return f"{val}{var!r:>4}"

print(val, var, task, compute, scope["compute"], C().m())
'''


class Passthrough(NeoTestCase):
    def test_plain_python_is_unchanged(self):
        result = self.assertClean(PLAIN)
        shebang, header, *rest = result.code.splitlines(keepends=True)
        self.assertEqual(header, HEADER)
        self.assertEqual(shebang + "".join(rest), PLAIN)

    def test_plain_python_still_runs_the_same(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(compile_neo(PLAIN).code, {"__name__": "plain"})
        self.assertEqual(out.getvalue(), "1 2 [2] 2 2 1   2\n")

    def test_crlf_files_keep_crlf(self):
        code = compile_neo("val xs = [1]\r\ndef f(a=[]):\r\n    return a\r\n").code
        self.assertEqual(code.count("\n"), code.count("\r\n"))

    def test_syntax_errors_point_at_the_original_column(self):
        result = compile_neo("val 名前 = = 1\n")
        self.assertEqual([(d.code, d.line, d.col) for d in result.errors], [("NEO0001", 1, 9)])


class Cli(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def run_main(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_writes_py_next_to_the_source(self):
        source = self.write("app.neo.py", "print('hi')\n")
        self.assertEqual(self.run_main(str(source))[0], 0)
        self.assertEqual((self.dir / "app.py").read_text(), HEADER.replace("test.neo.py", "app.neo.py") + "print('hi')\n")

    def test_errors_exit_1_and_write_nothing(self):
        source = self.write("bad.neo.py", "val x = 1\nx = 2\n")
        code, _, err = self.run_main(str(source))
        self.assertEqual(code, 1)
        self.assertIn("bad.neo.py:2:1: error NEO1001", err)
        self.assertFalse((self.dir / "bad.py").exists())

    def test_stdout_and_explain(self):
        source = self.write("app.neo.py", "def f(xs=[]):\n    return xs\n")
        code, out, err = self.run_main(str(source), "-o", "-", "--explain")
        self.assertEqual(code, 0)
        self.assertIn("if xs is None:", out)
        self.assertIn("explain: mutable default of 'xs' ([]) is now created on every call", err)

    def test_rejects_files_that_are_not_neo(self):
        source = self.write("plain.py", "x = 1\n")
        self.assertEqual(self.run_main(str(source))[0], 2)

    def test_runs_as_a_module(self):
        source = self.write("app.neo.py", "val xs = [1]\nprint(xs)\n")
        subprocess.run([sys.executable, "-m", "neo.compiler.cli", str(source)], check=True)
        done = subprocess.run([sys.executable, str(self.dir / "app.py")], check=True, capture_output=True, text=True)
        self.assertEqual(done.stdout, "(1,)\n")

    def test_neoc_script_is_installed(self):
        neoc = Path(sys.executable).with_name("neoc")
        if not neoc.exists():
            self.skipTest("neoc is not installed next to this interpreter")
        done = subprocess.run([str(neoc), "--version"], check=True, capture_output=True, text=True)
        self.assertTrue(done.stdout.startswith("neoc "))


if __name__ == "__main__":
    unittest.main()
