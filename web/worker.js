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

// 视频列表缓存（5 分钟 TTL）
const videoListCache = new Map();
const VIDEO_LIST_TTL = 5 * 60 * 1000;

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
  // 直接本地生成 buvid，跳过 SPI 调用以节省一次 socket 连接开销
  const rand = () => Math.floor(Math.random() * 16).toString(16);
  const now = Date.now();
  cachedBuvidCookie = `buvid3=${generateBuvid()}infoc; buvid4=FD${generateBuvid()}${now}-infoc; b_nut=${Math.floor(now / 1000)}; b_lsid=${Array.from({ length: 32 }, rand).join("")}`;
  cachedBuvidAt = now;
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

// 提取 aid 从 url，尝试回退解析
function extractAid(urlOrBvid) {
  // URL 格式：...?aid=123456 或者 /av123456
  const aidMatch = urlOrBvid.match(/[?&]aid=(\d+)/);
  if (aidMatch) return aidMatch[1];
  const avMatch = urlOrBvid.match(/av(\d+)/i);
  if (avMatch) return avMatch[1];
  return null;
}

// 获取视频信息（aid/cid/title/owner/pubdate）
// 先尝试 view API，失败再从 player 拿（player 有时没有 title）
async function getVideoInfo(bvid, cookieStr, url) {
  const aidFromUrl = extractAid(url || bvid);
  let viewUrl = `https://api.bilibili.com/x/web-interface/view?bvid=${bvid}`;
  if (aidFromUrl) viewUrl += `&aid=${aidFromUrl}`;

  try {
    const data = await biliApiGet(viewUrl, cookieStr);
    if (data.code === 0) {
      return {
        aid: data.data.aid,
        cid: data.data.cid,
        title: data.data.title,
        uploader: data.data.owner?.name || "",
        uploader_mid: data.data.owner?.mid || null,
        pubdate: data.data.pubdate,
      };
    }
  } catch (e) {
    // view 失败，继续尝试 player
  }

  // 从 player API 获取，必须 bvid + cid？实际上只要 bvid 也能拿到 cid 和 aid
  try {
    const data = await biliApiGet(
      `https://api.bilibili.com/x/player/wbi/v2?bvid=${bvid}${aidFromUrl ? `&aid=${aidFromUrl}` : ""}`,
      cookieStr
    );
    if (data.code === 0 && data.data?.cid) {
      return {
        aid: data.data.aid,
        cid: data.data.cid,
        title: data.data.title || data.data.video_title || "未知标题",
        uploader: (data.data.owner?.name || ""),
        uploader_mid: data.data.owner?.mid || null,
        pubdate: data.data.pubdate || Math.floor(Date.now() / 1000),
      };
    }
  } catch (e) {
    // 都失败了，抛出
  }

  throw new Error("无法获取视频信息，可能需要登录后上传 Cookie");
}

async function getSubtitleUrls(bvid, cid, cookieStr) {
  const data = await biliApiGet(
    `https://api.bilibili.com/x/player/wbi/v2?bvid=${bvid}&cid=${cid}`,
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

    if (request.method !== "POST") {
      return corsResponse({ error: "仅支持 POST 请求" }, 405);
    }

    if (urlObj.pathname === "/api/extract") {
      return handleExtract(request);
    }
    if (urlObj.pathname === "/api/search-up") {
      return handleSearchUp(request);
    }
    if (urlObj.pathname === "/api/up-videos") {
      return handleUpVideos(request);
    }

    return corsResponse({ error: "路径不存在" }, 404);
  },
};

// ── 处理字幕提取 ──
async function handleExtract(request) {
  const messages = [];
  const result = {
    success: true,
    messages,
    text: "",
    srt_content: "",
    title: "",
    uploader: "",
    uploader_mid: null,
    pubdate: null,
    error: "",
  };

  try {
    const formData = await request.formData();
    const url = formData.get("url")?.trim();
    if (!url) return corsResponse({ error: "请输入 B站视频链接" }, 400);

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

    const bvid = extractBvid(url);
    messages.push(`提取到 BV 号: ${bvid}`);

    const videoInfo = await getVideoInfo(bvid, cookieStr);
    const title = videoInfo.title || "未知标题";
    const cid = videoInfo.cid;
    messages.push(`视频标题: ${title}`);
    result.title = title;
    result.uploader = videoInfo.uploader || "";
    result.uploader_mid = videoInfo.uploader_mid || null;
    result.pubdate = videoInfo.pubdate;

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
      if (hasUserCookies) {
        result.error = "该视频没有可用的字幕";
      } else {
        result.error = "未发现公开字幕，可上传 B站 Cookie 获取 AI 识别字幕";
      }
    }
  } catch (err) {
    result.success = false;
    result.error = err.message;
    result.messages.push(`处理出错: ${err.message}`);
  }

  return corsResponse(result);
}

