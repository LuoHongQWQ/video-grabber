const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  result: null,
  format: null,
  height: 0,
  fps: 0,
  mode: "fast",
  corner: "none",
  wmSize: "medium",
  wmMode: "mask",
  tasks: new Map(),
  timer: null,
  ffmpeg: false,
};

const PLATFORM_LABEL = {
  douyin: "抖音",
  bilibili: "哔哩哔哩",
  xiaohongshu: "小红书",
  kuaishou: "快手",
};

function toast(message, type = "") {
  const wrap = $("#toastWrap");
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = message;
  wrap.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transform = "translateY(10px)";
    el.style.transition = "all .3s";
    setTimeout(() => el.remove(), 320);
  }, 3600);
}

async function api(path, body) {
  const res = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) {
    if (res.status === 401) {
      window.location.replace("/login");
      throw new Error("登录已失效，请重新登录");
    }
    const detail = data && data.detail ? data.detail : `请求失败（${res.status}）`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function formatCount(n) {
  const num = Number(n) || 0;
  if (num >= 100000000) return `${(num / 100000000).toFixed(1)} 亿`;
  if (num >= 10000) return `${(num / 10000).toFixed(1)} 万`;
  return String(num);
}

function fallbackCover() {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="640" height="400"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0f172a"/><stop offset="1" stop-color="#1e293b"/></linearGradient></defs><rect width="640" height="400" fill="url(#g)"/><text x="320" y="210" font-family="sans-serif" font-size="22" fill="#475569" text-anchor="middle">暂无封面</text></svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

function qualityTag(height) {
  if (height >= 2160) return "4K";
  if (height >= 1440) return "2K";
  if (height >= 1080) return "HD";
  return "SD";
}

/* ------------------------------ 健康检查 ------------------------------ */

async function checkHealth() {
  const badge = $("#healthBadge");
  const text = $("#healthText");
  try {
    const data = await api("/api/health");
    state.ffmpeg = Boolean(data.ffmpeg);
    badge.classList.add("ok");
    text.textContent = data.ffmpeg
      ? `引擎就绪 · yt-dlp ${data.ytdlp} · 并发 ${data.max_concurrent}`
      : "缺少 FFmpeg · 增强不可用";
    if (!data.ffmpeg) {
      badge.classList.remove("ok");
      badge.classList.add("bad");
      $("#enhanceBtn").disabled = true;
    }
    if (!data.cookies) {
      // 仅作为提示，不影响普通解析
      console.info("未检测到 cookies.txt");
    }
  } catch (err) {
    badge.classList.add("bad");
    text.textContent = "服务未连接";
  }
}

/* ------------------------------ 解析 ------------------------------ */

async function handleParse(event) {
  event.preventDefault();
  const input = $("#urlInput");
  const value = input.value.trim();
  const errorBox = $("#errorBox");
  errorBox.classList.remove("show");

  if (!value) {
    errorBox.textContent = "请先粘贴视频链接";
    errorBox.classList.add("show");
    input.focus();
    return;
  }

  const btn = $("#parseBtn");
  btn.classList.add("loading");
  btn.disabled = true;
  $(".btn-text", btn).textContent = "解析中…";

  try {
    const data = await api("/api/parse", { url: value });
    state.result = data;
    renderResult(data);
    toast(`解析成功：${PLATFORM_LABEL[data.platform] || data.platform}`, "success");
  } catch (err) {
    errorBox.textContent = err.message;
    errorBox.classList.add("show");
    toast(err.message, "error");
  } finally {
    btn.classList.remove("loading");
    btn.disabled = false;
    $(".btn-text", btn).textContent = "解析视频";
  }
}

function renderResult(data) {
  const section = $("#resultSection");
  section.classList.remove("hidden");
  $("#enhanceSection").classList.remove("hidden");

  const cover = $("#coverImg");
  cover.src = data.cover || fallbackCover();
  cover.onerror = () => {
    cover.onerror = null;
    cover.src = fallbackCover();
  };

  $("#durationBadge").textContent = formatDuration(data.duration);
  const tag = $("#platformTag");
  tag.textContent = PLATFORM_LABEL[data.platform] || data.platform;
  tag.style.background = "rgba(34,211,238,.16)";

  $("#videoTitle").textContent = data.title;
  $("#videoAuthor").textContent = data.author || "未知作者";

  const metaParts = [];
  if (data.view_count) metaParts.push(`${formatCount(data.view_count)} 播放`);
  if (data.like_count) metaParts.push(`${formatCount(data.like_count)} 点赞`);
  if (data.formats && data.formats.length) metaParts.push(`${data.formats.length} 个清晰度`);
  $("#videoMeta").textContent = metaParts.join(" · ") || "已获取原始码流";

  const desc = $("#videoDesc");
  if (data.description) {
    desc.textContent = data.description;
    desc.style.display = "";
  } else {
    desc.style.display = "none";
  }

  renderQualities(data.formats || []);
  section.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderQualities(formats) {
  const list = $("#qualityList");
  list.innerHTML = "";
  if (!formats.length) {
    list.textContent = "未获取到可用清晰度";
    return;
  }

  formats.forEach((fmt, index) => {
    const item = document.createElement("div");
    item.className = "quality-item" + (index === 0 ? " active" : "");
    item.dataset.index = String(index);

    const label = document.createElement("span");
    label.textContent = fmt.label || `${fmt.height}p`;
    item.appendChild(label);

    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = qualityTag(fmt.height || 0);
    item.appendChild(tag);

    item.addEventListener("click", () => {
      $$(".quality-item", list).forEach((el) => el.classList.remove("active"));
      item.classList.add("active");
      state.format = fmt;
    });
    list.appendChild(item);
  });

  state.format = formats[0];
}

/* ------------------------------ 下载 ------------------------------ */

async function startDownload(kind) {
  if (!state.result) {
    toast("请先解析视频", "error");
    return;
  }
  const data = state.result;
  const payload = {
    url: data.url,
    kind,
    title: data.title,
  };

  let label = "视频";
  if (kind === "video") {
    if (!state.format) {
      toast("请选择一个清晰度", "error");
      return;
    }
    payload.format = state.format;
    label = `视频 ${state.format.height ? state.format.height + "P" : ""}`.trim();
  } else if (kind === "audio") {
    payload.format_string = "bestaudio/best";
    label = "音频";
  } else if (kind === "cover") {
    if (!data.cover) {
      toast("该视频没有可用封面", "error");
      return;
    }
    payload.format = { id: "cover", direct_url: data.cover, format: "direct" };
    label = "封面";
  }

  await submitJob("/api/download", payload, `${label}`, data.title);
}

async function submitJob(endpoint, payload, label, title) {
  try {
    const res = await api(endpoint, payload);
    addTask(res.job_id, label, title);
    toast(`已加入队列：${label}`, "success");
    ensurePolling();
  } catch (err) {
    toast(err.message, "error");
  }
}

async function startEnhance() {
  if (!state.result) {
    toast("请先解析视频", "error");
    return;
  }
  if (!state.ffmpeg) {
    toast("服务器缺少 FFmpeg，无法增强", "error");
    return;
  }
  if (!state.height && !state.fps && state.corner === "none") {
    toast("请至少选择分辨率、帧率或水印处理中的一项", "error");
    return;
  }
  if (!state.format) {
    toast("请选择一个清晰度", "error");
    return;
  }

  const data = state.result;
  const parts = [];
  if (state.height) parts.push(`${state.height}P`);
  if (state.fps) parts.push(`${state.fps}fps`);
  if (state.corner !== "none") parts.push(state.wmMode === "cover" ? "遮盖水印" : "去水印");
  const label = `增强 ${parts.join(" / ")}`;

  await submitJob(
    "/api/enhance",
    {
      url: data.url,
      title: data.title,
      format: state.format,
      height: state.height,
      fps: state.fps,
      mode: state.mode,
      corner: state.corner,
      wm_size: state.wmSize,
      wm_mode: state.wmMode,
    },
    label,
    data.title
  );
}

/* ------------------------------ 任务队列 ------------------------------ */

function addTask(jobId, label, title) {
  const task = { id: jobId, label, title, status: "queued", progress: 0, stage: "排队中", files: {}, error: "" };
  state.tasks.set(jobId, task);

  const section = $("#queueSection");
  section.classList.remove("hidden");

  const card = document.createElement("div");
  card.className = "task";
  card.id = `task-${jobId}`;

  const head = document.createElement("div");
  head.className = "task-head";
  const name = document.createElement("div");
  name.className = "task-title";
  name.textContent = title || "未命名视频";
  const kind = document.createElement("span");
  kind.className = "task-kind";
  kind.textContent = label;
  const status = document.createElement("span");
  status.className = "task-status";
  status.textContent = "排队中";
  head.append(name, kind, status);

  const bar = document.createElement("div");
  bar.className = "bar";
  const fill = document.createElement("i");
  bar.appendChild(fill);

  const foot = document.createElement("div");
  foot.className = "task-foot";
  const stage = document.createElement("span");
  stage.textContent = "准备中";
  const actions = document.createElement("span");
  actions.className = "task-actions";
  foot.append(stage, actions);

  card.append(head, bar, foot);
  $("#queueList").prepend(card);
  section.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function updateTaskCard(task) {
  const card = $(`#task-${task.id}`);
  if (!card) return;
  const statusEl = $(".task-status", card);
  const fill = $(".bar i", card);
  const stage = $(".task-foot span", card);
  const actions = $(".task-actions", card);

  fill.style.width = `${Math.max(0, Math.min(100, task.progress || 0))}%`;
  statusEl.className = "task-status";

  if (task.status === "done") {
    statusEl.textContent = "已完成";
    statusEl.classList.add("done");
    stage.textContent = "文件已生成";
    if (actions.childElementCount > 0) return;
    Object.entries(task.files || {}).forEach(([key, file]) => {
      const link = document.createElement("a");
      link.href = file.url;
      link.textContent = "保存文件";
      link.setAttribute("download", file.name || "");
      if (key !== "video") link.textContent = key === "audio" ? "保存音频" : key === "cover" ? "保存封面" : "保存文件";
      actions.appendChild(link);
      actions.appendChild(document.createTextNode(" "));
    });
  } else if (task.status === "error") {
    statusEl.textContent = "失败";
    statusEl.classList.add("error");
    stage.textContent = task.error || "任务失败";
    fill.style.background = "linear-gradient(90deg,#fb7185,#f43f5e)";
  } else {
    statusEl.textContent = `${task.progress || 0}%`;
    stage.textContent = task.stage || "处理中";
  }
}

function ensurePolling() {
  if (state.timer) return;
  state.timer = setInterval(pollTasks, 900);
}

async function pollTasks() {
  const pending = Array.from(state.tasks.values()).filter(
    (t) => t.status === "queued" || t.status === "running"
  );
  if (!pending.length) {
    clearInterval(state.timer);
    state.timer = null;
    return;
  }

  await Promise.all(
    pending.map(async (task) => {
      try {
        const data = await api(`/api/jobs/${task.id}`);
        task.status = data.status;
        task.progress = data.progress;
        task.stage = data.stage;
        task.files = data.files || {};
        task.error = data.error || "";
        updateTaskCard(task);
        if (data.status === "done") {
          toast(`${task.label} 已完成，可保存文件`, "success");
        } else if (data.status === "error") {
          toast(`${task.label} 失败：${data.error}`, "error");
        }
      } catch (err) {
        task.status = "error";
        task.error = err.message;
        updateTaskCard(task);
      }
    })
  );
}

/* ------------------------------ 其余交互 ------------------------------ */

function bindSegmented(id, key) {
  const box = $(id);
  box.addEventListener("click", (event) => {
    const btn = event.target.closest("button");
    if (!btn) return;
    $$("button", box).forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state[key] = Number(btn.dataset.height ?? btn.dataset.fps ?? 0);
  });
}

async function loadConfig() {
  try {
    const cfg = await api("/api/config");
    const d = cfg.donation || {};
    if (d.title) $("#donateTitle").textContent = d.title;
    if (d.subtitle) $("#donateSubtitle").textContent = d.subtitle;
    if (d.note) $("#donateNote").textContent = d.note;

    if (d.alipay_qr) {
      const box = $("#alipayQr");
      box.innerHTML = "";
      const img = document.createElement("img");
      img.src = d.alipay_qr;
      img.alt = "支付宝收款码";
      box.appendChild(img);
    }
    if (d.wechat_qr) {
      const box = $("#wechatQr");
      box.innerHTML = "";
      const img = document.createElement("img");
      img.src = d.wechat_qr;
      img.alt = "微信收款码";
      box.appendChild(img);
    }

    const link = $("#afdianLink");
    if (d.afdian_url) {
      link.href = d.afdian_url;
    } else {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        toast("可在 config.json 中配置爱发电主页地址");
      });
    }
  } catch (err) {
    console.warn("配置加载失败", err);
  }
}

function openPreview() {
  if (!state.format) {
    toast("请先选择清晰度", "error");
    return;
  }
  const video = $("#previewVideo");
  if (!video.classList.contains("hidden")) {
    video.pause();
    video.src = "";
    video.classList.add("hidden");
    return;
  }
  if (!state.format.preview_url) {
    toast("该清晰度为分离音视频流，下载后即可完整观看", "error");
    return;
  }
  video.src = state.format.preview_url;
  video.classList.remove("hidden");
  video.play().catch(() => {});
}

document.addEventListener("DOMContentLoaded", () => {
  checkHealth();
  loadConfig();

  $("#parseForm").addEventListener("submit", handleParse);
  $("#pasteBtn").addEventListener("click", async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (!text) {
        toast("剪贴板为空");
        return;
      }
      $("#urlInput").value = text.trim();
      $("#urlInput").focus();
      toast("已粘贴，按回车或点击解析", "success");
    } catch {
      toast("浏览器未授权读取剪贴板，请手动粘贴", "error");
    }
  });

  $("#downloadVideoBtn").addEventListener("click", () => startDownload("video"));
  $("#downloadAudioBtn").addEventListener("click", () => startDownload("audio"));
  $("#downloadCoverBtn").addEventListener("click", () => startDownload("cover"));
  $("#enhanceBtn").addEventListener("click", startEnhance);
  $("#previewBtn").addEventListener("click", (event) => {
    event.stopPropagation();
    openPreview();
  });

  bindSegmented("#resOptions", "height");
  bindSegmented("#fpsOptions", "fps");
  $("#modeOptions").addEventListener("click", (event) => {
    const btn = event.target.closest("button");
    if (!btn) return;
    $$("#modeOptions button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.mode = btn.dataset.mode || "fast";
  });
  $("#watermarkOptions").addEventListener("click", (event) => {
    const btn = event.target.closest("button");
    if (!btn) return;
    $$("#watermarkOptions button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.corner = btn.dataset.corner || "none";
    const enabled = state.corner !== "none";
    $("#wmSizeGroup").classList.toggle("hidden", !enabled);
    $("#wmModeGroup").classList.toggle("hidden", !enabled);
  });
  $("#wmSizeOptions").addEventListener("click", (event) => {
    const btn = event.target.closest("button");
    if (!btn) return;
    $$("#wmSizeOptions button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.wmSize = btn.dataset.size || "medium";
  });
  $("#wmModeOptions").addEventListener("click", (event) => {
    const btn = event.target.closest("button");
    if (!btn) return;
    $$("#wmModeOptions button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.wmMode = btn.dataset.wmMode || "delogo";
  });
  $("#logoutBtn").addEventListener("click", async () => {
    try {
      await fetch("/api/logout", { method: "POST" });
    } catch (err) {
      /* 忽略网络错误，直接回登录页 */
    }
    window.location.replace("/login");
  });

  $("#clearQueueBtn").addEventListener("click", () => {
    const done = Array.from(state.tasks.values()).filter((t) => t.status === "done" || t.status === "error");
    done.forEach((t) => {
      state.tasks.delete(t.id);
      $(`#task-${t.id}`)?.remove();
    });
    if (!state.tasks.size) $("#queueSection").classList.add("hidden");
  });

  $("#guideBtn").addEventListener("click", () => $("#guideModal").classList.add("open"));
  $("#modalClose").addEventListener("click", () => $("#guideModal").classList.remove("open"));
  $("#guideModal").addEventListener("click", (event) => {
    if (event.target === $("#guideModal")) $("#guideModal").classList.remove("open");
  });

  document.addEventListener("click", (event) => {
    const video = $("#previewVideo");
    if (!video.classList.contains("hidden") && event.target !== video && !event.target.closest("#previewBtn")) {
      video.pause();
      video.src = "";
      video.classList.add("hidden");
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const video = $("#previewVideo");
    video.pause();
    video.src = "";
    video.classList.add("hidden");
    $("#guideModal").classList.remove("open");
  });
});
