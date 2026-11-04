"""Pre-pass: rewrite the Neo keywords into plain Python before ``ast.parse``.

Only these forms are recognized:

* ``val NAME ...`` and ``var NAME ...`` at the start of a statement
* ``NAME: val TYPE`` inside the parameter list of a ``def``
* ``task def`` (becomes ``async def``) and ``compute def`` (becomes ``def``)

Each of them is a pair of adjacent names (or a name followed by ``def``),
which valid Python never contains, so every valid Python file passes through
this stage unchanged.  Edits never add or remove lines; they only shift
columns, and ``PositionMap`` keeps track of that so diagnostics can point at
the original source.
"""

from __future__ import annotations

import io
import keyword
import tokenize
from dataclasses import dataclass, field

# A colon at bracket depth 0 on a line that starts with one of these ends a
# block header, so a statement may follow on the same line (``if x: val y = 1``).
_BLOCK_HEADS = frozenset(
    {"if", "elif", "else", "for", "while", "with", "try", "except", "finally",
     "def", "class", "async", "task", "compute"}
)
_SKIPPED = frozenset({tokenize.COMMENT, tokenize.NL, tokenize.ENCODING})
_LINE_BREAKS = frozenset({tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER})


def split_lines(source: str) -> list[str]:
    """Split like the Python tokenizer does: on ``\\n``, ``\\r\\n`` and ``\\r`` only."""
    return io.StringIO(source, newline="").readlines()


@dataclass(frozen=True)
class _Edit:
    line: int
    start: int  # character column in the original line
    end: int
    text: str


class PositionMap:
    """Translate character columns between the original and the rewritten source."""

    def __init__(self, edits: list[_Edit]) -> None:
        self._by_line: dict[int, list[_Edit]] = {}
        for edit in sorted(edits, key=lambda e: (e.line, e.start)):
            self._by_line.setdefault(edit.line, []).append(edit)

    def to_new(self, line: int, col: int) -> int:
        shift = 0
        for edit in self._by_line.get(line, ()):
            if col >= edit.end:
                shift += len(edit.text) - (edit.end - edit.start)
            elif col >= edit.start:
                return edit.start + shift
            else:
                break
        return col + shift

    def to_old(self, line: int, col: int) -> int:
        shift = 0
        for edit in self._by_line.get(line, ()):
            new_start = edit.start + shift
            new_end = new_start + len(edit.text)
            if col >= new_end:
                shift += len(edit.text) - (edit.end - edit.start)
            elif col >= new_start:
                return edit.start
            else:
                break
        return col - shift


@dataclass
class Prepared:
    """The rewritten source plus where each Neo construct ended up in it.

    Marker positions are ``(line, utf8_column)`` pairs in the rewritten source,
    the same coordinates ``ast`` nodes carry.
    """

    original: str
    source: str
    positions: PositionMap
    val_decls: set[tuple[int, int]] = field(default_factory=set)  # first target of the assignment
    var_decls: set[tuple[int, int]] = field(default_factory=set)
    val_params: set[tuple[int, int]] = field(default_factory=set)  # the ast.arg node
    tasks: set[tuple[int, int]] = field(default_factory=set)  # the AsyncFunctionDef node
    computes: set[tuple[int, int]] = field(default_factory=set)  # the FunctionDef node

    def __post_init__(self) -> None:
        self._original_lines = split_lines(self.original)
        self._lines = split_lines(self.source)

    def original_position(self, line: int, utf8_col: int) -> tuple[int, int]:
        """Map an ``ast`` position back to ``(line, character column)`` in the original."""
        text = self._lines[line - 1] if 0 < line <= len(self._lines) else ""
        col = len(text.encode("utf-8")[:utf8_col].decode("utf-8", errors="ignore"))
        return line, self.positions.to_old(line, col)

    def original_char_position(self, line: int, col: int) -> tuple[int, int]:
        """Like ``original_position`` for a character column of the rewritten source."""
        return line, self.positions.to_old(line, col)

    def original_line(self, line: int) -> str:
        if 0 < line <= len(self._original_lines):
            return self._original_lines[line - 1].rstrip("\r\n")
        return ""


def _is_name(tok: tokenize.TokenInfo | None) -> bool:
    return tok is not None and tok.type == tokenize.NAME and not keyword.iskeyword(tok.string)


