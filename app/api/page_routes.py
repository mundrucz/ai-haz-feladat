from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.api.deps import get_db, get_settings, get_templates, get_translator
from app.config import Settings
from app.db import Database
from app.i18n import Translator

router = APIRouter()


@router.get("/health")
async def health(db: Database = Depends(get_db)) -> JSONResponse:
    db_ok = await db.ping()
    return JSONResponse(
        {"status": "ok" if db_ok else "degraded", "database": "ok" if db_ok else "error"},
        status_code=200 if db_ok else 503,
    )


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    settings: Settings = Depends(get_settings),
    templates=Depends(get_templates),
    translator: Translator = Depends(get_translator),
) -> HTMLResponse:
    strings = translator.catalog(("ui.", "status.", "outcome.", "error."))
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "settings": settings,
            "strings": strings,
            "max_upload_mb": settings.max_upload_mb,
        },
    )
