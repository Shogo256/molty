# Python Neo

Python Neo is a strict superset of Python. Every valid Python file is a valid
Neo file. Neo adds a few keywords that turn everyday Python traps into errors
at compile time: shared mutable objects, the wrong kind of concurrency, and
type hints that protect nothing at run time.

`neoc` compiles `app.neo.py` to a plain, readable `app.py` that imports only
`neo.runtime`. Ordinary type checking is left to mypy and pyright, run on the
generated file.

## The trap, side by side

The same ten lines, run as plain Python and compiled by neoc:

```text
# plain Python                               # Python Neo (app.neo.py)
def add(item, items: list = []):             def add(item, items: list = []):
    items.append(item)                           items.append(item)
    return items                                 return items

print(add(1))  # [1]                         print(add(1))  # [1]
print(add(2))  # [1, 2]  <- same list!       print(add(2))  # [2]
print(add(3))  # [1, 2, 3]                   print(add(3))  # [3]

# one list, created when `def` ran           # a fresh list on every call
```

What neoc writes for the Neo side:

```python
def add(item, items: list | None = None):
    if items is None:  # neo: fresh default on every call
        items = []
    items.append(item)
    return items
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
```

`neoc` exits with 1 if any file has errors. Warnings are printed, but the
`.py` file is still written.

## What Neo adds

### `val` and `var`

```python
val config = {"host": "localhost", "ports": [8080, 8081]}
var count = 0
```

* A `val` can never be rebound in its scope (NEO1001). That covers `=`,
  `+=`, `for`, `with ... as`, `import`, `del`, `global` and `nonlocal`.
* Literals bound to a `val` are frozen at compile time, nested ones
  included: `list` becomes `tuple`, `dict` becomes `MappingProxyType`,
  `set` becomes `frozenset`. The annotation follows, so
  `val xs: list[int] = [1]` becomes `xs: tuple[int, ...] = (1,)`.
* Method calls on a `val` are not tracked. `config.append(...)` fails at run
  time with `AttributeError`, because the value is already immutable.
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

### Mutable default arguments

`list`, `dict` and `set` literals, and calls such as `list()` or
`collections.defaultdict(...)`, used as defaults are rewritten to a `None`
default plus a check in the body, as shown above. This is recorded, not
warned about: `neoc --explain` lists each rewrite.

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
  others are cancelled.
* A `compute` that calls a `task` is an error (NEO2001). A `task` that awaits
  a `compute` is fine.
* Arguments and the result of a `compute` must pickle. Types that are
  picklable by their hint (`int`, `str`, `bytes`, containers of them, enums,
  and module-level dataclasses, NamedTuples and TypedDicts) are accepted at
  compile time with no run-time cost. Types that never pickle (`threading.Lock`,
  generators, sockets, ...) are an error (NEO2004). Anything else is checked
  when the call is made, and the result is checked in the worker.
* A `compute def` must be a plain module-level function (NEO2005), because
  worker processes import it by name. As with any `multiprocessing` code,
  start your program under `if __name__ == "__main__":`.
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

Only functions with the decorator are checked. The check is shallow:
`list[int]` checks for `list` but not its elements.

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
| NEO2001 | error   | a `compute` calls a `task`                                   |
| NEO2002 | warning | `threading.Thread` inside a `task` or `compute`              |
| NEO2003 | warning | `asyncio.create_task` outside `scope()`                      |
| NEO2004 | error   | a `compute` argument or result cannot be pickled             |
| NEO2005 | error   | a `compute def` that is nested, a method, or a generator     |

## Limits of this first version

* A `val` has exactly one binding per scope. `if c: val x = 1 else: val x = 2`
  is a NEO1001.
* There is no ownership model. Aliasing a `val` parameter (`other = items;
  other.append(1)`) is not caught.
* The default-argument rewrite also hits defaults that are shared on purpose,
  such as `def lookup(key, _cache={})`. Such a function loses its cache under
  Neo; use `functools.cache` or a module-level dict instead.
* Requires CPython 3.14, the only version tested so far.

## Development

```bash
python -m unittest discover -s tests
```

The package and the tests use only the standard library.

## License

Apache License 2.0. See [LICENSE](LICENSE).
