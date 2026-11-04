```text
 _   _ _____ ___
| \ | | ____/ _ \
|  \| |  _|| | | |
| |\  | |__| |_| |
|_| \_|_____\___/
 ~~~~~~~~~~~~~~~~:>   Python Neo
```

# Python Neo

Python Neo is a strict superset of Python. Every valid Python file is a valid
Neo file. Neo adds a few keywords that turn everyday Python traps into errors
at compile time: shared mutable objects, the wrong kind of concurrency, and
type hints that protect nothing at run time.

`neoc` compiles `app.neo.py` to a plain, readable `app.py` that imports only
`neo.runtime`. Ordinary type checking is left to mypy and pyright, run on the
generated file.

## The trap, side by side

The same file, run as plain Python and compiled by neoc:

```text
# plain Python                            # Python Neo
def add(item, items: list = []):          def add(item, items: list = []):
    items.append(item)                        items.append(item)
    return items                              return items

print(add(1))  # [1]                      $ neoc app.neo.py
print(add(2))  # [1, 2]  <- same list     app.neo.py:1:29: warning NEO1004:
print(add(3))  # [1, 2, 3]                  default [] of 'items' is created
                                            once and shared by every call
# nothing tells you                       # neoc tells you before it runs
```

By default neoc does not change what the code means. The `.py` it writes
behaves exactly like the source, and the warning tells you about the trap.
There are two ways to act on it:

```python
def lookup(key, _cache={}):  # neo: cache
    ...                      # the sharing is on purpose: no warning
```

```bash
neoc --rewrite-defaults app.neo.py   # neoc gives every call a fresh list
```

`--rewrite-defaults` changes the meaning on purpose, and in one way only:
every call gets a fresh object. With it, neoc writes:

```python
def add(item, items: list | None = None):
    if items is None:  # neo: fresh default on every call
        items = []
    items.append(item)
    return items
```

## A superset, checked

Every `.py` file in the standard library compiles under neoc without errors.
Each one comes out byte for byte the same, apart from the one-line
generated-file header. We checked 628 files on CPython 3.14 (Ubuntu), and 633,
686 and 734 files on 3.13, 3.12 and 3.11 (python-build-standalone). To check
it on your own Python:

```bash
NEO_STDLIB=1 python -m unittest discover -s tests -p test_stdlib_superset.py
```

## Install and run

Not on PyPI yet. From a checkout:

```bash
pip install -e .
```

```bash
neoc app.neo.py              # writes app.py next to it
neoc src/                    # every *.neo.py under src/
neoc app.neo.py -o -         # print to stdout
neoc app.neo.py --explain    # list every rewrite neoc made
neoc app.neo.py --rewrite-defaults   # fix mutable defaults instead of warning
neoc app.neo.py -q           # no success line
neoc app.neo.py --force      # overwrite an app.py that neoc did not write
neoc --version
```

`neoc` exits with 1 if any file has errors. Warnings are printed, but the
`.py` file is still written. At a terminal, each file that compiles gets one
line on stderr:

```text
~~~~:>  app.neo.py -> app.py  ok (2 warnings)
```

When the output is piped, as in CI, that line is left out, and
`neoc --version` prints a plain `neoc 0.1.0` instead of the logo.

neoc refuses to write a `.py` file, and exits with 1, in these cases:

* the file is a symbolic link
* the file is one of the sources, as with `-o app.neo.py`
* the file is an existing regular file that neoc did not generate (`--force`
  overwrites it); `-o /dev/null`, to see only the diagnostics, is fine

When neoc walks a directory, it skips symbolic links, so it never reads
files outside the tree. A file named on the command line is read even if it
is a link.

neoc also opens the file with `O_NOFOLLOW`, so a link put in place after the
checks above cannot redirect the write either. Windows has no `O_NOFOLLOW`;
there, the check before writing is the only guard.

## What Neo adds

### `val` and `var`

```python
val config = {"host": "localhost", "ports": [8080, 8081]}
var count = 0
```

* A `val` can never be rebound in its scope (NEO1001). That covers `=`,
  `+=`, `for`, `with ... as`, `import`, `del`, `global` and `nonlocal`.
* A `val` may be declared once in each branch of an `if`/`elif`/`else` or a
  `match`, since only one of them runs:

  ```python
  if verbose:
      val LEVEL = "debug"
  else:
      val LEVEL = "info"
  ```
* Literals bound to a `val` are frozen at compile time, nested ones
  included: `list` becomes `tuple`, `dict` becomes `MappingProxyType`,
  `set` becomes `frozenset`. The annotation follows, so
  `val xs: list[int] = [1]` becomes `xs: tuple[int, ...] = (1,)`.
