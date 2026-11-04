"""Write the checked tree back out as plain Python.

The emitter patches the text produced by the pre-pass instead of unparsing
the tree, so the user's comments and layout survive.  Every rewrite is also
recorded as an ``Explanation`` for ``neoc --explain``.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass

from neo.compiler.rules import Analysis, ComputeInfo, MutableDefault
from neo.compiler.tokens import Prepared, split_lines

MAPPING_PROXY = "neo.runtime.MappingProxyType"
MAPPING = "neo.runtime.Mapping"

_CODING = re.compile(r"^[ \t\f]*#.*?coding[:=]")

# Edits at the same offset are applied in this order.
_HEADER, _IMPORT, _CODE = 0, 1, 2


@dataclass(frozen=True)
class Explanation:
    line: int
    col: int  # 0-based character column in the original source
    message: str

    def format(self, filename: str) -> str:
        return f"{filename}:{self.line}:{self.col + 1}: explain: {self.message}"


class _Text:
    """The rewritten source, addressed by ``ast`` positions (line, utf-8 column)."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.lines = split_lines(source)
        self.starts = [0]
        for line in self.lines:
            self.starts.append(self.starts[-1] + len(line))

    def offset(self, line: int, utf8_col: int) -> int:
        text = self.lines[line - 1]
        if not text.isascii():
            utf8_col = len(text.encode("utf-8")[:utf8_col].decode("utf-8", errors="ignore"))
        return self.starts[line - 1] + utf8_col

    def start(self, node: ast.AST) -> int:
        return self.offset(node.lineno, node.col_offset)

    def end(self, node: ast.AST) -> int:
        return self.offset(node.end_lineno, node.end_col_offset)

    def segment(self, node: ast.AST) -> str:
        return self.source[self.start(node) : self.end(node)]

    def line_start(self, line: int) -> int:
        return self.starts[min(line, len(self.lines) + 1) - 1]

    def before(self, node: ast.AST) -> str:
        """The text between the start of the node's line and the node."""
        return self.source[self.line_start(node.lineno) : self.start(node)]

    def starts_line(self, node: ast.AST) -> bool:
        return not self.before(node).strip()

    def indent(self, line: int) -> str:
        text = self.lines[line - 1]
        return text[: len(text) - len(text.lstrip(" \t\f"))]


def _is_docstring(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)


def _first_line(stmt: ast.stmt) -> int:
    decorators = getattr(stmt, "decorator_list", ())
    return min([stmt.lineno, *(d.lineno for d in decorators)])


