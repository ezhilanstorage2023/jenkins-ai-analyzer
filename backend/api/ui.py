"""Serves the review UI behind the same Basic auth as the API it calls."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from auth import require_role
from store.models import User

router = APIRouter()

_STATIC = Path(__file__).resolve().parent.parent / "static" / "review"

# Explicit whitelist: no path from the URL ever reaches the filesystem.
_FILES = {
    "index.html": "text/html",
    "review.js": "text/javascript",
    "review.css": "text/css",
}


@router.get("/ui/")
def ui_index(user: User = Depends(require_role("viewer"))):
    return ui_file("index.html", user)


@router.get("/ui/{name}")
def ui_file(name: str, user: User = Depends(require_role("viewer"))):
    if name not in _FILES:
        raise HTTPException(404, "Not found.")

    # no-cache = the browser may keep a copy but must revalidate it, so an
    # upgraded index.html is never paired with a stale cached review.js.
    return FileResponse(
        _STATIC / name,
        media_type=_FILES[name],
        headers={"Cache-Control": "no-cache"},
    )
