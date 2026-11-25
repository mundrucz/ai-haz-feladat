"""A hibás fájlok külön naplója: minden hibáról egy bejegyzés kerül az `app.failed_files` loggerbe.

A napló a memóriabeli lista korlátján (és az új feltöltéskor történő ürítésen) túl is megőrzi a hibákat.
A fájl beállítása a `core.logging_config.setup_failed_files_log`-ban van.
"""

import logging
from collections.abc import Sequence

from app.core.i18n import Translator
from app.core.logging_config import FAILED_FILES_LOGGER

SOURCE_INTAKE = "intake"
SOURCE_PROCESSING = "processing"
SOURCE_CANCELLED = "cancelled"
SOURCE_REPORT = "report"

_logger = logging.getLogger(FAILED_FILES_LOGGER)


class FailedFileLog:
    def __init__(self, translator: Translator | None = None) -> None:
        self._translator = translator

    def record(
        self,
        source: str,
        *,
        file_name: str,
        error_code: str | None,
        error_message: str | None = None,
        error_fields: Sequence[str] = (),
        content_hash: str | None = None,
        upload_id: str | None = None,
        size_bytes: int | None = None,
        uploaded_at: str | None = None,
        job_id: int | None = None,
        invoice_id: int | None = None,
    ) -> None:
        """`source`: hol akadt el a fájl (beléptetés, feldolgozás, megszakítás vagy az összesítésből kimaradás)."""
        _logger.info(
            "failed file",
            extra={
                "source": source,
                "file_name": file_name,
                "error_code": error_code,
                "error_text": self._describe(error_code),
                "error_message": error_message,
                "error_fields": list(error_fields),
                "content_hash": content_hash or None,
                "upload_id": upload_id,
                "size_bytes": size_bytes,
                "uploaded_at": uploaded_at,
                "job_id": job_id,
                "invoice_id": invoice_id,
            },
        )

    def _describe(self, code: str | None) -> str | None:
        if code is None or self._translator is None:
            return None
        return self._translator.get(f"error.{code}") or self._translator.get(f"ui.problem.{code}")
