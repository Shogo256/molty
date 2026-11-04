"""Step 2: val/var, freezing val literals, MOL1001/MOL1003, mutable defaults."""

import unittest
from types import MappingProxyType

from helpers import HEADER, MoltyTestCase, compile_molty, load


class ValRebinding(MoltyTestCase):
    def test_rebinding_a_val_is_an_error(self):
        self.assertError("val x = 1\nx = 2\n", "MOL1001")

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
                self.assertError(f"val x = 1\n{form}\n", "MOL1001")

    def test_rebinding_through_global_or_nonlocal_is_caught(self):
        self.assertError("val x = 1\ndef f():\n    global x\n    x = 2\n", "MOL1001")
        self.assertError(
            "def outer():\n    val x = 1\n    def inner():\n        nonlocal x\n        x = 2\n", "MOL1001"
        )

    def test_shadowing_in_another_scope_is_fine(self):
        self.assertClean("val x = 1\ndef f():\n    x = 2\n    return x\nys = [x for x in range(3)]\n")

    def test_val_in_a_loop_body_is_one_binding(self):
        self.assertClean("for i in range(3):\n    val y = i * 2\n")

    def test_one_val_per_branch_is_one_binding(self):
        source = """
            import sys
            if len(sys.argv) > 5:
                val MODE = ["verbose"]
            elif sys.argv:
                val MODE = ["normal"]
            else:
                val MODE = ["quiet"]
            match len(sys.argv):
                case 99:
                    val LEVEL = 1
                case _:
                    val LEVEL = 2
            def pick(flag):
                if flag:
                    val choice = {"a"}
                else:
                    val choice = {"b"}
                return choice
            """
        self.assertClean(source)
        mod = load(source)
        self.assertEqual((mod.MODE, mod.LEVEL), (("normal",), 2))
        self.assertEqual(mod.pick(False), frozenset({"b"}))

    def test_a_val_outside_and_inside_a_branch_is_still_an_error(self):
        self.assertError("val Z = 1\nif True:\n    val Z = 2\n", "MOL1001")
        self.assertError("if True:\n    val Z = 1\nif False:\n    val Z = 2\n", "MOL1001")

    def test_changing_a_val_that_is_frozen_in_every_branch_is_an_error(self):
        branches = "import sys\nif sys.argv:\n    val TAGS = ['a']\nelse:\n    val TAGS = ['b']\n"
        self.assertError(branches + "TAGS.append('c')\n", "MOL1005")
        mixed = "import sys\nif sys.argv:\n    val TAGS = ['a']\nelse:\n    val TAGS = sys.argv\n"
        self.assertClean(mixed + "TAGS.append('c')\n")

    def test_val_over_a_parameter_is_an_error(self):
        self.assertError("def f(x):\n    val x = 1\n", "MOL1001")

    def test_var_can_be_rebound(self):
        self.assertClean("var n = 0\nn += 1\nn = 5\n")

    def test_one_line_block(self):
        self.assertError("if True: val x = 1; x = 2\n", "MOL1001")


class FrozenValChanges(MoltyTestCase):
    def test_changing_a_frozen_val_is_an_error(self):
        changes = [
            "PRICES.append(3)",
            "PRICES[0] = 9",
            "del PRICES[0]",
            "CONFIG.update(port=80)",
            "CONFIG['port'] = 80",
            "TAGS.add('x')",
            "SQUARES.sort()",
            "A.append(1)",
            "def later():\n    PRICES.append(3)",
        ]
        header = (
            "val PRICES = [1, 2]\nval CONFIG = {'host': 'h'}\nval TAGS = set()\n"
            "val SQUARES = [i * i for i in range(3)]\nval A, B = [1], {2}\n"
        )
        for change in changes:
            with self.subTest(change):
                result = self.assertError(header + change + "\n", "MOL1005")
                self.assertIn("was frozen to a", result.errors[0].message)

    def test_changing_a_frozen_part_of_a_val_is_an_error(self):
        header = "val CONFIG = {'db': {'host': 'x'}, 'ports': [80], 'pairs': [[1, 2]]}\n"
        for change in ["CONFIG['db']['host'] = 'y'", "CONFIG['ports'].append(443)", "CONFIG['pairs'][0].append(3)"]:
            with self.subTest(change):
                result = self.assertError(header + change + "\n", "MOL1005")
                self.assertIn("(part of val 'CONFIG')", result.errors[0].message)

    def test_any_key_into_an_all_frozen_literal_is_an_error(self):
        for source in [
            "val ROUTES = {'get': ['/a'], 'post': ['/b']}\nkey = 'get'\nROUTES[key].append('/c')\n",
            "val GRID = [[0, 0], [0, 0]]\nfor i in range(2):\n    GRID[i][i] = 1\n",
        ]:
            with self.subTest(source):
                self.assertError(source, "MOL1005")

    def test_an_item_of_a_frozen_comprehension_is_frozen_too(self):
        for source in [
            "val ROWS = [list(range(2)) for _ in range(2)]\nROWS[0][1] = 9\n",
            "val M = {k: [k] for k in 'ab'}\nM['a'].append('x')\n",
        ]:
            with self.subTest(source):
                self.assertError(source, "MOL1005")

    def test_an_ordinary_item_of_a_comprehension_can_change(self):
        self.assertClean("rows = [[1], [2]]\nval R = [row for row in rows]\nR[0].append(3)\n")

    def test_any_key_into_a_literal_with_an_ordinary_item_is_fine(self):
        self.assertClean("val MIXED = {'db': {'h': 1}, 'n': 3}\nkey = 'db'\nMIXED[key]['h'] = 2\n")

    def test_an_ordinary_object_inside_a_frozen_val_can_change(self):
        self.assertClean(
            "db = {'host': 'x'}\n"
            "val CONFIG = {'db': db, 'ports': [80]}\n"
            "CONFIG['db']['host'] = 'y'\n"
            "key = 'db'\n"
            "CONFIG[key]['host'] = 'z'\n"
        )

    def test_reading_a_frozen_val_or_changing_anything_else_is_fine(self):
        self.assertClean(
            """
            import collections
            val PRICES = [1, 2]
            val CONFIG = {'host': 'h'}
            var items = [1]
            val counts = collections.Counter()
            class Cache:
                def clear(self): pass
            val cache = Cache()
            items.append(PRICES.count(1) + PRICES.index(2) + len(CONFIG.get('host', '')))
            counts.update('abc')
            cache.clear()
            def shadow():
                PRICES = []
                PRICES.append(1)
                return PRICES
            """
        )


