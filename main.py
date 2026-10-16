from __future__ import annotations

import os
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from openshorts.pipeline import process_video

load_dotenv()

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(ROOT / "output"))).expanduser().resolve()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="OpenShorts", description="حوّل فيديو YouTube طويلًا إلى شورتات عمودية.")
app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_runtime_api_key = ""


class CreateJobRequest(BaseModel):
    url: str = Field(min_length=10, max_length=2048)
    clip_count: int = Field(default=5, ge=1, le=12)
    min_seconds: int = Field(default=30, ge=15, le=90)
    max_seconds: int = Field(default=60, ge=20, le=120)


class SettingsRequest(BaseModel):
    api_key: str = Field(default="", max_length=500)


def _is_youtube_url(raw_url: str) -> bool:
    parsed = urlparse(raw_url.strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme in {"https", "http"} and host in {
        "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"
    }


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in job.items() if key not in {"work_dir", "source_path"}}


def _run_job(job_id: str, request: CreateJobRequest) -> None:
    def update(**values: Any) -> None:
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id].update(values)

    try:
        result = process_video(
            job_id=job_id,
            url=request.url,
            clip_count=request.clip_count,
            min_seconds=request.min_seconds,
            max_seconds=request.max_seconds,
            output_dir=OUTPUT_DIR,
            api_key=_runtime_api_key or os.getenv("OPENROUTER_API_KEY", ""),
            model=os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
            whisper_model=os.getenv("WHISPER_MODEL", "base"),
            max_video_seconds=int(os.getenv("MAX_UPLOAD_SECONDS", "14400")),
            progress=update,
        )
        update(status="completed", progress=100, stage="اكتمل تجهيز المقاطع", **result)
    except Exception as exc:
        message = str(exc).strip() or "تعذر إكمال معالجة الفيديو."
        update(status="failed", stage="تعذرت المعالجة", error=message[:500])


@app.get("/")
def home() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return {
        "openrouter_configured": bool(_runtime_api_key or os.getenv("OPENROUTER_API_KEY")),
        "model": os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
        "whisper_model": os.getenv("WHISPER_MODEL", "base"),
    }


@app.post("/api/settings")
def set_settings(request: SettingsRequest) -> dict[str, bool]:
    global _runtime_api_key
    _runtime_api_key = request.api_key.strip()
    return {"openrouter_configured": bool(_runtime_api_key or os.getenv("OPENROUTER_API_KEY"))}


@app.post("/api/jobs", status_code=202)
def create_job(request: CreateJobRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    if not _is_youtube_url(request.url):
        raise HTTPException(status_code=422, detail="أدخل رابط فيديو من YouTube بصيغة https://youtube.com أو https://youtu.be.")
    if request.max_seconds < request.min_seconds:
        raise HTTPException(status_code=422, detail="الحد الأعلى لطول المقطع يجب أن يكون أكبر من الحد الأدنى.")

    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "status": "queued",
        "progress": 0,
        "stage": "أُضيف الفيديو إلى قائمة المعالجة",
        "title": "",
        "clips": [],
        "selection_method": "",
        "error": "",
    }
    with _jobs_lock:
        _jobs[job_id] = job
    background_tasks.add_task(_run_job, job_id, request)
    return _public_job(job)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="لم يتم العثور على عملية المعالجة.")
        return _public_job(job)


@app.get("/api/jobs/{job_id}/clips/{clip_name}/{file_type}")
def download_clip(job_id: str, clip_name: str, file_type: str) -> FileResponse:
    if file_type not in {"mp4", "srt", "json"} or not re.fullmatch(r"clip-\d{2}", clip_name):
        raise HTTPException(status_code=404, detail="الملف غير موجود.")
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or job.get("status") != "completed":
        raise HTTPException(status_code=404, detail="الملف غير جاهز.")
    path = OUTPUT_DIR / job_id / f"{clip_name}.{file_type}"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="الملف غير موجود.")
    media_type = {"mp4": "video/mp4", "srt": "text/plain; charset=utf-8", "json": "application/json"}[file_type]
    return FileResponse(path, media_type=media_type, filename=path.name)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict[str, bool]:
    with _jobs_lock:
        job = _jobs.pop(job_id, None)
    if not job:
        raise HTTPException(status_code=404, detail="لم يتم العثور على العملية.")
    shutil.rmtree(OUTPUT_DIR / job_id, ignore_errors=True)
    return {"deleted": True}
