"""Step 6: @neo.checked, and NEO_CHECKS=0 turning every run-time check off."""

import asyncio
import inspect
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from helpers import NeoTestCase, compile_neo, load

import neo
from neo.runtime import CheckError

SOURCE = """
    import neo
    from typing import Any, Optional

    @neo.checked
    def scale(xs: list[int], factor: float = 1.0, *, label: Optional[str] = None) -> list[float]:
        return [x * factor for x in xs]

    @neo.checked
    def count(xs: list[int]) -> int:
        return len(xs)

    @neo.checked
    def loose(a, b: Any, c: object, *rest: int, **named: str) -> None:
        pass

    @neo.checked
    def later(node: "Node") -> str:
        return node.name

    class Node:
        name = "node"

    @neo.checked
    task def fetch(url: str) -> str:
        return url

    def unchecked(xs: list[int]) -> int:
        return len(xs)
    """


class Checked(NeoTestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load(SOURCE)

    def test_a_wrong_type_is_rejected(self):
        with self.assertRaisesRegex(CheckError, "scale\\(\\) argument 'xs' must be list, not str"):
            self.mod.scale("abc")
        with self.assertRaisesRegex(CheckError, "argument 'label' must be str \\| None, not int"):
            self.mod.scale([1], label=3)
        self.assertTrue(issubclass(CheckError, TypeError))

    def test_a_right_type_is_accepted(self):
        self.assertEqual(self.mod.scale([1, 2], 2), [2, 4])  # int is fine where float is expected
        self.assertEqual(self.mod.scale(xs=[1], label=None), [1.0])

    def test_the_check_is_shallow(self):
        self.assertEqual(self.mod.count(["not", "ints"]), 2)

    def test_unannotated_any_and_object_are_skipped(self):
        self.mod.loose(object(), object(), object(), 1, 2, k="v")
        with self.assertRaisesRegex(CheckError, "'\\*args\\[1\\]' must be int"):
            self.mod.loose(1, 2, 3, 4, "five")
        with self.assertRaisesRegex(CheckError, "'k' must be str"):
            self.mod.loose(1, 2, 3, k=4)

    def test_forward_references_resolve_on_first_call(self):
        self.assertEqual(self.mod.later(self.mod.Node()), "node")
        with self.assertRaises(CheckError):
            self.mod.later("not a node")

    def test_a_checked_task_is_still_a_coroutine_function(self):
        self.assertTrue(inspect.iscoroutinefunction(self.mod.fetch))
        self.assertEqual(asyncio.run(self.mod.fetch("u")), "u")
        with self.assertRaises(CheckError):
            asyncio.run(self.mod.fetch(1))

    @unittest.skipIf(sys.version_info < (3, 12), "without markcoroutinefunction the check runs when awaited")
    def test_a_checked_task_is_checked_when_called(self):
        with self.assertRaises(CheckError):
            self.mod.fetch(1)

    def test_functions_without_the_decorator_are_not_checked(self):
        self.assertEqual(self.mod.unchecked("abc"), 3)


class DeepAndReturnChecks(NeoTestCase):
    SOURCE = """
        import neo

        @neo.checked(deep=True)
        def deep(xs: list[int], table: dict[str, list[float]], pair: tuple[int, str], tags: set[str] | None = None) -> int:
            return len(xs)

        @neo.checked
        def shallow(xs: list[int]) -> int:
            return len(xs)

        @neo.checked(returns=True)
        def lies(n: int) -> str:
            return n

        @neo.checked(deep=True, returns=True)
        task def fetch(n: int) -> list[str]:
            return [str(n), n]
        """

    @classmethod
    def setUpClass(cls):
        cls.mod = load(cls.SOURCE)

    def test_deep_checks_name_the_item_that_is_wrong(self):
        cases = [
            (([1, "2"], {}, (1, "a")), "argument 'xs'[1] must be int, not str"),
            (([1], {"a": [1.0, "x"]}, (1, "a")), "argument 'table'['a'][1] must be float | int, not str"),
            (([1], {1: []}, (1, "a")), "argument 'table' key 1 must be str, not int"),
            (([1], {}, (1,)), "argument 'pair' must have 2 items, not 1"),
            (([1], {}, (1, "a"), {"ok", 3}), "argument 'tags' item 3 must be str, not int"),
        ]
        for args, message in cases:
            with self.subTest(message):
                with self.assertRaisesRegex(CheckError, re.escape(message)):
                    self.mod.deep(*args)
        self.assertEqual(self.mod.deep([1, 2], {"a": [1, 2.5]}, (1, "a"), None), 2)

    def test_without_deep_the_items_are_not_looked_at(self):
        self.assertEqual(self.mod.shallow(["a"]), 1)

    def test_returns_checks_the_return_value(self):
        with self.assertRaisesRegex(CheckError, "lies\\(\\) return value must be str, not int"):
            self.mod.lies(1)
        with self.assertRaisesRegex(CheckError, re.escape("fetch() return value[1] must be str, not int")):
            asyncio.run(self.mod.fetch(1))

    def test_neo_checks_0_turns_these_off_too(self):
        with mock.patch.dict(os.environ, {"NEO_CHECKS": "0"}):
            mod = load(self.SOURCE)
        self.assertEqual(mod.lies(1), 1)
        self.assertEqual(mod.deep(["x"], {}, (1, "a")), 1)


class FrozenValues(NeoTestCase):
    SOURCE = """
        import neo
        val PRICES = [100, 200]
        val LIMITS = {"daily": 500}
        @neo.checked
        def total(xs: val list[int], limits: val dict[str, int], tags: val set[str] | None = None) -> int:
            return sum(xs)
        @neo.checked
        def grow(xs: list[int]) -> list[int]:
            xs.append(0)
            return xs
        """

    def test_a_val_parameter_takes_frozen_values(self):
        result = self.assertClean(self.SOURCE)
        self.assertIn(
            "def total(xs: neo.runtime.Sequence[int], limits: neo.runtime.Mapping[str, int], "
            "tags: neo.runtime.AbstractSet[str] | None = None) -> int:",
            result.code,
        )
        mod = load(self.SOURCE)
        self.assertEqual(mod.total(mod.PRICES, mod.LIMITS, frozenset({"a"})), 300)
        self.assertEqual(mod.total([1, 2], {"daily": 1}), 3)

    def test_a_plain_list_parameter_still_wants_a_list(self):
        mod = load(self.SOURCE)
        with self.assertRaisesRegex(CheckError, "must be list, not tuple"):
            mod.grow(mod.PRICES)


class ChecksOff(NeoTestCase):
    def test_neo_checks_0_returns_the_function_untouched(self):
        with mock.patch.dict(os.environ, {"NEO_CHECKS": "0"}):
            mod = load(SOURCE)
        self.assertFalse(hasattr(mod.scale, "__wrapped__"))
        self.assertEqual(mod.count("abc"), 3)

    def test_neo_checks_0_turns_off_pickle_checks(self):
        source = "compute def f(cb):\n    return cb()\n"
        self.assertEqual(load(source).f.check_args, ("cb",))
        with mock.patch.dict(os.environ, {"NEO_CHECKS": "0"}):
            mod = load(source)
        self.assertEqual((mod.f.check_args, mod.f.check_result), ((), False))

    def test_from_the_environment_of_a_real_run(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        script = Path(directory) / "app.py"
        script.write_text(compile_neo(textwrap.dedent(SOURCE) + "print(count('abc'))\n").code)
        package_root = str(Path(neo.__file__).resolve().parent.parent)  # works without installing neo
        env = dict(os.environ, NEO_CHECKS="0", PYTHONPATH=package_root)
        done = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True)
        self.assertEqual(done.stdout, "3\n", done.stderr)
        env["NEO_CHECKS"] = "1"
        done = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True)
        self.assertIn("CheckError: count() argument 'xs' must be list, not str", done.stderr)


if __name__ == "__main__":
    unittest.main()
