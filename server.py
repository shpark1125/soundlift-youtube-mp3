"""Local YouTube audio converter. Run with: python server.py"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import imageio_ffmpeg
import yt_dlp


ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DOWNLOADS = ROOT / "downloads"
DOWNLOADS.mkdir(exist_ok=True)
CACHE = DOWNLOADS / "cache"
CACHE.mkdir(exist_ok=True)
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
JOBS: dict[str, dict] = {}
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
MAX_DURATION = 4 * 60 * 60


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
        elif parsed.path.startswith("/shorts/") or parsed.path.startswith("/live/"):
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
        "retries": 3,
        "extractor_retries": 2,
        **extra,
    }


def inspect(url: str) -> dict:
    canonical, video_id = canonical_url(url)
    cached = CACHE / f"{video_id}.json"
    if (CACHE / f"{video_id}.mp3").is_file() and cached.is_file():
        return json.loads(cached.read_text(encoding="utf-8"))
    with yt_dlp.YoutubeDL(ydl_options(skip_download=True)) as ydl:
        info = ydl.extract_info(canonical, download=False)
    if not info or info.get("is_live"):
        raise ValueError("실시간 영상은 지원하지 않습니다.")
    duration = info.get("duration") or 0
    if duration > MAX_DURATION:
        raise ValueError("4시간 이하의 영상만 변환할 수 있습니다.")
    audio_formats = [
        f for f in info.get("formats", [])
        if f.get("acodec") not in (None, "none") and f.get("vcodec") == "none"
    ]
    best = max(audio_formats, key=lambda f: f.get("abr") or f.get("tbr") or 0, default={})
    summary = {
        "id": video_id,
        "url": canonical,
        "title": info.get("title") or video_id,
        "channel": info.get("channel") or info.get("uploader") or "YouTube",
        "duration": duration,
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "source_codec": best.get("acodec") or "알 수 없음",
        "source_kbps": round(best.get("abr") or best.get("tbr") or 0),
    }
    cached.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    return summary


def convert(job_id: str, url: str):
    job = JOBS[job_id]
    folder = DOWNLOADS / job_id
    folder.mkdir(exist_ok=True)

    def progress(data):
        if data.get("status") == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            current = data.get("downloaded_bytes") or 0
            job["progress"] = min(85, round(current / total * 85)) if total else 10

    try:
        job["status"] = "downloading"
        with yt_dlp.YoutubeDL(ydl_options(
            format="bestaudio/best",
            outtmpl=str(folder / "source.%(ext)s"),
            progress_hooks=[progress],
        )) as ydl:
            info = ydl.extract_info(url, download=True)
        if (info.get("duration") or 0) > MAX_DURATION:
            raise ValueError("4시간 이하의 영상만 변환할 수 있습니다.")
        sources = [p for p in folder.iterdir() if p.name.startswith("source.") and not p.name.endswith(".part")]
        if not sources:
            raise RuntimeError("오디오 파일을 받지 못했습니다.")
        source = sources[0]
        output = folder / "audio.mp3"
        job["status"] = "encoding"
        job["progress"] = 90
        result = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
             "-vn", "-codec:a", "libmp3lame", "-b:a", "320k", "-id3v2_version", "3", str(output)],
            capture_output=True, text=True, timeout=MAX_DURATION + 120,
        )
        if result.returncode or not output.exists() or not output.stat().st_size:
            raise RuntimeError(result.stderr.strip()[-500:] or "MP3 인코딩에 실패했습니다.")
        source.unlink(missing_ok=True)
        cached_file = CACHE / f"{job['video_id']}.mp3"
        shutil.move(str(output), str(cached_file))
        job.update(status="ready", progress=100, title=info.get("title") or info.get("id"), size=cached_file.stat().st_size)
    except Exception as exc:
        detail = str(exc)
        if "HTTP Error 403" in detail:
            detail = "YouTube가 오디오 요청을 거부했습니다(403). 잠시 후 다시 시도하거나 yt-dlp를 업데이트해 주세요."
        job.update(status="error", error=detail, progress=0)


class Handler(BaseHTTPRequestHandler):
    def send_json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path not in {"/api/inspect", "/api/jobs"}:
            return self.send_json({"error": "찾을 수 없습니다."}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 8192:
                raise ValueError("요청 크기가 올바르지 않습니다.")
            body = json.loads(self.rfile.read(length))
            url, video_id = canonical_url(str(body.get("url", "")))
            if self.path == "/api/inspect":
                return self.send_json(inspect(url))
            job_id = uuid.uuid4().hex
            cached_file = CACHE / f"{video_id}.mp3"
            if cached_file.is_file():
                metadata = CACHE / f"{video_id}.json"
                title = json.loads(metadata.read_text(encoding="utf-8")).get("title", video_id) if metadata.is_file() else video_id
                JOBS[job_id] = {"id": job_id, "video_id": video_id, "status": "ready", "progress": 100,
                                "created": time.time(), "title": title, "size": cached_file.stat().st_size}
                return self.send_json({"id": job_id}, 202)
            JOBS[job_id] = {"id": job_id, "video_id": video_id, "status": "queued", "progress": 0, "created": time.time()}
            threading.Thread(target=convert, args=(job_id, url), daemon=True).start()
            return self.send_json({"id": job_id}, 202)
        except (ValueError, json.JSONDecodeError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            return self.send_json({"error": str(exc)}, 502)

    def do_GET(self):
        if self.path == "/":
            return self.send_file(WEB / "index.html", "text/html; charset=utf-8")
        if self.path == "/app.css":
            return self.send_file(WEB / "app.css", "text/css; charset=utf-8")
        if self.path == "/app.js":
            return self.send_file(WEB / "app.js", "text/javascript; charset=utf-8")
        match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})(/download)?", self.path)
        if not match:
            return self.send_json({"error": "찾을 수 없습니다."}, 404)
        job = JOBS.get(match.group(1))
        if not job:
            return self.send_json({"error": "작업을 찾을 수 없습니다."}, 404)
        if not match.group(2):
            return self.send_json({k: v for k, v in job.items() if k != "created"})
        if job["status"] != "ready":
            return self.send_json({"error": "아직 변환이 끝나지 않았습니다."}, 409)
        title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", job.get("title") or "audio").strip(" .")[:120]
        self.send_file(CACHE / f"{job['video_id']}.mp3", "audio/mpeg", f'{title or "audio"}.mp3')

    def send_file(self, path: Path, content_type: str, download_name: str | None = None):
        if not path.is_file():
            return self.send_json({"error": "파일을 찾을 수 없습니다."}, 404)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("X-Content-Type-Options", "nosniff")
        if download_name:
            from urllib.parse import quote
            self.send_header("Content-Disposition", f"attachment; filename=audio.mp3; filename*=UTF-8''{quote(download_name)}")
        self.end_headers()
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 3000), Handler)
    print("MP3 사이트: http://localhost:3000", flush=True)
    server.serve_forever()
