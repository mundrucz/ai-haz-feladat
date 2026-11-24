"""Folyamatban lévő és hibás feldolgozások nyilvántartása.

Szándékosan csak memóriában él: a feltöltött PDF-et és a hibás feldolgozás adatait nem tároljuk.
Sikeres feldolgozás után a bejegyzés eltűnik (az adat a számlák között jelenik meg), a hibás
a hibával együtt látszik, amíg az alkalmazás fut vagy a fájlt újra fel nem töltik. A feldolgozásba
be sem kerülő fájlok (üres, túl nagy, duplikátum) hibás bejegyzésként, korlátos számban megmaradnak.
"""

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

MAX_REJECTIONS = 500


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


class JobTracker:
    """Egyszálú eseményhurokból használjuk; a műveletek között nincs `await`, ezért nem kell zár."""

    def __init__(self) -> None:
        self._jobs: dict[int, Job] = {}
        self._by_hash: dict[str, int] = {}
        self._rejections: deque[int] = deque()
        self._next_id = 1

    def add(self, content_hash: str, filename: str, size_bytes: int) -> tuple[Job, AddOutcome]:
        """Új feldolgozás; a már folyamatban lévő tartalom duplikátum, a hibásat újra lehet kezdeni."""
        existing = self._by_hash.get(content_hash)
        if existing is not None:
            job = self._jobs[existing]
            if job.status is not JobStatus.FAILED:
                return job, AddOutcome.DUPLICATE
            job.status = JobStatus.PENDING
            job.filename = filename
            job.created_at = _now()
            job.error_code = job.error_message = None
            job.error_fields = ()
            return job, AddOutcome.REQUEUED
        job = Job(self._next_id, content_hash, filename, size_bytes)
        self._next_id += 1
        self._jobs[job.id] = job
        self._by_hash[content_hash] = job.id
        return job, AddOutcome.CREATED

    def get(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def list_all(self, ids: Sequence[int] | None = None) -> list[Job]:
        jobs = self._jobs.values() if ids is None else (self._jobs[i] for i in ids if i in self._jobs)
        return sorted(jobs, key=lambda job: job.id, reverse=True)

    def claim(self, job_id: int) -> Job | None:
        """`pending` -> `processing`; csak az nyeri meg, aki először kéri, így nincs dupla feldolgozás."""
        job = self._jobs.get(job_id)
        if job is None or job.status is not JobStatus.PENDING:
            return None
        job.status = JobStatus.PROCESSING
        return job

    def mark_failed(self, job_id: int, code: str, message: str, fields: tuple[str, ...] = ()) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.status = JobStatus.FAILED
            job.error_code, job.error_message, job.error_fields = code, message, fields

    def record_rejection(self, filename: str, size_bytes: int, code: str) -> Job:
        """Feldolgozásba be sem kerülő fájl (üres, túl nagy, duplikátum) naplózása, hogy az oka látszódjon.

        Nem része a tartalom szerinti nyilvántartásnak, ezért nem akadályozza a fájl későbbi feltöltését;
        a lista korlátos, a legrégebbi bejegyzések kiesnek.
        """
        job = Job(self._next_id, "", filename, size_bytes, JobStatus.FAILED, error_code=code)
        self._next_id += 1
        self._jobs[job.id] = job
        self._rejections.append(job.id)
        while len(self._rejections) > MAX_REJECTIONS:
            self._jobs.pop(self._rejections.popleft(), None)
        return job

    def finish(self, job_id: int) -> None:
        job = self._jobs.pop(job_id, None)
        if job is not None:
            self._by_hash.pop(job.content_hash, None)