// ── 搜索 UP 主 ──
async function handleSearchUp(request) {
  try {
    const body = await request.json();
    let keyword = (body.keyword || "").trim();
    if (!keyword) return corsResponse({ error: "请输入 UP 主名称或空间链接" }, 400);

    // 从空间链接提取 MID
    const midMatch = keyword.match(/space\.bilibili\.com\/(\d+)/);
    if (midMatch) {
      const mid = midMatch[1];
      // 通过 view 接口获取用户信息
      const data = await biliApiGet(
        `https://api.bilibili.com/x/space/acc/info?mid=${mid}`,
        ""
      );
      if (data.code === 0 && data.data) {
        return corsResponse({
          mid: data.data.mid,
          name: data.data.name,
          avatar: data.data.face,
        });
      }
      return corsResponse({ error: "未找到该 UP 主" }, 404);
    }

    // 搜索 UP 主名称
    const searchData = await biliApiGet(
      `https://api.bilibili.com/x/web-interface/search/all/v2?keyword=${encodeURIComponent(keyword)}`,
      ""
    );
    if (searchData.code !== 0) {
      return corsResponse({ error: "搜索失败: " + (searchData.message || "未知错误") }, 400);
    }
    const results = searchData.data?.result || [];
    for (const r of results) {
      if (r.result_type === "bili_user" && r.data?.length) {
        const u = r.data[0];
        return corsResponse({
          mid: u.mid,
          name: u.uname,
          avatar: u.upic,
        });
      }
    }
    return corsResponse({ error: "未找到该 UP 主，请检查名称是否正确" }, 404);
  } catch (err) {
    return corsResponse({ error: "搜索出错: " + err.message }, 500);
  }
}

// ── 获取 UP 主最新视频列表 ──
async function handleUpVideos(request) {
  try {
    const body = await request.json();
    const mid = body.mid;
    const name = body.name || "";
    if (!mid) return corsResponse({ error: "缺少 UP 主 ID" }, 400);

    // 检查缓存
    const cacheKey = mid;
    const cached = videoListCache.get(cacheKey);
    if (cached && Date.now() - cached.at < VIDEO_LIST_TTL) {
      return corsResponse({ videos: cached.videos });
    }

    let data = null;
    const buvid = await getBuvidCookies();

    // 并行尝试两种方式，谁先返回有效数据就用谁
    const attempts = [];

    // 方式1: 空间 API（socket 直连）
    attempts.push((async () => {
      try {
        const resp = await socketRequest(
          `https://api.bilibili.com/x/space/arc/search?mid=${mid}&ps=10&pn=1&order=pubdate`,
          { headers: { ...BILI_HEADERS, Cookie: buvid } }
        );
        if (resp.status === 200) {
          const json = JSON.parse(resp.body);
          if (json.code === 0 && json.data?.list?.vlist?.length) {
            return json;
          }
        }
      } catch {}
      return null;
    })());

    // 方式2: 搜索 API（仅当有名称时）
    if (name) {
      attempts.push((async () => {
        try {
          const searchData = await biliApiGet(
            `https://api.bilibili.com/x/web-interface/search/type?search_type=video&keyword=${encodeURIComponent(name)}&ps=50&pn=1`,
            buvid
          );
          if (searchData && searchData.code === 0 && searchData.data?.result) {
            // 按 mid 过滤（只保留该 UP 主的视频），再按发布时间排序
            let filtered = searchData.data.result.filter(v => v.bvid && v.mid === mid);
            if (filtered.length < 3) {
              // mid 过滤后太少，不限定 mid 再试一次
              filtered = searchData.data.result.filter(v => v.bvid);
            }
            filtered.sort((a, b) => (b.pubdate || 0) - (a.pubdate || 0));
            if (filtered.length > 0) {
              return { code: 0, data: { list: { vlist: filtered.slice(0, 10) } } };
            }
          }
        } catch {}
        return null;
      })());
    }

    // 等待所有尝试，取第一个成功的结果
    const results = await Promise.allSettled(attempts);
    for (const r of results) {
      if (r.status === 'fulfilled' && r.value) {
        data = r.value;
        break;
      }
    }

    if (!data) {
      return corsResponse({ error: "获取视频列表失败，请稍后重试" }, 400);
    }

    const vlist = data.data?.list?.vlist || [];
    const videos = vlist
      .filter((v) => v.bvid)
      .map((v) => ({
        bvid: v.bvid,
        title: (v.title || "").replace(/<[^>]+>/g, ""),
        pic: v.pic,
        created: v.created || v.pubdate,
        length: v.length || v.duration || "",
        play: v.play,
      }))
      .sort((a, b) => (b.created || 0) - (a.created || 0));

    // 写入缓存
    videoListCache.set(cacheKey, { videos, at: Date.now() });

    return corsResponse({ videos });
  } catch (err) {
    return corsResponse({ error: "获取视频列表出错: " + err.message }, 500);
  }
}

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
