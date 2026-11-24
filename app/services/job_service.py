"""Folyamatban lévő és hibás feldolgozások nyilvántartása.

Szándékosan csak memóriában él: a feltöltött PDF-et és a hibás feldolgozás adatait nem tároljuk.
Sikeres feldolgozás után a bejegyzés eltűnik (az adat a számlák között jelenik meg), a hibás
a hibával együtt látszik, amíg az alkalmazás fut, a fájlt újra fel nem töltik, vagy új feltöltés nem
indul. Hibás fájlból legfeljebb `MAX_FAILED_FILES` marad meg (a legrégebbiek kiesnek); minden hibáról
külön naplófájl is készül (`app.failed_files` logger), így a kiesett bejegyzés sem vész el.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from app.failed_files import SOURCE_CANCELLED, SOURCE_INTAKE, SOURCE_PROCESSING, FailedFileLog

MAX_FAILED_FILES = 500


class JobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    FAILED = "failed"


class AddOutcome(StrEnum):
    CREATED = "created"
    REQUEUED = "requeued"
    DUPLICATE = "duplicate"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Job:
    id: int
    content_hash: str
    filename: str
    size_bytes: int
    status: JobStatus = JobStatus.PENDING
    error_code: str | None = None
    error_message: str | None = None
    error_fields: tuple[str, ...] = ()
    created_at: str = field(default_factory=_now)
    upload_id: str | None = None


class JobTracker:
    """Egyszálú eseményhurokból használjuk; a műveletek között nincs `await`, ezért nem kell zár."""

    def __init__(self, failure_log: FailedFileLog | None = None) -> None:
        self._failure_log = failure_log or FailedFileLog()
        self._jobs: dict[int, Job] = {}
        self._by_hash: dict[str, int] = {}
        self._failed: dict[int, None] = {}
        self._next_id = 1

    def add(
        self, content_hash: str, filename: str, size_bytes: int, upload_id: str | None = None
    ) -> tuple[Job, AddOutcome]:
        """Új feldolgozás; a már folyamatban lévő tartalom duplikátum, a hibásat újra lehet kezdeni."""
        existing = self._by_hash.get(content_hash)
        if existing is not None:
            job = self._jobs[existing]
            if job.status is not JobStatus.FAILED:
                return job, AddOutcome.DUPLICATE
            job.status = JobStatus.PENDING
            job.filename = filename
            job.upload_id = upload_id
            job.created_at = _now()
            job.error_code = job.error_message = None
            job.error_fields = ()
            self._failed.pop(job.id, None)
            return job, AddOutcome.REQUEUED
        job = Job(self._next_id, content_hash, filename, size_bytes, upload_id=upload_id)
        self._next_id += 1
        self._jobs[job.id] = job
        self._by_hash[content_hash] = job.id
        return job, AddOutcome.CREATED

    def get(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def list_all(self, ids: Sequence[int] | None = None) -> list[Job]:
        jobs = self._jobs.values() if ids is None else (self._jobs[i] for i in ids if i in self._jobs)
        return sorted(jobs, key=lambda job: job.id, reverse=True)

    def has_active(self) -> bool:
        """Van-e még sorban álló vagy futó feldolgozás."""
        return any(job.status in (JobStatus.PENDING, JobStatus.PROCESSING) for job in self._jobs.values())

    def claim(self, job_id: int) -> Job | None:
        """`pending` -> `processing`; csak az nyeri meg, aki először kéri, így nincs dupla feldolgozás."""
        job = self._jobs.get(job_id)
        if job is None or job.status is not JobStatus.PENDING:
            return None
        job.status = JobStatus.PROCESSING
        return job

    def mark_failed(
        self, job_id: int, code: str, message: str, fields: tuple[str, ...] = (), source: str = SOURCE_PROCESSING
    ) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.status = JobStatus.FAILED
            job.error_code, job.error_message, job.error_fields = code, message, fields
            self._register_failure(job, source)

    def cancel_pending(self, upload_id: str) -> int:
        """Az adott feltöltés még fel nem dolgozott (`pending`) fájljait megszakítja; a futók befejeződnek.

        A megszakított fájl hibásként marad a listában, és újra feltölthető. A `claim` a nem `pending`
        feladatot már nem indítja el, ezért a sorban várakozó példány hatástalan lesz.
        """
        cancelled = [
            job for job in self._jobs.values() if job.upload_id == upload_id and job.status is JobStatus.PENDING
        ]
        for job in cancelled:
            self.mark_failed(job.id, "cancelled", "upload cancelled", source=SOURCE_CANCELLED)
        return len(cancelled)

    def clear_failed(self) -> int:
        """Elfelejti a hibás fájlokat (új feltöltés kezdetén); a folyamatban lévők érintetlenek.

        A sorban maradt megszakított feladat azonosítója ilyenkor megszűnik, a `claim` ezért nem indítja el.
        """
        failed = [self._jobs[job_id] for job_id in self._failed]
        for job in failed:
            self._forget(job)
        self._failed.clear()
        return len(failed)

    def record_rejection(
        self, filename: str, size_bytes: int, code: str, upload_id: str | None = None, content_hash: str | None = None
    ) -> Job:
        """Feldolgozásba be sem kerülő fájl (üres, túl nagy, duplikátum) felvétele hibásként, hogy az oka látszódjon.

        Nem része a tartalom szerinti nyilvántartásnak (a hash csak a naplóba kerül), ezért nem akadályozza
        a fájl későbbi feltöltését.
        """
        job = Job(self._next_id, "", filename, size_bytes, JobStatus.FAILED, error_code=code, upload_id=upload_id)
        self._next_id += 1
        self._jobs[job.id] = job
        self._register_failure(job, SOURCE_INTAKE, content_hash)
        return job

    def finish(self, job_id: int) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            self._forget(job)

    def _forget(self, job: Job) -> None:
        self._jobs.pop(job.id, None)
        self._failed.pop(job.id, None)
        if self._by_hash.get(job.content_hash) == job.id:
            del self._by_hash[job.content_hash]

    def _register_failure(self, job: Job, source: str, content_hash: str | None = None) -> None:
        """Naplózza a hibát, és a `MAX_FAILED_FILES` felettit a legrégebbi hibás fájlból elhagyja."""
        self._failure_log.record(
            source,
            file_name=job.filename,
            error_code=job.error_code,
            error_message=job.error_message,
            error_fields=job.error_fields,
            content_hash=content_hash or job.content_hash,
            upload_id=job.upload_id,
            size_bytes=job.size_bytes,
            uploaded_at=job.created_at,
            job_id=job.id,
        )
        self._failed.pop(job.id, None)
        self._failed[job.id] = None
        while len(self._failed) > MAX_FAILED_FILES:
            self._forget(self._jobs[next(iter(self._failed))])
