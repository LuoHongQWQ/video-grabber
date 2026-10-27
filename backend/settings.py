"""全局配置与平台识别工具。"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = BASE_DIR / "tools"
DOWNLOAD_DIR = BASE_DIR / "downloads"
CACHE_DIR = BASE_DIR / "cache"
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = BASE_DIR / "config.json"
COOKIE_FILE = CONFIG_DIR / "cookies.txt"

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

PLATFORMS = {
    "douyin": {
        "name": "抖音",
        "hosts": ("douyin.com", "iesdouyin.com"),
        "color": "#fe2c55",
    },
    "bilibili": {
        "name": "哔哩哔哩",
        "hosts": ("bilibili.com", "b23.tv", "bili2233.cn"),
        "color": "#00aeec",
    },
    "xiaohongshu": {
        "name": "小红书",
        "hosts": ("xiaohongshu.com", "xhslink.com"),
        "color": "#ff2442",
    },
    "kuaishou": {
        "name": "快手",
        "hosts": ("kuaishou.com", "kwai.com", "chenzhongtech.com", "gifshow.com", "kwai.net"),
        "color": "#ff7a00",
    },
}

URL_RE = re.compile(r"https?://[^\s<>\"'，。！？、；：【】（）\u4e00-\u9fff]+", re.I)


def find_ffmpeg() -> str | None:
    for name in ("ffmpeg.exe", "ffmpeg"):
        local = TOOLS_DIR / name
        if local.exists():
            return str(local)
    return shutil.which("ffmpeg")


FFMPEG = find_ffmpeg()


def normalize_url(text: str) -> str:
    """从分享口令中提取第一个链接。"""
    if not text:
        raise ValueError("请输入视频链接")
    text = text.strip()
    match = URL_RE.search(text)
    if match:
        return match.group(0).rstrip(".,;)")
    if text.startswith("BV") or text.startswith("bv"):
        return f"https://www.bilibili.com/video/{text}"
    raise ValueError("未识别到有效的链接，请粘贴完整的分享链接")


def detect_platform(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    for key, meta in PLATFORMS.items():
        if any(host == h or host.endswith("." + h) for h in meta["hosts"]):
            return key
    return None


def load_config() -> dict:
    default = {
        "site_name": "无痕视频下载",
        "auth": {"enabled": True, "password": "", "secret": ""},
        "limits": {"max_concurrent_jobs": 2},
        "donation": {
            "title": "支持这个项目",
            "subtitle": "如果它帮你节省了时间，欢迎请作者喝一杯咖啡",
            "alipay_qr": "",
            "wechat_qr": "",
            "afdian_url": "",
            "note": "收款码可在 config.json 中替换为自己的图片路径或链接。",
        },
    }
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            default.update(data)
            default["donation"] = {**default["donation"], **(data.get("donation") or {})}
            default["auth"] = {**default["auth"], **(data.get("auth") or {})}
            default["limits"] = {**default["limits"], **(data.get("limits") or {})}
        except Exception:
            pass
    return default


def save_config(config: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def get_auth_secret() -> str:
    """用于签发登录 cookie 的密钥，首次运行时自动生成并写入 config.json。"""
    global _SECRET_CACHE
    if _SECRET_CACHE:
        return _SECRET_CACHE
    config = load_config()
    secret = str((config.get("auth") or {}).get("secret") or "")
    if not secret:
        secret = secrets.token_hex(32)
        config.setdefault("auth", {})["secret"] = secret
        try:
            save_config(config)
        except Exception:
            pass
    _SECRET_CACHE = secret
    return secret


_SECRET_CACHE = ""


def ensure_auth_password(config: dict | None = None) -> str:
    """返回访问密码：环境变量优先，其次 config.json，都没有时随机生成并保存。"""
    global _PASSWORD_CACHE
    env = os.environ.get("VG_PASSWORD", "").strip()
    if env:
        return env
    if _PASSWORD_CACHE:
        return _PASSWORD_CACHE
    cfg = config if config is not None else load_config()
    auth = cfg.setdefault("auth", {})
    password = str(auth.get("password") or "")
    if not password:
        password = secrets.token_urlsafe(9)
        auth["password"] = password
        try:
            save_config(cfg)
        except OSError:
            pass
        print(
            f"[提示] 已生成访问密码：{password}"
            "（可修改 config.json 的 auth.password 或设置环境变量 VG_PASSWORD）",
            flush=True,
        )
    _PASSWORD_CACHE = password
    return password


_PASSWORD_CACHE = ""


def load_cookie_jar() -> dict[str, str]:
    """读取 Netscape 格式 cookies.txt，返回 Cookie 请求头字符串。"""
    if not COOKIE_FILE.exists():
        return {}
    jar: dict[str, list[str]] = {}
    for raw in COOKIE_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, _path, _secure, _exp, name, value = parts[:7]
        host = domain.lstrip(".").lower()
        jar.setdefault(host, []).append(f"{name}={value}")
    return {host: "; ".join(items) for host, items in jar.items()}


def cookie_header_for(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    jar = load_cookie_jar()
    for domain, header in jar.items():
        if host == domain or host.endswith("." + domain):
            return header
    return ""


def has_cookies() -> bool:
    return COOKIE_FILE.exists() and COOKIE_FILE.stat().st_size > 0