class Declarations(MoltyTestCase):
    def test_val_without_a_value_is_an_error(self):
        self.assertError("val x: int\n", "MOL1003")

    def test_val_with_a_value_is_fine(self):
        self.assertClean("val x: int = 1\n")

    def test_val_of_an_attribute_is_an_error(self):
        self.assertError("class C: pass\nc = C()\nval c.x = 1\n", "MOL1003")

    def test_val_must_start_an_assignment(self):
        self.assertError("val x\n", "MOL1003")
        self.assertError("val a = b = 1\n", "MOL1003")


class Freezing(MoltyTestCase):
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
            + "import molty.runtime\n"
            + 'config = molty.runtime.MappingProxyType({\n    "host": "localhost",  # dev\n    "ports": (8080,),\n})\n'
        )
        self.assertEqual(compile_molty(source).code, expected)

    def test_annotations_follow_the_frozen_type(self):
        code = compile_molty(
            'val xs: list[int] = [1]\nval m: dict[str, list[int]] = {"a": [1]}\nval s: set[str] = {"a"}\n'
        ).code
        self.assertIn("xs: tuple[int, ...] = (1,)", code)
        self.assertIn('m: molty.runtime.Mapping[str, tuple[int, ...]] = molty.runtime.MappingProxyType({"a": (1,)})', code)
        self.assertIn('s: frozenset[str] = frozenset({"a"})', code)

    def test_builtin_calls_are_frozen_unless_shadowed(self):
        self.assertEqual(load("val xs = list(range(3))\n").xs, (0, 1, 2))
        self.assertEqual(load("def list(x):\n    return [x]\nval xs = list(1)\n").xs, [1])

    def test_explain_records_the_freeze(self):
        result = compile_molty("val cfg = {'a': [1]}\n")
        self.assertEqual([e.message for e in result.explanations], ["val cfg: dict -> MappingProxyType"])


SHARED = "def add(x, items: list = []):\n    items.append(x)\n    return items\n"
WARNING = (
    "default [] of 'items' is created once and shared by every call; "
    "mark it '# molty: cache' if that is intended, or compile with --rewrite-defaults"
)


