"""Step 4: task and compute, NEO2001/NEO2002/NEO2004/NEO2005, pickle checks."""

import asyncio
import inspect
import os
import shutil
import tempfile
import time
import unittest
from concurrent.futures.process import BrokenProcessPool

from helpers import ComputeTestCase, NeoTestCase, compile_neo, load

from neo.runtime import ComputeError, ComputeFunction, PickleCheckError


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


class CoroutineUse(NeoTestCase):
    HEADER = (
        "import asyncio\n"
        "compute def digest(text: str) -> str:\n    return text[::-1]\n"
        "task def fetch(url: str) -> str:\n    return url\n"
    )

    def test_a_coroutine_used_as_a_value_warns(self):
        cases = {
            "def label(t: str) -> str:\n    return f'{t} {digest(t)}'\n": "used as a value",
            "def size(t: str) -> int:\n    return len(t) + digest(t)\n": "used as a value",
            "task def main():\n    fetch('u')\n": "thrown away",
            "task def main():\n    if digest('x'):\n        pass\n": "used as a value",
            "compute def twice(t: str) -> str:\n    return digest(t) * 2\n": "calls compute 'digest'",
            "def show(t: str):\n    print(digest(t))\n": "passed to 'print', which does not run it",
            "def count(t: str) -> int:\n    return len([digest(t)])\n": "passed to 'len', which does not run it",
            "f = digest\ng = f\ndef show(t: str):\n    print(g(t))\n": "compute 'digest' (called as 'g')",
        }
        for body, message in cases.items():
            with self.subTest(body):
                result = self.assertWarning(self.HEADER + body, "NEO2006")
                self.assertIn(message, result.warnings[0].message)

    def test_a_task_called_through_an_alias_from_compute_is_an_error(self):
        result = self.assertError(self.HEADER + "get = fetch\ncompute def c() -> str:\n    return get('u')\n", "NEO2001")
        self.assertIn("task 'fetch' (called as 'get')", result.errors[0].message)

    def test_an_awaited_alias_is_quiet(self):
        self.assertClean(self.HEADER + "f = digest\ntask def main() -> str:\n    return await f('x')\n")

    def test_a_coroutine_that_is_awaited_or_handed_on_is_quiet(self):
        self.assertClean(
            self.HEADER
            + "task def main(urls: list[str]):\n"
            "    a = await digest('x')\n"
            "    b = digest('y')\n"
            "    pending = [fetch(u) for u in urls]\n"
            "    results = await asyncio.gather(*pending, fetch('z'))\n"
            "    async with scope() as s:\n"
            "        s.spawn(digest('w'))\n"
            "    async with asyncio.TaskGroup() as tg:\n"
            "        tg.create_task(fetch('v'))\n"
            "    late = await asyncio.wait_for(fetch('t'), 1)\n"
            "    return a, await b, results, late\n"
            "def entry():\n"
            "    loop = asyncio.new_event_loop()\n"
            "    loop.run_until_complete(fetch('l'))\n"
            "    return asyncio.run(main(['u']))\n"
        )


class JobsThatEndBadly(ComputeTestCase):
    SOURCE = """
        import sys, time
        import neo

        class HTTPError(Exception):
            def __init__(self, status, message):
                super().__init__(f"{status} {message}")

        compute def unbuildable() -> int:
            raise HTTPError(404, "not found")

        compute def plain_error() -> int:
            raise ValueError("plain")

        compute def leave() -> int:
            sys.exit(3)

        compute def slow(n: int) -> int:
            time.sleep(0.3)
            return n

        @neo.isolated
        compute def unbuildable_isolated() -> int:
            raise HTTPError(500, "oops")

        @neo.isolated
        compute def leave_isolated() -> int:
            sys.exit(4)
        """

    def setUp(self):
        self.mod = load(self.SOURCE)

    def test_an_exception_the_caller_cannot_rebuild_fails_only_its_job(self):
        async def main():
            return await asyncio.gather(
                self.mod.unbuildable(), self.mod.slow(1), self.mod.slow(2), return_exceptions=True
            )

        error, one, two = asyncio.run(main())
        self.assertIsInstance(error, ComputeError)
        self.assertIn("HTTPError: 404 not found", str(error))
        self.assertEqual((one, two), (1, 2))
        with self.assertRaisesRegex(ComputeError, "HTTPError: 500 oops"):
            asyncio.run(self.mod.unbuildable_isolated())

    def test_an_ordinary_exception_keeps_its_type(self):
        with self.assertRaisesRegex(ValueError, "plain"):
            asyncio.run(self.mod.plain_error())

    def test_sys_exit_in_a_job_does_not_stop_the_caller(self):
        with self.assertRaisesRegex(ComputeError, r"called sys.exit\(3\)"):
            asyncio.run(self.mod.leave())
        with self.assertRaisesRegex(ComputeError, r"called sys.exit\(4\)"):
            asyncio.run(self.mod.leave_isolated())


