"""Vercel entrypoint for Soundlift's stateless MP3 conversion."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import imageio_ffmpeg
import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


ROOT = Path(__file__).resolve().parent
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
MAX_DURATION = 30 * 60  # Keep conversions within Vercel's function time limit.

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class VideoRequest(BaseModel):
    url: str


@app.get("/api/mode")
def mode():
    return {"mode": "cloud"}


def canonical_url(raw: str) -> tuple[str, str]:
    parsed = urlparse(raw.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("https://로 시작하는 유튜브 링크를 입력해 주세요.")
    if host in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.strip("/").split("/")[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif parsed.path.startswith(("/shorts/", "/live/")):
            video_id = parsed.path.split("/")[2]
        else:
            video_id = ""
    else:
        video_id = ""
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError("유효한 유튜브 영상 링크를 입력해 주세요.")
    return f"https://www.youtube.com/watch?v={video_id}", video_id


def ydl_options(**extra):
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 2,
        **extra,
    }


def friendly_error(exc: Exception) -> str:
    detail = str(exc)
    if "Sign in to confirm you" in detail:
        return "YouTube가 Vercel 서버의 요청을 차단했습니다. 이 영상은 온라인에서 변환할 수 없습니다. 로컬 버전을 이용해 주세요."
    if "HTTP Error 403" in detail:
        return "YouTube가 이 서버의 오디오 요청을 거부했습니다(403). 잠시 후 다시 시도해 주세요."
    return detail[-500:]


@app.post("/api/inspect")
def inspect_video(request: VideoRequest):
    try:
        url, video_id = canonical_url(request.url)
        with yt_dlp.YoutubeDL(ydl_options(skip_download=True)) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info or info.get("is_live"):
            raise ValueError("실시간 영상은 지원하지 않습니다.")
        duration = info.get("duration") or 0
        if duration > MAX_DURATION:
            raise ValueError("온라인 버전은 30분 이하의 영상만 변환할 수 있습니다.")
        formats = [
            fmt for fmt in info.get("formats", [])
            if fmt.get("acodec") not in (None, "none") and fmt.get("vcodec") == "none"
        ]
        best = max(formats, key=lambda fmt: fmt.get("abr") or fmt.get("tbr") or 0, default={})
        return {
            "id": video_id,
            "url": url,
            "title": info.get("title") or video_id,
            "channel": info.get("channel") or info.get("uploader") or "YouTube",
            "duration": duration,
            "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "source_codec": best.get("acodec") or "알 수 없음",
            "source_kbps": round(best.get("abr") or best.get("tbr") or 0),
        }
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, detail=friendly_error(exc)) from exc


@app.post("/api/convert")
def convert_video(request: VideoRequest):
    try:
        url, video_id = canonical_url(request.url)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc

    folder = Path(tempfile.mkdtemp(prefix="soundlift-"))
    try:
        with yt_dlp.YoutubeDL(ydl_options(
            format="bestaudio/best",
            outtmpl=str(folder / "source.%(ext)s"),
        )) as ydl:
            info = ydl.extract_info(url, download=True)
        if not info or info.get("is_live"):
            raise ValueError("실시간 영상은 지원하지 않습니다.")
        if (info.get("duration") or 0) > MAX_DURATION:
            raise ValueError("온라인 버전은 30분 이하의 영상만 변환할 수 있습니다.")
        sources = [p for p in folder.glob("source.*") if not p.name.endswith(".part")]
        if not sources:
            raise RuntimeError("오디오 파일을 받지 못했습니다.")
        output = folder / "audio.mp3"
        result = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(sources[0]), "-vn", "-codec:a", "libmp3lame", "-b:a", "320k",
             "-id3v2_version", "3", str(output)],
            capture_output=True, text=True, timeout=240,
        )
        if result.returncode or not output.is_file() or not output.stat().st_size:
            raise RuntimeError(result.stderr.strip()[-500:] or "MP3 인코딩에 실패했습니다.")
        title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", info.get("title") or video_id).strip(" .")[:120]
        filename = f"{title or video_id}.mp3"

        def stream_file():
            try:
                with output.open("rb") as mp3:
                    while chunk := mp3.read(1024 * 256):
                        yield chunk
            finally:
                shutil.rmtree(folder, ignore_errors=True)

        return StreamingResponse(
            stream_file(), media_type="audio/mpeg",
            headers={
                "Content-Disposition": f"attachment; filename=audio.mp3; filename*=UTF-8''{quote(filename)}",
                "Content-Length": str(output.stat().st_size),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except ValueError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(400, detail=str(exc)) from exc
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(502, detail=friendly_error(exc)) from exc


app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
