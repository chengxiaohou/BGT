// B站字幕提取 - Cloudflare Worker
// 接收前端请求，代理调用 B 站 API，返回字幕文本

import { connect } from "cloudflare:sockets";

const BILI_HEADERS = {
  "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
  "Referer": "https://www.bilibili.com/",
  "Origin": "https://www.bilibili.com",
  "Accept": "application/json, text/plain, */*",
  "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
  "Sec-Fetch-Dest": "empty",
  "Sec-Fetch-Mode": "cors",
  "Sec-Fetch-Site": "same-site",
  "Pragma": "no-cache",
  "Cache-Control": "no-cache",
};

const SUBTITLE_PRIORITY = ["zh-CN", "zh-Hans", "zh", "ai-zh", "zh-Hant"];

// 请求 B 站 JSON 接口（使用 fetch，如遇 412 则自动降级为 socket）
async function biliApiGet(url, cookieStr) {
  // 先尝试 socket（绕过 Cloudflare 的 CF-* 头），失败再试 fetch
  let lastError;
  try {
    const data = await socketApiGet(url, cookieStr);
    if (data.code !== 0) lastError = data.message;
    else return data;
  } catch (e) {
    lastError = e.message;
  }
  // socket 失败时，用 fetch 兜底
  try {
    const resp = await fetch(url, {
      headers: { ...BILI_HEADERS, Cookie: cookieStr },
    });
    if (resp.status !== 200) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    if (data.code !== 0) throw new Error(data.message || "接口返回错误");
    return data;
  } catch(e) {
    throw new Error(lastError || e.message);
  }
}

// 获取匿名设备标识（buvid3/buvid4/b_nut/b_lsid），结果缓存 10 分钟
let cachedBuvidCookie = "";
let cachedBuvidAt = 0;

function generateBuvid() {
  const hex = () => Math.floor(Math.random() * 16).toString(16);
  const uuid = Array.from({ length: 8 }, hex).join("") + "-" +
    Array.from({ length: 4 }, hex).join("") + "-" +
    Array.from({ length: 4 }, hex).join("") + "-" +
    Array.from({ length: 4 }, hex).join("") + "-" +
    Array.from({ length: 12 }, hex).join("");
  return uuid;
}

