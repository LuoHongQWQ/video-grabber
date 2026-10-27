# 无痕视频下载

一个跑在本机的视频解析与下载工具，支持抖音、哔哩哔哩、小红书、快手，附带视频画质增强与角落水印修复。

## 功能

- 粘贴分享链接或口令，自动识别平台并解析
- 选择清晰度下载视频，或只提取音频、下载封面
- 画质增强：提升分辨率与帧率（FFmpeg 编码）
- 水印处理：针对固定角落的半透明水印，逐帧估算遮挡并补回背景
- 访问密码保护、并发任务上限
- 全部数据保存在本地，不上传到任何第三方服务

## 环境要求

- Python 3.10+
- FFmpeg（二选一）
  - 把 `ffmpeg` 可执行文件放到 `tools/` 目录：Windows 为 `tools/ffmpeg.exe`
  - 或安装到系统 PATH：`ffmpeg -version` 能输出版本即可

## 安装

```bash
git clone <本仓库地址>
cd video-grabber

python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

## 配置

首次启动会自动生成 `config.json`（不入库），并随机生成一个访问密码打印在控制台。
也可以手动创建配置：

```bash
cp config.example.json config.json
```

常用配置项：

| 配置 | 说明 |
| --- | --- |
| `auth.enabled` | 是否启用访问密码，默认 `true` |
| `auth.password` | 访问密码；留空则启动时自动生成随机密码 |
| `limits.max_concurrent_jobs` | 同时处理的任务数上限 |

也可以用环境变量覆盖密码：

```bash
VG_PASSWORD=你的密码 python -m uvicorn backend.app:app
```

部分平台（如需要登录才能看高清内容）可放入 Netscape 格式的 `config/cookies.txt`，该文件已被 `.gitignore` 忽略，请勿上传。

## 启动

Windows：

```bat
start.bat
```

Linux / macOS：

```bash
chmod +x start.sh
./start.sh
```

或直接运行：

```bash
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

浏览器打开 http://127.0.0.1:8000 ，输入访问密码即可。

> 默认只监听 `127.0.0.1`。若要局域网访问，把 `--host` 改为 `0.0.0.0`；若要公网访问，请自行在前面加 HTTPS 反向代理，并注意下面的合规提醒。

## 目录结构

```
backend/     FastAPI 服务、解析器、任务与 FFmpeg 处理
static/      前端页面（HTML/CSS/JS）
tools/       FFmpeg 等本地可执行文件（不入库）
downloads/   下载与处理产物（不入库）
config/      cookies 等本地凭证（不入库）
```

## 常见问题

- **提示服务器缺少 FFmpeg**：确认 `tools/ffmpeg.exe` 存在，或 `ffmpeg` 在 PATH 中，然后重启服务。
- **解析失败**：平台接口会变动，先 `pip install -U yt-dlp`；仍失败可提交 Issue 附链接与报错。
- **访问密码忘了**：删除 `config.json` 后重启，会重新生成随机密码；或修改 `auth.password`。

## 使用须知与免责声明

本项目仅供个人学习、研究与备份自己有权访问的内容使用。

- 请遵守目标平台的服务条款与当地法律法规，不要用于批量抓取、二次分发或商业用途。
- 请勿用于获取付费、付费墙或受 DRM 保护的内容。
- 下载内容的版权归原作者所有；因使用本工具产生的任何纠纷与责任由使用者自行承担。
- 公网部署需自行完成备案等合规要求，并妥善保管访问密码与 cookies。

## 许可证

[MIT](LICENSE)
