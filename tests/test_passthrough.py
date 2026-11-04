"""Step 1: valid Python passes through unchanged, and moltyc writes .py files."""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import HEADER, MoltyTestCase, compile_molty

from molty.compiler.cli import _write, main

PLAIN = '''\
#!/usr/bin/env python3
"""Plain Python: every Molty keyword is an ordinary name here."""
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


class Passthrough(MoltyTestCase):
    def test_plain_python_is_unchanged(self):
        result = self.assertClean(PLAIN)
        shebang, header, *rest = result.code.splitlines(keepends=True)
        self.assertEqual(header, HEADER)
        self.assertEqual(shebang + "".join(rest), PLAIN)

    def test_plain_python_still_runs_the_same(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(compile_molty(PLAIN).code, {"__name__": "plain"})
        self.assertEqual(out.getvalue(), "1 2 [2] 2 2 1   2\n")

    def test_crlf_files_keep_crlf(self):
        code = compile_molty("val xs = [1]\r\ndef f(a=[]):\r\n    return a\r\n").code
        self.assertEqual(code.count("\n"), code.count("\r\n"))

    def test_syntax_errors_point_at_the_original_column(self):
        result = compile_molty("val 名前 = = 1\n")
        self.assertEqual([(d.code, d.line, d.col) for d in result.errors], [("MOL0001", 1, 9)])


class Terminal(io.StringIO):
    """Output that claims to be a terminal, as when a person runs moltyc."""

    def isatty(self):
        return True


class Cli(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def run_main(self, *args: str, tty: bool = False) -> tuple[int, str, str]:
        out, err = (Terminal(), Terminal()) if tty else (io.StringIO(), io.StringIO())
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_writes_py_next_to_the_source(self):
        source = self.write("app.molty.py", "print('hi')\n")
        self.assertEqual(self.run_main(str(source))[0], 0)
        self.assertEqual((self.dir / "app.py").read_text(), HEADER.replace("test.molty.py", "app.molty.py") + "print('hi')\n")

    def test_errors_exit_1_and_write_nothing(self):
        source = self.write("bad.molty.py", "val x = 1\nx = 2\n")
        code, _, err = self.run_main(str(source))
        self.assertEqual(code, 1)
        self.assertIn("bad.molty.py:2:1: error MOL1001", err)
        self.assertFalse((self.dir / "bad.py").exists())

    def test_a_warning_still_writes_the_file(self):
        source = self.write("app.molty.py", "def f(xs=[]):\n    return xs\n")
        code, _, err = self.run_main(str(source))
        self.assertEqual(code, 0)
        self.assertIn("app.molty.py:1:10: warning MOL1004", err)
        self.assertTrue((self.dir / "app.py").read_text().endswith("def f(xs=[]):\n    return xs\n"))

    def test_stdout_and_explain(self):
        source = self.write("app.molty.py", "def f(xs=[]):\n    return xs\n")
        code, out, err = self.run_main(str(source), "-o", "-", "--explain", "--rewrite-defaults")
        self.assertEqual(code, 0)
        self.assertIn("if xs is None:", out)
        self.assertIn("explain: mutable default of 'xs' ([]) is now created on every call", err)

    def test_a_success_line_on_a_terminal(self):
        clean = self.write("clean.molty.py", "x = 1\n")
        warned = self.write("warned.molty.py", "def f(xs=[]):\n    return xs\n")
        code, _, err = self.run_main(str(clean), str(warned), tty=True)
        self.assertEqual(code, 0)
        self.assertIn(f"~~~~:>  {clean} -> {self.dir / 'clean.py'}  ok\n", err)
        self.assertIn(f"~~~~:>  {warned} -> {self.dir / 'warned.py'}  ok (1 warning)\n", err)

    def test_no_success_line_when_quiet_piped_or_failed(self):
        clean = self.write("clean.molty.py", "x = 1\n")
        bad = self.write("bad.molty.py", "val x = 1\nx = 2\n")
        self.assertNotIn("~~~~:>", self.run_main(str(clean), "-q", tty=True)[2])
        self.assertNotIn("~~~~:>", self.run_main(str(clean))[2])
        self.assertNotIn("~~~~:>", self.run_main(str(bad), tty=True)[2])

    def test_version_shows_the_logo_only_on_a_terminal(self):
        _, out, _ = self.run_main("--version", tty=True)
        self.assertIn("|_|  |_|\\___/|_____|_|   |_|", out)
        self.assertIn(":>   Molty 0.1.0", out)
        self.assertEqual(self.run_main("--version")[1], "moltyc 0.1.0\n")

    def test_refuses_to_write_through_a_symlink(self):
        victim = self.write("victim.txt", "keep me\n")
        source = self.write("app.molty.py", "x = 1\n")
        (self.dir / "app.py").symlink_to(victim)
        code, _, err = self.run_main(str(source))
        self.assertEqual(code, 1)
        self.assertIn("is a symbolic link", err)
        self.assertEqual(victim.read_text(), "keep me\n")

    def test_refuses_to_overwrite_a_hand_written_file_without_force(self):
        source = self.write("app.molty.py", "x = 1\n")
        self.write("app.py", "print('mine')\n")
        code, _, err = self.run_main(str(source))
        self.assertEqual(code, 1)
        self.assertIn("was not generated by moltyc; use --force", err)
        self.assertEqual((self.dir / "app.py").read_text(), "print('mine')\n")
        self.assertEqual(self.run_main(str(source), "--force")[0], 0)
        self.assertTrue((self.dir / "app.py").read_text().startswith("# Generated by moltyc"))

    @unittest.skipUnless(os.path.exists(os.devnull) and os.devnull != "nul", "needs a POSIX /dev/null")
    def test_writes_to_dev_null_for_diagnostics_only(self):
        source = self.write("app.molty.py", "x = 1\n")
        self.assertEqual(self.run_main(str(source), "-o", os.devnull)[0], 0)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "needs O_NOFOLLOW")
    def test_the_write_itself_refuses_a_link_put_in_place_after_the_checks(self):
        victim = self.write("victim.txt", "keep me\n")
        (self.dir / "app.py").symlink_to(victim)
        with self.assertRaises(OSError):
            _write(self.dir / "app.py", "x = 1\n", "utf-8")
        self.assertEqual(victim.read_text(), "keep me\n")

    def test_overwrites_its_own_output(self):
        source = self.write("app.molty.py", "x = 1\n")
        self.assertEqual(self.run_main(str(source))[0], 0)
        source.write_text("x = 2\n")
        self.assertEqual(self.run_main(str(source))[0], 0)
        self.assertTrue((self.dir / "app.py").read_text().endswith("x = 2\n"))

    def test_refuses_to_write_over_the_source(self):
        source = self.write("app.molty.py", "val x = [1]\n")
        code, _, err = self.run_main(str(source), "-o", str(source))
        self.assertEqual(code, 1)
        self.assertIn("is a source file", err)
        self.assertEqual(source.read_text(), "val x = [1]\n")

    def test_a_directory_walk_skips_symlinks(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside)
        (outside / "secret.molty.py").write_text("SECRET = 1\n")
        tree = self.dir / "src"
        tree.mkdir()
        (tree / "real.molty.py").write_text("x = 1\n")
        (tree / "creds.molty.py").symlink_to(outside / "secret.molty.py")
        (tree / "linked").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.run_main(str(tree))[0], 0)
        self.assertEqual(sorted(p.name for p in tree.iterdir()), ["creds.molty.py", "linked", "real.molty.py", "real.py"])
        self.assertFalse((outside / "secret.py").exists())

    def test_rejects_files_that_are_not_molty(self):
        source = self.write("plain.py", "x = 1\n")
        self.assertEqual(self.run_main(str(source))[0], 2)

    def test_runs_as_a_module(self):
        source = self.write("app.molty.py", "val xs = [1]\nprint(xs)\n")
        subprocess.run([sys.executable, "-m", "molty.compiler.cli", str(source)], check=True)
        done = subprocess.run([sys.executable, str(self.dir / "app.py")], check=True, capture_output=True, text=True)
        self.assertEqual(done.stdout, "(1,)\n")

    def test_moltyc_script_is_installed(self):
        moltyc = Path(sys.executable).with_name("moltyc")
        if not moltyc.exists():
            self.skipTest("moltyc is not installed next to this interpreter")
        done = subprocess.run([str(moltyc), "--version"], check=True, capture_output=True, text=True)
        self.assertTrue(done.stdout.startswith("moltyc "))


if __name__ == "__main__":
    unittest.main()
