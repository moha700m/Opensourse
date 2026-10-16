from __future__ import annotations

import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from faster_whisper import WhisperModel
from openai import OpenAI
from yt_dlp import YoutubeDL

Progress = Callable[..., None]


def _run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise RuntimeError(detail[-1][:350] if detail else "تعذر تشغيل FFmpeg.")
    return result.stdout.strip()


def _seconds(value: float) -> str:
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    seconds = value % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:05.2f}"


def _srt_time(value: float) -> str:
    milliseconds = max(0, int(round(value * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def _escape_filter_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")


def _download(
    url: str,
    work_dir: Path,
    progress: Progress,
    max_video_seconds: int,
    cookies_path: Path | None = None,
) -> tuple[Path, str, float]:
    progress(status="processing", progress=5, stage="جلب فيديو YouTube")
    output_template = str(work_dir / "source.%(ext)s")
    base_options = {
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "socket_timeout": 30,
        "restrictfilenames": True,
        "max_filesize": 2 * 1024 * 1024 * 1024,
    }
    if cookies_path:
        base_options["cookiefile"] = str(cookies_path)
    # YouTube sometimes blocks one player API on cloud-hosted IPs. Retry with
    # other public player clients before reporting the original access error.
    client_profiles: list[tuple[str, ...] | None] = [None, ("tv_embedded",), ("ios",), ("android_vr",)]
    last_error: Exception | None = None
    fallback_started = False
    try:
        for profile in client_profiles:
            if fallback_started:
                progress(progress=5, stage="نجرب طريقة أخرى لجلب الفيديو من YouTube")
            options = dict(base_options)
            if profile:
                options["extractor_args"] = {"youtube": {"player_client": list(profile)}}
            try:
                with YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=False)
                    duration = float(info.get("duration") or 0)
                    title = str(info.get("title") or "فيديو YouTube")[:160]
                    if not duration:
                        raise RuntimeError("لم أتمكن من قراءة مدة الفيديو.")
                    if duration > max_video_seconds:
                        raise RuntimeError(f"طول الفيديو يتجاوز الحد الحالي ({max_video_seconds // 3600} ساعات).")
                    ydl.download([url])
                break
            except RuntimeError:
                raise
            except Exception as exc:
                last_error = exc
                for partial in work_dir.glob("source.*"):
                    partial.unlink(missing_ok=True)
                message = str(exc).lower()
                blocked = "sign in to confirm" in message or "not a bot" in message
                if not fallback_started and not blocked:
                    raise RuntimeError(f"تعذر جلب الفيديو: {str(exc)[:260]}") from exc
                fallback_started = fallback_started or blocked
        else:
            detail = str(last_error or "رفض YouTube طلب التنزيل.")[:260]
            raise RuntimeError(f"تعذر جلب الفيديو: {detail}") from last_error

        candidates = list(work_dir.glob("source.*"))
        if not candidates:
            raise RuntimeError("انتهى تنزيل الفيديو من دون إنشاء ملف.")
        return candidates[0], title, duration
    finally:
        if cookies_path:
            cookies_path.unlink(missing_ok=True)


def _transcribe(source: Path, model_name: str, work_dir: Path, progress: Progress) -> tuple[list[dict[str, Any]], str]:
    audio_path = work_dir / "audio.wav"
    progress(status="processing", progress=30, stage="استخراج الصوت")
    _run(["ffmpeg", "-y", "-threads", "1", "-protocol_whitelist", "file,pipe", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio_path)])
    progress(progress=34, stage=f"تفريغ الكلام بالنموذج المحلي ({model_name})؛ قد يستغرق ذلك وقتًا أول مرة")
    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=2, num_workers=1,
                         download_root=os.getenv("WHISPER_CACHE_DIR"))
    segments_iterator, info = model.transcribe(str(audio_path), beam_size=1, vad_filter=True, word_timestamps=False)
    segments: list[dict[str, Any]] = []
    for segment in segments_iterator:
        text = re.sub(r"\s+", " ", segment.text).strip()
        if text:
            segments.append({"start": float(segment.start), "end": float(segment.end), "text": text})
    if not segments:
        raise RuntimeError("لم أتمكن من استخراج كلام واضح من الفيديو.")
    return segments, str(getattr(info, "language", "unknown"))


def _format_transcript(segments: list[dict[str, Any]]) -> str:
    return "\n".join(f"[{_seconds(item['start'])}] {item['text']}" for item in segments)


def _clean_model_json(raw: str) -> Any:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first < 0 or last < first:
        raise ValueError("Model response did not contain a JSON object")
    return json.loads(cleaned[first:last + 1])


