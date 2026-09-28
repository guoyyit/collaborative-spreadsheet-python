"""Formula parsing, dependency analysis and evaluation.

Supported formula grammar (case-insensitive cell references)::

    expr    := term (('+' | '-') term)*
    term    := factor (('*' | '/') factor)*
    factor  := ['-'|'+'] atom
    atom    := NUMBER | CELL_REF | '(' expr ')'
    CELL_REF := [A-Z]+[1-9][0-9]*        e.g. A1, B22, AA10

Referencing an empty cell evaluates to 0.  Referencing a text cell in an
arithmetic context raises :class:`ValueError_` (``#VALUE!``); division by
zero raises :class:`DivZero` (``#DIV/0!``); an error in a referenced cell
propagates unchanged, so invalid references never masquerade as numbers.
"""

from dataclasses import dataclass

from .models import CELL_FORMULA, CELL_NUMBER, CELL_TEXT

#: inclusive address bounds (internal coordinates are 0-based)
MAX_ROW = 1_000_000 - 1
MAX_COL = 26 * 26 * 26 - 1  # ZZZ


class FormulaError(Exception):
    """Base class for formula evaluation errors."""

    code = "#ERROR!"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)
        self.message = message or self.code


class ParseError(FormulaError):
    code = "#ERROR!"


class RefError(FormulaError):
    """A referenced address is invalid / out of range."""

    code = "#REF!"


class CycleError(FormulaError):
    code = "#CYCLE!"


class ValueError_(FormulaError):
    code = "#VALUE!"


class DivZero(FormulaError):
    code = "#DIV/0!"


_ERROR_CLASSES: dict[str, type[FormulaError]] = {}


def error_by_code(code: str, message: str = "") -> FormulaError:
    """Reconstruct the specific formula error type from its display code."""
    if not _ERROR_CLASSES:
        for cls in (ParseError, RefError, CycleError, ValueError_, DivZero):
            _ERROR_CLASSES[cls.code] = cls
    return _ERROR_CLASSES.get(code, FormulaError)(message or code)


@dataclass(frozen=True)
class ParsedFormula:
    text: str
    refs: frozenset[tuple[int, int]]


# ---------------------------------------------------------------------------
# tokenizer
# ---------------------------------------------------------------------------
_TOKEN_NUMBER = "N"
_TOKEN_REF = "R"
_TOKEN_OP = "O"
_TOKEN_LP = "("
_TOKEN_RP = ")"


def _tokenize(body: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "+-*/()":
            tokens.append((_TOKEN_OP if ch in "+-*/" else ch, ch))
            i += 1
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and body[i + 1].isdigit()):
            j = i
            dots = 0
            while j < n and (body[j].isdigit() or body[j] == "."):
                if body[j] == ".":
                    dots += 1
                    if dots > 1:
                        raise ParseError(f"bad number at position {j}")
                j += 1
            tokens.append((_TOKEN_NUMBER, body[i:j]))
            i = j
            continue
        if ch.isalpha():
            j = i
            while j < n and body[j].isalpha():
                j += 1
            letters = body[i:j]
            k = j
            while k < n and body[k].isdigit():
                k += 1
            if k == j:
                raise ParseError(f"cell reference needs a row number at position {i}")
            row_str = body[j:k]
            if row_str.startswith("0"):
                raise RefError(f"row numbers start at 1 (got {row_str})")
            coord = _decode_ref(letters.upper(), row_str)
            tokens.append((_TOKEN_REF, f"{coord[0]},{coord[1]}"))
            i = k
            continue
        raise ParseError(f"unexpected character {ch!r} at position {i}")
    return tokens


def _decode_ref(letters: str, row_str: str) -> tuple[int, int]:
    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch) - ord("A") + 1)
    col -= 1
    row = int(row_str) - 1
    if row > MAX_ROW or col > MAX_COL:
        raise RefError(f"reference {letters}{row_str} is out of range")
    return row, col


def encode_ref(row: int, col: int) -> str:
    """Encode internal 0-based (row, col) as a spreadsheet-style address."""
    n = col + 1
    letters = ""
    while n:
        n, r = divmod(n - 1, 26)
        letters = chr(ord("A") + r) + letters
    return f"{letters}{row + 1}"


# ---------------------------------------------------------------------------
# public helpers
# ---------------------------------------------------------------------------
def is_formula(text: str) -> bool:
    return text.startswith("=")


def parse_formula(text: str) -> ParsedFormula:
    if not is_formula(text):
        raise ParseError("not a formula")
    body = text[1:]
    if not body.strip():
        raise ParseError("empty formula")
    tokens = _tokenize(body)
    refs: set[tuple[int, int]] = set()
    for kind, val in tokens:
        if kind == _TOKEN_REF:
            r, c = val.split(",")
            refs.add((int(r), int(c)))
    # full syntax check via the recursive-descent parser
    _Parser(tokens).parse()
    return ParsedFormula(text=text, refs=frozenset(refs))


