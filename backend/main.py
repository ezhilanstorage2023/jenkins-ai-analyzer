from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from groq import Groq
import os
from utils.log_processor import extract_error_context
from models import AnalysisResponse
from graph import analyze_log, build_analysis_response
load_dotenv()

api_key = os.getenv("GROQ_API_KEY")

print("Groq API key loaded:", api_key is not None)

client = Groq(
    api_key=api_key
)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
    "http://127.0.0.1:5500",
    "http://localhost:5500",
],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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

@app.get("/test-llm")
def test_llm():

    response = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[
            {
                "role": "user",
                "content": "Explain Jenkins in one sentence."
            }
        ]
    )

    return {
        "response": response.choices[0].message.content
    }

@app.post(
    "/analyze-log",
    response_model=AnalysisResponse
)
async def analyze_uploaded_log(file: UploadFile = File(...)):
    content = await file.read()

    text = content.decode(
        "utf-8",
        errors="replace"
    )

    result = analyze_log(text)

    analysis = build_analysis_response(
        result,
        file.filename
    )

    return analysis