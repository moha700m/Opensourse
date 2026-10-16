from __future__ import annotations

import os
import asyncio
import json
import re
import shutil
import threading
import uuid
import base64
import binascii
import hmac
import hashlib
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from openshorts.pipeline import process_video

load_dotenv()

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(ROOT / "output"))).expanduser().resolve()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024)))
UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}

app = FastAPI(title="OpenShorts", description="حوّل فيديو YouTube طويلًا إلى شورتات عمودية.")
app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_processing_lock = threading.Lock()
_upload_locks: dict[str, asyncio.Lock] = {}
_runtime_api_key = ""
_app_password = os.getenv("APP_PASSWORD", "")
_app_username = os.getenv("APP_USERNAME", "openshorts")
SESSION_COOKIE = "openshorts_session"
SESSION_SECONDS = 7 * 24 * 60 * 60


def _same_text(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _session_signature(payload: str) -> str:
    key = hmac.new(_app_password.encode("utf-8"), b"openshorts-session-v1", hashlib.sha256).digest()
    return hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()


def _new_session() -> str:
    raw = json.dumps({"user": _app_username, "expires": int(time.time()) + SESSION_SECONDS}).encode("utf-8")
    payload = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return payload + "." + _session_signature(payload)


def _valid_session(token: str) -> bool:
    try:
        if len(token) > 4096:
            return False
        payload, signature = token.split(".", 1)
        if not _same_text(signature, _session_signature(payload)):
            return False
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return data.get("user") == _app_username and int(data.get("expires", 0)) > time.time()
    except (ValueError, TypeError, UnicodeError, AttributeError, binascii.Error):
        return False


@app.middleware("http")
async def require_deployment_password(request: Request, call_next):
    if not _app_password or request.url.path in {"/healthz", "/login", "/api/login"}:
        return await call_next(request)

    if _valid_session(request.cookies.get(SESSION_COOKIE, "")):
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
        or not _same_text(username, _app_username)
        or not _same_text(password, _app_password)
    ):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "سجّل الدخول إلى الموقع أولًا."}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


class ClipOptions(BaseModel):
    clip_count: int = Field(default=5, ge=1, le=12)
    min_seconds: int = Field(default=30, ge=15, le=90)
    max_seconds: int = Field(default=60, ge=20, le=120)


class CreateJobRequest(ClipOptions):
    url: str = Field(min_length=10, max_length=2048)


class CreateUploadRequest(ClipOptions):
    filename: str = Field(min_length=1, max_length=240)
    size: int = Field(ge=1, le=MAX_UPLOAD_BYTES)


class SettingsRequest(BaseModel):
    api_key: str = Field(default="", max_length=500)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=500)


@app.get("/login")
def login_page(request: Request):
    if not _app_password or _valid_session(request.cookies.get(SESSION_COOKIE, "")):
        return RedirectResponse("/", status_code=303)
    return FileResponse(WEB_DIR / "login.html", headers={"Cache-Control": "no-store"})


@app.post("/api/login")
def login(request: LoginRequest, connection: Request):
    if _app_password and not (
        _same_text(request.username.strip(), _app_username)
        and _same_text(request.password, _app_password)
    ):
        raise HTTPException(status_code=401, detail="بيانات الدخول غير صحيحة. تحقق من اسم المستخدم وكلمة المرور.")
    response = JSONResponse({"authenticated": True}, headers={"Cache-Control": "no-store"})
    response.set_cookie(
        SESSION_COOKIE, _new_session(), max_age=SESSION_SECONDS,
        httponly=True, secure=connection.url.scheme == "https" or bool(os.getenv("RENDER")),
        samesite="lax", path="/",
    )
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


def _is_youtube_url(raw_url: str) -> bool:
    parsed = urlparse(raw_url.strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme in {"https", "http"} and host in {
        "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"
    }


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in job.items() if key not in {"work_dir", "source_path", "options"}}


def _persist_job(job: dict[str, Any]) -> None:
    directory = OUTPUT_DIR / job["id"]
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "job.json.tmp"
    temporary.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    temporary.replace(directory / "job.json")


def _new_job(job_id: str) -> dict[str, Any]:
    return {"id": job_id, "status": "queued", "progress": 0,
            "stage": "أُضيف الفيديو إلى قائمة المعالجة", "title": "", "clips": [],
            "selection_method": "", "error": ""}


def _validate_options(request: ClipOptions) -> None:
    if request.max_seconds < request.min_seconds:
        raise HTTPException(status_code=422, detail="الحد الأعلى لطول المقطع يجب أن يكون أكبر من الحد الأدنى.")


def _run_job(
    job_id: str, request: ClipOptions, cookies_path: Path | None = None,
    source_path: Path | None = None, source_title: str = "",
) -> None:
    def update(**values: Any) -> None:
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id].update(values)
                _persist_job(_jobs[job_id])

    try:
        # Only one transcription/render at a time on a small Render instance.
        with _processing_lock:
            result = process_video(
                job_id=job_id,
                url=getattr(request, "url", ""),
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
                source_path=source_path,
                source_title=source_title,
            )
        update(status="completed", progress=100, stage="اكتمل تجهيز المقاطع", **result)
    except Exception as exc:
        message = str(exc).strip() or "تعذر إكمال معالجة الفيديو."
        update(status="failed", stage="تعذرت المعالجة", error=message[:500])
    finally:
        if cookies_path:
            cookies_path.unlink(missing_ok=True)


