from __future__ import annotations

import os
import re
import shutil
import threading
import uuid
import base64
import binascii
import hmac
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
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
_app_password = os.getenv("APP_PASSWORD", "")
_app_username = os.getenv("APP_USERNAME", "openshorts")


@app.middleware("http")
async def require_deployment_password(request: Request, call_next):
    if not _app_password or request.url.path == "/healthz":
        return await call_next(request)

    scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
    try:
        credentials = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        credentials = ""
    username, separator, password = credentials.partition(":")
    if (
        scheme.lower() != "basic"
        or not separator
        or not hmac.compare_digest(username, _app_username)
        or not hmac.compare_digest(password, _app_password)
    ):
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="OpenShorts"'},
        )
    return await call_next(request)


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


def _run_job(job_id: str, request: CreateJobRequest, cookies_path: Path | None = None) -> None:
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
            cookies_path=cookies_path,
        )
        update(status="completed", progress=100, stage="اكتمل تجهيز المقاطع", **result)
    except Exception as exc:
        message = str(exc).strip() or "تعذر إكمال معالجة الفيديو."
        update(status="failed", stage="تعذرت المعالجة", error=message[:500])
    finally:
        if cookies_path:
            cookies_path.unlink(missing_ok=True)


@app.get("/")
def home() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/healthz")
def health() -> dict[str, bool]:
    return {"ok": True}


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


def _queue_job(
    request: CreateJobRequest,
    background_tasks: BackgroundTasks,
    cookies_text: str | None = None,
) -> dict[str, Any]:
    if not _is_youtube_url(request.url):
        raise HTTPException(status_code=422, detail="أدخل رابط فيديو من YouTube بصيغة https://youtube.com أو https://youtu.be.")
    if request.max_seconds < request.min_seconds:
        raise HTTPException(status_code=422, detail="الحد الأعلى لطول المقطع يجب أن يكون أكبر من الحد الأدنى.")

    job_id = uuid.uuid4().hex[:12]
    cookies_path = None
    if cookies_text:
        job_dir = OUTPUT_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cookies_path = job_dir / ".youtube-cookies.txt"
        descriptor = os.open(cookies_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as cookie_file:
            cookie_file.write(cookies_text)

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
    background_tasks.add_task(_run_job, job_id, request, cookies_path)
    return _public_job(job)


@app.post("/api/jobs", status_code=202)
def create_job(request: CreateJobRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    return _queue_job(request, background_tasks)


def _youtube_cookie_file(contents: bytes) -> str:
    if len(contents) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="حجم ملف cookies أكبر من 2 ميغابايت.")
    try:
        raw = contents.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="ملف cookies يجب أن يكون نصًا بترميز UTF-8.") from exc

    allowed_domains = {"youtube.com", "google.com"}
    kept_lines: list[str] = []
    kept_cookies = 0
    for line in raw.splitlines():
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            kept_lines.append(line)
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        domain = fields[0].removeprefix("#HttpOnly_").lstrip(".").lower()
        if any(domain == base or domain.endswith("." + base) for base in allowed_domains):
            kept_lines.append(line)
            kept_cookies += 1
    if kept_cookies == 0:
        raise HTTPException(status_code=422, detail="لم أجد cookies لـ YouTube أو Google في الملف.")
    return "\n".join(kept_lines) + "\n"


@app.post("/api/jobs/with-cookies", status_code=202)
async def create_job_with_cookies(
    background_tasks: BackgroundTasks,
    request_json: str = Form(),
    cookies: UploadFile = File(),
) -> dict[str, Any]:
    try:
        request = CreateJobRequest.model_validate_json(request_json)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="إعدادات الفيديو غير صالحة.") from exc
    contents = await cookies.read(2 * 1024 * 1024 + 1)
    await cookies.close()
    cookies_text = _youtube_cookie_file(contents)
    return _queue_job(request, background_tasks, cookies_text)


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
