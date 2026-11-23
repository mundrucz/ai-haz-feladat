class AppError(Exception):
    """Az alkalmazás saját hibáinak őse; a határokon (API, háttérfeladat) kezeljük."""

    code = "app_error"
    status_code = 500

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class NotFoundError(AppError):
    code = "not_found"
    status_code = 404


class InvalidInputError(AppError):
    code = "invalid_input"
    status_code = 400


class ProcessingError(AppError):
    """Egy dokumentum feldolgozása nem sikerült; a hiba a dokumentumnál jelenik meg, a többit nem érinti."""

    code = "processing_error"
    status_code = 422

    def __init__(self, message: str, *, code: str | None = None, fields: tuple[str, ...] = ()) -> None:
        super().__init__(message, code=code)
        self.fields = fields
