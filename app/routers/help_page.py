from __future__ import annotations

from pathlib import Path

import markdown
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="templates")

_README_PATH = Path(__file__).resolve().parent.parent.parent / "README.md"


@router.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    try:
        md_text = _README_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        md_text = "_README.md not found._"
    html_body = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "toc", "attr_list"],
    )
    return templates.TemplateResponse(request, "help.html", {
        "readme_html": html_body,
    })
