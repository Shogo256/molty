"""Step 2: val/var, freezing val literals, NEO1001/NEO1003, mutable defaults."""

import unittest
from types import MappingProxyType

from helpers import HEADER, NeoTestCase, compile_neo, load


class ValRebinding(NeoTestCase):
    def test_rebinding_a_val_is_an_error(self):
        self.assertError("val x = 1\nx = 2\n", "NEO1001")

    def test_reading_a_val_is_fine(self):
        self.assertClean("val x = 1\ny = x + 1\n")

    def test_every_way_of_rebinding_is_caught(self):
        forms = [
            "x += 1",
            "x: int = 2",
            "del x",
            "for x in range(3): pass",
            "with open('f') as x: pass",
            "import x",
            "from os import path as x",
            "def x(): pass",
            "class x: pass",
            "(x := 5)",
            "try:\n    pass\nexcept Exception as x:\n    pass",
            "val x = 2",
        ]
        for form in forms:
            with self.subTest(form):
                self.assertError(f"val x = 1\n{form}\n", "NEO1001")

    def test_rebinding_through_global_or_nonlocal_is_caught(self):
        self.assertError("val x = 1\ndef f():\n    global x\n    x = 2\n", "NEO1001")
        self.assertError(
            "def outer():\n    val x = 1\n    def inner():\n        nonlocal x\n        x = 2\n", "NEO1001"
        )

    def test_shadowing_in_another_scope_is_fine(self):
        self.assertClean("val x = 1\ndef f():\n    x = 2\n    return x\nys = [x for x in range(3)]\n")

    def test_val_in_a_loop_body_is_one_binding(self):
        self.assertClean("for i in range(3):\n    val y = i * 2\n")

    def test_val_over_a_parameter_is_an_error(self):
        self.assertError("def f(x):\n    val x = 1\n", "NEO1001")

    def test_var_can_be_rebound(self):
        self.assertClean("var n = 0\nn += 1\nn = 5\n")

    def test_one_line_block(self):
        self.assertError("if True: val x = 1; x = 2\n", "NEO1001")


class Declarations(NeoTestCase):
    def test_val_without_a_value_is_an_error(self):
        self.assertError("val x: int\n", "NEO1003")

    def test_val_with_a_value_is_fine(self):
        self.assertClean("val x: int = 1\n")

    def test_val_of_an_attribute_is_an_error(self):
        self.assertError("class C: pass\nc = C()\nval c.x = 1\n", "NEO1003")

    def test_val_must_start_an_assignment(self):
        self.assertError("val x\n", "NEO1003")
        self.assertError("val a = b = 1\n", "NEO1003")