class BrokenPool(ComputeTestCase):
    def test_a_crashed_worker_does_not_break_later_calls(self):
        mod = load(
            """
            import os
            compute def crash() -> int:
                os._exit(1)
            compute def cheap() -> int:
                return 1
            task def main() -> list:
                outcomes = []
                for job in (crash, cheap, cheap):
                    try:
                        outcomes.append(await job())
                    except Exception as exc:
                        outcomes.append(type(exc).__name__)
                return outcomes
            """
        )
        self.assertEqual(asyncio.run(mod.main()), ["BrokenProcessPool", 1, 1])


class Isolated(ComputeTestCase):
    SOURCE = """
        import os, time
        import neo

        @neo.isolated
        compute def whoami() -> int:
            return os.getpid()

        @neo.isolated
        compute def fail() -> int:
            raise ValueError("boom in the child")

        @neo.isolated
        compute def crash() -> int:
            os._exit(3)

        @neo.isolated
        compute def stuck(pid_file: str) -> int:
            with open(pid_file, "w") as f:
                f.write(str(os.getpid()))
            time.sleep(60)
            return 0
        """

    def setUp(self):
        self.mod = load(self.SOURCE)

    def test_each_call_runs_in_a_process_of_its_own(self):
        first, second = asyncio.run(self._twice())
        self.assertNotIn(os.getpid(), (first, second))
        self.assertNotEqual(first, second)

    async def _twice(self):
        return await self.mod.whoami(), await self.mod.whoami()

    def test_the_exception_keeps_its_type(self):
        with self.assertRaisesRegex(ValueError, "boom in the child"):
            asyncio.run(self.mod.fail())

    def test_a_crash_is_reported_as_a_broken_process(self):
        with self.assertRaisesRegex(BrokenProcessPool, "exited with code 3"):
            asyncio.run(self.mod.crash())

    def test_a_timed_out_call_kills_and_reaps_its_process(self):
        pid_file = os.path.join(tempfile.mkdtemp(), "pid")
        self.addCleanup(shutil.rmtree, os.path.dirname(pid_file))
        with self.assertRaises(TimeoutError):
            asyncio.run(asyncio.wait_for(self.mod.stuck(pid_file), 2))
        pid = int(open(pid_file).read())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)  # also succeeds for a zombie that was never reaped
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail(f"process {pid} is still there after the timeout")

    def test_isolated_only_applies_to_compute(self):
        self.assertError("import neo\n@neo.isolated\ndef plain():\n    pass\n", "NEO2007")
        self.assertClean("from neo import isolated\n@isolated\ncompute def f() -> int:\n    return 1\n")


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

    def test_a_container_without_item_types_is_left_to_run_time(self):
        result = self.assertClean("compute def f(d: dict, xs: list, t: tuple) -> dict:\n    return d\n")
        self.assertIn('@neo.runtime.compute(check_args=("d", "xs", "t"), check_result=True)', result.code)


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

    def test_a_bare_dict_holding_a_lambda_fails_at_the_call(self):
        with self.assertRaisesRegex(PickleCheckError, "NEO2004: argument 'config' of compute 'read'"):
            self.mod.read({"n": 1, "f": lambda: 0})

    def test_a_val_dict_reaches_the_worker(self):
        self.assertEqual(asyncio.run(self.mod.read(self.mod.CONFIG)), 3)


if __name__ == "__main__":
    unittest.main()
