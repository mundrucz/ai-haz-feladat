import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api import document_routes
from app.api import invoice_routes
from app.api import report_routes
from app.api.page_routes import router
from app.clients.llm_client import LlmClient
from app.clients.mnb_client import MnbClient
from app.core.config import ConfigFallback, Settings, load_settings, resolve_locale
from app.core.errors import AppError
from app.core.i18n import Translator, available_locales
from app.core.logging_config import setup_failed_files_log, setup_logging
from app.db.database import Database
from app.extraction.company import Company
from app.extraction.llm import LlmExtractor
from app.repositories.invoice_repository import InvoiceRepository
from app.repositories.rate_repository import RateRepository
from app.services.failed_file_service import FailedFileLog
from app.services.invoice_service import InvoiceAnalyzer
from app.services.job_service import JobTracker
from app.services.pdf_service import extract_text
from app.services.processing_service import DocumentProcessor
from app.services.rate_service import RateService
from app.services.report_service import ReportService
from app.services.upload_service import UploadService

WEB_DIR = Path(__file__).parent / "web"
logger = logging.getLogger(__name__)


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)


def create_app(settings: Settings | None = None, http_client: httpx.AsyncClient | None = None) -> FastAPI:
    """Composition root: itt épülnek össze a függőségek, máshol nem példányosítunk szolgáltatást."""
    config_fallbacks: list[ConfigFallback] = []
    if settings is None:
        settings, config_fallbacks = load_settings()
    locales_dir = WEB_DIR / "locales"
    settings, locale_fallback = resolve_locale(settings, available_locales(locales_dir))
    if locale_fallback is not None:
        config_fallbacks = [*config_fallbacks, locale_fallback]
    translator = Translator(locales_dir, settings.default_locale)
    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    templates.env.globals["t"] = translator.translate

    def asset(path: str) -> str:
        """Statikus fájl URL-je a módosítási idejével, így módosítás után a böngésző nem a régi példányt használja."""
        version = int((WEB_DIR / "static" / path).stat().st_mtime)
        return f"/static/{path}?v={version}"

    templates.env.globals["asset"] = asset

    db = Database(settings.database_path)
    failure_log = FailedFileLog(translator)
    jobs = JobTracker(failure_log)
    invoice_repository = InvoiceRepository(db)
    company = Company(settings.company_name, settings.company_tax_number, settings.company_eu_vat)

    owned_http: httpx.AsyncClient | None = None
    if http_client is None:
        http_client = owned_http = httpx.AsyncClient()
    llm_extractor = LlmExtractor(
        LlmClient(
            http_client,
            api_key=settings.openrouter_api_key.get_secret_value(),
            model=settings.openrouter_model,
            base_url=settings.openrouter_base_url,
            timeout=settings.llm_timeout_seconds,
            concurrency=settings.llm_concurrency,
        )
    )

    mnb_client = MnbClient(
        http_client,
        url=settings.mnb_url,
        timeout=settings.mnb_timeout_seconds,
        concurrency=settings.mnb_concurrency,
    )
    rate_service = RateService(
        RateRepository(db),
        mnb_client,
        lookback_days=settings.mnb_lookback_days,
        currencies_ttl_days=settings.mnb_currencies_ttl_days,
    )
    analyzer = InvoiceAnalyzer(llm_extractor, invoice_repository, company, rate_service, settings.report_currency)
    processor = DocumentProcessor(
        jobs, extract_text, analyzer.analyze, settings.processing_concurrency, settings.processing_queue_size
    )
    upload_service = UploadService(jobs, invoice_repository, processor, settings.max_upload_bytes)
    report_service = ReportService(invoice_repository, rate_service, settings.report_currency, failure_log)
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging()
        for fallback in config_fallbacks:
            logger.warning(
                "invalid setting %s=%r (%s), using default %r",
                fallback.variable,
                fallback.value,
                fallback.reason,
                fallback.default,
                extra={"setting": fallback.variable, "default": fallback.default},
            )
        setup_failed_files_log(settings.failed_files_log_path)
        await db.init()
        await processor.start()
        logger.info("application started", extra={"llm_model": settings.openrouter_model})
        try:
            yield
        finally:
            await processor.stop()
            if owned_http is not None:
                await owned_http.aclose()
            await db.close()

    app = FastAPI(title=translator.translate("app.title"), lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.jobs = jobs
    app.state.invoice_repository = invoice_repository
    app.state.processor = processor
    app.state.upload_service = upload_service
    app.state.report_service = report_service
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

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.include_router(router)
    app.include_router(document_routes.router)
    app.include_router(invoice_routes.router)
    app.include_router(report_routes.router)
    return app
