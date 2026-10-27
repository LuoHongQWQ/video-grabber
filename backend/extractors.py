"""视频解析：抖音 / B站 / 小红书 / 快手。"""
from __future__ import annotations

import json
import re
from typing import Any

import requests
import yt_dlp

from .settings import (
    COOKIE_FILE,
    UA_DESKTOP,
    UA_MOBILE,
    cookie_header_for,
    detect_platform,
)

PLATFORM_HINT = {
    "douyin": "抖音",
    "bilibili": "哔哩哔哩",
    "xiaohongshu": "小红书",
    "kuaishou": "快手",
}


class ParseError(Exception):
    pass


def _base_opts() -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "socket_timeout": 25,
        "retries": 2,
        "http_headers": {"User-Agent": UA_DESKTOP},
    }
    if COOKIE_FILE.exists():
        opts["cookiefile"] = str(COOKIE_FILE)
    return opts


def _human_size(size: float | int | None) -> str:
    if not size:
        return ""
    mb = float(size) / 1048576
    return f"{mb:.1f} MB" if mb >= 1 else f"{float(size) / 1024:.0f} KB"


def _format_label(fmt: dict[str, Any]) -> str:
    parts: list[str] = []
    height = fmt.get("height")
    if height:
        parts.append(f"{height}p")
    elif fmt.get("format_note"):
        parts.append(str(fmt["format_note"]))
    fps = fmt.get("fps")
    if fps and float(fps) > 30:
        parts.append(f"{int(fps)}fps")
    ext = fmt.get("ext")
    if ext:
        parts.append(ext)
    codec = (fmt.get("vcodec") or "").split(".")[0]
    if codec and codec != "none":
        parts.append(codec)
    size = _human_size(fmt.get("filesize") or fmt.get("filesize_approx"))
    if size:
        parts.append(size)
    return " · ".join(parts) or "默认画质"


def _build_result(info: dict[str, Any], platform: str, original_url: str) -> dict[str, Any]:
    if info.get("_type") in ("playlist", "multi_video"):
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise ParseError("未解析到可下载的视频")
        info = entries[0]

    title = info.get("title") or info.get("description") or "未命名视频"
    title = re.sub(r"\s+", " ", str(title)).strip()[:120]
    author = info.get("uploader") or info.get("creator") or info.get("channel") or ""
    duration = info.get("duration") or 0

    thumbs = info.get("thumbnails") or []
    cover = info.get("thumbnail") or ""
    if thumbs:
        ranked = sorted(
            [t for t in thumbs if t.get("url")],
            key=lambda t: (t.get("width") or 0) * (t.get("height") or 0),
            reverse=True,
        )
        if ranked:
            cover = ranked[0]["url"]

    formats: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for fmt in info.get("formats") or []:
        if not fmt.get("url"):
            continue
        vcodec = fmt.get("vcodec")
        acodec = fmt.get("acodec")
        if not vcodec or vcodec == "none":
            continue
        if fmt.get("ext") in ("mhtml", "none"):
            continue
        height = fmt.get("height") or 0
        if not height and not fmt.get("format_note"):
            continue
        key = (height, fmt.get("ext"), (fmt.get("fps") or 0) > 30)
        if key in seen:
            continue
        seen.add(key)
        has_audio = bool(acodec and acodec != "none")
        formats.append(
            {
                "id": fmt.get("format_id"),
                "label": _format_label(fmt),
                "height": height,
                "width": fmt.get("width") or 0,
                "ext": fmt.get("ext") or "mp4",
                "size": fmt.get("filesize") or fmt.get("filesize_approx") or 0,
                "format": str(fmt.get("format_id")) if has_audio else f"{fmt.get('format_id')}+bestaudio/{fmt.get('format_id')}",
                "direct_url": fmt.get("url"),
                "preview": acodec != "none",
                "fps": fmt.get("fps") or 0,
            }
        )

    formats.sort(key=lambda f: (f["height"], f["size"]), reverse=True)
    formats = formats[:10]

    # 兜底：没有细分格式时使用直链
    if not formats and info.get("url"):
        formats.append(
            {
                "id": "best",
                "label": info.get("format_note") or "原画（无水印）",
                "height": info.get("height") or 0,
                "width": info.get("width") or 0,
                "ext": info.get("ext") or "mp4",
                "size": info.get("filesize") or 0,
                "format": "best",
                "direct_url": info.get("url"),
                "preview": True,
                "fps": info.get("fps") or 0,
            }
        )

    return {
        "ok": True,
        "platform": platform,
        "platform_name": PLATFORM_HINT.get(platform, platform),
        "title": title,
        "author": str(author),
        "duration": int(float(duration)),
        "cover": cover,
        "description": (info.get("description") or "")[:300],
        "view_count": info.get("view_count") or 0,
        "like_count": info.get("like_count") or 0,
        "url": original_url,
        "id": info.get("id"),
        "formats": formats,
        "has_audio": any(f["preview"] for f in formats),
        "audio_hint": "默认导出最佳音轨（M4A/AAC）",
    }


