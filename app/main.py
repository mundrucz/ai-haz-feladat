import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api import documents as documents_api
from app.api.routes import router
from app.config import Settings
from app.db import Database
from app.documents import DocumentRepository
from app.errors import AppError
from app.i18n import Translator
from app.logging_setup import setup_logging
from app.pdf import extract_text
from app.processing import DocumentProcessor
from app.storage import FileStore
from app.upload import UploadService

BASE_DIR = Path(__file__).parent
logger = logging.getLogger(__name__)


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Composition root: itt épülnek össze a függőségek, máshol nem példányosítunk szolgáltatást."""
    settings = settings or Settings()
    translator = Translator(BASE_DIR / "locales", settings.default_locale)
    templates = Jinja2Templates(directory=BASE_DIR / "templates")
    templates.env.globals["t"] = translator.translate

    db = Database(settings.database_path)
    repository = DocumentRepository(db)
    store = FileStore(settings.upload_dir)
    processor = DocumentProcessor(repository, store, extract_text, settings.processing_concurrency)
    upload_service = UploadService(repository, store, processor, settings.max_upload_bytes)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(settings.log_level, settings.log_format)
        await db.init()
        await processor.start()
        logger.info("application started", extra={"llm_available": settings.llm_available})
        try:
            yield
        finally:
            await processor.stop()

    app = FastAPI(title=translator.translate("app.title"), lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.repository = repository
    app.state.processor = processor
    app.state.upload_service = upload_service
    app.state.translator = translator
    app.state.templates = templates

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.warning("request failed", extra={"code": exc.code, "path": request.url.path})
        return _error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", extra={"path": request.url.path})
        return _error_response(500, "internal_error", translator.translate("error.internal"))

    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    app.include_router(router)
    app.include_router(documents_api.router)
    return app
