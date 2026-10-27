"""任务系统：下载 / 提取音频 / 封面 / 画质增强。"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import requests
import yt_dlp

from .settings import DOWNLOAD_DIR, FFMPEG, UA_DESKTOP, detect_platform, load_config

JOBS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()

MAX_CONCURRENT = max(1, int((load_config().get("limits") or {}).get("max_concurrent_jobs") or 2))
_SEM = threading.Semaphore(MAX_CONCURRENT)

KIND_LABEL = {
    "video": "视频",
    "audio": "音频",
    "cover": "封面",
    "enhanced": "增强视频",
}


def _safe_name(name: str, fallback: str = "video") -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", str(name or "")).strip().strip(".")
    return (name or fallback)[:80]


def create_job(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "kind": kind,
        "status": "queued",
        "progress": 0,
        "stage": "排队中",
        "error": "",
        "title": payload.get("title") or "未命名视频",
        "files": {},
        "created": time.time(),
    }
    with _LOCK:
        JOBS[job_id] = job
    thread = threading.Thread(target=_run_job, args=(job, payload), daemon=True)
    thread.start()
    return job


def get_job(job_id: str) -> dict[str, Any] | None:
    return JOBS.get(job_id)


def _set(job: dict[str, Any], **kwargs: Any) -> None:
    job.update(kwargs)


def _run_job(job: dict[str, Any], payload: dict[str, Any]) -> None:
    _set(job, status="queued", stage=f"排队中（并发上限 {MAX_CONCURRENT}）")
    _SEM.acquire()
    try:
        _set(job, status="running", stage="开始处理")
        kind = job["kind"]
        if kind == "cover":
            _job_cover(job, payload)
        elif kind == "enhance":
            _job_enhance(job, payload)
        else:
            _job_media(job, payload)
        _set(job, status="done", progress=100, stage="完成")
    except Exception as exc:  # noqa: BLE001
        _set(job, status="error", stage="失败", error=str(exc)[:300])
    finally:
        _SEM.release()


def _referer_for(url: str) -> str:
    platform = detect_platform(url)
    return {
        "douyin": "https://www.douyin.com/",
        "bilibili": "https://www.bilibili.com/",
        "xiaohongshu": "https://www.xiaohongshu.com/",
        "kuaishou": "https://www.kuaishou.com/",
    }.get(platform or "", "https://www.douyin.com/")


def _request_headers(url: str) -> dict[str, str]:
    return {
        "User-Agent": UA_DESKTOP,
        "Referer": _referer_for(url),
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


def _download_direct(
    direct_url: str,
    page_url: str,
    dest: Path,
    progress_cb: Callable[[float], None],
) -> Path:
    with requests.get(
        direct_url,
        headers=_request_headers(page_url),
        stream=True,
        timeout=30,
        allow_redirects=True,
    ) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        received = 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                fh.write(chunk)
                received += len(chunk)
                if total:
                    progress_cb(min(received / total, 1.0))
        if dest.stat().st_size == 0:
            raise RuntimeError("下载失败：文件为空")
    return dest


def _download_ytdlp(
    url: str,
    fmt_string: str,
    dest_prefix: Path,
    job: dict[str, Any],
    progress_cb: Callable[[float], None],
    audio_only: bool = False,
) -> Path:
    def hook(status: dict[str, Any]) -> None:
        if status.get("status") == "downloading":
            total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
            downloaded = status.get("downloaded_bytes") or 0
            if total:
                progress_cb(min(downloaded / total, 0.99))
        elif status.get("status") == "finished":
            progress_cb(1.0)

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "socket_timeout": 25,
        "retries": 3,
        "format": fmt_string,
        "outtmpl": str(dest_prefix) + ".%(ext)s",
        "progress_hooks": [hook],
        "http_headers": {"User-Agent": UA_DESKTOP, "Referer": _referer_for(url)},
        "postprocessors": [],
    }
    if not audio_only:
        opts["merge_output_format"] = "mp4"
    if FFMPEG:
        opts["ffmpeg_location"] = str(Path(FFMPEG).parent)
    if (DOWNLOAD_DIR.parent / "config" / "cookies.txt").exists():
        opts["cookiefile"] = str(DOWNLOAD_DIR.parent / "config" / "cookies.txt")

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    candidates = sorted(
        dest_prefix.parent.glob(dest_prefix.name + ".*"),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    candidates = [c for c in candidates if c.suffix not in (".part", ".ytdl")]
    if not candidates:
        raise RuntimeError("下载完成但未找到输出文件")
    return candidates[0]


def _register_file(job: dict[str, Any], kind: str, path: Path, download_name: str) -> None:
    job["files"][kind] = {
        "name": download_name,
        "path": str(path),
        "size": path.stat().st_size,
    }


def _job_media(job: dict[str, Any], payload: dict[str, Any]) -> None:
    url = payload["url"]
    kind = payload["kind"]
    fmt = payload.get("format") or {}
    title = _safe_name(payload.get("title") or "video")
    work_dir = DOWNLOAD_DIR / job["id"]
    work_dir.mkdir(parents=True, exist_ok=True)

    _set(job, status="running", stage="准备下载", progress=1)

    if kind == "video":
        dest = work_dir / "video.mp4"
        if fmt.get("format") == "direct" and fmt.get("direct_url"):
            _download_direct(
                fmt["direct_url"],
                url,
                dest,
                lambda p: _set(job, progress=2 + int(p * 88), stage=f"下载中 {int(p * 100)}%"),
            )
        else:
            target = _download_ytdlp(
                url,
                fmt.get("format") or "best",
                work_dir / "video",
                job,
                lambda p: _set(job, progress=2 + int(p * 88), stage=f"下载中 {int(p * 100)}%"),
            )
            if target.resolve() != dest.resolve():
                if dest.exists():
                    dest.unlink()
                target.rename(dest)
        _set(job, progress=95, stage="写入文件")
        _register_file(job, "video", dest, f"{title}.mp4")
        return

    if kind == "audio":
        target = _download_ytdlp(
            url,
            payload.get("format_string") or "bestaudio/best",
            work_dir / "audio",
            job,
            lambda p: _set(job, progress=2 + int(p * 88), stage=f"提取音频 {int(p * 100)}%"),
            audio_only=True,
        )
        ext = target.suffix.lstrip(".") or "m4a"
        final = work_dir / f"audio.{ext}"
        if target.resolve() != final.resolve():
            if final.exists():
                final.unlink()
            target.rename(final)
        _register_file(job, "audio", final, f"{title}_音频.{ext}")
        return

    raise RuntimeError("未知的下载类型")


def _job_cover(job: dict[str, Any], payload: dict[str, Any]) -> None:
    cover = payload.get("cover") or ""
    url = payload["url"]
    if not cover:
        raise RuntimeError("该视频没有可用封面")
    _set(job, status="running", stage="下载封面", progress=10)
    work_dir = DOWNLOAD_DIR / job["id"]
    work_dir.mkdir(parents=True, exist_ok=True)
    resp = requests.get(cover, headers=_request_headers(url), timeout=30, stream=True)
    resp.raise_for_status()
    ctype = (resp.headers.get("Content-Type") or "").lower()
    ext = "jpg"
    for key, value in (("png", "png"), ("webp", "webp"), ("jpeg", "jpg")):
        if key in ctype:
            ext = value
            break
    dest = work_dir / f"cover.{ext}"
    with dest.open("wb") as fh:
        for chunk in resp.iter_content(64 * 1024):
            if chunk:
                fh.write(chunk)
    _set(job, progress=95)
    title = _safe_name(payload.get("title") or "cover")
    _register_file(job, "cover", dest, f"{title}_封面.{ext}")


# ----------------------------- FFmpeg 增强 -----------------------------

def _probe(path: Path) -> dict[str, float]:
    if not FFMPEG:
        raise RuntimeError("未找到 FFmpeg，无法进行画质增强")
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stderr = proc.stderr or ""
    info = {"duration": 0.0, "fps": 0.0, "width": 0.0, "height": 0.0}
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if match:
        h, m, s = match.groups()
        info["duration"] = int(h) * 3600 + int(m) * 60 + float(s)
    match = re.search(r"(\d+(?:\.\d+)?)\s*fps", stderr)
    if match:
        info["fps"] = float(match.group(1))
    match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", stderr)
    if match:
        info["width"], info["height"] = float(match.group(1)), float(match.group(2))
    return info


def _delogo_filter(
    corner: str,
    width: float,
    height: float,
    size: str = "medium",
    mode: str = "delogo",
) -> str | None:
    """按指定角落与覆盖范围生成去水印滤镜，支持插值修复或纯色遮盖。"""
    width, height = int(width or 0), int(height or 0)
    if corner not in ("top-right", "top-left", "bottom-right", "bottom-left"):
        return None
    if not width or not height:
        raise RuntimeError("无法读取源视频尺寸，去水印失败")

    ratios = {
        "small": (0.16, 0.07),
        "medium": (0.22, 0.10),
        "large": (0.30, 0.14),
    }
    ratio_w, ratio_h = ratios.get(size, ratios["medium"])
    box_w = max(48, int(width * ratio_w))
    box_h = max(28, int(height * ratio_h))
    margin = max(4, int(width * 0.004))
    if corner == "top-right":
        x, y = width - box_w - margin, margin
    elif corner == "top-left":
        x, y = margin, margin
    elif corner == "bottom-right":
        x, y = width - box_w - margin, height - box_h - margin
    else:
        x, y = margin, height - box_h - margin

    x, y = max(1, x), max(1, y)
    box_w = min(box_w, width - x - 2)
    box_h = min(box_h, height - y - 2)
    if box_w < 16 or box_h < 12:
        raise RuntimeError("视频分辨率过低，无法定位水印区域")

    if mode == "cover":
        return f"drawbox=x={x}:y={y}:w={box_w}:h={box_h}:color=black@1:t=fill"
    return f"delogo=x={x}:y={y}:w={box_w}:h={box_h}"


def _box_blur(image: Any, radius: int) -> Any:
    """用积分图做盒式模糊，用来估计水印周围的本底亮度。"""
    import numpy as np

    r = max(1, int(radius))
    padded = np.pad(image, ((r, r), (r, r)), mode="edge")
    integral = np.pad(padded.cumsum(0).cumsum(1), ((1, 0), (1, 0)), mode="constant")
    k = 2 * r + 1
    blurred = (
        integral[k:, k:] - integral[:-k, k:] - integral[k:, :-k] + integral[:-k, :-k]
    ) / float(k * k)
    return blurred[: image.shape[0], : image.shape[1]]


def _masked_box_blur(channel: Any, mask: Any, radius: int) -> Any:
    """只在 mask 为 True 的像素上做盒式模糊，避免水印自身把本底亮度拉高。"""
    import numpy as np

    r = max(1, int(radius))
    values = channel * mask
    padded_v = np.pad(values, ((r, r), (r, r)), mode="edge")
    padded_m = np.pad(mask.astype(np.float32), ((r, r), (r, r)), mode="edge")
    integ_v = np.pad(padded_v.cumsum(0).cumsum(1), ((1, 0), (1, 0)), mode="constant")
    integ_m = np.pad(padded_m.cumsum(0).cumsum(1), ((1, 0), (1, 0)), mode="constant")
    k = 2 * r + 1
    sum_v = integ_v[k:, k:] - integ_v[:-k, k:] - integ_v[k:, :-k] + integ_v[:-k, :-k]
    sum_m = integ_m[k:, k:] - integ_m[:-k, k:] - integ_m[k:, :-k] + integ_m[:-k, :-k]
    sum_v = sum_v[: channel.shape[0], : channel.shape[1]]
    sum_m = sum_m[: channel.shape[0], : channel.shape[1]]
    return sum_v / np.maximum(sum_m, 6.0)


def _shift_array(array: Any, dy: int, dx: int, fill: float = 0.0) -> Any:
    """按整数偏移平移数组，空出来的边缘用 fill 填充。"""
    import numpy as np

    out = np.full_like(array, fill)
    h, w = array.shape[:2]
    if abs(dy) >= h or abs(dx) >= w:
        return out
    src_y = slice(max(0, -dy), min(h, h - dy))
    dst_y = slice(max(0, dy), min(h, h + dy))
    src_x = slice(max(0, -dx), min(w, w - dx))
    dst_x = slice(max(0, dx), min(w, w + dx))
    out[dst_y, dst_x] = array[src_y, src_x]
    return out


def _analyze_watermark(
    source: Path,
    corner: str,
    width: int,
    height: int,
    work_dir: Path,
    duration: float = 0,
) -> dict[str, Any] | None:
    """分析水印：生成轮廓蒙版、恒覆盖核心蒙版，并估算半透明水印的透明度。"""
    import numpy as np
    from PIL import Image

    width, height = int(width or 0), int(height or 0)
    if corner not in ("top-right", "top-left", "bottom-right", "bottom-left"):
        return None
    if not width or not height:
        return None

    # 抽样 8~80 帧，覆盖整段视频，同时避免长视频占用过多内存
    if duration and duration > 0:
        sample_fps = min(4.0, max(0.15, 80.0 / duration))
    else:
        sample_fps = 1.0

    search_w = max(80, min(int(width * 0.34), width - 2))
    search_h = max(60, min(int(height * 0.17), height - 2))
    margin = max(4, int(width * 0.006))
    if corner == "top-right":
        x, y = width - search_w - margin, margin
    elif corner == "top-left":
        x, y = margin, margin
    elif corner == "bottom-right":
        x, y = width - search_w - margin, height - search_h - margin
    else:
        x, y = margin, height - search_h - margin

    sample = work_dir / "mask_sample.raw"
    try:
        subprocess.run(
            [
                FFMPEG,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-vf",
                f"crop={search_w}:{search_h}:{x}:{y},fps={sample_fps},format=rgb24",
                "-f",
                "rawvideo",
                str(sample),
            ],
            check=True,
            timeout=180,
        )
        buf = np.frombuffer(sample.read_bytes(), dtype=np.uint8)
        frame_size = search_w * search_h * 3
        count = buf.size // frame_size
        if count < 3:
            return None
        frames = buf[: count * frame_size].reshape(count, search_h, search_w, 3).astype(np.float32)
        lum = frames.mean(axis=3)
        base = float(np.median(lum))
        threshold = max(100.0, base + 25.0)
        raw_union = (lum >= threshold).any(axis=0)
        if raw_union.sum() < 150 or raw_union.sum() > search_w * search_h * 0.4:
            return None

        # 水印会缓慢漂移：先逐帧定位水印亮度重心并平移对齐，再做统计，
        # 否则同一像素在不同帧对应水印的不同部位，透明度会被算错。
        radius = max(12, min(search_w, search_h) // 5)
        blurred = np.stack([_box_blur(frame, radius) for frame in lum])
        bright = lum - blurred > 12
        counts = bright.sum(axis=(1, 2))
        visible = counts >= max(int(counts.max() * 0.3), 300)
        if int(visible.sum()) < 3:
            visible = counts > 200
        if int(visible.sum()) < 3:
            return None

        centroids: list[tuple[float, float] | None] = []
        for i in range(count):
            if not visible[i] or counts[i] < 200:
                centroids.append(None)
            else:
                ys, xs = np.nonzero(bright[i])
                centroids.append((float(ys.mean()), float(xs.mean())))
        known = [c for c in centroids if c]
        ref_cy = float(np.median([c[0] for c in known]))
        ref_cx = float(np.median([c[1] for c in known]))
        aligned_rgb = []
        for i, frame in enumerate(frames):
            c = centroids[i]
            if c is None:
                aligned_rgb.append(frame)
                continue
            dy = int(round(ref_cy - c[0]))
            dx = int(round(ref_cx - c[1]))
            aligned_rgb.append(
                np.stack(
                    [
                        _shift_array(frame[..., ch], dy, dx, fill=float(np.median(frame[..., ch])))
                        for ch in range(3)
                    ],
                    axis=-1,
                )
            )
        aligned_rgb = np.stack(aligned_rgb)[visible]
        aligned = aligned_rgb.mean(axis=3)

        # obs = (1-a)*bg + a*255：bg 取低分位（像素露出真实背景时），peak 取高分位（被覆盖时）
        bg_rgb = np.percentile(aligned_rgb, 10, axis=0)
        peak_rgb = np.percentile(aligned_rgb, 95, axis=0)
        bg = bg_rgb.mean(axis=2)
        peak = peak_rgb.mean(axis=2)
        span = np.maximum(peak - bg, 1.0)

        # 对齐后的覆盖范围：反混合按帧平移后使用，只需覆盖“水印主体位置”
        aligned_union = ((aligned >= (bg + 0.5 * span)) & (aligned >= 100.0)).any(axis=0)
        if aligned_union.sum() < 150 or aligned_union.sum() > search_w * search_h * 0.4:
            aligned_union = raw_union

        # 逐像素透明度：三通道最小二乘，白色水印时 a = Σ(peak-bg)(255-bg) / Σ(255-bg)^2
        alpha_px = (
            ((peak_rgb - bg_rgb) * (255.0 - bg_rgb)).sum(axis=2)
            / np.maximum(((255.0 - bg_rgb) ** 2).sum(axis=2), 25.0)
        ).astype(np.float32)
        reliable = aligned_union & ((peak - bg) > 20) & (peak > 110)
        alpha = 0.75
        if int(reliable.sum()) > 200:
            # 用高分位取“水印主体”的透明度，避免抗锯齿边缘把估值拉低
            alpha = float(np.percentile(alpha_px[reliable], 90))
        alpha = min(0.9, max(0.6, alpha))

        stats_path = work_dir / "mask_stats.npz"
        np.savez_compressed(
            stats_path,
            bg=bg.astype(np.float32),
            bg_rgb=bg_rgb.astype(np.float32),
            alpha=np.float32(alpha),
            union=aligned_union,
            region=np.array([x, y, search_w, search_h], dtype=np.int32),
            ref_centroid=np.array([ref_cy, ref_cx], dtype=np.float32),
            blur_radius=np.int32(radius),
        )

        # 去水印蒙版用 raw_union：水印逐帧漂移，只有原始位置的并集能保证每帧都被覆盖
        mask_union = raw_union
        for _ in range(3):  # 膨胀 3 像素，覆盖半透明边缘与抗锯齿
            grown = mask_union.copy()
            grown[1:, :] |= mask_union[:-1, :]
            grown[:-1, :] |= mask_union[1:, :]
            grown[:, 1:] |= mask_union[:, :-1]
            grown[:, :-1] |= mask_union[:, 1:]
            mask_union = grown

        full_mask = np.zeros((height, width), dtype=np.uint8)
        full_mask[y : y + search_h, x : x + search_w] = mask_union.astype(np.uint8) * 255
        mask_path = work_dir / "mask.png"
        Image.fromarray(full_mask, "L").save(mask_path)

        return {
            "alpha": alpha,
            "union": mask_path,
            "stats": stats_path,
            "region": (x, y, search_w, search_h),
        }
    except Exception:  # noqa: BLE001
        return None
    finally:
        sample.unlink(missing_ok=True)


def _watermark_box(
    source: Path,
    corner: str,
    width: int,
    height: int,
    duration: float,
    work_dir: Path,
    cap: tuple[int, int, int, int],
) -> tuple[int, int, int, int, Any]:
    """在角落条带里找出贴着角落的那块静态亮区（水印本体）的外接框。

    只取靠角落的第一段连续亮列，避免把旁边的公告文字、弹窗一起卷进来；
    再用跨帧稳定度过滤掉运动画面里的亮点。同时返回该框内的静态水印字形蒙版，
    逐帧按这个蒙版补背景，既不会漏掉水印，也不会误伤原画面里的其他白色元素。
    检测失败时退回用户选的范围，蒙版为空。
    """
    import numpy as np

    cap_x, cap_y, cap_w, cap_h = cap
    fallback = (cap_x, cap_y, cap_w, cap_h, None)
    margin = max(4, int(width * 0.006))
    det_w = max(cap_w, min(int(width * 0.34), width - 2))
    det_h = max(cap_h, min(int(height * 0.20), height - 2))
    det_x = margin if corner.endswith("left") else width - det_w - margin
    det_y = margin if corner.startswith("top") else height - det_h - margin
    det_x = max(0, min(det_x, width - det_w))
    det_y = max(0, min(det_y, height - det_h))

    sample = work_dir / "wm_detect.raw"
    try:
        sample_fps = (
            min(4.0, max(0.15, 60.0 / duration)) if duration and duration > 0 else 1.0
        )
        subprocess.run(
            [
                FFMPEG,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-vf",
                f"crop={det_w}:{det_h}:{det_x}:{det_y},fps={sample_fps},format=rgb24",
                "-f",
                "rawvideo",
                str(sample),
            ],
            check=True,
            timeout=180,
        )
        buf = np.frombuffer(sample.read_bytes(), dtype=np.uint8)
        frame_size = det_w * det_h * 3
        count = buf.size // frame_size
        if count < 3:
            return fallback
        frames = buf[: count * frame_size].reshape(count, det_h, det_w, 3).astype(np.float32)
        lum = frames.mean(axis=3)
        base = float(np.median(lum))
        # 阈值要同时高于“背景/按钮底色”、又低于水印本身：
        # 取整条带的高分位作参照，避免把亮红色按钮之类的内容也当成水印。
        threshold = max(base + 55.0, 0.7 * float(np.percentile(lum, 99.0)))
        threshold = min(threshold, 215.0)
        stable = (lum >= threshold).mean(axis=0) >= 0.6
        if int(stable.sum()) < 60:
            return fallback

        cols = stable.any(axis=0)
        runs: list[list[int]] = []
        gap_merge = max(10, int(width * 0.02))
        start: int | None = None
        last = 0
        for i, flag in enumerate(cols):
            if not flag:
                continue
            if start is None:
                start = i
            elif i - last > gap_merge:
                runs.append([start, last])
                start = i
            last = i
        if start is not None:
            runs.append([start, last])
        if not runs:
            return fallback
        run = runs[0] if corner.endswith("left") else runs[-1]
        rows = np.nonzero(stable[:, run[0] : run[1] + 1].any(axis=1))[0]
        if rows.size == 0:
            return fallback

        pad_x = max(6, int(width * 0.010))
        pad_y = max(4, int(height * 0.008))
        # 外接框按高阈值定位，字形蒙版用稍低的阈值，把笔画边缘一起包进来
        low = (lum >= max(base + 25.0, threshold * 0.82)).mean(axis=0) >= 0.5
        x0 = max(cap_x, det_x, det_x + run[0] - pad_x)
        x1 = min(cap_x + cap_w, det_x + det_w, det_x + run[1] + 1 + pad_x)
        y0 = max(cap_y, det_y, det_y + int(rows[0]) - pad_y)
        y1 = min(cap_y + cap_h, det_y + det_h, det_y + int(rows[-1]) + 1 + pad_y)
        if x1 - x0 < 24 or y1 - y0 < 12:
            return fallback
        mask = (stable | low)[y0 - det_y : y1 - det_y, x0 - det_x : x1 - det_x].copy()
        return x0, y0, x1 - x0, y1 - y0, mask
    except Exception:  # noqa: BLE001
        return fallback
    finally:
        sample.unlink(missing_ok=True)


def _horizontal_fill(image: Any, mask: Any, seeds: Any = None) -> Any:
    """把 mask 覆盖的像素按同一行左右邻居做线性插值补上。

    水印笔画只有几个像素宽，同一行两侧几乎就是它遮住的背景，
    这样补出来的画面比整块模糊清晰得多，也不会跨行糊成一片。
    seeds 指定哪些像素可以作为参考（默认是 mask 之外的全部像素）；
    传入时会把保护起来的不透明内容排除在外，避免白色文字被“拖”进缺口。
    """
    import numpy as np

    h, w = mask.shape
    out = image.copy()
    rows = np.nonzero(mask.any(axis=1))[0]
    if rows.size == 0:
        return out
    valid = (seeds & ~mask)[rows] if seeds is not None else (~mask)[rows]
    cols = np.arange(w)
    left = np.maximum.accumulate(np.where(valid, cols, -1), axis=1)
    right = np.minimum.accumulate(np.where(valid, cols, w)[:, ::-1], axis=1)[:, ::-1]
    left_px = out[rows[:, None], np.clip(left, 0, w - 1)]
    right_px = out[rows[:, None], np.clip(right, 0, w - 1)]
    t = np.zeros(mask.shape[1], dtype=np.float32)[None, :].repeat(rows.size, axis=0)
    both = (left >= 0) & (right < w)
    t[both] = (cols - left)[both] / np.maximum((right - left)[both], 1)
    filled = left_px * (1.0 - t)[..., None] + right_px * t[..., None]
    filled = np.where((left < 0)[..., None], right_px, filled)
    filled = np.where((right >= w)[..., None], left_px, filled)
    sub = mask[rows]
    current = out[rows[:, None], cols[None, :]]
    out[rows[:, None], cols[None, :]] = np.where(sub[..., None], filled, current)
    return out


def _fill_glyph(image: Any, mask: Any, seeds: Any = None) -> Any:
    """用横向 + 纵向插值的平均结果补上水印笔画。

    笔画只有几像素宽时，同一行/同一列两侧几乎就是它遮住的背景：
    两个方向取平均既不会像单方向插值那样拉出横条纹，也比整块模糊清晰得多。
    """
    import numpy as np

    horizontal = _horizontal_fill(image, mask, seeds)
    vertical = _horizontal_fill(
        np.ascontiguousarray(image.transpose(1, 0, 2)),
        np.ascontiguousarray(mask.T),
        np.ascontiguousarray(seeds.T) if seeds is not None else None,
    ).transpose(1, 0, 2)
    out = image.copy()
    out[mask] = 0.5 * (horizontal[mask] + vertical[mask])
    return out


def _deblend_pass(
    source: Path,
    work_dir: Path,
    timed: float,
    job: dict[str, Any],
    corner: str = "top-right",
    wm_size: str = "medium",
) -> Path | None:
    """去水印：只修补水印字形本身，保留周围的原始画面纹理。

    1. 抽帧找出“贴着角落的静态亮块”，自动收缩到水印本体的外接框，
       不会像整块 delogo 那样把旁边一大片画面一起抹平；
    2. 逐帧估计每个像素被白色水印“抬亮”的比例（不透明度），只把比例足够高的
       像素当作水印，因此水印在角落缓慢漂移时也能跟上；
    3. 补背景用横、纵两个方向邻像素插值的平均，笔画窄，补出来的背景
       几乎与原画面一致；不透明的纯白按钮文字会被保护掉。
    """
    import numpy as np

    meta = _probe(source)
    width, height = int(meta["width"] or 0), int(meta["height"] or 0)
    fps = float(meta["fps"] or 0) or 30.0
    if not width or not height or corner not in (
        "top-right", "top-left", "bottom-right", "bottom-left"
    ):
        return None
    ratios = {"small": (0.16, 0.07), "medium": (0.22, 0.10), "large": (0.30, 0.14)}
    ratio_w, ratio_h = ratios.get(wm_size, ratios["medium"])
    margin = max(4, int(width * 0.006))
    cap_w = max(80, min(int(width * ratio_w), width - 2))
    cap_h = max(60, min(int(height * ratio_h), height - 2))
    cap_x = margin if corner.endswith("left") else width - cap_w - margin
    cap_y = margin if corner.startswith("top") else height - cap_h - margin
    cap_x = max(0, min(cap_x, width - cap_w))
    cap_y = max(0, min(cap_y, height - cap_h))

    x, y, rw, rh, wm_mask = _watermark_box(
        source, corner, width, height, timed, work_dir, (cap_x, cap_y, cap_w, cap_h)
    )
    radius = max(8, min(rw, rh) // 8)
    levels = np.arange(256, dtype=np.float32) / 255.0
    to_linear = (levels**2.2).astype(np.float32)

    # 上传者名字水印会在角落缓慢漂移，可能漂出静态检测框，所以修补范围外扩一圈；
    # 真正的填充仍由逐帧检测决定，外扩区域不会被整片处理。
    pad_x = max(12, int(rw * 0.06))
    pad_y = max(18, int(rh * 0.40))
    rx0, ry0 = max(0, x - pad_x), max(0, y - pad_y)
    rx1, ry1 = min(width, x + rw + pad_x), min(height, y + rh + pad_y)

    out = work_dir / "deblend.mp4"
    frame_size = width * height * 3
    total = max(1, int(round((timed or 0) * fps)))

    decode_cmd = [
        FFMPEG, "-v", "error", "-i", str(source),
        "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]
    encode_cmd = [
        FFMPEG, "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}", "-r", f"{fps:.6f}",
        "-i", "pipe:0",
        "-i", str(source),
        "-map", "0:v", "-map", "1:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "copy",
        "-shortest", "-movflags", "+faststart", str(out),
    ]

    decoder = None
    encoder = None
    try:
        decoder = subprocess.Popen(
            decode_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1024 * 1024
        )
        encoder = subprocess.Popen(
            encode_cmd, cwd=str(work_dir), stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, bufsize=1024 * 1024,
        )
        assert decoder.stdout is not None and encoder.stdin is not None
        _set(job, progress=43, stage="修补水印字形 0%")
        index = 0
        while True:
            chunk = decoder.stdout.read(frame_size)
            if len(chunk) < frame_size:
                break
            frame = np.frombuffer(chunk, dtype=np.uint8).reshape(height, width, 3).copy()
            region = frame[ry0:ry1, rx0:rx1]
            if wm_mask is not None:
                # 半透明白水印叠在画面上：最小通道总是被抬向 255，且抬升比例
                # 就是水印不透明度。用“局部本底 + 最小通道”逐帧估计不透明度，
                # 不透明度足够高的像素才当作水印，因此漂移后的笔画也能跟上，
                # 而普通背景和半透明灰色文字不会被误判。
                mn = region.min(axis=2).astype(np.float32)
                bg_mask = mn < float(np.percentile(mn, 70.0))
                bg_min = _masked_box_blur(mn, bg_mask, radius)
                alpha = (mn - bg_min) / np.maximum(255.0 - bg_min, 40.0)
                glyph = (alpha > 0.14) & ((mn - bg_min) > 10.0)
            else:
                # 没检测到静态水印：退化为逐帧局部本底 + 亮度增量判断
                lin = to_linear[region]
                bg_lin = np.stack(
                    [_box_blur(lin[..., ch], radius) for ch in range(3)], axis=-1
                )
                lum_lin = lin.mean(axis=2)
                glyph = (lum_lin - bg_lin.mean(axis=2)) > 0.02
            # 本身不透明的纯白原画面文字（如按钮上的 Esc）才需要保护；
            # 水印即使在亮背景上也很少达到这么白，用最小通道区分。
            solid = region.min(axis=2) > 228.0
            for _ in range(2):
                grown = solid.copy()
                grown[1:, :] |= solid[:-1, :]
                grown[:-1, :] |= solid[1:, :]
                grown[:, 1:] |= solid[:, :-1]
                grown[:, :-1] |= solid[:, 1:]
                solid = grown
            glyph &= ~solid
            # 膨胀两像素，把水印的抗锯齿边缘与压缩振铃一起盖住，
            # 否则笔画边缘会残留一圈细白/细黑鬼影。
            for _ in range(2):
                grown = glyph.copy()
                grown[1:, :] |= glyph[:-1, :]
                grown[:-1, :] |= glyph[1:, :]
                grown[:, 1:] |= glyph[:, :-1]
                grown[:, :-1] |= glyph[:, 1:]
                glyph = grown
            if glyph.any():
                # 用同一行的左右邻居线性插值补上水印笔画：
                # 笔画只有几像素宽，插值出来的背景与原画面几乎一致，
                # 不会有整块模糊，也不会像逐通道逆混合那样过减留下暗影。
                filled = _fill_glyph(region.astype(np.float32), glyph, ~solid)
                frame[ry0:ry1, rx0:rx1] = np.clip(
                    np.rint(filled), 0, 255
                ).astype(np.uint8)
            encoder.stdin.write(frame.tobytes())
            index += 1
            if index % 10 == 0:
                done = min(index / total, 0.995)
                _set(job, progress=43 + int(done * 12), stage=f"修补水印字形 {int(done * 100)}%")
        encoder.stdin.close()
        decoder.wait(timeout=30)
        if encoder.wait() != 0 or not out.exists():
            out.unlink(missing_ok=True)
            return None
        return out
    except Exception:  # noqa: BLE001
        out.unlink(missing_ok=True)
        return None
    finally:
        for proc in (decoder, encoder):
            if proc and proc.poll() is None:
                proc.kill()


def _job_enhance(job: dict[str, Any], payload: dict[str, Any]) -> None:
    if not FFMPEG:
        raise RuntimeError("未找到 FFmpeg，请将 ffmpeg.exe 放入 tools 目录")

    work_dir = DOWNLOAD_DIR / job["id"]
    work_dir.mkdir(parents=True, exist_ok=True)
    target_height = int(payload.get("height") or 0)
    target_fps = int(payload.get("fps") or 0)

    source: Path | None = None
    src_job_id = payload.get("source_job_id")
    if src_job_id:
        src_job = JOBS.get(src_job_id) or {}
        info = (src_job.get("files") or {}).get("video")
        if not info:
            raise RuntimeError("源视频尚未下载完成")
        source = Path(info["path"])
    else:
        url = payload.get("url") or ""
        fmt = payload.get("format") or {}
        title = _safe_name(payload.get("title") or "video")
        _set(job, status="running", stage="下载源视频", progress=2)
        if fmt.get("format") == "direct" and fmt.get("direct_url"):
            source = _download_direct(
                fmt["direct_url"],
                url,
                work_dir / "source.mp4",
                lambda p: _set(job, progress=2 + int(p * 38), stage=f"下载源视频 {int(p * 100)}%"),
            )
        else:
            source = _download_ytdlp(
                url,
                fmt.get("format") or "best",
                work_dir / "source",
                job,
                lambda p: _set(job, progress=2 + int(p * 38), stage=f"下载源视频 {int(p * 100)}%"),
            )

    _set(job, status="running", stage="分析源视频", progress=42)
    meta = _probe(source)
    src_height = int(meta["height"] or 0)
    src_fps = float(meta["fps"] or 0)
    timed = float(meta["duration"] or 0)

    filters: list[str] = []
    corner = str(payload.get("corner") or "none")
    wm_size = str(payload.get("wm_size") or "medium")
    wm_mode = str(payload.get("wm_mode") or "delogo")
    watermark: str | None = None
    wm_applied = False
    deblend_used = False
    if corner != "none":
        if wm_mode in ("mask", "deblend"):
            # 半透明白色水印优先做逐像素逆混合：能还原被冲淡的背景纹理，
            # 而 removelogo/delogo 的整块插值会把水印附近抹平、看起来发糊。
            deblended = _deblend_pass(source, work_dir, timed, job, corner, wm_size)
            if deblended:
                source = deblended
                deblend_used = True
                wm_applied = True
            else:
                _set(job, stage="识别水印轮廓", progress=43)
                analysis = _analyze_watermark(
                    source, corner, meta["width"], meta["height"], work_dir, timed
                )
                if analysis and analysis.get("union"):
                    watermark = "removelogo=mask.png"
                else:
                    watermark = _delogo_filter(
                        corner, meta["width"], meta["height"], wm_size, "delogo"
                    )
        else:
            watermark = _delogo_filter(corner, meta["width"], meta["height"], wm_size, wm_mode)
    if watermark:
        filters.append(watermark)
        wm_applied = True
    if target_height and (not src_height or target_height != src_height):
        filters.append(f"scale=-2:{target_height}:flags=lanczos")

    do_interp = target_fps and (not src_fps or target_fps > src_fps + 0.5)
    mode = str(payload.get("mode") or "fast")
    if do_interp:
        if mode == "quality":
            # 运动补偿插帧：效果最好，但耗时可达片长的数十倍
            filters.append(
                f"minterpolate=fps={target_fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"
            )
        else:
            # 混合插帧：速度接近实时，适合长视频
            filters.append(f"minterpolate=fps={target_fps}:mi_mode=blend:scd=none")

    out = work_dir / "enhanced.mp4"
    cmd = [FFMPEG, "-y", "-hide_banner", "-i", str(source)]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        "-nostats",
        str(out),
    ]

    main_start, main_span = (56, 43) if deblend_used else (45, 54)
    _set(job, progress=main_start, stage="转码中 0%")
    proc = subprocess.Popen(
        cmd,
        cwd=str(work_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if not line.startswith("out_time_ms="):
            continue
        try:
            seconds = int(line.split("=", 1)[1]) / 1_000_000
        except ValueError:
            continue
        if timed > 0:
            ratio = min(seconds / timed, 0.995)
        else:
            ratio = 0.5
        _set(job, progress=main_start + int(ratio * main_span), stage=f"转码中 {int(ratio * 100)}%")
    code = proc.wait()
    if code != 0 or not out.exists():
        raise RuntimeError("FFmpeg 转码失败，可能是源文件损坏或参数过高")

    title = _safe_name(payload.get("title") or "video")
    suffix = []
    if target_height:
        suffix.append(f"{target_height}p")
    if do_interp:
        suffix.append(f"{target_fps}fps")
    if wm_applied:
        suffix.append("去水印")
    label = "_".join(suffix) or "enhanced"
    _register_file(job, "enhanced", out, f"{title}_{label}.mp4")
    _set(job, progress=99)
