from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.config import Settings
from app.db import Database
from app.i18n import Translator
from app.invoices import InvoiceRepository
from app.jobs import JobTracker
from app.upload import UploadService


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_translator(request: Request) -> Translator:
    return request.app.state.translator


def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def get_jobs(request: Request) -> JobTracker:
    return request.app.state.jobs


def get_invoice_repository(request: Request) -> InvoiceRepository:
    return request.app.state.invoice_repository


def get_upload_service(request: Request) -> UploadService:
    return request.app.state.upload_service
