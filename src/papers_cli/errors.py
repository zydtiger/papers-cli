from __future__ import annotations


class PapersError(Exception):
    """Expected error that can be rendered as a stable CLI response."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: int = 1,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.details = details or {}