def _parse_with_ytdlp(url: str, platform: str) -> dict[str, Any]:
    try:
        with yt_dlp.YoutubeDL(_base_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        raise ParseError(_friendly_error(str(exc), platform)) from exc
    if not info:
        raise ParseError("解析失败，请检查链接是否有效")
    return _build_result(info, platform, url)


def _friendly_error(raw: str, platform: str) -> str:
    text = raw.lower()
    name = PLATFORM_HINT.get(platform, platform)
    if "unsupported url" in text:
        return f"暂不支持该链接，请确认是{name}的视频地址"
    if "no video formats" in text or "no formats" in text:
        return f"{name}未找到视频流：可能是图文笔记，或需要配置 cookies.txt 后重试"
    if "login" in text or "cookie" in text or "risk" in text or "verify" in text:
        return f"{name}需要登录信息才能解析，请按页面说明配置 cookies.txt 后重试"
    if "404" in text or "not found" in text:
        return "视频不存在或已被删除"
    if "timed out" in text or "timeout" in text:
        return "网络请求超时，请稍后重试"
    if "private" in text or "permission" in text:
        return "该视频为私密内容，无法下载"
    return f"解析失败：{raw[:160]}"


# ----------------------------- 快手 -----------------------------

def _extract_json_object(html: str, marker: str) -> dict | None:
    idx = html.find(marker)
    if idx < 0:
        return None
    start = html.find("{", idx)
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for pos in range(start, min(len(html), start + 3_000_000)):
        ch = html[pos]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : pos + 1])
                except Exception:  # noqa: BLE001
                    return None
    return None


