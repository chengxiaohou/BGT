// B站字幕提取 - Cloudflare Worker
// 接收前端请求，代理调用 B 站 API，返回字幕文本

const BILI_HEADERS = {
  "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
  "Referer": "https://www.bilibili.com/",
};

const SUBTITLE_PRIORITY = ["zh-CN", "zh-Hans", "zh", "ai-zh", "zh-Hant"];

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
async function getVideoInfo(bvid) {
  const url = `https://api.bilibili.com/x/web-interface/view?bvid=${bvid}`;
  const resp = await fetch(url, { headers: BILI_HEADERS });
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

// 下载字幕内容
async function fetchSubtitle(url) {
  if (url.startsWith("//")) url = "https:" + url;
  const resp = await fetch(url, { headers: BILI_HEADERS });
  const data = await resp.json();
  const lines = [];
  for (const item of data.body || []) {
    if (item.content) lines.push(item.content);
  }
  return lines.join("\n");
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
      let cookieStr = "";
      if (cookieFile && cookieFile.size > 0) {
        const text = await cookieFile.text();
        cookieStr = parseNetscapeCookies(text);
      }

      // 1) 提取 BV 号
      const bvid = extractBvid(url);
      messages.push(`提取到 BV 号: ${bvid}`);

      // 2) 获取视频信息
      const videoInfo = await getVideoInfo(bvid);
      const title = videoInfo.title || "未知标题";
      const cid = videoInfo.cid;
      messages.push(`视频标题: ${title}`);
      result.title = title;
      result.uploader = videoInfo.owner?.name || "";
      result.pubdate = videoInfo.pubdate;

      // 3) 获取字幕
      let subtitles = [];
      if (cookieStr) {
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
        result.text = await fetchSubtitle(chosen.url);
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