* Changing a frozen `val` in place, as in `PRICES.append(3)` or
  `CONFIG["port"] = 80`, is an error (NEO1005); it would fail at run time
  anyway. So is changing a frozen part of it, as in `CONFIG["db"]["host"] = "y"`,
  and `ROUTES[key].append(x)` when every item of `ROUTES` is a frozen literal.
  A value you build up step by step belongs in a `var`.
* A `val` bound to any other object, such as `val cache = LRUCache()`, only
  promises not to be rebound. Its methods can be called as usual.
* `json.dumps` does not accept a frozen dict. Convert it first:
  `json.dumps(dict(config))`.
* `var` and plain `x = ...` work exactly as in Python.

### `val` parameters

```python
def total(items: val list[int]) -> int:
    items.append(0)    # NEO1002
    return sum(items)
```

Rebinding a `val` parameter, or calling one of the built-in mutating methods
of `list`, `dict` and `set` on it (`append`, `update`, `items[k] = v`, `+=`,
...), is an error. The full list is `MUTATING_METHODS` in
`neo/compiler/rules.py`. Callers pass the object as usual; nothing is copied.

Because the function promises not to change the argument, neoc widens its
annotation to the read-only type: `list[int]` becomes `Sequence[int]`,
`dict[K, V]` becomes `Mapping[K, V]`, and `set[T]` becomes `AbstractSet[T]`.
Frozen `val` values (a tuple, a `MappingProxyType`, a `frozenset`) can then be
passed in, and both `@neo.checked` and mypy accept them.

### Mutable default arguments

A `list`, `dict` or `set` literal, or a call such as `list()` or
`collections.defaultdict(...)`, used as a default is a NEO1004 warning. The
code is left alone.

* A `# neo: cache` comment on the parameter's line marks the sharing as
  intended. The warning goes away, and the parameter is never rewritten.
* `neoc --rewrite-defaults` rewrites each unmarked default to a `None`
  default plus a check in the body, as shown above. `--explain` lists every
  rewrite.
* It only moves a default whose names are builtins, module-level imports, or
  the loop variables of a comprehension inside the default. Any other name
  would mean something else in the function body: a class attribute is not
  visible there, and a variable would be read at call time instead of when
  `def` ran. Such a default keeps the warning, and so does a default of a
  `lambda`.

### `task`, `compute` and `scope`

```python
task def fetch(url: str) -> bytes: ...        # I/O-bound: an asyncio coroutine
compute def digest(data: bytes) -> bytes: ... # CPU-bound: runs in a process pool

task def main(urls: list[str]) -> list[bytes]:
    async with scope() as s:
        for url in urls:
            s.spawn(fetch(url))
    pages = s.results()                   # everything spawned has finished here
    return [await digest(page) for page in pages]
```

* `task def` compiles to `async def`.
* Calling a `compute` function returns a coroutine that runs the body in a
  shared `ProcessPoolExecutor`, so you can `await` it or `spawn` it.
* `scope()` wraps `asyncio.TaskGroup`. `s.results()` returns the results in
  spawn order once the block has exited. If one spawned task fails, the
  others are cancelled, the block raises an `ExceptionGroup`, and
  `s.results()` is no longer usable. Partial results have to be collected by
  the tasks themselves.
* A `compute` that calls a `task` is an error (NEO2001). A `task` that awaits
  a `compute` is fine.
* A call to a `task` or `compute` whose coroutine never gets run is a warning
  (NEO2006). That covers using the result in an f-string or an expression,
  dropping it, and passing it to an ordinary function such as `print(work(n))`.
  Awaiting it, assigning it, returning it, and passing it to something that
  runs coroutines (`s.spawn`, `asyncio.run`, `asyncio.gather`,
  `asyncio.wait_for`, `create_task`, `run_until_complete`, ...) are fine; to
  hand it to a helper of your own, assign it to a name first. A `compute`
  calling another `compute` is also NEO2006, because a worker process cannot
  await. The check covers functions defined in the same file, called by their
  own name or through a plain alias such as `f = work`.
* A job that calls `sys.exit()`, or raises an exception the caller cannot
  rebuild (one whose `__init__` takes two arguments, say), fails with
  `neo.runtime.ComputeError` instead. Its message names the original
  exception, and the worker's traceback comes with it. Other jobs are not
  affected, and the caller's program keeps running.
* If a worker process dies (a crash, `os._exit`, running out of memory), the
  calls in flight fail with `BrokenProcessPool`. The next call starts a fresh
  pool. A failed call is not retried, since it may be the one that crashed.
