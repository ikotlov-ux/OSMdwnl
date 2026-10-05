"""Классы ошибок с кодами возврата (раздел 6.1 и 16 ТЗ)."""
from __future__ import annotations


class OSMDwnlError(Exception):
    exit_code = 1
    code = "E_GENERIC"

    def __init__(self, message: str, cause: str | None = None, action: str | None = None,
                 code: str | None = None):
        super().__init__(message)
        self.message = message
        self.cause = cause
        self.action = action
        if code:
            self.code = code

    def describe(self) -> str:
        parts = [f"[{self.code}] {self.message}"]
        if self.cause:
            parts.append(f"  Причина: {self.cause}")
        if self.action:
            parts.append(f"  Что сделать: {self.action}")
        return "\n".join(parts)


class ArgumentError(OSMDwnlError):
    exit_code = 2
    code = "E_ARGS"


class AOIError(OSMDwnlError):
    exit_code = 2
    code = "E_AOI"


class RecipeError(OSMDwnlError):
    exit_code = 3
    code = "E_RECIPE"


class NetworkError(OSMDwnlError):
    exit_code = 4
    code = "E_NETWORK"


class QueryTooLarge(NetworkError):
    """Overpass сообщил timeout / out of memory / maxsize — нужно дробить запрос."""
    code = "E_QUERY_TOO_LARGE"


class ParseError(OSMDwnlError):
    exit_code = 5
    code = "E_PARSE"


class OutputError(OSMDwnlError):
    exit_code = 6
    code = "E_OUTPUT"


class QualityError(OSMDwnlError):
    exit_code = 7
    code = "E_QC"