def _analyze_chunk(
    client: OpenAI,
    model: str,
    chunk: list[dict[str, Any]],
    duration: float,
    min_seconds: int,
    max_seconds: int,
) -> list[dict[str, Any]]:
    prompt = f"""أنت محرر فيديو متخصص في اختيار لحظات قصيرة مستقلة وجذابة من بودكاست ومقابلات وفيديوهات طويلة.
حلل التفريغ التالي وحدد حتى 3 لحظات تستحق أن تتحول إلى شورت.

قواعد الاختيار:
- ابحث عن فكرة مفهومة بذاتها، قصة قصيرة، رأي لافت، إجابة مفاجئة، نصيحة عملية أو لحظة عاطفية. لا تختر جملة مبتورة.
- ابدأ قبل أول كلمة أو سؤال مهم بقليل، وأنهِ بعد اكتمال الفكرة. لا تتجاوز النص المتاح.
- طول المقطع من {min_seconds} إلى {max_seconds} ثانية. استخدم الثواني كأرقام عشرية من بداية الفيديو.
- فضّل لحظات من مواضع مختلفة، وقيّم قوة البداية والوضوح والتشويق من 1 إلى 10.
- اكتب العنوان والسبب بلغة التفريغ قدر الإمكان، وبالعربية إذا كان الكلام بالعربية.
- التوقيتات تقريبية ولكن يجب أن تكون ضمن المدى [{chunk[0]['start']:.2f}, {min(duration, chunk[-1]['end']):.2f}].

أعد JSON فقط بالشكل:
{{"clips":[{{"start":123.4,"end":168.2,"title":"عنوان قصير","hook":"أول جملة جاذبة","reason":"لماذا يصلح هذا المقطع؟","score":8}}]}}

التفريغ مع التوقيت:
{_format_transcript(chunk)}"""
    response = client.chat.completions.create(
        model=model,
        temperature=0.2,
        max_tokens=1100,
        messages=[
            {"role": "system", "content": "أنت محرر مقاطع قصير. أخرج JSON صالحًا فقط ولا تخترع توقيتات أو اقتباسات."},
            {"role": "user", "content": prompt},
        ],
    )
    content = response.choices[0].message.content or "{}"
    decoded = _clean_model_json(content)
    clips = decoded.get("clips", []) if isinstance(decoded, dict) else []
    return [item for item in clips if isinstance(item, dict)]


