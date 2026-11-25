import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.core.errors import AppError
from app.services.job_service import JobTracker
from app.services.pdf_service import PdfText

logger = logging.getLogger(__name__)

TextExtractor = Callable[[bytes], Awaitable[PdfText]]
InvoiceAnalysis = Callable[[str, str, str], Awaitable[None]]


class DocumentProcessor:
    """Háttérfeldolgozó: korlátozott számú munkás, folyamaton belüli sorral.

    A PDF bájtjai csak a sorban, a feldolgozás idejére élnek a memóriában, lemezre nem kerülnek.
    Egy dokumentum hibája csak az adott dokumentumot érinti; a munkások nem állnak le.
    Az `analyze(content_hash, filename, text)` állítja elő és menti a számlaadatot.
    """

    def __init__(
        self,
        jobs: JobTracker,
        extractor: TextExtractor,
        analyze: InvoiceAnalysis,
        concurrency: int = 4,
    ) -> None:
        self._jobs = jobs
        self._extractor = extractor
        self._analyze = analyze
        self._concurrency = max(1, concurrency)
        self._queue: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []

    async def start(self) -> None:
        self._workers = [asyncio.create_task(self._worker()) for _ in range(self._concurrency)]

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

    def enqueue(self, job_id: int, data: bytes) -> None:
        self._queue.put_nowait((job_id, data))

    async def join(self) -> None:
        await self._queue.join()

    async def _worker(self) -> None:
        while True:
            job_id, data = await self._queue.get()
            try:
                await self.process(job_id, data)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker failed", extra={"job_id": job_id})
            finally:
                del data
                self._queue.task_done()

    async def process(self, job_id: int, data: bytes) -> None:
        job = self._jobs.claim(job_id)
        if job is None:
            return
        try:
            result = await self._extractor(data)
            await self._analyze(job.content_hash, job.filename, result.text)
        except AppError as exc:
            logger.warning("job failed: %s", exc.message, extra={"job_id": job_id, "code": exc.code})
            self._jobs.mark_failed(job_id, exc.code, exc.message, getattr(exc, "fields", ()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("unexpected processing error", extra={"job_id": job_id})
            self._jobs.mark_failed(job_id, "unexpected_error", str(exc))
        else:
            self._jobs.finish(job_id)
