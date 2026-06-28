"""PPTX export route."""
import re as _re
import logging
from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel
from backend.api import OUTPUTS_DIR, resolve_output_url

router = APIRouter(tags=["pptx"])
logger = logging.getLogger("api.pptx")


class PPTXExportRequest(BaseModel):
    html_content: str = ""
    html_url: str = ""
    title: str = ""
    mode: str = "final"
    frames_per_slide: int = 3


@router.post("/skills/ppt-animation/export-pptx")
async def export_pptx(req: PPTXExportRequest):
    from backend.tools.ppt_export import export_skill_to_pptx_async

    html = req.html_content
    if not html and req.html_url:
        file_path = resolve_output_url(req.html_url)
        if file_path and file_path.exists() and file_path.suffix.lower() == ".html":
            html = file_path.read_text(encoding="utf-8")

    if not html:
        return {"error": "No HTML content provided"}
    if not _re.search(r"<!DOCTYPE\s+html|<html[\s>]", html, _re.IGNORECASE):
        return {"error": "Content does not appear to be HTML"}

    try:
        result = await export_skill_to_pptx_async(
            html,
            title=req.title or "",
            mode=req.mode,
            frames_per_slide=req.frames_per_slide,
        )
        return {
            "success": True,
            "file_url": result["file_url"],
            "download_url": result["download_url"],
            "slides": result["slides"],
            "pages": result.get("pages", result["slides"]),
            "title": result["title"],
            "mode": result.get("mode", req.mode),
            "rendered": result.get("rendered", ""),
            "warning": result.get("warning", ""),
        }
    except Exception as e:
        logger.error(f"PPTX export error: {e}")
        return {"error": f"Export failed: {e}"}


@router.get("/skills/ppt-animation/export-pptx/{filename}")
async def download_pptx(filename: str):
    from pathlib import Path
    safe_name = Path(filename).name
    file_path = OUTPUTS_DIR / safe_name
    if not file_path.exists() or not safe_name.endswith(".pptx"):
        return {"error": "File not found"}
    return FileResponse(
        path=str(file_path),
        filename=safe_name,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