async function getBuvidCookies() {
  if (cachedBuvidCookie && Date.now() - cachedBuvidAt < 10 * 60 * 1000) {
    return cachedBuvidCookie;
  }
  try {
    // 优先用 socket 调 SPI 接口（避免 CF-* 头），失败再用 fetch
    let spiData = null;
    try {
      const socketResp = await socketRequest("https://api.bilibili.com/x/frontend/finger/spi", {
        headers: BILI_HEADERS,
      });
      if (socketResp.status === 200) {
        spiData = JSON.parse(socketResp.body);
      }
    } catch {}
    if (!spiData) {
      try {
        const fetchResp = await fetch("https://api.bilibili.com/x/frontend/finger/spi", {
          headers: BILI_HEADERS,
        });
        if (fetchResp.status === 200) spiData = await fetchResp.json();
      } catch {}
    }
    const parts = [];
    if (spiData?.data) {
      const { b_3, b_4 } = spiData.data;
      if (b_3) parts.push(`buvid3=${b_3}`);
      if (b_4) parts.push(`buvid4=${b_4}`);
    }
    // 无论 SPI 是否成功，都生成保底标识
    if (!parts.some(p => p.startsWith("buvid3"))) {
      parts.push(`buvid3=${generateBuvid()}infoc`);
    }
    if (!parts.some(p => p.startsWith("buvid4"))) {
      parts.push(`buvid4=FD${generateBuvid()}${Date.now()}-infoc`);
    }
    const rand = () => Math.floor(Math.random() * 16).toString(16);
    parts.push(`b_nut=${Math.floor(Date.now() / 1000)}`);
    parts.push(`b_lsid=${Array.from({ length: 32 }, rand).join("")}`);
    cachedBuvidCookie = parts.join("; ");
    cachedBuvidAt = Date.now();
  } catch {
    const rand = () => Math.floor(Math.random() * 16).toString(16);
    const now = Date.now();
    cachedBuvidCookie = `buvid3=${generateBuvid()}infoc; buvid4=FD${generateBuvid()}${now}-infoc; b_nut=${Math.floor(now / 1000)}; b_lsid=${Array.from({ length: 32 }, rand).join("")}`;
    cachedBuvidAt = now;
  }
  return cachedBuvidCookie;
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

async function getVideoInfo(bvid, cookieStr) {
  const data = await biliApiGet(
    `https://api.bilibili.com/x/web-interface/view?bvid=${bvid}`,
    cookieStr
  );
  if (data.code !== 0) throw new Error(data.message || "获取视频信息失败");
  return data.data;
}

async function getSubtitleUrls(bvid, cid, cookieStr) {
  const data = await biliApiGet(
    `https://api.bilibili.com/x/player/v2?bvid=${bvid}&cid=${cid}`,
    cookieStr
  );
  if (data.code !== 0) return [];
  const subtitles = [];
  if (data.data?.subtitle?.subtitles) {
    for (const sub of data.data.subtitle.subtitles) {
      if (sub.subtitle_url) {
        subtitles.push({
          lang: sub.lan,
          lang_name: sub.lan_doc,
          url: sub.subtitle_url,
        });
      }
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
  try {
    if (url.startsWith("//")) url = "https:" + url;
    let resp;
    try {
      resp = await fetch(url, { headers: BILI_HEADERS });
    } catch {
      resp = null;
    }
    let data;
    if (resp && resp.status === 200) {
      data = await resp.json();
    } else {
      // 降级: socket 请求
      const socketResp = await socketRequest(url, { headers: BILI_HEADERS });
      if (socketResp.status !== 200) throw new Error(`HTTP ${socketResp.status}`);
      try {
        data = JSON.parse(socketResp.body);
      } catch {
        throw new Error("字幕文件解析失败");
      }
    }
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
  } catch (err) {
    throw new Error(`字幕下载失败: ${err.message}（URL: ${String(url).slice(0, 80)}）`);
  }
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

    const urlObj = new URL(request.url);

    // 调试端点：测试 SPI 接口
    if (urlObj.pathname === "/api/debug") {
      const bvid = urlObj.searchParams.get("bvid") || "BV1uT4y1P7CX";
      const cookieStr = await getBuvidCookies();
      const results = { cookie: cookieStr, bvid };
      // 测试 socket: player API 无 cid
      try {
        const socketResp = await socketRequest(`https://api.bilibili.com/x/player/v2?bvid=${bvid}`, {
          headers: { ...BILI_HEADERS, Cookie: cookieStr },
        });
        results.player_no_cid = { status: socketResp.status, body: socketResp.body.slice(0, 1000) };
      } catch (e) {
        results.player_no_cid = { error: e.message };
      }
      // 测试 socket: player API 用返回的 cid
      try {
        const data = JSON.parse(results.player_no_cid?.body || "{}");
        const cid = data?.data?.cid || "1";
        const socketResp2 = await socketRequest(`https://api.bilibili.com/x/player/v2?bvid=${bvid}&cid=${cid}`, {
          headers: { ...BILI_HEADERS, Cookie: cookieStr },
        });
        results.player_with_cid = { status: socketResp2.status, body: socketResp2.body.slice(0, 4000) };
      } catch (e) {
        results.player_with_cid = { error: e.message };
      }
      return corsResponse(results);
    }

    if (request.method !== "POST") {
      return corsResponse({ error: "仅支持 POST 请求" }, 405);
    }

    if (urlObj.pathname !== "/api/extract") {
      return corsResponse({ error: "路径不存在" }, 404);
    }

    const messages = [];
    const result = {
      success: true,
      messages,
      text: "",
      srt_content: "",
      title: "",
      uploader: "",
      pubdate: null,
      error: "",
    };

    try {
      const formData = await request.formData();
      const url = formData.get("url")?.trim();
      if (!url) return corsResponse({ error: "请输入 B站视频链接" }, 400);

      // 有用户上传的 Cookie 时优先使用（含登录态），否则生成匿名设备标识
      const cookieFile = formData.get("cookies");
      let hasUserCookies = false;
      let cookieStr = "";
      if (cookieFile && cookieFile.size > 0) {
        const text = await cookieFile.text();
        const userCookies = parseNetscapeCookies(text);
        if (userCookies) {
          cookieStr = userCookies;
          hasUserCookies = true;
        }
      }
      if (!cookieStr) cookieStr = await getBuvidCookies();

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
        subtitles = await getSubtitleUrls(bvid, cid, cookieStr);
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

// ── Socket 降级方案（当 fetch 被 B 站 WAF 拦截时使用）──
const MAX_RESPONSE_BYTES = 4 * 1024 * 1024;

function concatChunks(chunks) {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const c of chunks) {
    out.set(c, offset);
    offset += c.length;
  }
  return out;
}

const CRLF = new TextEncoder().encode("\r\n");
const CRLFCRLF = new TextEncoder().encode("\r\n\r\n");

function bytesIndexOf(buf, needle, from = 0) {
  outer: for (let i = from; i + needle.length <= buf.length; i++) {
    for (let j = 0; j < needle.length; j++) {
      if (buf[i + j] !== needle[j]) continue outer;
    }
    return i;
  }
  return -1;
}

function decodeChunkedBytes(buf) {
  const out = [];
  let pos = 0;
  while (pos < buf.length) {
    const lineEnd = bytesIndexOf(buf, CRLF, pos);
    if (lineEnd === -1) break;
    const sizeText = new TextDecoder().decode(buf.slice(pos, lineEnd)).trim();
    const size = parseInt(sizeText, 16);
    if (!Number.isFinite(size) || size <= 0) break;
    const chunkStart = lineEnd + 2;
    out.push(buf.slice(chunkStart, chunkStart + size));
    pos = chunkStart + size + 2;
  }
  return concatChunks(out);
}

function parseHttpResponse(buf) {
  const headerEnd = bytesIndexOf(buf, CRLFCRLF);
  if (headerEnd === -1) throw new Error("B站返回了无效的 HTTP 响应");
  const lines = new TextDecoder().decode(buf.slice(0, headerEnd)).split("\r\n");
  const status = Number((lines[0].match(/^HTTP\/1\.[01]\s+(\d+)/) || [])[1]);
  const headers = {};
  for (let i = 1; i < lines.length; i++) {
    const m = lines[i].match(/^([^:]+):\s*(.*)$/);
    if (m) headers[m[1].toLowerCase()] = m[2];
  }
  let body = buf.slice(headerEnd + 4);
  if ((headers["transfer-encoding"] || "").toLowerCase().includes("chunked")) {
    body = decodeChunkedBytes(body);
  }
  return { status, headers, body: new TextDecoder().decode(body) };
}

async function socketRequest(rawUrl, { headers = {}, method = "GET" } = {}) {
  const u = new URL(rawUrl);
  const isHttps = u.protocol === "https:";
  const port = Number(u.port) || (isHttps ? 443 : 80);
  const socket = connect(
    { hostname: u.hostname, port },
    { secureTransport: isHttps ? "on" : "off", allowHalfOpen: true }
  );
  try {
    const writer = socket.writable.getWriter();
    const allHeaders = {
      Host: u.host,
      Connection: "close",
      ...headers,
    };
    const head =
      `${method} ${u.pathname}${u.search} HTTP/1.1\r\n` +
      Object.entries(allHeaders)
        .map(([k, v]) => `${k}: ${v}`)
        .join("\r\n") +
      "\r\n\r\n";
    await writer.write(new TextEncoder().encode(head));
    const reader = socket.readable.getReader();
    const chunks = [];
    let total = 0;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      chunks.push(value);
      total += value.length;
      if (total > MAX_RESPONSE_BYTES) throw new Error("B站响应过大");
    }
    return parseHttpResponse(concatChunks(chunks));
  } finally {
    try { socket.close(); } catch {}
  }
}

async function socketApiGet(url, cookieStr) {
  const headers = { ...BILI_HEADERS };
  if (cookieStr) headers.Cookie = cookieStr;
  const resp = await socketRequest(url, { headers });
  if (resp.status !== 200) throw new Error(`B站接口请求失败: HTTP ${resp.status}`);
  let data;
  try {
    data = JSON.parse(resp.body);
  } catch {
    throw new Error("B站接口返回了非 JSON 数据");
  }
  return data;
}
