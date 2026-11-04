"""Step 3: val parameters and NEO1002, one test per row of the mutating-method table."""

import unittest

from helpers import NeoTestCase, compile_neo, load

from neo.compiler.rules import MUTATING_METHODS

# One entry per row of MUTATING_METHODS: the explicit call, plus the operator
# spelling for the dunder rows.
EXAMPLES = {
    "append": ["items.append(1)"],
    "extend": ["items.extend([1])"],
    "insert": ["items.insert(0, 1)"],
    "sort": ["items.sort()"],
    "reverse": ["items.reverse()"],
    "remove": ["items.remove(1)"],
    "pop": ["items.pop()"],
    "clear": ["items.clear()"],
    "update": ["items.update({})"],
    "popitem": ["items.popitem()"],
    "setdefault": ["items.setdefault('k', 1)"],
    "add": ["items.add(1)"],
    "discard": ["items.discard(1)"],
    "difference_update": ["items.difference_update({1})"],
    "intersection_update": ["items.intersection_update({1})"],
    "symmetric_difference_update": ["items.symmetric_difference_update({1})"],
    "__setitem__": ["items.__setitem__(0, 1)", "items[0] = 1", "items[0] += 1", "items[1:] = []"],
    "__delitem__": ["items.__delitem__(0)", "del items[0]"],
    "__iadd__": ["items.__iadd__([1])", "items += [1]"],
    "__imul__": ["items.__imul__(2)", "items *= 2"],
    "__ior__": ["items.__ior__({1})", "items |= {1}"],
    "__iand__": ["items.__iand__({1})", "items &= {1}"],
    "__isub__": ["items.__isub__({1})", "items -= {1}"],
    "__ixor__": ["items.__ixor__({1})", "items ^= {1}"],
}


class MutatingMethodTable(NeoTestCase):
    def test_every_row_has_a_test(self):
        self.assertEqual(set(EXAMPLES), set(MUTATING_METHODS))


def _row_test(statements):
    def test(self):
        for statement in statements:
            with self.subTest(statement):
                self.assertError(f"def f(items: val list):\n    {statement}\n", "NEO1002")

    return test


for _method, _statements in EXAMPLES.items():
    setattr(MutatingMethodTable, f"test_row_{_method}", _row_test(_statements))


class ValParameters(NeoTestCase):
    def test_rebinding_is_an_error(self):
        self.assertError("def f(items: val list[int]):\n    items = []\n", "NEO1002")

    def test_reading_is_fine(self):
        self.assertClean(
            "def f(items: val list[int]) -> int:\n"
            "    copy = items.copy()\n"
            "    copy.append(1)\n"
            "    return len(items) + items.count(1) + items.index(0) + items[0]\n"
        )

    def test_mutating_a_plain_parameter_is_fine(self):
        self.assertClean("def f(items: list[int]):\n    items.append(1)\n")

    def test_mutation_from_a_closure_is_caught(self):
        self.assertError("def f(items: val list):\n    def g():\n        items.append(1)\n    g()\n", "NEO1002")
        self.assertError("def f(items: val list):\n    def g():\n        nonlocal items\n        items = []\n", "NEO1002")

    def test_a_shadowing_closure_is_fine(self):
        self.assertClean("def f(items: val list):\n    def g(items):\n        items.append(1)\n    return g\n")

    def test_any_parameter_kind(self):
        for params in ["*, items: val dict", "items: val dict = {}", "items: val dict, /", "x, items: val 'dict'"]:
            with self.subTest(params):
                self.assertError(f"def f({params}):\n    items.update(a=1)\n", "NEO1002")

    def test_val_is_stripped_and_nothing_is_copied(self):
        result = compile_neo("def same(items: val list[int], *, key: val str = 'k'):\n    return items\n")
        self.assertIn("def same(items: list[int], *, key: str = 'k'):", result.code)
        mod = load("def same(items: val list[int]):\n    return items\n")
        mine = [1]
        self.assertIs(mod.same(mine), mine)

    def test_a_type_named_val_is_left_alone(self):
        source = "val = int\ndef f(x: val = 1, y: val | None = None):\n    x.bit_length()\n    return x\n"
        result = self.assertClean(source)
        self.assertTrue(result.code.endswith(source))


if __name__ == "__main__":
    unittest.main()