class Freezing(NeoTestCase):
    def test_literals_become_immutable(self):
        mod = load(
            """
            val xs = [1, 2]
            val one = [1]
            val cfg = {"a": 1}
            val tags = {"x"}
            val nested = {"ports": [80, 443], "opts": {"debug": {True}}}
            val squares = [i * i for i in range(3)]
            val a, b = [1], {2}
            """
        )
        self.assertEqual(mod.xs, (1, 2))
        self.assertEqual(mod.one, (1,))
        self.assertIsInstance(mod.cfg, MappingProxyType)
        self.assertEqual(mod.tags, frozenset({"x"}))
        self.assertEqual(mod.nested["ports"], (80, 443))
        self.assertIsInstance(mod.nested["opts"], MappingProxyType)
        self.assertEqual(mod.nested["opts"]["debug"], frozenset({True}))
        self.assertEqual(mod.squares, (0, 1, 4))
        self.assertEqual((mod.a, mod.b), ((1,), frozenset({2})))

    def test_mutation_fails_at_run_time(self):
        mod = load("val xs = [1]\nval cfg = {'a': 1}\n")
        with self.assertRaises(AttributeError):
            mod.xs.append(2)
        with self.assertRaises(TypeError):
            mod.cfg["b"] = 2

    def test_var_and_plain_assignments_stay_mutable(self):
        mod = load("var xs = [1]\nys = {'a': 1}\n")
        self.assertIsInstance(mod.xs, list)
        self.assertIsInstance(mod.ys, dict)

    def test_output_keeps_comments_and_layout(self):
        source = 'val config = {\n    "host": "localhost",  # dev\n    "ports": [8080],\n}\n'
        expected = (
            HEADER
            + "import neo.runtime\n"
            + 'config = neo.runtime.MappingProxyType({\n    "host": "localhost",  # dev\n    "ports": (8080,),\n})\n'
        )
        self.assertEqual(compile_neo(source).code, expected)

    def test_annotations_follow_the_frozen_type(self):
        code = compile_neo(
            'val xs: list[int] = [1]\nval m: dict[str, list[int]] = {"a": [1]}\nval s: set[str] = {"a"}\n'
        ).code
        self.assertIn("xs: tuple[int, ...] = (1,)", code)
        self.assertIn('m: neo.runtime.Mapping[str, tuple[int, ...]] = neo.runtime.MappingProxyType({"a": (1,)})', code)
        self.assertIn('s: frozenset[str] = frozenset({"a"})', code)

    def test_builtin_calls_are_frozen_unless_shadowed(self):
        self.assertEqual(load("val xs = list(range(3))\n").xs, (0, 1, 2))
        self.assertEqual(load("def list(x):\n    return [x]\nval xs = list(1)\n").xs, [1])

    def test_explain_records_the_freeze(self):
        result = compile_neo("val cfg = {'a': [1]}\n")
        self.assertEqual([e.message for e in result.explanations], ["val cfg: dict -> MappingProxyType"])


class MutableDefaults(NeoTestCase):
    def test_a_default_list_is_fresh_on_every_call(self):
        mod = load("def add(x, items: list = []):\n    items.append(x)\n    return items\n")
        self.assertEqual(mod.add(1), [1])
        self.assertEqual(mod.add(2), [2])

    def test_an_immutable_default_is_untouched(self):
        source = "def f(items: tuple = ()):\n    return items\n"
        result = self.assertClean(source)
        self.assertEqual(result.code, HEADER + source)
        self.assertEqual(result.explanations, [])

    def test_the_rewrite_is_explained_not_warned(self):
        result = self.assertClean("def f(items: list = []):\n    return items\n")
        self.assertEqual(
            [e.message for e in result.explanations], ["mutable default of 'items' ([]) is now created on every call"]
        )
        self.assertIn("def f(items: list | None = None):", result.code)

    def test_every_shape_of_function(self):
        mod = load(
            '''
            import collections
            def a(d={}): d["k"] = d.get("k", 0) + 1; return d
            def b(*, s=set()):
                """Docstring stays first."""
                s.add(1)
                return s
            def c(q=collections.deque()):
                """Only a docstring."""
            def d(xs=[1, 2]): return xs
            class K:
                def m(self, seen: "list[int]" = []):
                    seen.append(1)
                    return seen
            '''
        )
        self.assertEqual(mod.a(), {"k": 1})
        self.assertEqual(mod.a(), {"k": 1})
        self.assertEqual(mod.b(), {1})
        self.assertEqual(mod.b(), {1})
        self.assertEqual(mod.b.__doc__, "Docstring stays first.")
        self.assertIsNone(mod.c())
        self.assertEqual(mod.c.__doc__, "Only a docstring.")
        self.assertIsNot(mod.d(), mod.d())
        self.assertEqual(mod.K().m(), [1])
        self.assertEqual(mod.K().m(), [1])

    def test_passing_a_value_still_works(self):
        mod = load("def add(x, items=[]):\n    items.append(x)\n    return items\n")
        mine = [0]
        self.assertIs(mod.add(1, mine), mine)
        self.assertEqual(mine, [0, 1])


if __name__ == "__main__":
    unittest.main()