def _heuristic_clips(
    segments: list[dict[str, Any]], clip_count: int, min_seconds: int, max_seconds: int, duration: float
) -> list[dict[str, Any]]:
    hooks = re.compile(
        r"\b(why|how|secret|mistake|never|best|worst|truth|because|imagine|first|important|surprise|" 
        r"لازم|ليش|لماذا|كيف|الحقيقة|السر|خطأ|أهم|أفضل|أسوأ|تخيل|مفاجأة|لكن|لأن|تجربة|المشكلة|" 
        r"نجاح|فشل|قرار|نصيحة|السبب|أخيرًا)\b",
        re.IGNORECASE,
    )
    length = max_seconds
    step = max(10, length // 3)
    candidates: list[dict[str, Any]] = []
    start = 0.0
    while start + min_seconds <= duration:
        end = min(start + length, duration)
        spoken = [s for s in segments if s["end"] > start and s["start"] < end]
        text = " ".join(s["text"] for s in spoken)
        if len(text.split()) >= 18:
            score = min(10.0, 2.0 + len(hooks.findall(text)) * 0.85 + text.count("?") * 0.6 + text.count("!") * 0.4)
            candidates.append({
                "start": start,
                "end": end,
                "title": (text[:76].rsplit(" ", 1)[0] + "…") if len(text) > 76 else text,
                "hook": text[:110],
                "reason": "اختيار محلي حسب كثافة الأسئلة والكلمات الدالة على التشويق.",
                "score": score,
            })
        start += step
    candidates.sort(key=lambda item: item["score"], reverse=True)
    chosen: list[dict[str, Any]] = []
    for item in candidates:
        if all(item["end"] <= existing["start"] or item["start"] >= existing["end"] for existing in chosen):
            chosen.append(item)
            if len(chosen) >= clip_count:
                break
    return sorted(chosen, key=lambda item: item["start"])


def _choose_clips(
    segments: list[dict[str, Any]],
    duration: float,
    clip_count: int,
    min_seconds: int,
    max_seconds: int,
    api_key: str,
    model: str,
    progress: Progress,
) -> tuple[list[dict[str, Any]], str]:
    if not api_key:
        return _heuristic_clips(segments, clip_count, min_seconds, max_seconds, duration), "اختيار محلي بالكلمات المفتاحية"

    client = OpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        default_headers={"HTTP-Referer": "http://localhost:8000", "X-Title": "OpenShorts"},
    )
    # Analyze overlapping five-minute transcript blocks to keep requests small and cover the full video.
    chunks: list[list[dict[str, Any]]] = []
    cursor = 0.0
    while cursor < duration:
        block = [segment for segment in segments if cursor <= segment["start"] < cursor + 300]
        if block:
            chunks.append(block)
        cursor += 300

    raw_candidates: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        progress(progress=48 + int(16 * index / max(1, len(chunks))), stage=f"تحليل اللحظات المميزة ({index}/{len(chunks)}) عبر OpenRouter")
        try:
            chunk_candidates = _analyze_chunk(client, model, chunk, duration, min_seconds, max_seconds)
            for candidate in chunk_candidates:
                try:
                    candidate_start = float(candidate.get("start"))
                    candidate_end = float(candidate.get("end"))
                except (TypeError, ValueError):
                    continue
                # Reject timestamps that point to a different block of the video.
                if candidate_start < max(0.0, chunk[0]["start"] - 8):
                    continue
                if candidate_end > min(duration, chunk[-1]["end"] + max_seconds + 8):
                    continue
                raw_candidates.append(candidate)
        except Exception as exc:
            if index == len(chunks) and not raw_candidates:
                raise RuntimeError(f"تعذر تحليل النص عبر OpenRouter: {str(exc)[:220]}") from exc

    valid: list[dict[str, Any]] = []
    for item in raw_candidates:
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
            score = max(0.0, min(10.0, float(item.get("score", 5))))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(end):
            continue
        start = max(0.0, min(duration, start))
        end = max(0.0, min(duration, end))
        if end <= start:
            continue
        if end - start < min_seconds:
            end = min(duration, start + min_seconds)
        if end - start > max_seconds:
            end = start + max_seconds
        if end - start < min_seconds:
            start = max(0.0, end - min_seconds)
        valid.append({
            "start": round(start, 2),
            "end": round(end, 2),
            "title": str(item.get("title") or "لحظة مميزة")[:90],
            "hook": str(item.get("hook") or "")[:200],
            "reason": str(item.get("reason") or "")[:240],
            "score": score,
        })

    valid.sort(key=lambda item: item["score"], reverse=True)
    chosen: list[dict[str, Any]] = []
    for item in valid:
        if all(item["end"] <= existing["start"] + 8 or item["start"] >= existing["end"] - 8 for existing in chosen):
            chosen.append(item)
            if len(chosen) >= clip_count:
                break
    if not chosen:
        return _heuristic_clips(segments, clip_count, min_seconds, max_seconds, duration), "اختيار محلي احتياطي"
    return sorted(chosen, key=lambda item: item["start"]), f"OpenRouter · {model}"


def _wrap_caption(text: str, max_chars: int = 42) -> list[list[str]]:
    words = text.split()
    captions: list[list[str]] = []
    group: list[str] = []
    for word in words:
        candidate = " ".join([*group, word])
        if group and (len(candidate) > max_chars * 2 or len(group) >= 13):
            captions.append(_caption_lines(group, max_chars))
            group = [word]
        else:
            group.append(word)
    if group:
        captions.append(_caption_lines(group, max_chars))
    return captions


def _caption_lines(words: list[str], max_chars: int) -> list[str]:
    lines: list[str] = []
    line = ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if len(candidate) > max_chars and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines[:2]


def _write_srt(path: Path, segments: list[dict[str, Any]], clip_start: float, clip_end: float) -> None:
    rows: list[str] = []
    cue = 1
    for segment in segments:
        start = max(clip_start, segment["start"])
        end = min(clip_end, segment["end"])
        if end - start < 0.08:
            continue
        captions = _wrap_caption(segment["text"])
        if not captions:
            continue
        span = end - start
        for caption_index, lines in enumerate(captions):
            cue_start = start + span * caption_index / len(captions)
            cue_end = start + span * (caption_index + 1) / len(captions)
            rows.extend([
                str(cue),
                f"{_srt_time(cue_start - clip_start)} --> {_srt_time(cue_end - clip_start)}",
                "\n".join(lines),
                "",
            ])
            cue += 1
    path.write_text("\n".join(rows), encoding="utf-8")