class MutableDefaults(MoltyTestCase):
    """By default moltyc only warns, so valid Python keeps its meaning."""

    def test_a_mutable_default_warns_and_is_left_alone(self):
        result = self.assertWarning(SHARED, "MOL1004")
        warning = result.warnings[0]
        self.assertEqual((warning.line, warning.col, warning.message), (1, 25, WARNING))
        self.assertEqual(result.code, HEADER + SHARED)
        self.assertEqual(result.explanations, [])
        mod = load(SHARED)
        self.assertEqual(mod.add(1), [1])
        self.assertEqual(mod.add(2), [1, 2])  # Python's meaning, unchanged

    def test_an_immutable_default_is_quiet(self):
        source = "def f(items: tuple = (), n: int = 0, s=frozenset()):\n    return items\n"
        self.assertEqual(self.assertClean(source).code, HEADER + source)

    def test_every_mutable_form_warns(self):
        forms = [
            "[]", "{}", "{1}", "[i for i in range(3)]", "{i: i for i in range(3)}", "{i for i in ()}",
            "list()", "dict(a=1)", "set()", "bytearray()", "collections.defaultdict(list)", "collections.deque()",
        ]
        for form in forms:
            with self.subTest(form):
                self.assertWarning(f"import collections\ndef f(x={form}):\n    return x\n", "MOL1004")
        self.assertWarning("def f(*, x=[]):\n    return x\n", "MOL1004")
        self.assertWarning("f = lambda x=[]: x\n", "MOL1004")

    def test_a_shadowed_builtin_is_not_mutable(self):
        self.assertClean("def list():\n    return ()\ndef f(x=list()):\n    return x\n")

    def test_molty_cache_marks_the_sharing_as_intended(self):
        self.assertClean("def lookup(key, _cache={}):  # molty: cache\n    return _cache.setdefault(key, len(_cache))\n")
        self.assertClean("def lookup(\n    key,\n    _cache={},  # molty: cache\n):\n    return _cache\n")

    def test_molty_cache_on_another_line_does_not_count(self):
        self.assertWarning("# molty: cache\ndef lookup(key, _cache={}):\n    return _cache\n", "MOL1004")
        self.assertWarning("def lookup(  # molty: cache\n    key,\n    _cache={},\n):\n    return _cache\n", "MOL1004")


class RewriteDefaults(MoltyTestCase):
    """moltyc --rewrite-defaults: a fresh object per call, recorded for --explain."""

    def test_a_default_list_is_fresh_on_every_call(self):
        mod = load(SHARED, rewrite_defaults=True)
        self.assertEqual(mod.add(1), [1])
        self.assertEqual(mod.add(2), [2])

    def test_the_rewrite_is_explained_not_warned(self):
        result = self.assertClean("def f(items: list = []):\n    return items\n", rewrite_defaults=True)
        self.assertEqual(
            [e.message for e in result.explanations], ["mutable default of 'items' ([]) is now created on every call"]
        )
        self.assertIn("def f(items: list | None = None):", result.code)

    def test_molty_cache_is_still_left_alone(self):
        source = "def lookup(key, _cache={}):  # molty: cache\n    return _cache\n"
        self.assertEqual(self.assertClean(source, rewrite_defaults=True).code, HEADER + source)

    def test_a_default_that_reads_other_names_is_not_moved(self):
        # Moved into the body, [UNIT] would raise NameError and [seq] would read seq at call time.
        source = """
            class C:
                UNIT = 10
                def scaled(self, factor=[UNIT]):
                    return factor
            seq = 1
            def ticket(ids=[seq]):
                return ids
            seq = 2
            """
        result = self.assertCompiles(source, rewrite_defaults=True)
        messages = [w.message for w in result.warnings]
        self.assertEqual([w.code for w in result.warnings], ["MOL1004", "MOL1004"])
        self.assertIn("because it uses 'UNIT'", messages[0])
        self.assertIn("because it uses 'seq'", messages[1])
        self.assertEqual(result.explanations, [])
        mod = load(source, rewrite_defaults=True)
        self.assertEqual(mod.C().scaled(), [10])
        self.assertEqual(mod.ticket(), [1])

    def test_a_default_whose_name_the_function_rebinds_is_not_moved(self):
        # In the body, 'list' is the local assigned below and 'set' is the parameter.
        source = """
            def a(x=list()):
                list = [9]
                return x, list
            def b(items=set(), set=None):
                return items
            """
        result = self.assertCompiles(source, rewrite_defaults=True)
        self.assertEqual([w.code for w in result.warnings], ["MOL1004", "MOL1004"])
        self.assertIn("because it uses 'list'", result.warnings[0].message)
        self.assertIn("because it uses 'set'", result.warnings[1].message)
        mod = load(source, rewrite_defaults=True)
        self.assertEqual(mod.a(), ([], [9]))
        self.assertEqual(mod.b(), set())

    def test_builtins_imports_and_its_own_loop_variables_are_moved(self):
        source = """
            import collections
            def a(x=[1, 2]): return x
            def b(x=collections.deque()): return x
            def c(x=[i * i for i in range(3)]): return x
            def d(x=list(range(2))): return x
            """
        result = self.assertClean(source, rewrite_defaults=True)
        self.assertEqual(len(result.explanations), 4)
        mod = load(source, rewrite_defaults=True)
        self.assertEqual(mod.c(), [0, 1, 4])
        self.assertIsNot(mod.a(), mod.a())

    def test_a_lambda_cannot_be_rewritten(self):
        result = self.assertWarning("f = lambda x=[]: x\n", "MOL1004", rewrite_defaults=True)
        self.assertIn("--rewrite-defaults cannot rewrite a lambda", result.warnings[0].message)

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
            ''',
            rewrite_defaults=True,
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
        mod = load("def add(x, items=[]):\n    items.append(x)\n    return items\n", rewrite_defaults=True)
        mine = [0]
        self.assertIs(mod.add(1, mine), mine)
        self.assertEqual(mine, [0, 1])


if __name__ == "__main__":
    unittest.main()