def _starts_type(tok: tokenize.TokenInfo | None) -> bool:
    # ``x: val list[int]`` or ``x: val "Forward"``; ``x: val`` alone is a plain annotation.
    return _is_name(tok) or (tok is not None and tok.type == tokenize.STRING)


def _removal(tok: tokenize.TokenInfo, following: tokenize.TokenInfo) -> _Edit:
    """Delete ``tok`` and the whitespace after it."""
    end = following.start[1] if following.start[0] == tok.start[0] else tok.end[1]
    return _Edit(tok.start[0], tok.start[1], end, "")


def prepare(source: str) -> Prepared:
    """Rewrite Neo-only syntax to plain Python.  Raises ``SyntaxError`` or
    ``tokenize.TokenError`` when the source cannot even be tokenized."""
    readline = io.StringIO(source, newline="").readline
    toks = [t for t in tokenize.generate_tokens(readline) if t.type not in _SKIPPED]

    edits: list[_Edit] = []
    markers: list[tuple[str, int, int]] = []  # (kind, line, original character column)

    depth = 0
    at_statement = True
    head: str | None = None  # first name of the current statement
    def_depth: int | None = None  # bracket depth of a ``def`` whose ``(`` has not been seen
    param_depth: int | None = None  # bracket depth inside the current parameter list
    expect_param = False
    param: tokenize.TokenInfo | None = None

    for i, tok in enumerate(toks):
        if tok.type in _LINE_BREAKS:
            at_statement, head = True, None
            continue
        nxt = toks[i + 1] if i + 1 < len(toks) else None

        if at_statement:
            at_statement = False
            head = tok.string if tok.type == tokenize.NAME else None
            if tok.type == tokenize.NAME and tok.string in ("val", "var") and _is_name(nxt):
                edits.append(_removal(tok, nxt))
                markers.append((tok.string, *nxt.start))
                head = None
                continue
            if tok.type == tokenize.NAME and tok.string == "task" and nxt is not None and nxt.string == "def":
                edits.append(_Edit(tok.start[0], tok.start[1], tok.end[1], "async"))
                markers.append(("task", *tok.start))
                continue
            if tok.type == tokenize.NAME and tok.string == "compute" and nxt is not None and nxt.string == "def":
                edits.append(_removal(tok, nxt))
                markers.append(("compute", *nxt.start))
                continue

        if tok.type == tokenize.NAME:
            if tok.string == "def":
                def_depth = depth
            elif param_depth == depth and expect_param:
                param, expect_param = tok, False
            continue

        if tok.type != tokenize.OP:
            continue
        if tok.string in "([{":
            if tok.string == "(" and def_depth == depth:
                def_depth, param_depth = None, depth + 1
                expect_param, param = True, None
            depth += 1
        elif tok.string in ")]}":
            depth -= 1
            if param_depth is not None and depth < param_depth:
                param_depth = None
        elif depth == 0 and tok.string == ";":
            at_statement = True
        elif depth == 0 and tok.string == ":" and head in _BLOCK_HEADS:
            at_statement = True
        elif param_depth == depth:
            if tok.string == ",":
                expect_param, param = True, None
            elif tok.string == "=":
                param = None
            elif tok.string == ":" and param is not None:
                after = toks[i + 2] if i + 2 < len(toks) else None
                if nxt is not None and nxt.type == tokenize.NAME and nxt.string == "val" and _starts_type(after):
                    edits.append(_removal(nxt, after))
                    markers.append(("val_param", *param.start))
                param = None

    return _apply(source, edits, markers)


def _apply(source: str, edits: list[_Edit], markers: list[tuple[str, int, int]]) -> Prepared:
    lines = split_lines(source)
    for edit in sorted(edits, key=lambda e: (e.line, e.start), reverse=True):
        text = lines[edit.line - 1]
        lines[edit.line - 1] = text[: edit.start] + edit.text + text[edit.end :]

    positions = PositionMap(edits)
    prepared = Prepared(original=source, source="".join(lines), positions=positions)
    buckets = {
        "val": prepared.val_decls,
        "var": prepared.var_decls,
        "val_param": prepared.val_params,
        "task": prepared.tasks,
        "compute": prepared.computes,
    }
    for kind, line, col in markers:
        new_col = positions.to_new(line, col)
        utf8_col = len(lines[line - 1][:new_col].encode("utf-8"))
        buckets[kind].add((line, utf8_col))
    return prepared
