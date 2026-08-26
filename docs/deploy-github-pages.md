# 部署到 GitHub Pages + Cloudflare Worker（方案 A）

## 架构说明

GitHub Pages 只能托管静态网页，不能运行 Python 后端，所以本项目拆成两部分：

- **前端**：`web/index.html`（纯静态页面），部署到 GitHub Pages，地址为 `https://chengxiaohou.github.io/BGT/`
- **后端**：`web/worker.js`（Cloudflare Worker，免费），负责代理调用 B 站 API、解析上传的 Cookie、生成 txt / SRT
- **自动部署**：`.github/workflows/deploy.yml`，代码推送到 `main` 后自动部署前后端

> 注意：Cloudflare Worker 是 JavaScript 环境，无法运行本项目 Python 版带的语音识别（sherpa-onnx）。因此线上页面只能提取视频已有的 CC / AI 字幕，没有字幕的视频会提示“没有可用字幕”。

## 一次性配置（需要你手动完成，约 5 分钟）

### 1. GitHub Pages 开启 Actions 发布

1. 打开仓库页面：`https://github.com/chengxiaohou/BGT`
2. 进入 **Settings → Pages**
3. 在 **Build and deployment → Source** 下拉框选择 **GitHub Actions**（不是 Deploy from a branch）

> 如果仓库是私有的，请确认你的 GitHub 套餐支持 Pages（公开仓库没有这个限制）。

### 2. 创建 Cloudflare API Token

1. 注册 / 登录 [Cloudflare](https://dash.cloudflare.com/)（免费即可）
2. 右上角头像 → **My Profile → API Tokens → Create Token**
3. 模板选择 **Edit Cloudflare Workers**（预设权限），点击 **Continue to summary → Create Token**
4. 复制生成的 Token（只显示一次，请立即保存）

### 3. 把 Token 存入 GitHub Secrets

1. 打开仓库 **Settings → Secrets and variables → Actions → New repository secret**
2. Name 填：`CLOUDFLARE_API_TOKEN`
3. Secret 填：第 2 步复制的 Token

### 4. 确认 Cloudflare 的 workers.dev 子域名已启用

第一次用 Cloudflare Workers 时，需要先在 Cloudflare 控制台 **Workers & Pages** 页面启用一个 `*.workers.dev` 子域名（按提示操作即可）。没启用的话部署会失败。

## 部署方式

### 自动部署

把代码推送到 GitHub 的 `main` 分支，会自动执行部署：

1. 用 Cloudflare 部署 Worker（`web/worker.js`），得到 `https://bili-subtitles.你的子域名.workers.dev`
2. 把这个地址自动写入前端页面，替换 `WORKER_URL_PLACEHOLDER`
3. 把 `web/index.html` 发布到 GitHub Pages

也可以在仓库 **Actions → 部署前端到 GitHub Pages + Worker 到 Cloudflare → Run workflow** 手动触发。

### 本地调试

```bash
cd web
npx wrangler@4 dev --port 8787
```

然后把 `web/index.html` 里的 `WORKER_URL_PLACEHOLDER` 临时改成 `http://127.0.0.1:8787`，用浏览器打开 `web/index.html` 测试。注意：这个临时改动**不要提交**，线上部署时会自动替换成真实地址。

## 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 部署第一步报“未配置 CLOUDFLARE_API_TOKEN” | 没有添加 Secret，回到上面第 3 步 |
| 报“无法解析 Worker 地址” | 多个 Cloudflare 账号需要额外在 workflow 里配置账号 ID；或 workers.dev 子域名未启用 |
| 页面提示“页面缺少后端地址” | 打开了未部署的本地文件，按“本地调试”操作 |
| 提取结果为空 | 视频本身没有 CC / AI 字幕；AI 字幕需要上传登录 B 站的 Cookie（Netscape 格式），且 Cookie 未过期 |
| 私有仓库 Pages 打不开 | 私有仓库需要支持的 GitHub 套餐，或把仓库设为公开 |

> 技术备注：B 站会拦截 Cloudflare 数据中心 IP 的请求（HTTP 412）。本项目 Worker 已改用
> Cloudflare 底层 Socket API 直连 B 站接口绕过该拦截，同时自动处理 B 站的 buvid 设备标识，
> 因此线上无需额外配置即可正常访问。

## 功能对比（网页版 vs 线上版）

| 功能 | 原 Flask 网页版 | GitHub Pages 线上版 |
| --- | --- | --- |
| 提取 CC / AI 字幕 | ✅ | ✅（含 Cookie 上传） |
| 下载 txt | ✅ | ✅ |
| 下载 SRT | 仅语音识别时才有 | ✅（字幕自带时间轴） |
| 无字幕时的语音识别 | ✅ | ❌（Worker 无法运行 Python） |
