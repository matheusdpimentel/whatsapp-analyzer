from whatsapp_analyzer.exit_codes import ExitCode


class AppError(Exception):
    """An expected failure that the CLI reports with a stable code instead of a traceback.

    `message` and `hint` are already localized; `code` is a stable English identifier.
    """

    def __init__(
        self,
        code: str,
        exit_code: ExitCode,
        message: str,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.message = message
        self.hint = hint
