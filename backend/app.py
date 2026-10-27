"""FastAPI 入口。"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import mimetypes
from pathlib import Path
from urllib.parse import quote

import requests
import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import jobs
from .extractors import ParseError, parse_url
from .settings import (
    BASE_DIR,
    FFMPEG,
    PLATFORMS,
    UA_DESKTOP,
    detect_platform,
    ensure_auth_password,
    get_auth_secret,
    has_cookies,
    load_config,
    normalize_url,
)

app = FastAPI(title="无痕视频下载", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 首次启动时生成 config.json（含随机访问密码与签名密钥）
ensure_auth_password()
get_auth_secret()


def _auth_settings() -> tuple[bool, str]:
    auth = load_config().get("auth") or {}
    if not bool(auth.get("enabled", True)):
        return False, ""
    return True, ensure_auth_password()


def _auth_token() -> str:
    _, password = _auth_settings()
    return hmac.new(get_auth_secret().encode(), password.encode(), hashlib.sha256).hexdigest()


def _authorized(request: Request) -> bool:
    enabled, _ = _auth_settings()
    if not enabled:
        return True
    cookie = request.cookies.get("vg_auth") or ""
    return bool(cookie) and hmac.compare_digest(cookie, _auth_token())


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    enabled, _ = _auth_settings()
    path = request.url.path
    if not enabled:
        return await call_next(request)
    if path in ("/login", "/api/login", "/favicon.ico") or path.startswith("/static/"):
        return await call_next(request)
    if _authorized(request):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "请先登录"}, status_code=401)
    return RedirectResponse(url="/login", status_code=302)


class LoginRequest(BaseModel):
    password: str


class ParseRequest(BaseModel):
    url: str


class FormatModel(BaseModel):
    id: str | None = None
    label: str | None = None
    height: int | None = 0
    ext: str | None = "mp4"
    size: int | None = 0
    format: str | None = "best"
    direct_url: str | None = None
    preview: bool | None = False
    fps: float | None = 0


class DownloadRequest(BaseModel):
    url: str
    kind: str = Field(default="video")
    title: str | None = None
    format: FormatModel | None = None
    format_string: str | None = None


class EnhanceRequest(BaseModel):
    url: str | None = None
    title: str | None = None
    format: FormatModel | None = None
    source_job_id: str | None = None
    height: int = 1080
    fps: int = 60
    mode: str = "fast"
    corner: str = "none"
    wm_size: str = "medium"
    wm_mode: str = "delogo"


@app.get("/login")
async def login_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "login.html")


@app.post("/api/login")
async def login(req: LoginRequest) -> JSONResponse:
    enabled, password = _auth_settings()
    if enabled and not hmac.compare_digest(req.password.strip(), password):
        raise HTTPException(status_code=401, detail="访问密码错误")
    response = JSONResponse({"ok": True})
    response.set_cookie(
        "vg_auth",
        _auth_token(),
        max_age=7 * 24 * 3600,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@app.post("/api/logout")
async def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie("vg_auth", path="/")
    return response


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict:
    enabled, _ = _auth_settings()
    return {
        "ok": True,
        "ffmpeg": FFMPEG or "",
        "ytdlp": yt_dlp.version.__version__,
        "cookies": has_cookies(),
        "auth": enabled,
        "max_concurrent": jobs.MAX_CONCURRENT,
        "platforms": [
            {"key": key, "name": meta["name"], "color": meta["color"]}
            for key, meta in PLATFORMS.items()
        ],
    }


@app.get("/api/config")
async def config() -> dict:
    cfg = load_config()
    return {"site_name": cfg.get("site_name"), "donation": cfg.get("donation") or {}}


@app.post("/api/parse")
async def parse(req: ParseRequest) -> dict:
    try:
        url = normalize_url(req.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    platform = detect_platform(url)
    if not platform:
        raise HTTPException(status_code=400, detail="目前支持：抖音、B站、小红书、快手")

    try:
        result = await asyncio.to_thread(parse_url, url)
    except ParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"解析异常：{exc}") from exc

    for fmt in result.get("formats") or []:
        if fmt.get("preview") and fmt.get("direct_url"):
            fmt["preview_url"] = "/api/proxy?url=" + quote(fmt["direct_url"], safe="") + "&referer=" + quote(url, safe="")
    return result


@app.post("/api/download")
async def download(req: DownloadRequest) -> dict:
    try:
        url = normalize_url(req.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    kind = req.kind if req.kind in ("video", "audio", "cover") else "video"
    payload = {
        "url": url,
        "kind": kind,
        "title": req.title,
        "format": req.format.model_dump() if req.format else {},
        "format_string": req.format_string,
    }
    if kind == "cover":
        payload["cover"] = req.format.direct_url if req.format and req.format.direct_url else ""
        if not payload["cover"]:
            raise HTTPException(status_code=400, detail="缺少封面地址")

    job = jobs.create_job(kind, payload)
    return {"job_id": job["id"], "kind": kind}


@app.post("/api/enhance")
async def enhance(req: EnhanceRequest) -> dict:
    if not FFMPEG:
        raise HTTPException(status_code=503, detail="服务器缺少 FFmpeg，无法进行增强")
    if not req.source_job_id and not req.url:
        raise HTTPException(status_code=400, detail="缺少源视频")

    height = max(0, min(int(req.height or 0), 2160))
    fps = max(0, min(int(req.fps or 0), 120))
    corners = ("top-right", "top-left", "bottom-right", "bottom-left")
    corner = req.corner if req.corner in corners else "none"
    if height == 0 and fps == 0 and corner == "none":
        raise HTTPException(status_code=400, detail="请至少选择分辨率、帧率或水印处理中的一项")
    mode = req.mode if req.mode in ("fast", "quality") else "fast"
    wm_size = req.wm_size if req.wm_size in ("small", "medium", "large") else "medium"
    wm_mode = req.wm_mode if req.wm_mode in ("mask", "delogo", "cover", "deblend") else "mask"

    payload = {
        "url": normalize_url(req.url) if req.url else "",
        "title": req.title,
        "format": req.format.model_dump() if req.format else {},
        "source_job_id": req.source_job_id,
        "height": height,
        "fps": fps,
        "mode": mode,
        "corner": corner,
        "wm_size": wm_size,
        "wm_mode": wm_mode,
    }
    job = jobs.create_job("enhance", payload)
    return {"job_id": job["id"], "kind": "enhance"}


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str) -> dict:
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {
        "id": job["id"],
        "kind": job["kind"],
        "status": job["status"],
        "progress": job["progress"],
        "stage": job["stage"],
        "error": job["error"],
        "title": job["title"],
        "files": {
            key: {"name": value["name"], "size": value["size"], "url": f"/api/jobs/{job_id}/file/{key}"}
            for key, value in job["files"].items()
        },
    }


@app.get("/api/jobs/{job_id}/file/{kind}")
async def job_file(job_id: str, kind: str) -> FileResponse:
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    info = job["files"].get(kind)
    if not info:
        raise HTTPException(status_code=404, detail="文件尚未生成")
    path = Path(info["path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件已过期")
    media_type, _ = mimetypes.guess_type(str(path))
    return FileResponse(path, filename=info["name"], media_type=media_type or "application/octet-stream")


@app.get("/api/proxy")
async def proxy(url: str = Query(...), referer: str = Query(default="")) -> StreamingResponse:
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="非法地址")
    headers = {"User-Agent": UA_DESKTOP, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    try:
        resp = requests.get(url, headers=headers, stream=True, timeout=30, allow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"资源加载失败：{exc}") from exc

    def iterator():
        try:
            for chunk in resp.iter_content(64 * 1024):
                if chunk:
                    yield chunk
        finally:
            resp.close()

    return StreamingResponse(
        iterator(),
        media_type=resp.headers.get("Content-Type", "application/octet-stream"),
    )
