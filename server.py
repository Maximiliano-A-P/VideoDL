import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

import uvicorn
import yt_dlp
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

try:
    import curl_cffi  # noqa: F401
    from yt_dlp.networking.impersonate import ImpersonateTarget
    IMPERSONATE = ImpersonateTarget("chrome")
except Exception:
    IMPERSONATE = None

HOST, PORT = "127.0.0.1", 8000
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
CONFIG_PATH = Path.home() / ".videodl" / "config.json"
DEFAULT_DIR = Path(os.environ.get("DL_DIR", Path.home() / "Downloads" / "VideoDL"))
TOKEN = secrets.token_urlsafe(16)

READY = threading.Event()
LOCK = threading.Lock()  # protege JOBS
CFG_LOCK = threading.Lock()  # protege la carpeta de descargas
PICK_LOCK = threading.Lock()  # un solo selector de carpeta a la vez
JOBS: Dict[str, dict] = {}
POOL = ThreadPoolExecutor(max_workers=3)
FFMPEG: Optional[str] = None
_download_dir: Path = DEFAULT_DIR
ACTIVE = ("queued", "downloading", "processing")


# ---------- ffmpeg ----------
def find_ffmpeg() -> Optional[str]:
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    local = BASE_DIR / "ffmpeg" / exe
    if local.exists():
        return str(local)
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


# ---------- carpeta de descargas (se recuerda entre ejecuciones) ----------
def load_config():
    global _download_dir
    chosen = DEFAULT_DIR
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("download_dir")
        if saved and Path(saved).is_dir():
            chosen = Path(saved)
    except Exception:
        pass
    try:
        chosen.mkdir(parents=True, exist_ok=True)
    except OSError:
        chosen = DEFAULT_DIR
        chosen.mkdir(parents=True, exist_ok=True)
    with CFG_LOCK:
        _download_dir = chosen


def get_dir() -> Path:
    with CFG_LOCK:
        return _download_dir


def set_dir(path: Path):
    global _download_dir
    with CFG_LOCK:
        _download_dir = path
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps({"download_dir": str(path)}), encoding="utf-8")