def _parse_kuaishou(url: str) -> dict[str, Any]:
    headers = {
        "User-Agent": UA_DESKTOP,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    cookie = cookie_header_for("kuaishou.com")
    if cookie:
        headers["Cookie"] = cookie
    try:
        session = requests.Session()
        resp = session.get(url, headers=headers, timeout=20, allow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"快手页面访问失败：{exc}") from exc

    html = resp.text
    state = (
        _extract_json_object(html, "window.__APOLLO_STATE__=")
        or _extract_json_object(html, "__APOLLO_STATE__")
        or _extract_json_object(html, "window.__INITIAL_STATE__=")
    )
    if not state:
        raise ParseError("快手解析失败：页面未返回视频数据，请配置 cookies.txt 后重试")

    photo = None
    for key, value in state.items() if isinstance(state, dict) else []:
        if str(key).startswith("Photo:") and isinstance(value, dict) and (value.get("photoUrl") or value.get("photoH265Url")):
            photo = value
            break
    if photo is None:
        raise ParseError("快手解析失败：未找到视频资源，视频可能已删除或需要登录")

    caption = photo.get("caption") or photo.get("captionText") or "快手视频"
    cover = photo.get("coverUrl") or photo.get("poster") or ""
    duration = photo.get("duration") or 0
    try:
        duration = float(duration) / 1000 if float(duration) > 3000 else float(duration)
    except Exception:  # noqa: BLE001
        duration = 0

    user = ""
    for key, value in state.items() if isinstance(state, dict) else []:
        if str(key).startswith("User:") and isinstance(value, dict):
            user = value.get("name") or value.get("userName") or ""
            break
    if not user:
        user = photo.get("userName") or ""

    streams: list[dict[str, Any]] = []
    manifest = photo.get("manifest")
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except Exception:  # noqa: BLE001
            manifest = None
    if isinstance(manifest, dict):
        for group in manifest.get("adaptationSet") or []:
            for rep in group.get("representation") or []:
                stream_url = rep.get("url")
                if not stream_url:
                    continue
                height = rep.get("height") or 0
                streams.append(
                    {
                        "id": f"ks{len(streams)}",
                        "label": f"{height}p · 无水印原画" if height else "无水印原画",
                        "height": height,
                        "width": rep.get("width") or 0,
                        "ext": "mp4",
                        "size": 0,
                        "format": "direct",
                        "direct_url": stream_url,
                        "preview": True,
                        "fps": 0,
                    }
                )

    if not streams:
        direct = photo.get("photoUrl") or photo.get("photoH265Url")
        if direct:
            streams.append(
                {
                    "id": "ks0",
                    "label": "无水印原画",
                    "height": photo.get("height") or 0,
                    "width": photo.get("width") or 0,
                    "ext": "mp4",
                    "size": 0,
                    "format": "direct",
                    "direct_url": direct,
                    "preview": True,
                    "fps": 0,
                }
            )

    if not streams:
        raise ParseError("快手解析失败：未获取到可播放地址")

    streams.sort(key=lambda s: s["height"], reverse=True)
    return {
        "ok": True,
        "platform": "kuaishou",
        "platform_name": "快手",
        "title": re.sub(r"\s+", " ", str(caption)).strip()[:120],
        "author": user,
        "duration": int(duration),
        "cover": cover,
        "description": "",
        "view_count": photo.get("viewCount") or 0,
        "like_count": photo.get("likeCount") or 0,
        "url": url,
        "id": photo.get("id"),
        "formats": streams,
        "has_audio": True,
        "audio_hint": "默认导出最佳音轨（M4A/AAC）",
    }


# ----------------------------- 抖音兜底 -----------------------------

DOUYIN_ID_RE = re.compile(
    r"(?:/(?:video|note|slides)/|modal_id=|aweme_id=|item_ids=|object_id=|item_id=)(\d{8,})",
    re.IGNORECASE,
)


def _douyin_id(text: str) -> str:
    match = DOUYIN_ID_RE.search(text or "")
    return match.group(1) if match else ""


def _douyin_item_from_router(data: Any) -> dict | None:
    """从分享页 _ROUTER_DATA 中提取第一条视频数据。"""
    if not isinstance(data, dict):
        return None
    loader = data.get("loaderData") or {}
    for value in loader.values():
        if not isinstance(value, dict):
            continue
        items = ((value.get("videoInfoRes") or {}).get("item_list") or [])
        if items:
            return items[0]
        if isinstance(value.get("aweme_detail"), dict):
            return value["aweme_detail"]
    return None


def _parse_douyin_fallback(url: str) -> dict[str, Any]:
    headers = {"User-Agent": UA_MOBILE, "Referer": "https://www.douyin.com/"}

    # 分享口令 / 用户主页等非标准链接：先换成标准视频页再交给 yt-dlp
    early_id = _douyin_id(url)
    if early_id:
        try:
            return _parse_with_ytdlp(f"https://www.douyin.com/video/{early_id}", "douyin")
        except ParseError:
            pass

    html = ""
    final_url = url
    try:
        resp = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
        final_url = resp.url or url
        html = resp.text or ""
    except Exception:  # noqa: BLE001
        pass

    vid = early_id or _douyin_id(final_url)
    item = _douyin_item_from_router(_extract_json_object(html, "window._ROUTER_DATA"))

    # 兜底 1：直接请求 iesdouyin 分享页
    if item is None and vid:
        try:
            share_html = requests.get(
                f"https://www.iesdouyin.com/share/video/{vid}/",
                headers=headers,
                timeout=20,
            ).text
            item = _douyin_item_from_router(_extract_json_object(share_html, "window._ROUTER_DATA"))
        except Exception:  # noqa: BLE001
            pass

    # 兜底 2：旧版 iteminfo 接口
    if item is None and vid:
        try:
            payload = requests.get(
                f"https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={vid}",
                headers=headers,
                timeout=20,
            ).json()
            items = payload.get("item_list") or []
            item = items[0] if items else None
        except Exception:  # noqa: BLE001
            pass

    if item is None:
        if not vid:
            raise ParseError(
                "抖音解析失败：未能从链接中识别视频 ID。请使用 v.douyin.com 短链，或包含 /video/、modal_id、object_id 的完整链接"
            )
        raise ParseError(
            "已识别抖音视频 ID，但抖音接口要求登录态：请用浏览器插件导出 Netscape 格式 cookies，"
            "保存为 config/cookies.txt 后重试"
        )

    video = item.get("video") or {}
    direct = ""
    play = {}
    for key in ("play_addr", "play_addr_h264", "play_addr_265"):
        candidate = video.get(key) or {}
        if candidate.get("url_list"):
            play = candidate
            break
    uri = play.get("uri") or ""
    if play.get("url_list"):
        direct = str(play["url_list"][-1]).replace("playwm", "play")
    if not direct and uri:
        direct = f"https://www.douyin.com/aweme/v1/play/?video_id={uri}&ratio=1080p&line=0"
    if not direct:
        raise ParseError("抖音解析失败：未获取到无水印播放地址，请配置 cookies.txt 后重试")

    cover = ""
    for key in ("cover", "origin_cover", "dynamic_cover"):
        urls = (video.get(key) or {}).get("url_list") or []
        if urls:
            cover = urls[0]
            break

    width = int(video.get("width") or 1080)
    height = int(video.get("height") or 1920)
    return {
        "ok": True,
        "platform": "douyin",
        "platform_name": "抖音",
        "title": re.sub(r"\s+", " ", str(item.get("desc") or "抖音视频")).strip()[:120],
        "author": ((item.get("author") or {}).get("nickname") or ""),
        "duration": int((video.get("duration") or 0) / 1000),
        "cover": cover,
        "description": "",
        "view_count": (item.get("statistics") or {}).get("play_count") or 0,
        "like_count": (item.get("statistics") or {}).get("digg_count") or 0,
        "url": url,
        "id": item.get("aweme_id") or vid,
        "formats": [
            {
                "id": "douyin_nw",
                "label": f"{height}p · 无水印 · mp4",
                "height": height,
                "width": width,
                "ext": "mp4",
                "size": 0,
                "format": "direct",
                "direct_url": direct,
                "preview": True,
                "fps": 0,
            }
        ],
        "has_audio": True,
        "audio_hint": "默认导出最佳音轨（M4A/AAC）",
    }


def parse_url(url: str) -> dict[str, Any]:
    platform = detect_platform(url)
    if not platform:
        raise ParseError("目前支持：抖音、B站、小红书、快手")

    if platform == "kuaishou":
        return _parse_kuaishou(url)

    try:
        return _parse_with_ytdlp(url, platform)
    except ParseError:
        if platform == "douyin":
            return _parse_douyin_fallback(url)
        raise
