"""Cell address parsing/formatting (A1 style, zero-indexed rows/cols)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CellAddr:
    row: int  # 0-based
    col: int  # 0-based

    def format(self) -> str:
        n = self.col + 1
        letters = ""
        while n > 0:
            n, rem = divmod(n - 1, 26)
            letters = chr(ord("A") + rem) + letters
        return f"{letters}{self.row + 1}"


def parse_addr(text: str) -> CellAddr:
    """Parse 'A1' into CellAddr(row=0, col=0).

    Raises ValueError on malformed input. Columns are A-Z letter sequences,
    rows are positive integers.
    """
    s = text.strip()
    i = 0
    while i < len(s) and ("A" <= s[i] <= "Z" or "a" <= s[i] <= "z"):
        i += 1
    if i == 0 or i == len(s):
        raise ValueError(f"invalid cell address: {text!r}")
    col_text, row_text = s[:i], s[i:]
    col = 0
    for ch in col_text.upper():
        col = col * 26 + (ord(ch) - ord("A") + 1)
    col -= 1
    if not row_text.isdigit():
        raise ValueError(f"invalid row in address: {text!r}")
    row = int(row_text) - 1
    if row < 0 or col < 0:
        raise ValueError(f"invalid cell address: {text!r}")
    return CellAddr(row=row, col=col)
