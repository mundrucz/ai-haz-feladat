from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.config import Settings
from app.db import Database
from app.documents import DocumentRepository
from app.i18n import Translator
from app.upload import UploadService


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_translator(request: Request) -> Translator:
    return request.app.state.translator


def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def get_repository(request: Request) -> DocumentRepository:
    return request.app.state.repository


def get_upload_service(request: Request) -> UploadService:
    return request.app.state.upload_service