* Cancelling a `compute` call, or letting it time out (`asyncio.wait_for`, a
  failing scope), stops the waiting but not the work. The job runs to the end
  and keeps its worker busy, so later `compute` calls can queue behind jobs
  nobody is waiting for any more.
* For long jobs that must really stop, put `@neo.isolated` on the
  `compute def`. Each call then runs in a process of its own, and cancelling
  or timing out the call kills that process at once, without touching any
  other call. Starting the process costs about 10 ms per call on Linux
  (forkserver) and about 170 ms where only spawn exists, as on Windows. At
  most as many isolated calls run at once as there are CPUs. `@neo.isolated`
  on anything other than a `compute def` is an error (NEO2007).

  ```python
  import neo

  @neo.isolated
  compute def render(job: Job) -> bytes: ...
  ```
* Arguments and the result of a `compute` must pickle. Types that are
  picklable by their hint (`int`, `str`, `bytes`, containers of them with the
  item types written out, enums, and module-level dataclasses, NamedTuples and
  TypedDicts) are accepted at compile time with no run-time cost. A bare
  `dict` or `list` says nothing about its items, so it is checked at run time. Types that never pickle (`threading.Lock`,
  generators, sockets, ...) are an error (NEO2004). Anything else is checked
  when the call is made, and the result is checked in the worker.
* Stopped with SIGTERM (the default of `kill`, systemd and Docker), Python
  skips its exit handlers, so the forkserver and idle workers stay behind.
  Turn SIGTERM into a normal exit when the program starts; Ctrl-C needs
  nothing.

  ```python
  import signal, sys
  signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
  ```
* A `compute def` must be a plain module-level function (NEO2005), because
  worker processes import it by name. As with any `multiprocessing` code,
  start your program under `if __name__ == "__main__":`, and run it from a
  file. Code fed through stdin or a REPL fails with confusing errors, because
  the workers cannot import the main module.
* Creating a `threading.Thread` inside a `task` or `compute` is a warning
  (NEO2002), and so is `asyncio.create_task` outside a scope (NEO2003).
  Neither is forbidden.

### `@neo.checked`

```python
import neo

@neo.checked
def scale(xs: list[int], factor: float) -> list[float]: ...

scale("abc", 2)   # CheckError: scale() argument 'xs' must be list, not str
```

Only functions with the decorator are checked. By default the check covers
the arguments only and is shallow: `list[int]` checks for `list` but not its
elements. Two options go further, at the cost of more work on every call:

```python
@neo.checked(deep=True)      # items of lists, tuples, sets and dicts too
def load(rows: list[dict[str, int]]) -> None: ...
# CheckError: load() argument 'rows'[2]['id'] must be int, not str

@neo.checked(returns=True)   # the return value too
def name() -> str: ...
```

The check follows `isinstance`, as mypy does, so `True` passes where an
`int` is expected, and an `int` passes where a `float` is.

### `NEO_CHECKS=0`

This turns off every run-time check: `@neo.checked` returns the function
unchanged, and `compute` stops checking picklability.

## Diagnostics

| Code    | Kind    | Meaning                                                      |
|---------|---------|--------------------------------------------------------------|
| NEO0001 | error   | syntax error                                                 |
| NEO1001 | error   | a `val` is rebound                                           |
| NEO1002 | error   | a `val` parameter is rebound or mutated                      |
| NEO1003 | error   | malformed `val`/`var` declaration (`val x: int` with no value, `val a.b = 1`, ...) |
| NEO1004 | warning | a mutable default argument, shared by every call             |
| NEO1005 | error   | a frozen `val` is changed in place                           |
| NEO2001 | error   | a `compute` calls a `task`                                   |
| NEO2002 | warning | `threading.Thread` inside a `task` or `compute`              |
| NEO2003 | warning | `asyncio.create_task` outside `scope()`                      |
| NEO2004 | error   | a `compute` argument or result cannot be pickled             |
| NEO2005 | error   | a `compute def` that is nested, a method, or a generator     |
| NEO2006 | warning | a `task` or `compute` called but not awaited or handed on    |
| NEO2007 | error   | `@neo.isolated` on something other than a `compute def`      |

## Limits of this first version

* There is no ownership model. Aliasing a `val` parameter (`other = items;
  other.append(1)`) is not caught.
* Tested on CPython 3.11, 3.12, 3.13 and 3.14. On 3.11, a `@neo.checked`
  task is checked when it is awaited rather than when it is called, because
  `inspect.markcoroutinefunction` only exists from 3.12.

## Development

```bash
python -m unittest discover -s tests
```

The package and the tests use only the standard library. The `neo` package
passes `mypy --strict`.

## License

Apache License 2.0. See [LICENSE](LICENSE).