class _Emitter:
    def __init__(self, tree: ast.Module, prepared: Prepared, analysis: Analysis, filename: str) -> None:
        self.tree = tree
        self.prepared = prepared
        self.analysis = analysis
        self.names = analysis.names
        self.filename = filename
        self.text = _Text(prepared.source)
        first = self.text.lines[0] if self.text.lines else "\n"
        self.newline = "\r\n" if first.endswith("\r\n") else "\r" if first.endswith("\r") else "\n"
        self.edits: list[tuple[int, int, int, int, str]] = []  # start, end, order, sequence, text
        self.explanations: list[Explanation] = []
        self.uses_runtime = False

    def run(self) -> tuple[str, list[Explanation]]:
        for decl in self.analysis.val_decls:
            self._freeze_declaration(decl)
        by_function: dict[ast.AST, list[MutableDefault]] = {}
        for item in self.analysis.mutable_defaults:  # only with --rewrite-defaults
            by_function.setdefault(item.func, []).append(item)
        for items in by_function.values():
            self._rewrite_mutable_defaults(items)
        for task in self.analysis.tasks:
            self._explain(task, f"task '{task.name}' compiles to a coroutine (async def)")
        for info in self.analysis.computes:
            self._decorate_compute(info)
        for name in self.analysis.scope_names:
            self._replace(name, "neo.runtime.scope")
            self._explain(name, "scope() is neo.runtime.scope, a wrapper around asyncio.TaskGroup")
            self.uses_runtime = True
        if self.uses_runtime:
            self._import_runtime()
        self._header()
        self.explanations.sort(key=lambda e: (e.line, e.col))
        return self._apply(), self.explanations

    # editing helpers -------------------------------------------------------------

    def _edit(self, start: int, end: int, text: str, order: int = _CODE) -> None:
        if self.newline != "\n":  # generated lines follow the file's line endings
            text = re.sub(r"(?<!\r)\n", self.newline, text)
        self.edits.append((start, end, order, len(self.edits), text))

    def _replace(self, node: ast.AST, text: str) -> None:
        self._edit(self.text.start(node), self.text.end(node), text)

    def _insert(self, offset: int, text: str, order: int = _CODE) -> None:
        self._edit(offset, offset, text, order)

    def _wrap(self, node: ast.AST, opener: str) -> None:
        self._insert(self.text.start(node), opener)
        self._insert(self.text.end(node), ")")

    def _explain(self, node: ast.AST, message: str) -> None:
        line, col = self.prepared.original_position(node.lineno, node.col_offset)
        self.explanations.append(Explanation(line, col, message))

    def _apply(self) -> str:
        source = self.text.source
        out, cursor = [], 0
        for start, end, _, _, text in sorted(self.edits):
            if start < cursor:
                raise AssertionError(f"overlapping edits at offset {start}")
            out += [source[cursor:start], text]
            cursor = end
        out.append(source[cursor:])
        return "".join(out)

    # val: freeze literals ---------------------------------------------------------

    def _freeze_declaration(self, decl: ast.Assign | ast.AnnAssign) -> None:
        target = decl.targets[0] if isinstance(decl, ast.Assign) else decl.target
        value = decl.value
        if (
            isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple)
            and len(target.elts) == len(value.elts)
            and not any(isinstance(e, ast.Starred) for e in [*target.elts, *value.elts])
        ):
            frozen = [kind for elt in value.elts if (kind := self._freeze(elt))]
        else:
            frozen = [kind] if (kind := self._freeze(value)) else []
        if not frozen:
            return
        if isinstance(decl, ast.AnnAssign):
            self._freeze_annotation(decl.annotation)
        self._explain(decl, f"val {ast.unparse(target)}: {', '.join(frozen)}")

    def _freeze(self, expr: ast.expr) -> str | None:
        """Make a container literal immutable, nested literals included.

        Inner literals are rewritten before outer ones so that, at a shared
        offset, the inner closing bracket comes first.
        """
        text = self.text
        if isinstance(expr, ast.List):
            for elt in expr.elts:
                self._freeze(elt)
            start, end = text.start(expr), text.end(expr)
            self._edit(start, start + 1, "(")
            if len(expr.elts) == 1 and not self._has_trailing_comma(expr.elts[0], end - 1):
                self._insert(text.end(expr.elts[0]), ",")
            self._edit(end - 1, end, ")")
            return "list -> tuple"
        if isinstance(expr, ast.Tuple):
            changed = [kind for elt in expr.elts if (kind := self._freeze(elt))]
            return "items of the tuple frozen" if changed else None
        if isinstance(expr, ast.Set):
            for elt in expr.elts:
                self._freeze(elt)
            self._wrap(expr, "frozenset(")
            return "set -> frozenset"
        if isinstance(expr, ast.Dict):
            for value in expr.values:
                self._freeze(value)
            self._wrap(expr, MAPPING_PROXY + "(")
            self.uses_runtime = True
            return "dict -> MappingProxyType"
        if isinstance(expr, ast.ListComp):
            self._freeze(expr.elt)
            start, end = text.start(expr), text.end(expr)
            self._edit(start, start + 1, "tuple(")
            self._edit(end - 1, end, ")")
            return "list comprehension -> tuple"
        if isinstance(expr, ast.SetComp):
            self._freeze(expr.elt)
            self._wrap(expr, "frozenset(")
            return "set comprehension -> frozenset"
        if isinstance(expr, ast.DictComp):
            self._freeze(expr.value)
            self._wrap(expr, MAPPING_PROXY + "(")
            self.uses_runtime = True
            return "dict comprehension -> MappingProxyType"
        if isinstance(expr, ast.Call):
            called = self.names.qualified_name(expr.func)
            if called == "list":
                self._replace(expr.func, "tuple")
                return "list() -> tuple()"
            if called == "set":
                self._replace(expr.func, "frozenset")
                return "set() -> frozenset()"
            if called == "dict":
                self._wrap(expr, MAPPING_PROXY + "(")
                self.uses_runtime = True
                return "dict() -> MappingProxyType"
        return None

    def _has_trailing_comma(self, last: ast.expr, close: int) -> bool:
        between = self.text.source[self.text.end(last) : close]
        return any("," in line.split("#", 1)[0] for line in between.splitlines())

    def _freeze_annotation(self, node: ast.expr) -> None:
        """Rewrite ``list[int]`` to ``tuple[int, ...]`` and so on, to match the frozen value."""
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            self._freeze_annotation(node.left)
            self._freeze_annotation(node.right)
            return
        if isinstance(node, ast.Subscript):
            base = self.names.qualified_name(node.value)
            args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            if base in ("list", "typing.List") and not isinstance(node.slice, ast.Tuple):
                self._freeze_annotation(node.slice)
                self._replace(node.value, "tuple")
                self._insert(self.text.end(node.slice), ", ...")
            elif base in ("set", "typing.Set"):
                self._freeze_annotation(node.slice)
                self._replace(node.value, "frozenset")
            elif base in ("dict", "typing.Dict") and len(args) == 2:
                self._freeze_annotation(args[1])
                self._replace(node.value, MAPPING)
                self.uses_runtime = True
            elif base in ("tuple", "typing.Tuple", "typing.Union", "typing.Optional"):
                for arg in args:
                    self._freeze_annotation(arg)
            return
        base = self.names.qualified_name(node)
        if base in ("list", "typing.List"):
            self._replace(node, "tuple")
        elif base in ("set", "typing.Set"):
            self._replace(node, "frozenset")
        elif base in ("dict", "typing.Dict"):
            self._replace(node, MAPPING)
            self.uses_runtime = True

    # mutable default arguments ------------------------------------------------------

    def _rewrite_mutable_defaults(self, items: list[MutableDefault]) -> None:
        checks = []
        for item in items:
            arg, default = item.arg, item.default
            expr = self.text.segment(default)
            self._replace(default, "None")
            if arg.annotation is not None and not self._allows_none(arg.annotation):
                self._optional(arg.annotation)
            checks.append((arg.arg, expr))
            self._explain(arg, f"mutable default of '{arg.arg}' ({expr}) is now created on every call")
        self._insert_default_checks(items[0].func, checks)

    def _allows_none(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Constant):
            return node.value is None or (isinstance(node.value, str) and bool(re.search(r"\b(None|Optional|Any)\b", node.value)))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return self._allows_none(node.left) or self._allows_none(node.right)
        if isinstance(node, ast.Subscript):
            base = self.names.qualified_name(node.value)
            args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            if base == "typing.Optional":
                return True
            if base == "typing.Union":
                return any(self._allows_none(arg) for arg in args)
            if base == "typing.Annotated":
                return self._allows_none(args[0])
            return False
        return self.names.qualified_name(node) in ("typing.Any", "object")

    def _optional(self, annotation: ast.expr) -> None:
        """``list`` -> ``list | None`` so the None sentinel type-checks."""
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            quote = self.text.segment(annotation)[0]
            if quote in "'\"" and quote not in annotation.value:
                self._replace(annotation, f"{quote}{annotation.value} | None{quote}")
            else:
                self._replace(annotation, repr(f"{annotation.value} | None"))
        elif isinstance(annotation, (ast.Name, ast.Attribute, ast.Subscript, ast.BinOp)):
            self._insert(self.text.end(annotation), " | None")
        else:
            self._wrap(annotation, "(")
            self._insert(self.text.end(annotation), " | None")

    def _insert_default_checks(self, func: ast.FunctionDef | ast.AsyncFunctionDef, checks: list[tuple[str, str]]) -> None:
        text = self.text
        body = func.body
        outer = text.indent(func.lineno)
        if text.starts_line(body[0]):
            indent = text.before(body[0])
        else:
            indent = outer + ("\t" if "\t" in outer else "    ")
        unit = indent[len(outer) :] if indent.startswith(outer) and len(indent) > len(outer) else "    "
        lines = []
        for name, expr in checks:
            lines.append(f"if {name} is None:  # neo: fresh default on every call")
            lines.append(f"{unit}{name} = {expr}")
        block = ("\n" + indent).join(lines)

        inline = not text.starts_line(body[0])  # ``def f(x=[]): return x``
        start = text.start(body[0])
        if inline:  # move the body onto its own line, dropping the space after the colon
            before = text.before(body[0])
            gap = len(before) - len(before.rstrip())
            self._edit(start - gap, start, "\n" + indent)
        if not _is_docstring(body[0]):
            self._insert(start, block + "\n" + indent)
            return
        if len(body) == 1:
            self._insert(text.end(body[0]), "\n" + indent + block)
        else:
            lead = "" if text.starts_line(body[1]) else "\n" + indent
            self._insert(text.start(body[1]), lead + block + "\n" + indent)

    # compute ---------------------------------------------------------------------

    def _decorate_compute(self, info: ComputeInfo) -> None:
        func = info.node
        line = _first_line(func)
        checked = [f"'{name}'" for name in info.check_args]
        if info.check_result:
            checked.append("the return value")
        options = []
        if info.check_args:
            names = ", ".join(f'"{name}"' for name in info.check_args)
            options.append(f"check_args=({names}{',' if len(info.check_args) == 1 else ''})")
        if info.check_result:
            options.append("check_result=True")
        decorator = "@neo.runtime.compute" + (f"({', '.join(options)})" if options else "")
        comment = "  # neo: runs in a worker process"
        if checked:
            comment += f"; picklability of {', '.join(checked)} is checked at run time"
        self._insert(self.text.line_start(line), self.text.indent(line) + decorator + comment + "\n")
        self.uses_runtime = True
        detail = f"run-time pickle checks for {', '.join(checked)}" if checked else "all arguments and the result are picklable by type"
        self._explain(func, f"compute '{func.name}' runs in the process pool; {detail}")

    # module prologue -----------------------------------------------------------------

    def _import_runtime(self) -> None:
        body = self.tree.body
        index = 1 if body and _is_docstring(body[0]) else 0
        while index < len(body) and isinstance(body[index], ast.ImportFrom) and body[index].module == "__future__":
            index += 1
        if index == 0:
            offset = self.text.line_start(_first_line(body[0]))
        else:
            offset = self.text.line_start(body[index - 1].end_lineno + 1)
        source = self.text.source
        lead = "\n" if offset == len(source) and source and not source.endswith(("\n", "\r")) else ""
        self._insert(offset, lead + "import neo.runtime\n", _IMPORT)

    def _header(self) -> None:
        lines = self.text.lines
        index = 1 if lines and lines[0].startswith("#!") else 0
        if index < len(lines) and index < 2 and _CODING.match(lines[index]):
            index += 1
        name = os.path.basename(self.filename)
        source = f" from {name}" if name and not name.startswith("<") else ""
        offset = self.text.line_start(index + 1)
        lead = "\n" if offset == len(self.text.source) and lines and not lines[-1].endswith(("\n", "\r")) else ""
        self._insert(offset, f"{lead}# Generated by neoc{source}; edit the .neo.py source, not this file.\n", _HEADER)


def emit(tree: ast.Module, prepared: Prepared, analysis: Analysis, filename: str) -> tuple[str, list[Explanation]]:
    return _Emitter(tree, prepared, analysis, filename).run()