# Restore completed results and interrupted uploads when the process restarts.
for _state_file in OUTPUT_DIR.glob("*/job.json"):
    try:
        _saved = json.loads(_state_file.read_text(encoding="utf-8"))
        if not re.fullmatch(r"[0-9a-f]{12}", str(_saved.get("id", ""))):
            continue
        if _saved["id"] != _state_file.parent.name:
            continue
        if _saved.get("status") in {"queued", "processing"}:
            _saved.update(status="failed", stage="انقطعت المعالجة", error="أُعيد تشغيل الخادم أثناء المعالجة. أعد إرسال الفيديو.")
        if _saved.get("status") == "uploading":
            _partial = _state_file.parent / "upload.part"
            _saved["received_bytes"] = _partial.stat().st_size if _partial.exists() else 0
        _jobs[_saved["id"]] = _saved
    except (OSError, ValueError, KeyError, TypeError):
        continue


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
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "upload_chunk_bytes": UPLOAD_CHUNK_BYTES,
        "login_required": bool(_app_password),
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
    _validate_options(request)

    job_id = uuid.uuid4().hex[:12]
    cookies_path = None
    if cookies_text:
        job_dir = OUTPUT_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cookies_path = job_dir / ".youtube-cookies.txt"
        descriptor = os.open(cookies_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as cookie_file:
            cookie_file.write(cookies_text)

    job = _new_job(job_id)
    with _jobs_lock:
        _jobs[job_id] = job
        _persist_job(job)
    background_tasks.add_task(_run_job, job_id, request, cookies_path)
    return _public_job(job)


@app.post("/api/jobs", status_code=202)
def create_job(request: CreateJobRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    return _queue_job(request, background_tasks)


@app.post("/api/uploads", status_code=201)
async def create_upload(request: CreateUploadRequest) -> dict[str, Any]:
    _validate_options(request)
    filename = Path(request.filename.replace("\\", "/")).name
    if Path(filename).suffix.lower() not in VIDEO_EXTENSIONS:
        raise HTTPException(status_code=422, detail="اختر ملف فيديو MP4 أو MOV أو WebM أو MKV أو AVI.")
    job_id = uuid.uuid4().hex[:12]
    directory = OUTPUT_DIR / job_id
    directory.mkdir(mode=0o700)
    (directory / "upload.part").touch(mode=0o600)
    job = {**_new_job(job_id), "status": "uploading", "stage": "بانتظار رفع الفيديو من الجوال",
           "filename": filename, "size": request.size, "received_bytes": 0,
           "options": request.model_dump(include={"clip_count", "min_seconds", "max_seconds"})}
    with _jobs_lock:
        _jobs[job_id] = job
        _persist_job(job)
    return _public_job(job)


def _upload_job(job_id: str) -> dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or "size" not in job:
            raise HTTPException(status_code=404, detail="انتهت جلسة الرفع. اختر الفيديو مرة أخرى.")
        return job


@app.put("/api/uploads/{job_id}")
async def upload_chunk(job_id: str, request: Request, offset: int = 0) -> dict[str, Any]:
    job = _upload_job(job_id)
    async with _upload_locks.setdefault(job_id, asyncio.Lock()):
        if job["status"] != "uploading":
            raise HTTPException(status_code=409, detail="انتهى رفع هذا الفيديو بالفعل.")
        partial = OUTPUT_DIR / job_id / "upload.part"
        received = partial.stat().st_size
        if offset != received:
            raise HTTPException(status_code=409, detail={"received_bytes": received})
        data = bytearray()
        async for block in request.stream():
            data.extend(block)
            if len(data) > UPLOAD_CHUNK_BYTES:
                raise HTTPException(status_code=413, detail="جزء الرفع أكبر من الحد المسموح.")
        if not data or received + len(data) > job["size"]:
            raise HTTPException(status_code=422, detail="حجم بيانات الرفع لا يطابق الفيديو المختار.")
        # A dropped request never appends half a chunk; the next request can
        # safely resume from the committed byte offset.
        with partial.open("ab") as destination:
            destination.write(data)
        with _jobs_lock:
            job.update(received_bytes=received + len(data),
                       progress=round(25 * (received + len(data)) / job["size"]),
                       stage="جارٍ رفع الفيديو من الجوال")
            _persist_job(job)
        return _public_job(job)


@app.post("/api/uploads/{job_id}/complete", status_code=202)
async def complete_upload(job_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
    job = _upload_job(job_id)
    async with _upload_locks.setdefault(job_id, asyncio.Lock()):
        # Retrying after a lost response must not queue the same video twice.
        if job["status"] != "uploading":
            return _public_job(job)
        partial = OUTPUT_DIR / job_id / "upload.part"
        if partial.stat().st_size != job["size"]:
            raise HTTPException(status_code=409, detail="لم يكتمل رفع الفيديو بعد.")
        source = partial.with_name("upload" + Path(job["filename"]).suffix.lower())
        partial.replace(source)
        with _jobs_lock:
            job.update(status="queued", progress=25, stage="اكتمل الرفع؛ بانتظار المعالجة", source_path=str(source))
            _persist_job(job)
        background_tasks.add_task(_run_job, job_id, ClipOptions(**job["options"]), None, source, Path(job["filename"]).stem)
        return _public_job(job)


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
        if job.get("status") == "uploading":
            partial = OUTPUT_DIR / job_id / "upload.part"
            job["received_bytes"] = partial.stat().st_size if partial.exists() else 0
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
        job = _jobs.get(job_id)
        if job and (job["status"] in {"queued", "processing"} or
                    (job_id in _upload_locks and _upload_locks[job_id].locked())):
            raise HTTPException(status_code=409, detail="انتظر انتهاء العملية الحالية قبل الحذف.")
        job = _jobs.pop(job_id, None)
    if not job:
        raise HTTPException(status_code=404, detail="لم يتم العثور على العملية.")
    shutil.rmtree(OUTPUT_DIR / job_id, ignore_errors=True)
    return {"deleted": True}
