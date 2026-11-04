"""Step 5: scope(), NEO2003, and task and compute spawned together."""

import asyncio
import os
import unittest

from helpers import ComputeTestCase, compile_neo, load

MIXED = """
    import asyncio
    import os

    compute def pid_and_square(n: int) -> tuple[int, int]:
        return os.getpid(), n * n

    task def label(n: int, delay: float) -> str:
        await asyncio.sleep(delay)
        return f"task-{n}"

    task def main() -> list:
        async with scope() as s:
            s.spawn(label(1, 0.05))
            s.spawn(pid_and_square(3))
            s.spawn(label(2, 0.0))
            s.spawn(pid_and_square(4))
        return s.results()
    """


class Scopes(ComputeTestCase):
    def test_task_and_compute_spawned_together(self):
        first, (pid3, nine), second, (pid4, sixteen) = asyncio.run(load(MIXED).main())
        self.assertEqual((first, second), ("task-1", "task-2"))  # spawn order, not finish order
        self.assertEqual((nine, sixteen), (9, 16))
        self.assertNotIn(os.getpid(), (pid3, pid4))

    def test_scope_compiles_to_the_runtime_task_group(self):
        code = compile_neo(MIXED).code
        self.assertIn("async with neo.runtime.scope() as s:", code)
        self.assertIn("s.spawn(pid_and_square(3))", code)

    def test_results_only_after_the_block(self):
        mod = load(
            """
            task def early():
                async with scope() as s:
                    s.spawn(early_helper())
                    s.results()
            task def early_helper():
                return 1
            task def late():
                async with scope() as s:
                    pass
                s.spawn(early_helper())
            """
        )
        with self.assertRaises(ExceptionGroup) as caught:  # TaskGroup groups the body's error
            asyncio.run(mod.early())
        self.assertRegex(str(caught.exception.exceptions[0]), "results\\(\\) is available once")
        with self.assertRaisesRegex(RuntimeError, "already exited"):
            asyncio.run(mod.late())

    def test_a_failure_cancels_the_rest(self):
        mod = load(
            """
            import asyncio
            cancelled = []
            task def slow():
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    cancelled.append(True)
                    raise
            task def boom():
                raise ValueError("boom")
            task def main():
                async with scope() as s:
                    s.spawn(slow())
                    s.spawn(boom())
            """
        )
        with self.assertRaises(ExceptionGroup) as caught:
            asyncio.run(mod.main())
        self.assertIsInstance(caught.exception.exceptions[0], ValueError)
        self.assertEqual(mod.cancelled, [True])

    def test_plain_with_is_rejected(self):
        mod = load("def f():\n    with scope():\n        pass\n")
        with self.assertRaisesRegex(TypeError, "async with"):
            mod.f()


class CreateTask(ComputeTestCase):
    def test_create_task_outside_a_scope_warns(self):
        self.assertWarning(
            "import asyncio\ntask def f(): pass\ntask def main():\n    asyncio.create_task(f())\n", "NEO2003"
        )

    def test_create_task_inside_a_scope_is_quiet(self):
        self.assertClean(
            "from asyncio import create_task\ntask def f(): pass\n"
            "task def main():\n    async with scope() as s:\n        create_task(f())\n"
        )

    def test_a_nested_function_does_not_inherit_the_scope(self):
        self.assertWarning(
            "import asyncio\ntask def main():\n    async with scope() as s:\n"
            "        def later():\n            asyncio.create_task(main())\n",
            "NEO2003",
        )


class ScopeNames(ComputeTestCase):
    def test_a_user_defined_scope_is_left_alone(self):
        source = """
            import asyncio, contextlib
            @contextlib.asynccontextmanager
            async def scope():
                yield "mine"
            task def main():
                async with scope() as s:
                    asyncio.create_task(asyncio.sleep(0))
                return s
            """
        result = self.assertWarning(source, "NEO2003")  # not a Neo scope, so create_task still warns
        self.assertNotIn("neo.runtime", result.code)
        self.assertEqual(asyncio.run(load(source).main()), "mine")

    def test_scope_imported_from_neo(self):
        source = """
            from neo import scope
            task def one():
                return 1
            task def main():
                async with scope() as s:
                    s.spawn(one())
                return s.results()
            """
        self.assertNotIn("neo.runtime.scope", self.assertClean(source).code)
        self.assertEqual(asyncio.run(load(source).main()), [1])


if __name__ == "__main__":
    unittest.main()