def _render_clip(
    source: Path,
    out_dir: Path,
    clip: dict[str, Any],
    index: int,
    segments: list[dict[str, Any]],
) -> dict[str, Any]:
    clip_name = f"clip-{index:02d}"
    srt_path = out_dir / f"{clip_name}.srt"
    json_path = out_dir / f"{clip_name}.json"
    video_path = out_dir / f"{clip_name}.mp4"
    _write_srt(srt_path, segments, clip["start"], clip["end"])
    subtitle_filter = f"subtitles='{_escape_filter_path(srt_path)}':force_style='FontName=Noto Sans,FontSize=17,PrimaryColour=&H00FFFFFF,OutlineColour=&H00101218,BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=220'"
    # Crop before upscaling so landscape inputs do not create huge temporary
    # frames. This keeps the 1080x1920 export within small-instance memory.
    video_filter = f"crop='trunc(min(iw,ih*9/16)/2)*2':'trunc(min(ih,iw*16/9)/2)*2',scale=1080:1920,setsar=1,{subtitle_filter}"
    _run([
        "ffmpeg", "-y", "-filter_threads", "1", "-threads", "1", "-ss", f"{clip['start']:.3f}",
        "-protocol_whitelist", "file,pipe", "-i", str(source),
        "-t", f"{clip['end'] - clip['start']:.3f}", "-vf", video_filter,
        "-c:v", "libx264", "-threads", "1", "-preset", "veryfast", "-tune", "zerolatency",
        "-crf", "22", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(video_path),
    ])
    metadata = {
        "title": clip["title"],
        "start": clip["start"],
        "end": clip["end"],
        "duration": round(clip["end"] - clip["start"], 2),
        "hook": clip.get("hook", ""),
        "reason": clip.get("reason", ""),
        "score": clip.get("score", 0),
        "video": video_path.name,
        "subtitles": srt_path.name,
    }
    json_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        **metadata,
        "id": clip_name,
        "download_url": f"/api/jobs/{out_dir.name}/clips/{clip_name}/mp4",
        "subtitle_url": f"/api/jobs/{out_dir.name}/clips/{clip_name}/srt",
    }


def _probe_uploaded_video(source: Path, max_video_seconds: int) -> float:
    try:
        metadata = json.loads(_run([
            "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
            "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(source),
        ]))
        duration = float(metadata.get("format", {}).get("duration", 0))
    except (ValueError, TypeError, RuntimeError) as exc:
        raise RuntimeError("ملف الفيديو غير قابل للقراءة. اختر ملف MP4 أو MOV صالحًا.") from exc
    track_types = {stream.get("codec_type") for stream in metadata.get("streams", [])}
    if not math.isfinite(duration) or duration <= 0 or "video" not in track_types:
        raise RuntimeError("الملف المرفوع لا يحتوي فيديو صالحًا.")
    if "audio" not in track_types:
        raise RuntimeError("الفيديو لا يحتوي مسارًا صوتيًا. اختر فيديو فيه كلام لاستخراج اللحظات.")
    if duration > max_video_seconds:
        raise RuntimeError(f"مدة الفيديو تتجاوز الحد المسموح ({max_video_seconds // 60} دقيقة).")
    return duration


def process_video(
    job_id: str,
    url: str,
    clip_count: int,
    min_seconds: int,
    max_seconds: int,
    output_dir: Path,
    api_key: str,
    model: str,
    whisper_model: str,
    max_video_seconds: int,
    progress: Progress,
    cookies_path: Path | None = None,
    source_path: Path | None = None,
    source_title: str = "",
) -> dict[str, Any]:
    out_dir = output_dir / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        if source_path:
            progress(status="processing", progress=27, stage="التحقق من الفيديو المرفوع")
            source = source_path
            title = source_title or "فيديو من الجوال"
            duration = _probe_uploaded_video(source, max_video_seconds)
        else:
            source, title, duration = _download(url, out_dir, progress, max_video_seconds, cookies_path)
        if duration < min_seconds:
            raise RuntimeError(f"الفيديو أقصر من {min_seconds} ثانية. اختر مدة أقصر للمقاطع.")
        progress(title=title)
        segments, language = _transcribe(source, whisper_model, out_dir, progress)
        clips, method = _choose_clips(segments, duration, clip_count, min_seconds, max_seconds, api_key, model, progress)
        if not clips:
            raise RuntimeError("لم أجد مقاطع كلامية مناسبة ضمن الطول المطلوب.")
        rendered: list[dict[str, Any]] = []
        for index, clip in enumerate(clips, start=1):
            progress(progress=66 + int(32 * (index - 1) / len(clips)), stage=f"إنشاء المقطع العمودي {index} من {len(clips)}")
            rendered.append(_render_clip(source, out_dir, clip, index, segments))
        return {
            "title": title,
            "duration": duration,
            "language": language,
            "selection_method": method,
            "clips": rendered,
        }
    finally:
        # Retain the finished clips, not the large intermediate/source files.
        for intermediate in [out_dir / "audio.wav", *out_dir.glob("source.*")]:
            intermediate.unlink(missing_ok=True)
        if source_path:
            source_path.unlink(missing_ok=True)
