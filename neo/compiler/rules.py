"""Neo-specific checks on the parsed tree.

Codes:

    NEO1001  a ``val`` name is rebound
    NEO1002  a ``val`` parameter is rebound, or mutated through a built-in mutating method
    NEO1003  a malformed ``val``/``var`` declaration
    NEO1004  (warning) a mutable default argument, shared by every call
    NEO2001  a ``compute`` function calls a ``task``
    NEO2002  (warning) ``threading.Thread`` created inside a ``task`` or ``compute``
    NEO2003  (warning) ``asyncio.create_task`` called outside ``scope()``
    NEO2004  a ``compute`` argument or return type cannot be pickled
    NEO2005  a ``compute def`` that worker processes cannot import

Ordinary type checking is left to mypy and pyright.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import NamedTuple

from neo.compiler.tokens import Prepared

ERROR = "error"
WARNING = "warning"

# Mutating methods of the built-in list, dict and set.  NEO1002 looks at
# exactly these names and nothing else; there is no ownership model.
MUTATING_METHODS: dict[str, tuple[str, ...]] = {
    "append": ("list",),
    "extend": ("list",),
    "insert": ("list",),
    "sort": ("list",),
    "reverse": ("list",),
    "remove": ("list", "set"),
    "pop": ("list", "dict", "set"),
    "clear": ("list", "dict", "set"),
    "update": ("dict", "set"),
    "popitem": ("dict",),
    "setdefault": ("dict",),
    "add": ("set",),
    "discard": ("set",),
    "difference_update": ("set",),
    "intersection_update": ("set",),
    "symmetric_difference_update": ("set",),
    "__setitem__": ("list", "dict"),
    "__delitem__": ("list", "dict"),
    "__iadd__": ("list",),
    "__imul__": ("list",),
    "__ior__": ("dict", "set"),
    "__iand__": ("set",),
    "__isub__": ("set",),
    "__ixor__": ("set",),
}

NEO_SCOPE_NAMES = frozenset({"scope", "neo.scope", "neo.runtime.scope"})

# Defaults that build a new mutable object; Python evaluates them only once.
MUTABLE_FACTORIES = frozenset({
    "list", "dict", "set", "bytearray",
    "collections.defaultdict", "collections.OrderedDict", "collections.Counter", "collections.deque",
})
_MUTABLE_DISPLAYS = (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: str
    message: str
    line: int
    col: int  # 0-based character column in the original source

    def format(self, filename: str) -> str:
        return f"{filename}:{self.line}:{self.col + 1}: {self.severity} {self.code}: {self.message}"


# --- scopes and name resolution --------------------------------------------


@dataclass(eq=False)
class Binding:
    name: str
    node: ast.AST  # where the name is bound; used for positions
    kind: str  # assign, del, param, def, class, import, except, match, walrus, type_param
    target: str | None = None  # the dotted name an import binds


class Scope:
    def __init__(self, node: ast.AST, kind: str, parent: Scope | None) -> None:
        self.node = node
        self.kind = kind  # module, function, class, comprehension
        self.parent = parent
        self.bindings: dict[str, list[Binding]] = {}
        self.globals: set[str] = set()
        self.nonlocals: set[str] = set()

    def bind(self, binding: Binding) -> None:
        self.bindings.setdefault(binding.name, []).append(binding)


def defaults(args: ast.arguments) -> list[tuple[ast.arg, ast.expr]]:
    """Each parameter that has a default, paired with it."""
    positional = [*args.posonlyargs, *args.args]
    pairs = list(zip(positional[len(positional) - len(args.defaults) :], args.defaults))
    pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
    return pairs


def all_args(args: ast.arguments) -> list[ast.arg]:
    found = [*args.posonlyargs, *args.args]
    if args.vararg:
        found.append(args.vararg)
    found += args.kwonlyargs
    if args.kwarg:
        found.append(args.kwarg)
    return found


class Names:
    """Python's scoping rules, just far enough to tell what a name refers to."""

    def __init__(self, tree: ast.Module) -> None:
        self.module = Scope(tree, "module", None)
        self.scopes: list[Scope] = [self.module]
        self.scope_of: dict[ast.AST, Scope] = {tree: self.module}  # node -> scope it opens
        self.def_scope: dict[ast.AST, Scope] = {}  # def/class node -> scope its name is bound in
        self.load_scope: dict[ast.Name, Scope] = {}
        self.loads: dict[str, list[ast.Name]] = {}
        _ScopeBuilder(self).visit_all(tree.body)
        self._apply_declarations()

    def _apply_declarations(self) -> None:
        # Bindings under ``global``/``nonlocal`` belong to the scope that owns the name.
        for scope in self.scopes:
            for name in scope.globals:
                for binding in scope.bindings.pop(name, []):
                    self.module.bind(binding)
            for name in scope.nonlocals:
                owner = scope.parent
                while owner is not None and (
                    owner.kind != "function" or name in owner.nonlocals or name not in owner.bindings
                ):
                    owner = owner.parent
                moved = scope.bindings.pop(name, [])
                if owner is not None:
                    for binding in moved:
                        owner.bind(binding)

    def owner(self, name: str, scope: Scope) -> Scope | None:
        """The scope that binds ``name`` as seen from ``scope``; None for builtins."""
        current: Scope | None = scope
        while current is not None:
            if current is scope or current.kind != "class":
                if name in current.globals:
                    return self.module if name in self.module.bindings else None
                if name not in current.nonlocals and name in current.bindings:
                    return current
            current = current.parent
        return None

    def qualified_name(self, node: ast.AST, scope: Scope | None = None) -> str | None:
        """``threading.Thread`` for ``Thread`` imported from threading, ``list`` for
        the builtin, None when the name is bound to something we cannot follow."""
        if isinstance(node, ast.Name):
            where = self.load_scope.get(node, scope)
            if where is None:
                return None
            owner = self.owner(node.id, where)
            if owner is None:
                return node.id
            targets = {b.target for b in owner.bindings[node.id]}
            if len(targets) == 1 and None not in targets:
                return targets.pop()
            return None
        if isinstance(node, ast.Attribute):
            base = self.qualified_name(node.value, scope)
            return f"{base}.{node.attr}" if base else None
        return None


