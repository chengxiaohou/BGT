// B站字幕提取 - Cloudflare Worker
// 接收前端请求，代理调用 B 站 API，返回字幕文本

const BILI_HEADERS = {
  "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
  "Referer": "https://www.bilibili.com/",
};

const SUBTITLE_PRIORITY = ["zh-CN", "zh-Hans", "zh", "ai-zh", "zh-Hant"];

// 通过 B 站指纹接口获取 buvid3/buvid4：数据中心 IP 直接请求 B 站接口会被风控拦
// 412，带上这个匿名标识可正常访问。结果在进程内缓存 10 分钟，避免每次多一次请求。
let cachedBuvidCookie = "";
let cachedBuvidAt = 0;

async function getBuvidCookies() {
  if (cachedBuvidCookie && Date.now() - cachedBuvidAt < 10 * 60 * 1000) {
    return cachedBuvidCookie;
  }
  try {
    const resp = await fetch("https://api.bilibili.com/x/frontend/finger/spi", {
      headers: BILI_HEADERS,
    });
    if (!resp.ok) return "";
    const data = await resp.json();
    const { b_3, b_4 } = data?.data || {};
    const parts = [];
    if (b_3) parts.push(`buvid3=${b_3}`);
    if (b_4) parts.push(`buvid4=${b_4}`);
    cachedBuvidCookie = parts.join("; ");
    cachedBuvidAt = Date.now();
  } catch {
    cachedBuvidCookie = "";
  }
  return cachedBuvidCookie;
}

// 合并 Cookie：匿名指纹在前，用户上传的登录 Cookie 在后
function mergeCookies(...parts) {
  return parts.filter(Boolean).join("; ");
}

// 解析 Netscape Cookie 格式，返回 cookie 字符串
function parseNetscapeCookies(text) {
  const cookies = [];
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const parts = trimmed.split("\t");
    if (parts.length < 6) continue;
    const [domain, , , , , name, ...rest] = parts;
    if (domain.includes("bilibili.com")) {
      cookies.push(`${name}=${rest.join("")}`);
    }
  }
  return cookies.join("; ");
}

// 提取 BV 号
function extractBvid(text) {
  const urlMatch = text.match(/https?:\/\/[^\s]+/);
  const candidate = urlMatch ? urlMatch[0].replace(/[，。,.;；、]+$/, "") : text;
  const match = candidate.match(/BV[0-9A-Za-z]{10}/);
  if (!match) throw new Error("未能识别出 B站视频链接");
  return match[0];
}

// 调用 B 站 API 获取视频信息
async function getVideoInfo(bvid, cookieStr) {
  const url = `https://api.bilibili.com/x/web-interface/view?bvid=${bvid}`;
  const headers = { ...BILI_HEADERS };
  if (cookieStr) headers["Cookie"] = cookieStr;
  const resp = await fetch(url, { headers });
  if (!resp.ok) throw new Error(`B站接口请求失败: HTTP ${resp.status}`);
  const data = await resp.json();
  if (data.code !== 0) throw new Error(data.message || "获取视频信息失败");
  return data.data;
}

// 获取字幕列表
async function getSubtitleUrls(bvid, cid, cookieStr) {
  const url = `https://api.bilibili.com/x/player/v2?bvid=${bvid}&cid=${cid}`;
  const headers = { ...BILI_HEADERS };
  if (cookieStr) headers["Cookie"] = cookieStr;
  const resp = await fetch(url, { headers });
  if (!resp.ok) return [];
  const data = await resp.json();
  if (data.code !== 0) return [];
  const subtitles = [];
  if (data.data?.subtitle?.subtitles) {
    for (const sub of data.data.subtitle.subtitles) {
      subtitles.push({
        lang: sub.lan,
        lang_name: sub.lan_doc,
        url: sub.subtitle_url,
      });
    }
  }
  return subtitles;
}

