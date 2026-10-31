from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from api import webhooks
from graph import analyze_log, build_analysis_response
from llm import LLMProviderError, LLMUnavailableError
from logging_config import configure_logging
from models import AnalysisResponse
from settings import get_settings

settings = get_settings()

configure_logging(settings.log_level)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhooks.router)


@app.get("/")
def home():
    return {
        "message": "Jenkins AI Analyzer API is running"
    }


@app.post("/upload-log")
async def upload_log(file: UploadFile = File(...)):
    content = await file.read()

    text = content.decode("utf-8", errors="replace")

    lines = text.splitlines()

    error_lines = [
        line for line in lines
        if "ERROR" in line.upper()
    ]

    warning_lines = [
        line for line in lines
        if "WARNING" in line.upper()
    ]

    build_failed = (
        "FAILURE" in text.upper()
        or "BUILD FAILED" in text.upper()
        or "EXIT CODE 1" in text.upper()
    )

    return {
        "filename": file.filename,
        "size": len(content),
        "total_lines": len(lines),
        "error_count": len(error_lines),
        "warning_count": len(warning_lines),
        "build_failed": build_failed,
    }


@app.post(
    "/analyze-log",
    response_model=AnalysisResponse
)
async def analyze_uploaded_log(file: UploadFile = File(...)):
    max_upload_bytes = get_settings().max_upload_bytes

    content = await file.read(max_upload_bytes + 1)

    if len(content) > max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Log file exceeds {max_upload_bytes} bytes."
        )

    text = content.decode(
        "utf-8",
        errors="replace"
    )

    # The graph makes blocking LLM calls; keep them off the event loop.
    try:
        result = await run_in_threadpool(analyze_log, text)
    except LLMUnavailableError:
        raise HTTPException(
            status_code=503,
            detail="LLM is not configured (GROQ_API_KEY missing)."
        )
    except LLMProviderError:
        raise HTTPException(
            status_code=503,
            detail="LLM provider request failed."
        )

    return build_analysis_response(
        result,
        file.filename
    )
