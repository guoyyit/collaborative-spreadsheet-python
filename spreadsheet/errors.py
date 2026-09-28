"""Domain errors and formula error values."""

from __future__ import annotations


class SpreadsheetError(Exception):
    """Base class for service-level errors."""


class NotFoundError(SpreadsheetError):
    """Referenced sheet/client/resource does not exist."""


class PermissionDeniedError(SpreadsheetError):
    """The client is not allowed to perform the operation."""

    def __init__(self, message: str = "permission denied") -> None:
        super().__init__(message)


class DuplicateSubmitError(SpreadsheetError):
    """A batch with the same client batch id has already been committed."""


class FormulaSyntaxError(SpreadsheetError):
    """Formula text could not be parsed."""


class CircularReferenceError(SpreadsheetError):
    """Setting a formula would create a dependency cycle."""


class CellError:
    """An error value held by a formula cell (propagated, never treated as number)."""

    __slots__ = ("kind", "message")

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.message = message

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, CellError)
            and self.kind == other.kind
            and self.message == other.message
        )

    def __hash__(self) -> int:
        return hash((self.kind, self.message))

    def __repr__(self) -> str:
        return f"CellError({self.kind!r}, {self.message!r})"


REF_ERROR = "REF"
CIRCULAR_ERROR = "CIRCULAR"
SYNTAX_ERROR = "SYNTAX"
DIV_ZERO_ERROR = "DIV0"
VALUE_ERROR = "VALUE"