def detect_cycle(
    target: tuple[int, int],
    refs: frozenset[tuple[int, int]],
    graph: dict[tuple[int, int], frozenset[tuple[int, int]]],
    max_depth: int = 64,
) -> list[tuple[int, int]] | None:
    """Return a path ``target -> ... -> target`` if adding ``target -> refs``
    closes a cycle, otherwise ``None``.

    Only nodes present in ``graph`` (i.e. formula cells) have outgoing edges.
    Chains longer than ``max_depth`` are reported as cycles so evaluation can
    never be trapped by pathological reference chains.
    """
    if target in refs:
        return [target, target]  # direct self-reference

    # Explicit-path DFS.  ``path`` is the chain of nodes visited from target
    # via the candidate edge target -> refs then existing graph edges.
    def dfs(node: tuple[int, int], path: list[tuple[int, int]]) -> list | None:
        if len(path) > max_depth:
            return path + [node, target]
        for nxt in graph.get(node, frozenset()):
            if nxt == target:
                return path + [target]
            if nxt in path:
                continue  # an internal cycle that does not reach target
            found = dfs(nxt, path + [nxt])
            if found is not None:
                return found
        return None

    for ref in refs:
        found = dfs(ref, [target, ref])
        if found is not None:
            return found
    return None


def evaluate(formula: str, values: dict[tuple[int, int], object]) -> float:
    """Evaluate a formula against resolved cell values.

    ``values`` maps referenced coordinates to ``int``/``float`` for numbers,
    ``str`` for text, or a :class:`FormulaError` for error cells.  Missing
    coordinates read as 0 (empty cell).
    """
    parsed = parse_formula(formula)
    tokens = _tokenize(formula[1:])
    return _Parser(tokens).evaluate(values, [ref for ref in parsed.refs])


def classify(text: str) -> tuple[str, object]:
    """Classify a raw cell string into ``(kind, parsed_value)``."""
    if text == "":
        return "blank", ""
    if is_formula(text):
        return CELL_FORMULA, parse_formula(text)
    try:
        return CELL_NUMBER, float(text)
    except ValueError:
        return CELL_TEXT, text


def format_number(x: float) -> str:
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return repr(x)


# ---------------------------------------------------------------------------
# recursive-descent parser / evaluator
# ---------------------------------------------------------------------------
class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.pos = 0

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _next(self) -> tuple[str, str]:
        tok = self._peek()
        if tok is None:
            raise ParseError("unexpected end of formula")
        self.pos += 1
        return tok

    # -- syntax validation --------------------------------------------------
    def parse(self) -> None:
        self._expr()
        if self.pos != len(self.tokens):
            raise ParseError(f"unexpected token {self.tokens[self.pos][1]!r}")

    def _expr(self) -> None:
        self._term()
        while self._peek() is not None and self._peek()[1] in "+-":
            self._next()
            self._term()

    def _term(self) -> None:
        self._factor()
        while self._peek() is not None and self._peek()[1] in "*/":
            self._next()
            self._factor()

    def _factor(self) -> None:
        tok = self._peek()
        if tok is not None and tok[0] == _TOKEN_OP and tok[1] in "+-":
            self._next()
        self._atom()

    def _atom(self) -> None:
        tok = self._next()
        if tok[0] in (_TOKEN_NUMBER, _TOKEN_REF):
            return
        if tok[0] == _TOKEN_LP:
            self._expr()
            close = self._next()
            if close[0] != _TOKEN_RP:
                raise ParseError("missing closing parenthesis")
            return
        raise ParseError(f"unexpected token {tok[1]!r}")

    # -- evaluation ---------------------------------------------------------
    def evaluate(self, values: dict[tuple[int, int], object], _refs: list) -> float:
        result = self._eval_expr(values)
        if self.pos != len(self.tokens):
            raise ParseError(f"unexpected token {self.tokens[self.pos][1]!r}")
        return result

    def _ref_value(self, raw: str, values: dict[tuple[int, int], object]) -> float:
        r, c = (int(x) for x in raw.split(","))
        if (r, c) not in values:
            return 0.0  # empty cell
        v = values[(r, c)]
        if isinstance(v, FormulaError):
            raise v
        if isinstance(v, bool):  # guard: bool is a subclass of int
            return float(v)
        if isinstance(v, (int, float)):
            return float(v)
        if v == "" or v is None:
            return 0.0
        raise ValueError_(f"text used in arithmetic: {v!r}")

    def _eval_expr(self, values) -> float:
        result = self._eval_term(values)
        while True:
            tok = self._peek()
            if tok is None or tok[1] not in "+-":
                return result
            self._next()
            rhs = self._eval_term(values)
            result = result + rhs if tok[1] == "+" else result - rhs

    def _eval_term(self, values) -> float:
        result = self._eval_factor(values)
        while True:
            tok = self._peek()
            if tok is None or tok[1] not in "*/":
                return result
            self._next()
            rhs = self._eval_factor(values)
            if tok[1] == "/":
                if rhs == 0:
                    raise DivZero("division by zero")
                result = result / rhs
            else:
                result = result * rhs

    def _eval_factor(self, values) -> float:
        tok = self._peek()
        sign = 1.0
        if tok is not None and tok[0] == _TOKEN_OP and tok[1] in "+-":
            self._next()
            sign = -1.0 if tok[1] == "-" else 1.0
        return sign * self._eval_atom(values)

    def _eval_atom(self, values) -> float:
        tok = self._next()
        if tok[0] == _TOKEN_NUMBER:
            return float(tok[1])
        if tok[0] == _TOKEN_REF:
            return self._ref_value(tok[1], values)
        if tok[0] == _TOKEN_LP:
            value = self._eval_expr(values)
            close = self._next()
            if close[0] != _TOKEN_RP:
                raise ParseError("missing closing parenthesis")
            return value
        raise ParseError(f"unexpected token {tok[1]!r}")
