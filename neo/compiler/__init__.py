"""The Neo compiler: ``.neo.py`` source in, plain Python out.

    pre-pass (tokens) -> ast.parse -> checks (rules) -> emit
"""

from __future__ import annotations

import ast
import tokenize
from dataclasses import dataclass, field

from neo.compiler import emit, rules, tokens
from neo.compiler.emit import Explanation
from neo.compiler.rules import ERROR, WARNING, Diagnostic

__all__ = ["CompileResult", "Diagnostic", "Explanation", "compile_source"]


@dataclass
class CompileResult:
    code: str | None  # None when there are errors
    diagnostics: list[Diagnostic] = field(default_factory=list)
    explanations: list[Explanation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.code is not None

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == ERROR]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == WARNING]


def compile_source(source: str, filename: str = "<neo>") -> CompileResult:
    try:
        prepared = tokens.prepare(source)
    except (SyntaxError, tokenize.TokenError) as exc:
        return CompileResult(None, [_untokenizable(source, filename, exc)])
    try:
        tree = ast.parse(prepared.source, filename)
    except SyntaxError as exc:
        return CompileResult(None, [_syntax_error(exc, prepared, utf8=False)])
    try:
        # Errors the parser lets through, such as 'await' outside an async function.
        compile(tree, filename, "exec", dont_inherit=True)
    except SyntaxError as exc:
        return CompileResult(None, [_syntax_error(exc, prepared, utf8=True)])

    analysis = rules.analyze(tree, prepared)
    if analysis.failed:
        return CompileResult(None, analysis.diagnostics)
    code, explanations = emit.emit(tree, prepared, analysis, filename)
    return CompileResult(code, analysis.diagnostics, explanations)


def _syntax_error(exc: SyntaxError, prepared: tokens.Prepared, utf8: bool) -> Diagnostic:
    line = exc.lineno or 1
    col = max((exc.offset or 1) - 1, 0)
    # The parser reports character offsets; errors from compiling the tree carry utf-8 ones.
    line, col = prepared.original_position(line, col) if utf8 else prepared.original_char_position(line, col)
    return Diagnostic("NEO0001", ERROR, exc.msg, line, col)


def _untokenizable(source: str, filename: str, exc: Exception) -> Diagnostic:
    try:
        ast.parse(source, filename)  # the parser gives the better message
    except SyntaxError as parse_error:
        return Diagnostic("NEO0001", ERROR, parse_error.msg, parse_error.lineno or 1, max((parse_error.offset or 1) - 1, 0))
    return Diagnostic("NEO0001", ERROR, str(exc), 1, 0)
