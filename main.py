import os
import uuid
from typing import Dict

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from analyzer import detect_intent, execute_analysis, explain_result, generate_initial_insights
from config import APP_TITLE, UPLOAD_DIR, get_llm
from data_loader import (
    calculate_business_metrics,
    detect_dataset_type,
    get_preview,
    get_statistics,
    load_dataset,
    validate_dataset,
)

app = FastAPI(title=APP_TITLE)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory session store: {session_id: {"df": ..., "metrics": ..., "summary": ...}}
SESSIONS: Dict[str, dict] = {}


class AskRequest(BaseModel):
    session_id: str
    question: str


@app.on_event("startup")
def check_api_key() -> None:
    if not os.environ.get("GROQ_API_KEY"):
        # Don't crash the whole app -- let /api/health report it instead.
        print("WARNING: GROQ_API_KEY is not set.")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "groq_key_set": bool(os.environ.get("GROQ_API_KEY"))}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict:
    file_bytes = await file.read()
    try:
        df = load_dataset(file_bytes, file.filename)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Could not read this file: {exc}") from exc

    warnings = validate_dataset(df)
    if warnings:
        raise HTTPException(status_code=400, detail="; ".join(warnings))

    save_path = os.path.join(UPLOAD_DIR, file.filename)
    with open(save_path, "wb") as f:
        f.write(file_bytes)

    llm = get_llm()
    metrics = calculate_business_metrics(df)
    dataset_summary = generate_initial_insights(df, metrics, llm=llm)
    stats = get_statistics(df)

    session_id = str(uuid.uuid4())
    SESSIONS[session_id] = {
        "df": df,
        "metrics": metrics,
        "summary": dataset_summary,
    }

    return {
        "session_id": session_id,
        "dataset_type": dataset_summary.dataset_type,
        "summary": dataset_summary.summary,
        "data_quality_notes": dataset_summary.data_quality_notes,
        "suggested_questions": dataset_summary.suggested_questions,
        "metrics": metrics.model_dump(),
        "preview": {
            "columns": [str(c) for c in get_preview(df).columns],
            "rows": get_preview(df).astype(str).values.tolist(),
        },
        "stats": {
            "row_count": stats["row_count"],
            "column_count": stats["column_count"],
            "missing_values": {str(k): int(v) for k, v in stats["missing_values"].items()},
            "duplicate_rows": stats["duplicate_rows"],
        },
    }


@app.post("/api/sample")
def load_sample() -> dict:
    file_name = "filtered_sales.csv"
    if not os.path.exists(file_name):
        raise HTTPException(status_code=404, detail="Sample dataset not found on server.")
    with open(file_name, "rb") as f:
        file_bytes = f.read()

    df = load_dataset(file_bytes, file_name)
    llm = get_llm()
    metrics = calculate_business_metrics(df)
    dataset_summary = generate_initial_insights(df, metrics, llm=llm)
    stats = get_statistics(df)

    session_id = str(uuid.uuid4())
    SESSIONS[session_id] = {
        "df": df,
        "metrics": metrics,
        "summary": dataset_summary,
    }

    return {
        "session_id": session_id,
        "dataset_type": dataset_summary.dataset_type,
        "summary": dataset_summary.summary,
        "data_quality_notes": dataset_summary.data_quality_notes,
        "suggested_questions": dataset_summary.suggested_questions,
        "metrics": metrics.model_dump(),
        "preview": {
            "columns": [str(c) for c in get_preview(df).columns],
            "rows": get_preview(df).astype(str).values.tolist(),
        },
        "stats": {
            "row_count": stats["row_count"],
            "column_count": stats["column_count"],
            "missing_values": {str(k): int(v) for k, v in stats["missing_values"].items()},
            "duplicate_rows": stats["duplicate_rows"],
        },
    }


@app.post("/api/ask")
def ask(req: AskRequest) -> dict:
    session = SESSIONS.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found. Please reload your dataset.")

    df = session["df"]
    metrics = session["metrics"]
    dataset_summary = session["summary"]

    llm = get_llm()
    intent = detect_intent(req.question, df)
    result = execute_analysis(df, intent, metrics)
    response = explain_result(req.question, intent, result, dataset_summary, metrics, llm=llm)

    return {
        "question": req.question,
        "intent": intent["type"],
        "result": result,
        "answer": response.answer,
        "explanation": response.explanation,
        "recommendation": response.recommendation,
    }


# Serve the frontend
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse("static/index.html")