class _ScopeBuilder(ast.NodeVisitor):
    def __init__(self, names: Names) -> None:
        self.names = names
        self.current = names.module

    def visit_all(self, nodes) -> None:
        for node in nodes:
            self.visit(node)

    def _bind(self, name: str, node: ast.AST, kind: str, scope: Scope | None = None, target: str | None = None) -> None:
        (scope or self.current).bind(Binding(name, node, kind, target))

    def _enter(self, node: ast.AST, kind: str) -> Scope:
        outer = self.current
        self.current = Scope(node, kind, outer)
        self.names.scopes.append(self.current)
        self.names.scope_of[node] = self.current
        return outer

    def _signature(self, args: ast.arguments) -> None:
        self.visit_all(args.defaults)
        self.visit_all(d for d in args.kw_defaults if d is not None)
        self.visit_all(a.annotation for a in all_args(args) if a.annotation is not None)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.visit_all(node.decorator_list)
        self._signature(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        self._bind(node.name, node, "def")
        self.names.def_scope[node] = self.current
        outer = self._enter(node, "function")
        for param in getattr(node, "type_params", ()):
            self._bind(param.name, param, "type_param")
        for arg in all_args(node.args):
            self._bind(arg.arg, arg, "param")
        self.visit_all(node.body)
        self.current = outer

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._signature(node.args)
        outer = self._enter(node, "function")
        for arg in all_args(node.args):
            self._bind(arg.arg, arg, "param")
        self.visit(node.body)
        self.current = outer

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.visit_all(node.decorator_list)
        self.visit_all(node.bases)
        self.visit_all(node.keywords)
        self._bind(node.name, node, "class")
        self.names.def_scope[node] = self.current
        outer = self._enter(node, "class")
        for param in getattr(node, "type_params", ()):
            self._bind(param.name, param, "type_param")
        self.visit_all(node.body)
        self.current = outer

    def _comprehension(self, node, *results: ast.expr) -> None:
        self.visit(node.generators[0].iter)
        outer = self._enter(node, "comprehension")
        for i, generator in enumerate(node.generators):
            if i:
                self.visit(generator.iter)
            self.visit(generator.target)
            self.visit_all(generator.ifs)
        self.visit_all(results)
        self.current = outer

    def visit_ListComp(self, node) -> None:
        self._comprehension(node, node.elt)

    visit_SetComp = visit_GeneratorExp = visit_ListComp

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._comprehension(node, node.key, node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        scope = self.current
        while scope.kind == "comprehension":
            scope = scope.parent
        self._bind(node.target.id, node.target, "walrus", scope=scope)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.names.load_scope[node] = self.current
            self.names.loads.setdefault(node.id, []).append(node)
        else:
            self._bind(node.id, node, "del" if isinstance(node.ctx, ast.Del) else "assign")

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is None and isinstance(node.target, ast.Name):
            self.visit(node.annotation)  # ``x: int`` declares x but binds nothing
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                self._bind(alias.asname, node, "import", target=alias.name)
            else:
                top = alias.name.partition(".")[0]
                self._bind(top, node, "import", target=top)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        prefix = "." * node.level + (f"{node.module}." if node.module else "")
        for alias in node.names:
            if alias.name != "*":
                self._bind(alias.asname or alias.name, node, "import", target=prefix + alias.name)

    def visit_Global(self, node: ast.Global) -> None:
        self.current.globals.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.current.nonlocals.update(node.names)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._bind(node.name, node, "except")
        self.generic_visit(node)

    def visit_MatchAs(self, node) -> None:
        if node.name:
            self._bind(node.name, node, "match")
        self.generic_visit(node)

    visit_MatchStar = visit_MatchAs

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self._bind(node.rest, node, "match")
        self.generic_visit(node)


# --- picklability of compute signatures -------------------------------------

PICKLABLE, UNPICKLABLE, UNKNOWN = "picklable", "unpicklable", "unknown"

_PICKLABLE_TYPES = frozenset({
    "int", "float", "complex", "bool", "str", "bytes", "bytearray", "range", "slice",
    "decimal.Decimal", "fractions.Fraction", "uuid.UUID",
    "datetime.date", "datetime.datetime", "datetime.time", "datetime.timedelta", "datetime.timezone",
    "pathlib.Path", "pathlib.PurePath", "pathlib.PosixPath", "pathlib.PurePosixPath",
    "pathlib.WindowsPath", "pathlib.PureWindowsPath",
})
# Picklable when every type argument is.
_PICKLABLE_CONTAINERS = frozenset({
    "list", "dict", "set", "frozenset", "tuple",
    "typing.List", "typing.Dict", "typing.Set", "typing.FrozenSet", "typing.Tuple",
    "typing.OrderedDict", "typing.Counter", "typing.Deque",
    "collections.OrderedDict", "collections.Counter", "collections.deque",
})
# Never picklable, whatever the value.
_UNPICKLABLE_TYPES = frozenset({
    "threading.Lock", "threading.RLock", "threading.Condition", "threading.Semaphore",
    "threading.BoundedSemaphore", "threading.Event", "threading.Thread", "queue.Queue",
    "asyncio.Future", "asyncio.Task", "asyncio.Lock", "asyncio.Event", "asyncio.Queue",
    "asyncio.AbstractEventLoop", "socket.socket", "sqlite3.Connection", "sqlite3.Cursor",
    "io.TextIOWrapper", "io.BufferedReader", "io.BufferedWriter", "io.FileIO",
    "types.ModuleType", "types.FrameType", "types.TracebackType", "types.GeneratorType",
    "types.CoroutineType", "typing.Generator", "typing.AsyncGenerator", "typing.Coroutine",
    "collections.abc.Generator", "collections.abc.AsyncGenerator", "collections.abc.Coroutine",
})
_ENUM_BASES = frozenset({"enum.Enum", "enum.IntEnum", "enum.StrEnum", "enum.Flag", "enum.IntFlag"})
_RECORD_BASES = frozenset({"typing.NamedTuple", "typing.TypedDict"})


def _union(verdicts: list[str]) -> str:
    if all(v == PICKLABLE for v in verdicts):
        return PICKLABLE
    if all(v == UNPICKLABLE for v in verdicts):
        return UNPICKLABLE
    return UNKNOWN


def _every(verdicts: list[str]) -> str:
    if UNPICKLABLE in verdicts:
        return UNPICKLABLE
    return PICKLABLE if all(v == PICKLABLE for v in verdicts) else UNKNOWN


class _Picklability:
    """Decide from a type hint alone whether values of that type pickle."""

    def __init__(self, names: Names) -> None:
        self.names = names

    def classify(self, node: ast.expr | None, scope: Scope, seen: frozenset = frozenset()) -> str:
        if node is None:
            return UNKNOWN
        if isinstance(node, ast.Constant):
            if node.value is None or node.value is Ellipsis:
                return PICKLABLE
            if isinstance(node.value, str):
                try:
                    return self.classify(ast.parse(node.value, mode="eval").body, scope, seen)
                except SyntaxError:
                    return UNKNOWN
            return UNKNOWN
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return _union([self.classify(node.left, scope, seen), self.classify(node.right, scope, seen)])
        if isinstance(node, ast.Subscript):
            base = self.names.qualified_name(node.value, scope)
            args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            if base == "typing.Literal":
                return PICKLABLE
            if base == "typing.Annotated":
                return self.classify(args[0], scope, seen)
            if base == "typing.Optional":
                return _union([PICKLABLE, self.classify(args[0], scope, seen)])
            if base == "typing.Union":
                return _union([self.classify(arg, scope, seen) for arg in args])
            if base in _PICKLABLE_CONTAINERS:
                return _every([self.classify(arg, scope, seen) for arg in args])
            return UNPICKLABLE if base in _UNPICKLABLE_TYPES else UNKNOWN
        if isinstance(node, (ast.Name, ast.Attribute)):
            base = self.names.qualified_name(node, scope)
            if base in _PICKLABLE_TYPES or base in _PICKLABLE_CONTAINERS:
                return PICKLABLE
            if base in _UNPICKLABLE_TYPES:
                return UNPICKLABLE
            if base is None and isinstance(node, ast.Name):
                return self._module_class(node, scope, seen)
        return UNKNOWN

    def _module_class(self, node: ast.Name, scope: Scope, seen: frozenset) -> str:
        # Dataclasses, NamedTuples, TypedDicts and enums defined at module level.
        names = self.names
        where = names.load_scope.get(node, scope)
        if names.owner(node.id, where) is not names.module:
            return UNKNOWN
        bindings = names.module.bindings[node.id]
        if len(bindings) != 1 or bindings[0].kind != "class":
            return UNKNOWN
        cls = bindings[0].node
        if cls in seen:
            return PICKLABLE
        bases = {names.qualified_name(base, names.module) for base in cls.bases}
        if bases & _ENUM_BASES:
            return PICKLABLE
        decorators = {
            names.qualified_name(d.func if isinstance(d, ast.Call) else d, names.module)
            for d in cls.decorator_list
        }
        if "dataclasses.dataclass" not in decorators and not bases & _RECORD_BASES:
            return UNKNOWN
        body_scope = names.scope_of[cls]
        fields = [
            stmt.annotation
            for stmt in cls.body
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            and not self._is_classvar(stmt.annotation, body_scope)
        ]
        return _every([self.classify(f, body_scope, seen | {cls}) for f in fields])

    def _is_classvar(self, node: ast.expr, scope: Scope) -> bool:
        base = node.value if isinstance(node, ast.Subscript) else node
        return self.names.qualified_name(base, scope) == "typing.ClassVar"


# --- the checks --------------------------------------------------------------


@dataclass
class ComputeInfo:
    node: ast.FunctionDef
    check_args: tuple[str, ...]  # parameters whose picklability is only known at run time
    check_result: bool


@dataclass
class MutableDefault:
    func: ast.FunctionDef | ast.AsyncFunctionDef
    arg: ast.arg
    default: ast.expr


@dataclass
class Analysis:
    names: Names
    diagnostics: list[Diagnostic] = field(default_factory=list)
    val_decls: list[ast.Assign | ast.AnnAssign] = field(default_factory=list)
    tasks: list[ast.AsyncFunctionDef] = field(default_factory=list)
    computes: list[ComputeInfo] = field(default_factory=list)
    scope_names: list[ast.Name] = field(default_factory=list)  # bare ``scope`` -> neo.runtime.scope
    mutable_defaults: list[MutableDefault] = field(default_factory=list)  # to rewrite (--rewrite-defaults)

    @property
    def failed(self) -> bool:
        return any(d.severity == ERROR for d in self.diagnostics)


class _Context(NamedTuple):
    task: ast.AsyncFunctionDef | None
    compute: ast.FunctionDef | None
    in_scope: bool


_AUGMENTED = {
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.MatMult: "@", ast.Div: "/", ast.FloorDiv: "//",
    ast.Mod: "%", ast.Pow: "**", ast.LShift: "<<", ast.RShift: ">>", ast.BitOr: "|",
    ast.BitXor: "^", ast.BitAnd: "&",
}


def _declared_names(target: ast.expr) -> list[ast.Name] | None:
    if isinstance(target, ast.Name):
        return [target]
    if isinstance(target, ast.Starred):
        return _declared_names(target.value)
    if isinstance(target, ast.Tuple):
        found: list[ast.Name] = []
        for elt in target.elts:
            names = _declared_names(elt)
            if names is None:
                return None
            found += names
        return found
    return None


def _local_nodes(body: list[ast.stmt]):
    """Walk a function body without descending into nested functions or classes."""
    stack = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


class _Analyzer:
    def __init__(self, tree: ast.Module, prepared: Prepared, rewrite_defaults: bool) -> None:
        self.tree = tree
        self.prepared = prepared
        self.rewrite_defaults = rewrite_defaults
        self.names = Names(tree)
        self.result = Analysis(self.names)
        self.parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        self.val_targets: set[ast.Name] = set()
        self.val_params: list[tuple[ast.arg, ast.FunctionDef | ast.AsyncFunctionDef]] = []
        self.task_nodes: set[ast.AST] = set()
        self.compute_nodes: list[ast.FunctionDef] = []

    def run(self) -> Analysis:
        self._match_markers()
        self._check_val_names()
        self._check_val_params()
        self._check_mutable_defaults()
        self._check_computes()
        self._visit(self.tree, _Context(None, None, False))
        self._collect_scope_names()
        self.result.diagnostics.sort(key=lambda d: (d.line, d.col, d.code))
        return self.result

    def _report(self, code: str, severity: str, where: ast.AST | tuple[int, int], message: str) -> None:
        line, col = where if isinstance(where, tuple) else (where.lineno, where.col_offset)
        line, col = self.prepared.original_position(line, col)
        self.result.diagnostics.append(Diagnostic(code, severity, message, line, col))

    # markers left by the pre-pass ------------------------------------------------

    def _match_markers(self) -> None:
        prepared = self.prepared
        found: dict[str, set[tuple[int, int]]] = {"val": set(), "var": set()}
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                target = node.targets[0] if isinstance(node, ast.Assign) else node.target
                key = (target.lineno, target.col_offset)
                for kind, keys in (("val", prepared.val_decls), ("var", prepared.var_decls)):
                    if key in keys:
                        found[kind].add(key)
                        self._declaration(node, kind)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = (node.lineno, node.col_offset)
                if isinstance(node, ast.AsyncFunctionDef) and key in prepared.tasks:
                    self.task_nodes.add(node)
                    self.result.tasks.append(node)
                elif isinstance(node, ast.FunctionDef) and key in prepared.computes:
                    self.compute_nodes.append(node)
                for arg in all_args(node.args):
                    if (arg.lineno, arg.col_offset) in prepared.val_params:
                        self.val_params.append((arg, node))
        for kind, keys in (("val", prepared.val_decls), ("var", prepared.var_decls)):
            for key in sorted(keys - found[kind]):
                self._report("NEO1003", ERROR, key, f"'{kind}' must start an assignment, as in '{kind} name = value'")

    def _declaration(self, node: ast.Assign | ast.AnnAssign, kind: str) -> None:
        if isinstance(node, ast.Assign) and len(node.targets) > 1:
            self._report("NEO1003", ERROR, node, f"'{kind}' declares one target; split 'a = b = ...' into separate declarations")
            return
        target = node.targets[0] if isinstance(node, ast.Assign) else node.target
        names = _declared_names(target)
        if names is None:
            self._report("NEO1003", ERROR, target, f"'{kind}' can only declare plain names, not '{ast.unparse(target)}'")
            return
        if kind == "val" and node.value is None:
            self._report("NEO1003", ERROR, node, f"'val {names[0].id}' needs a value; a val can never be assigned later")
            return
        if kind == "val":
            self.result.val_decls.append(node)
            self.val_targets.update(names)

    # NEO1001 ---------------------------------------------------------------------

    def _check_val_names(self) -> None:
        for scope in self.names.scopes:
            for name, bindings in scope.bindings.items():
                declared = [b for b in bindings if b.node in self.val_targets]
                if not declared:
                    continue
                first = min(declared, key=lambda b: (b.node.lineno, b.node.col_offset))
                for binding in bindings:
                    if binding is first:
                        continue
                    if binding.kind == "param":
                        self._report("NEO1001", ERROR, first.node, f"cannot declare val '{name}': it is already a parameter")
                    else:
                        self._report("NEO1001", ERROR, binding.node,
                                     f"cannot rebind '{name}': it is declared val at line {first.node.lineno}")

    # NEO1002 ---------------------------------------------------------------------

    def _check_val_params(self) -> None:
        for arg, func in self.val_params:
            scope = self.names.scope_of[func]
            for binding in scope.bindings.get(arg.arg, []):
                if binding.node is arg:
                    continue
                parent = self.parents.get(binding.node)
                if isinstance(parent, ast.AugAssign) and parent.target is binding.node:
                    message = f"val parameter '{arg.arg}' is modified by '{_AUGMENTED[type(parent.op)]}='"
                else:
                    message = f"cannot rebind val parameter '{arg.arg}'"
                self._report("NEO1002", ERROR, binding.node, message)
            for use in self.names.loads.get(arg.arg, []):
                if self.names.owner(arg.arg, self.names.load_scope[use]) is not scope:
                    continue
                how = self._mutation(use)
                if how:
                    self._report("NEO1002", ERROR, use, f"val parameter '{arg.arg}' is mutated by {how}")

    def _mutation(self, use: ast.Name) -> str | None:
        parent = self.parents.get(use)
        if isinstance(parent, ast.Attribute) and parent.attr in MUTATING_METHODS:
            call = self.parents.get(parent)
            if isinstance(call, ast.Call) and call.func is parent:
                return f".{parent.attr}()"
        if isinstance(parent, ast.Subscript) and parent.value is use:
            if isinstance(parent.ctx, ast.Store):
                return "item assignment (__setitem__)"
            if isinstance(parent.ctx, ast.Del):
                return "item deletion (__delitem__)"
        return None

    # NEO1004 ---------------------------------------------------------------------

    def _check_mutable_defaults(self) -> None:
        cache_lines = self.prepared.cache_lines
        for func in ast.walk(self.tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            for arg, default in defaults(func.args):
                if not self._is_mutable_default(default):
                    continue
                if cache_lines.intersection(range(arg.lineno, default.end_lineno + 1)):
                    continue  # ``# neo: cache``: the sharing is intended
                if self.rewrite_defaults and not isinstance(func, ast.Lambda):
                    self.result.mutable_defaults.append(MutableDefault(func, arg, default))
                    continue
                shown = ast.unparse(default)
                if len(shown) > 24:
                    shown = shown[:21] + "..."
                if isinstance(func, ast.Lambda):
                    hint = "mark it '# neo: cache' if that is intended; --rewrite-defaults cannot rewrite a lambda"
                else:
                    hint = "mark it '# neo: cache' if that is intended, or compile with --rewrite-defaults"
                self._report("NEO1004", WARNING, default,
                             f"default {shown} of '{arg.arg}' is created once and shared by every call; {hint}")

    def _is_mutable_default(self, node: ast.expr) -> bool:
        if isinstance(node, _MUTABLE_DISPLAYS):
            return True
        return isinstance(node, ast.Call) and self.names.qualified_name(node.func) in MUTABLE_FACTORIES

    # NEO2004, NEO2005 ------------------------------------------------------------

    def _check_computes(self) -> None:
        picklability = _Picklability(self.names)
        for func in self.compute_nodes:
            scope = self.names.def_scope[func]
            if scope is not self.names.module:
                self._report("NEO2005", ERROR, func,
                             f"compute '{func.name}' must be defined at module level; worker processes import it by name")
                continue
            if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in _local_nodes(func.body)):
                self._report("NEO2005", ERROR, func,
                             f"compute '{func.name}' cannot be a generator; a generator cannot leave the worker process")
                continue
            check_args = []
            for arg in all_args(func.args):
                verdict = picklability.classify(arg.annotation, scope)
                if verdict == UNPICKLABLE:
                    self._report("NEO2004", ERROR, arg.annotation,
                                 f"argument '{arg.arg}' of compute '{func.name}' is '{ast.unparse(arg.annotation)}', "
                                 "which cannot be pickled into a worker process")
                elif verdict == UNKNOWN:
                    check_args.append(arg.arg)
            verdict = picklability.classify(func.returns, scope)
            if verdict == UNPICKLABLE:
                self._report("NEO2004", ERROR, func.returns,
                             f"compute '{func.name}' returns '{ast.unparse(func.returns)}', "
                             "which cannot be pickled back from a worker process")
            self.result.computes.append(ComputeInfo(func, tuple(check_args), verdict == UNKNOWN))

    # NEO2001, NEO2002, NEO2003 ----------------------------------------------------

    def _visit(self, node: ast.AST, ctx: _Context) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            ctx = _Context(
                task=node if node in self.task_nodes else ctx.task,
                compute=node if node in self.compute_nodes else ctx.compute,
                in_scope=False,
            )
        elif isinstance(node, ast.AsyncWith) and any(self._is_neo_scope(i.context_expr) for i in node.items):
            for item in node.items:
                self._visit(item, ctx)
            for stmt in node.body:
                self._visit(stmt, ctx._replace(in_scope=True))
            return
        elif isinstance(node, ast.Call):
            self._check_call(node, ctx)
        for child in ast.iter_child_nodes(node):
            self._visit(child, ctx)

    def _is_neo_scope(self, expr: ast.expr) -> bool:
        return isinstance(expr, ast.Call) and self.names.qualified_name(expr.func) in NEO_SCOPE_NAMES

    def _check_call(self, call: ast.Call, ctx: _Context) -> None:
        func = call.func
        if ctx.compute is not None and isinstance(func, ast.Name) and self._is_task(func):
            self._report("NEO2001", ERROR, call,
                         f"compute '{ctx.compute.name}' calls task '{func.id}'; a worker process has no event loop "
                         "to run it (call compute from task instead)")
        qualified = self.names.qualified_name(func)
        if qualified == "threading.Thread" and (ctx.task or ctx.compute):
            kind, owner = ("task", ctx.task) if ctx.task else ("compute", ctx.compute)
            self._report("NEO2002", WARNING, call,
                         f"threading.Thread created inside {kind} '{owner.name}'; "
                         "use task for I/O and compute for CPU work instead of a raw thread")
        if qualified == "asyncio.create_task" and not ctx.in_scope:
            self._report("NEO2003", WARNING, call,
                         "asyncio.create_task outside 'async with scope()'; nothing waits for the task, "
                         "so spawn it in a scope instead")

    def _is_task(self, name: ast.Name) -> bool:
        owner = self.names.owner(name.id, self.names.load_scope[name])
        return owner is not None and any(
            b.kind == "def" and b.node in self.task_nodes for b in owner.bindings[name.id]
        )

    def _collect_scope_names(self) -> None:
        for name in self.names.loads.get("scope", []):
            if self.names.owner("scope", self.names.load_scope[name]) is None:
                self.result.scope_names.append(name)


def analyze(tree: ast.Module, prepared: Prepared, rewrite_defaults: bool = False) -> Analysis:
    return _Analyzer(tree, prepared, rewrite_defaults).run()
