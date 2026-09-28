"""Formula parsing, dependency extraction and evaluation.

Supported grammar (after the leading '=')::

    expr   := term (('+'|'-') term)*
    term   := factor (('*'|'/') factor)*
    factor := ('+'|'-') factor | atom
    atom   := NUMBER | CELLREF | '(' expr ')' | FUNCNAME '(' arglist ')'
    arglist:= arg (',' arg)*
    arg    := expr | CELLREF ':' CELLREF        (ranges only inside calls)

Values: numbers (float), strings (non-numeric literals), or CellError.
A direct reference to an empty cell is a REF error (never silently coerced to
0); aggregate functions skip empty cells inside ranges. Error values
propagate through dependent formulas and are never treated as ordinary numbers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from spreadsheet.celladdr import CellAddr, parse_addr
from spreadsheet.errors import (
    CIRCULAR_ERROR,
    DIV_ZERO_ERROR,
    REF_ERROR,
    SYNTAX_ERROR,
    VALUE_ERROR,
    CellError,
    FormulaSyntaxError,
)

# A computed cell value: number, string literal, or an error value.
Value = float | str | CellError

# Resolver: already-computed value of a referenced cell, None means empty.
Getter = Callable[[CellAddr], Value | None]

_AGGREGATES = {"SUM", "AVERAGE", "AVG", "MIN", "MAX", "COUNT"}
_UNARY_FNS = {"ABS"}
_KNOWN_FNS = _AGGREGATES | _UNARY_FNS


@dataclass(frozen=True, slots=True)
class ParsedFormula:
    text: str
    dependencies: frozenset[CellAddr]
    ast: tuple = field(default=())


# --------------------------------------------------------------------------- #
# Tokenizer
# --------------------------------------------------------------------------- #
def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif ch in "+-*/(),:":
            tokens.append((ch, ch))
            i += 1
        elif ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            j = i
            dot = False
            while j < n and (text[j].isdigit() or (text[j] == "." and not dot)):
                dot = dot or text[j] == "."
                j += 1
            tokens.append(("NUM", text[i:j]))
            i = j
        elif ch.isalpha() or ch == "_":
            j = i
            while j < n and (text[j].isalpha() or text[j] == "_"):
                j += 1
            if j < n and text[j].isdigit():
                # A1 style reference: letters immediately followed by digits
                k = j
                while k < n and text[k].isdigit():
                    k += 1
                tokens.append(("REF", text[i:k]))
                i = k
            else:
                tokens.append(("NAME", text[i:j].upper()))
                i = j
        else:
            raise FormulaSyntaxError(f"unexpected character {ch!r} in {text!r}")
    tokens.append(("EOF", ""))
    return tokens


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = _tokenize(text)
        self.pos = 0
        self.deps: set[CellAddr] = set()
        self.allow_range = 0

    def peek(self) -> tuple[str, str]:
        return self.tokens[self.pos]

    def take(self) -> tuple[str, str]:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, kind: str) -> tuple[str, str]:
        tok = self.peek()
        if tok[0] != kind:
            raise FormulaSyntaxError(f"expected {kind} but got {tok[1]!r} in {self.text!r}")
        return self.take()

    def parse(self) -> tuple:
        node = self.expr()
        if self.peek()[0] != "EOF":
            raise FormulaSyntaxError(f"trailing input in {self.text!r}")
        return node

    def expr(self) -> tuple:
        node = self.term()
        while self.peek()[0] in ("+", "-"):
            op = self.take()[0]
            node = ("bin", op, node, self.term())
        return node

    def term(self) -> tuple:
        node = self.factor()
        while self.peek()[0] in ("*", "/"):
            op = self.take()[0]
            node = ("bin", op, node, self.factor())
        return node

    def factor(self) -> tuple:
        kind, val = self.peek()
        if kind in ("+", "-"):
            self.take()
            return ("unary", val, self.factor())
        return self.atom()

    def _ref(self, label: str) -> tuple:
        try:
            addr = parse_addr(label)
        except ValueError as exc:
            raise FormulaSyntaxError(str(exc)) from exc
        self.deps.add(addr)
        return ("ref", addr)

    def atom(self) -> tuple:
        kind, val = self.peek()
        if kind == "NUM":
            self.take()
            try:
                return ("num", float(val))
            except ValueError as exc:
                raise FormulaSyntaxError(f"bad number {val!r}") from exc
        if kind == "REF":
            self.take()
            node = self._ref(val)
            if self.allow_range and self.peek()[0] == ":":
                self.take()
                _, end_val = self.expect("REF")
                end = self._ref(end_val)
                return ("range", node[1], end[1])
            if self.peek()[0] == ":":
                raise FormulaSyntaxError("range is only allowed inside a function call")
            return node
        if kind == "(":
            self.take()
            node = self.expr()
            self.expect(")")
            return node
        if kind == "NAME":
            self.take()
            name = val
            self.expect("(")
            if name not in _KNOWN_FNS:
                raise FormulaSyntaxError(f"unknown function {name!r}")
            args: list[tuple] = []
            self.allow_range += 1
            if self.peek()[0] != ")":
                args.append(self.expr())
                while self.peek()[0] == ",":
                    self.take()
                    args.append(self.expr())
            self.allow_range -= 1
            self.expect(")")
            return ("call", name, tuple(args))
        raise FormulaSyntaxError(f"unexpected token {val!r} in {self.text!r}")


def parse_formula(text: str) -> ParsedFormula:
    """Parse a formula string (without leading '='). Raises FormulaSyntaxError."""
    if not text.strip():
        raise FormulaSyntaxError("empty formula")
    parser = _Parser(text)
    ast = parser.parse()
    return ParsedFormula(text=text, dependencies=frozenset(parser.deps), ast=ast)


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
def _expand_range(start: CellAddr, end: CellAddr) -> list[CellAddr]:
    r0, r1 = sorted((start.row, end.row))
    c0, c1 = sorted((start.col, end.col))
    return [CellAddr(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]


def _as_number(v: Value) -> float:
    if isinstance(v, CellError):
        raise _Err(v)
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v.strip() != "":
        try:
            return float(v)
        except ValueError as exc:
            raise _Err(CellError(VALUE_ERROR, f"text {v!r} is not a number")) from exc
    raise _Err(CellError(VALUE_ERROR, "expected a number"))


class _Err(Exception):
    def __init__(self, cell_error: CellError) -> None:
        super().__init__(cell_error.message)
        self.cell_error = cell_error


def _eval(node: tuple, get: Getter) -> Value:
    tag = node[0]
    if tag == "num":
        return node[1]
    if tag == "unary":
        v = _as_number(_eval(node[2], get))
        return v if node[1] == "+" else -v
    if tag == "bin":
        left = _as_number(_eval(node[2], get))
        right = _as_number(_eval(node[3], get))
        op = node[1]
        if op == "+":
            return left + right
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if right == 0:
            raise _Err(CellError(DIV_ZERO_ERROR, "division by zero"))
        return left / right
    if tag == "ref":
        v = get(node[1])
        if v is None:
            raise _Err(CellError(REF_ERROR, f"missing reference to {node[1].format()}"))
        if isinstance(v, CellError):
            raise _Err(v)
        return v
    if tag == "range":
        raise _Err(CellError(SYNTAX_ERROR, "range is only allowed inside a function call"))
    if tag == "call":
        return _call(node[1], node[2], get)
    raise _Err(CellError(VALUE_ERROR, f"unknown node {tag!r}"))


def _range_args(args: tuple[tuple, ...], get: Getter) -> list[Value]:
    values: list[Value] = []
    for arg in args:
        if arg[0] == "range":
            for addr in _expand_range(arg[1], arg[2]):
                v = get(addr)
                if v is not None:
                    values.append(v)
        else:
            values.append(_eval(arg, get))
    return values


def _call(name: str, args: tuple[tuple, ...], get: Getter) -> Value:
    if name in _AGGREGATES:
        values = _range_args(args, get)
        nums: list[float] = []
        for v in values:
            if isinstance(v, CellError):
                raise _Err(v)
            if v == "" or v is None:
                continue
            nums.append(_as_number(v))
        if name == "SUM":
            return sum(nums)
        if name == "COUNT":
            return float(len(nums))
        if not nums:
            kind = DIV_ZERO_ERROR if name in ("AVERAGE", "AVG") else REF_ERROR
            msg = "cannot average an empty range" if kind == DIV_ZERO_ERROR else "empty range"
            raise _Err(CellError(kind, msg))
        if name in ("AVERAGE", "AVG"):
            return sum(nums) / len(nums)
        if name == "MIN":
            return min(nums)
        if name == "MAX":
            return max(nums)
    if name == "ABS":
        if len(args) != 1:
            raise _Err(CellError(VALUE_ERROR, "ABS takes exactly one argument"))
        return abs(_as_number(_eval(args[0], get)))
    raise _Err(CellError(VALUE_ERROR, f"unknown function {name!r}"))


def eval_formula(parsed: ParsedFormula, get: Getter) -> Value:
    try:
        return _eval(parsed.ast, get)
    except _Err as exc:
        return exc.cell_error
    except RecursionError:
        return CellError(CIRCULAR_ERROR, "circular reference")


def literal_value(raw: str) -> Value:
    """Interpret non-formula raw content as number or string."""
    try:
        return float(raw)
    except ValueError:
        return raw


def eval_cell_value(raw: str | None, get: Getter) -> Value:
    """Interpret a cell's raw content.

    ``raw`` starting with '=' is evaluated as a formula; ``get`` resolves
    referenced cells to their already-computed values (None for empty).
    Any failure yields a CellError, never a silently coerced number.
    """
    if raw is None or raw == "":
        return 0
    if raw.startswith("="):
        parsed = parse_formula(raw[1:])
        return eval_formula(parsed, get)
    return literal_value(raw)