def pick_folder_dialog(initial: str):
    """Se ejecuta en un proceso aparte (ver --pick-folder) para no mezclar tkinter con el servidor."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askdirectory(initialdir=initial, title="Elegí la carpeta de descargas", mustexist=True)
    root.destroy()
    print(path or "")


# ---------- app ----------
@asynccontextmanager
async def lifespan(_: FastAPI):
    global FFMPEG
    load_config()
    FFMPEG = find_ffmpeg()
    READY.set()
    yield


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def check_host(request: Request, call_next):
    if request.headers.get("host", "") not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "Host no permitido"}, status_code=403)
    return await call_next(request)


def auth(x_token: str = Header(default="")):
    if not secrets.compare_digest(x_token.encode(), TOKEN.encode()):
        raise HTTPException(401, "Token inválido")


class InfoReq(BaseModel):
    url: str
    headers: Optional[Dict[str, str]] = None  # para la extensión (fase 2)
    cookie: Optional[str] = None


class DownloadReq(InfoReq):
    mode: str = "video"  # "video" | "audio"
    height: Optional[int] = None
    audio_format: str = "mp3"  # mp3 | m4a | opus | wav


def check_url(url: str):
    if not re.match(r"^https?://", url.strip(), re.I):
        raise HTTPException(422, {"code": "url", "message": "El link debe empezar con http:// o https://"})


def clean_error(msg: str) -> dict:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", msg).replace("ERROR: ", "").strip()
    if "DRM" in msg:
        return {"code": "drm", "message": "Este contenido tiene DRM y no se puede descargar."}
    if "Unsupported URL" in msg:
        return {"code": "unsupported", "message": "No se encontró ningún video en este link."}
    if "Requested format is not available" in msg and not FFMPEG:
        return {"code": "ffmpeg", "message": "Este sitio separa video y audio. Instalá ffmpeg para poder descargarlo."}
    return {"code": "error", "message": msg[:300]}


def base_opts(req: InfoReq) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    headers = dict(req.headers or {})
    if req.cookie:
        headers["Cookie"] = req.cookie
    if headers:
        opts["http_headers"] = headers
    if FFMPEG:
        opts["ffmpeg_location"] = FFMPEG
    if IMPERSONATE:
        opts["impersonate"] = IMPERSONATE
    return opts


# ---------- tamaños estimados ----------
def fsize(f: dict, duration) -> Optional[int]:
    s = f.get("filesize") or f.get("filesize_approx")
    if not s and f.get("tbr") and duration:
        s = f["tbr"] * 125 * duration  # kbit/s -> bytes/s * duración
    return int(s) if s else None


def best_by_bitrate(fmts: list) -> Optional[dict]:
    return max(fmts, key=lambda f: f.get("tbr") or 0) if fmts else None


def build_options(formats: list, duration):
    no = (None, "none")
    video = [f for f in formats if f.get("vcodec") not in no and f.get("height")]
    audio_only = [f for f in formats if f.get("acodec") not in no and f.get("vcodec") in no]
    best_audio = best_by_bitrate(audio_only)
    audio_size = fsize(best_audio, duration) if best_audio else None
    options = []
    for h in sorted({f["height"] for f in video}, reverse=True):
        at_h = [f for f in video if f["height"] == h]
        video_only = [f for f in at_h if f.get("acodec") in no]
        v = best_by_bitrate(video_only)
        needs_audio = v is not None
        if v is None:
            v = best_by_bitrate(at_h)
        size = fsize(v, duration)
        if size is not None and needs_audio and audio_size:
            size += audio_size
        options.append({"height": h, "size": size})
    return options, audio_size


# ---------- endpoints ----------
@app.get("/", response_class=HTMLResponse)
def index():
    html = (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")
    return html.replace("__TOKEN__", TOKEN)


@app.get("/health")
def health():
    return {"ready": READY.is_set(), "ffmpeg": FFMPEG is not None}


@app.get("/config", dependencies=[Depends(auth)])
def get_config():
    return {"download_dir": str(get_dir())}


@app.post("/config/folder", dependencies=[Depends(auth)])
def pick_folder():
    if not PICK_LOCK.acquire(blocking=False):
        raise HTTPException(409, "Ya hay un selector de carpeta abierto.")
    try:
        cmd = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve())]
        proc = subprocess.run(
            cmd + ["--pick-folder", str(get_dir())],
            capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    finally:
        PICK_LOCK.release()
    lines = proc.stdout.strip().splitlines()
    chosen = lines[-1].strip() if lines else ""
    if not chosen:
        if proc.returncode != 0:
            raise HTTPException(500, "No se pudo abrir el selector de carpetas.")
        return {"download_dir": str(get_dir()), "changed": False}  # canceló
    path = Path(chosen)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if not path.is_dir() or not os.access(path, os.W_OK):
        raise HTTPException(422, "No se puede escribir en esa carpeta.")
    set_dir(path)
    return {"download_dir": str(path), "changed": True}


@app.post("/info", dependencies=[Depends(auth)])
def info(req: InfoReq):
    check_url(req.url)
    try:
        with yt_dlp.YoutubeDL({**base_opts(req), "skip_download": True}) as ydl:
            data = ydl.extract_info(req.url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(422, clean_error(str(e)))
    duration = data.get("duration")
    options, audio_size = build_options(data.get("formats") or [], duration)
    return {
        "title": data.get("title") or "Sin título",
        "thumbnail": data.get("thumbnail"),
        "duration": duration,
        "uploader": data.get("uploader"),
        "options": options,
        "audio_size": audio_size,
    }


def update(job_id: str, **kw):
    with LOCK:
        JOBS[job_id].update(kw)


def run_job(job_id: str, req: DownloadReq, out_dir: Path):
    def hook(d):
        title = (d.get("info_dict") or {}).get("title")
        if title:
            update(job_id, title=title)
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            pct = d["downloaded_bytes"] / total * 100 if total else None
            update(job_id, status="downloading", progress=pct, speed=d.get("speed"), eta=d.get("eta"))
        elif d["status"] == "finished":
            update(job_id, status="processing", progress=100, speed=None, eta=None)

    tmp = tempfile.mkdtemp(prefix="vdl_")  # carpeta temporal propia de este job
    try:
        opts = {
            **base_opts(req),
            "paths": {"home": str(out_dir), "temp": tmp},
            "outtmpl": "%(title).150B [%(id)s].%(ext)s",
            "windowsfilenames": True,
            "progress_hooks": [hook],
        }
        if req.mode == "audio":
            if not FFMPEG:
                raise RuntimeError("Hace falta ffmpeg para extraer audio.")
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": req.audio_format, "preferredquality": "0"}
            ]
        else:
            h = f"[height<={req.height}]" if req.height else ""
            if FFMPEG:
                opts["format"] = f"bv*{h}+ba/b{h}/b"
                opts["merge_output_format"] = "mp4/mkv"
            else:  # sin ffmpeg no se pueden unir pistas: usa un solo archivo
                opts["format"] = f"b{h}/b"
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(req.url, download=True)
        done = (data.get("requested_downloads") or [{}])[0].get("filepath")
        update(job_id, status="done", progress=100, file=Path(done).name if done else None)
    except yt_dlp.utils.DownloadError as e:
        update(job_id, status="error", error=clean_error(str(e))["message"])
    except Exception as e:
        update(job_id, status="error", error=str(e)[:300])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/download", dependencies=[Depends(auth)])
def download(req: DownloadReq):
    check_url(req.url)
    if req.mode not in ("video", "audio"):
        raise HTTPException(422, {"code": "mode", "message": "Modo inválido."})
    if req.audio_format not in ("mp3", "m4a", "opus", "wav"):
        raise HTTPException(422, {"code": "fmt", "message": "Formato de audio inválido."})
    out_dir = get_dir()  # cada descarga fija su carpeta al empezar
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise HTTPException(422, {"code": "dir", "message": "No se puede usar la carpeta de descargas."})
    job_id = uuid.uuid4().hex[:10]
    with LOCK:
        # el mismo video + modo comparten nombre de archivo: no se permiten dos a la vez
        if any(j["url"] == req.url and j["mode"] == req.mode and j["status"] in ACTIVE for j in JOBS.values()):
            raise HTTPException(409, {"code": "dup", "message": "Ya se está descargando este video."})
        JOBS[job_id] = {
            "id": job_id, "url": req.url, "title": req.url, "mode": req.mode, "status": "queued",
            "progress": 0, "speed": None, "eta": None, "file": None, "error": None,
            "dir": str(out_dir), "created": time.time(),
        }
    POOL.submit(run_job, job_id, req, out_dir)
    return {"id": job_id}


@app.get("/jobs", dependencies=[Depends(auth)])
def jobs():
    with LOCK:
        items = [dict(j) for j in JOBS.values()]
    return sorted(items, key=lambda j: j["created"], reverse=True)[:30]


@app.get("/jobs/{job_id}", dependencies=[Depends(auth)])
def job(job_id: str):
    with LOCK:
        j = JOBS.get(job_id)
        if not j:
            raise HTTPException(404, "Job no encontrado")
        return dict(j)


def open_browser_when_up():
    for _ in range(50):
        try:
            with socket.create_connection((HOST, PORT), timeout=0.3):
                break
        except OSError:
            time.sleep(0.2)
    else:
        return
    webbrowser.open(f"http://{HOST}:{PORT}")


if __name__ == "__main__":
    if "--pick-folder" in sys.argv:
        i = sys.argv.index("--pick-folder")
        pick_folder_dialog(sys.argv[i + 1] if len(sys.argv) > i + 1 else str(Path.home()))
        sys.exit(0)
    load_config()
    print(f"VideoDL en http://{HOST}:{PORT}")
    print(f"Descargas en: {get_dir()}")
    print(f"Token (para la extensión): {TOKEN}")
    threading.Thread(target=open_browser_when_up, daemon=True).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")