"""Step 4: task and compute, NEO2001/NEO2002/NEO2004/NEO2005, pickle checks."""

import asyncio
import inspect
import os
import unittest

from helpers import ComputeTestCase, NeoTestCase, compile_neo, load

from neo.runtime import ComputeFunction, PickleCheckError


class Tasks(NeoTestCase):
    def test_a_task_is_a_coroutine_function(self):
        mod = load(
            """
            import asyncio
            task def double(n: int) -> int:
                await asyncio.sleep(0)
                return n * 2
            class Client:
                task def get(self) -> str:
                    return "ok"
            """
        )
        self.assertTrue(inspect.iscoroutinefunction(mod.double))
        self.assertEqual(asyncio.run(mod.double(2)), 4)
        self.assertEqual(asyncio.run(mod.Client().get()), "ok")

    def test_task_def_reads_as_async_def(self):
        self.assertIn("async def fetch(url) -> bytes:", compile_neo("task def fetch(url) -> bytes:\n    ...\n").code)


class ComputeFunctions(ComputeTestCase):
    def test_compute_runs_in_another_process(self):
        mod = load(
            """
            import os
            compute def worker_pid() -> int:
                return os.getpid()
            task def main() -> int:
                return await worker_pid()
            """
        )
        self.assertIsInstance(mod.worker_pid, ComputeFunction)
        self.assertNotEqual(asyncio.run(mod.main()), os.getpid())

    def test_compute_calling_a_task_is_an_error(self):
        self.assertError(
            "task def fetch():\n    return 1\ncompute def crunch() -> int:\n    return fetch()\n", "NEO2001"
        )

    def test_task_calling_compute_is_fine(self):
        source = (
            "compute def square(n: int) -> int:\n    return n * n\n"
            "task def main() -> int:\n    return await square(7)\n"
        )
        self.assertClean(source)
        self.assertEqual(asyncio.run(load(source).main()), 49)

    def test_compute_must_live_at_module_level(self):
        self.assertError("def outer():\n    compute def inner() -> int:\n        return 1\n", "NEO2005")
        self.assertError("class C:\n    compute def m(self) -> int:\n        return 1\n", "NEO2005")
        self.assertError("compute def gen() -> int:\n    yield 1\n", "NEO2005")

    def test_module_level_compute_is_fine(self):
        self.assertClean("compute def f() -> int:\n    def helper():\n        yield 1\n    return sum(helper())\n")


class Threads(NeoTestCase):
    def test_a_thread_inside_task_or_compute_warns(self):
        self.assertWarning("import threading\ntask def t():\n    threading.Thread(target=print).start()\n", "NEO2002")
        self.assertWarning(
            "from threading import Thread as T\ncompute def c() -> None:\n    T(target=print).start()\n", "NEO2002"
        )

    def test_a_thread_elsewhere_is_quiet(self):
        self.assertClean("import threading\ndef plain():\n    threading.Thread(target=print).start()\n")


class CompileTimePickleChecks(NeoTestCase):
    def test_an_unpicklable_type_is_an_error(self):
        self.assertError("import threading\ncompute def f(lock: threading.Lock) -> None:\n    pass\n", "NEO2004")
        self.assertError(
            "from typing import Generator\ncompute def f() -> Generator[int, None, None]:\n    return iter([])\n",
            "NEO2004",
        )
        self.assertError("import socket\ncompute def f(items: list[socket.socket]) -> None:\n    pass\n", "NEO2004")

    def test_picklable_types_need_no_run_time_check(self):
        result = self.assertClean(
            """
            import enum
            from dataclasses import dataclass
            from typing import Optional

            class Color(enum.Enum):
                RED = 1

            @dataclass
            class Point:
                x: int
                y: float
                tags: list[str]

            compute def f(a: int, b: list[str], c: dict[str, bytes], d: tuple[int, ...],
                          e: Optional[str], p: Point, color: Color, *rest: float, **kw: "str | None") -> list[Point]:
                return [p]
            """
        )
        self.assertIn("@neo.runtime.compute  # neo: runs in a worker process\n", result.code)
        self.assertIn("all arguments and the result are picklable by type", result.explanations[0].message)

    def test_unknown_types_are_left_to_run_time(self):
        result = self.assertClean("compute def f(cb, n: int, obj: object):\n    return cb(n)\n")
        self.assertIn('@neo.runtime.compute(check_args=("cb", "obj"), check_result=True)', result.code)


class RunTimePickleChecks(ComputeTestCase):
    SOURCE = """
        compute def call(fn, x: int) -> int:
            return fn(x)
        compute def make(n: int):
            return lambda: n
        compute def read(config: dict) -> int:
            return config["n"]
        val CONFIG = {"n": 3}
        """

    def setUp(self):
        self.mod = load(self.SOURCE)

    def test_an_unpicklable_argument_fails_at_the_call(self):
        with self.assertRaisesRegex(PickleCheckError, "NEO2004: argument 'fn' of compute 'call'"):
            self.mod.call(lambda x: x, 1)

    def test_a_picklable_argument_goes_through(self):
        self.assertEqual(asyncio.run(self.mod.call(abs, -3)), 3)

    def test_an_unpicklable_result_fails_in_the_worker(self):
        with self.assertRaisesRegex(PickleCheckError, "NEO2004: the result of compute 'make'"):
            asyncio.run(self.mod.make(1))

    def test_a_val_dict_reaches_the_worker(self):
        self.assertEqual(asyncio.run(self.mod.read(self.mod.CONFIG)), 3)


if __name__ == "__main__":
    unittest.main()