// 将秒数格式化为 SRT 时间轴 hh:mm:ss,mmm
function formatSrtTime(seconds) {
  const sec = Math.max(0, seconds);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  const ms = Math.round((sec % 1) * 1000);
  const pad = (n, w = 2) => String(n).padStart(w, "0");
  return `${pad(h)}:${pad(m)}:${pad(s)},${pad(ms, 3)}`;
}

// 下载字幕内容，同时生成纯文本与 SRT（字幕 JSON 带 from/to 时间戳）
async function fetchSubtitle(url) {
  if (url.startsWith("//")) url = "https:" + url;
  const resp = await fetch(url, { headers: BILI_HEADERS });
  if (!resp.ok) throw new Error(`字幕下载失败: HTTP ${resp.status}`);
  const data = await resp.json();
  const lines = [];
  const srtLines = [];
  for (const item of data.body || []) {
    if (!item.content) continue;
    lines.push(item.content);
    if (typeof item.from === "number" && typeof item.to === "number") {
      srtLines.push(
        `${srtLines.length + 1}\n${formatSrtTime(item.from)} --> ${formatSrtTime(item.to)}\n${item.content}\n`
      );
    }
  }
  return { text: lines.join("\n"), srt: srtLines.join("\n") };
}

// 构建 CORS 响应
function corsResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    },
  });
}

export default {
  async fetch(request) {
    // CORS 预检
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "POST, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type",
        },
      });
    }

    if (request.method !== "POST") {
      return corsResponse({ error: "仅支持 POST 请求" }, 405);
    }

    const urlObj = new URL(request.url);
    if (urlObj.pathname !== "/api/extract") {
      return corsResponse({ error: "路径不存在" }, 404);
    }

    const messages = [];
    const result = { success: true, messages, text: "", srt_content: "", title: "", uploader: "", pubdate: null, error: "" };

    try {
      const formData = await request.formData();
      const url = formData.get("url")?.trim();
      if (!url) return corsResponse({ error: "请输入 B站视频链接" }, 400);

      const cookieFile = formData.get("cookies");
      const buvidCookies = await getBuvidCookies();
      let cookieStr = buvidCookies;
      let hasUserCookies = false;
      if (cookieFile && cookieFile.size > 0) {
        const text = await cookieFile.text();
        const userCookies = parseNetscapeCookies(text);
        if (userCookies) {
          cookieStr = mergeCookies(buvidCookies, userCookies);
          hasUserCookies = true;
        }
      }

      // 1) 提取 BV 号
      const bvid = extractBvid(url);
      messages.push(`提取到 BV 号: ${bvid}`);

      // 2) 获取视频信息
      const videoInfo = await getVideoInfo(bvid, cookieStr);
      const title = videoInfo.title || "未知标题";
      const cid = videoInfo.cid;
      messages.push(`视频标题: ${title}`);
      result.title = title;
      result.uploader = videoInfo.owner?.name || "";
      result.pubdate = videoInfo.pubdate;

      // 3) 获取字幕
      let subtitles = [];
      if (hasUserCookies) {
        messages.push("正在使用上传的 Cookie 获取字幕…");
        subtitles = await getSubtitleUrls(bvid, cid, cookieStr);
      }
      if (!subtitles.length) {
        messages.push("尝试获取公开字幕…");
        subtitles = await getSubtitleUrls(bvid, cid);
      }

      if (subtitles.length) {
        const names = subtitles.map((s) => s.lang_name);
        messages.push(`发现字幕: ${names.join(", ")}`);
        let chosen = null;
        for (const lang of SUBTITLE_PRIORITY) {
          chosen = subtitles.find((s) => s.lang === lang);
          if (chosen) break;
        }
        if (!chosen) chosen = subtitles[0];
        messages.push(`使用字幕: ${chosen.lang_name}`);
        const sub = await fetchSubtitle(chosen.url);
        result.text = sub.text;
        result.srt_content = sub.srt;
      } else {
        messages.push("未发现可用字幕");
        result.success = false;
        result.error = "该视频没有可用的字幕";
      }
    } catch (err) {
      result.success = false;
      result.error = err.message;
      result.messages.push(`处理出错: ${err.message}`);
    }

    return corsResponse(result);
  },
};
