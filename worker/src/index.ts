export interface Env {
  SECRET_TOKEN: string;
  PULSE_KV: KVNamespace;
}

type PublicNewsItem = {
  title: string;
  url: string;
  source: string;
  summary?: string;
};

type NewsItem = PublicNewsItem & {
  publishedAt?: number;
  score?: number;
};

type SourceType = "huggingface_daily_papers" | "rss" | "rss_fallback" | "arxiv";

type DataSource = {
  id: string;
  name: string;
  type: SourceType;
  enabled: boolean;
  url?: string;
  urls?: string[];
  category?: string;
  limit?: number;
};

type WorkerConfig = {
  sources: DataSource[];
};

type CachePayload = {
  items: PublicNewsItem[];
  fetchedAt: string;
  sourceCount: number;
};

type WorkerRequestInit = RequestInit & {
  cf?: {
    cacheEverything?: boolean;
    cacheTtl?: number;
  };
};

const CONFIG_KEY = "pulse:config";
const CACHE_KEY = "pulse:latest";
const MAX_ITEMS = 15;
const PER_SOURCE_BASE_LIMIT = 3;
const REQUEST_TIMEOUT_MS = 8000;
const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
};
const HTML_HEADERS = {
  "content-type": "text/html; charset=utf-8",
  "cache-control": "no-store",
};

const DEFAULT_SOURCES: DataSource[] = [
  {
    id: "huggingface-daily-papers",
    name: "Hugging Face Daily Papers",
    type: "huggingface_daily_papers",
    enabled: true,
    limit: 20,
  },
  {
    id: "techcrunch-ai",
    name: "TechCrunch AI",
    type: "rss",
    enabled: true,
    url: "https://techcrunch.com/category/artificial-intelligence/feed/",
    limit: 12,
  },
  {
    id: "arxiv-cs-cl",
    name: "arXiv cs.CL",
    type: "arxiv",
    enabled: true,
    category: "cs.CL",
    limit: 10,
  },
  {
    id: "arxiv-cs-lg",
    name: "arXiv cs.LG",
    type: "arxiv",
    enabled: true,
    category: "cs.LG",
    limit: 10,
  },
  {
    id: "machine-heart",
    name: "Machine Heart",
    type: "rss_fallback",
    enabled: true,
    urls: [
      "https://www.jiqizhixin.com/rss",
      "https://decemberpei.cyou/rssbox/wechat-jiqizhixin.xml",
    ],
    limit: 10,
  },
];

const AI_KEYWORDS = [
  "ai",
  "artificial intelligence",
  "llm",
  "large language model",
  "model",
  "benchmark",
  "evaluation",
  "eval",
  "agent",
  "reasoning",
  "multimodal",
  "transformer",
  "qwen",
  "deepseek",
  "kimi",
  "doubao",
];

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (!env.SECRET_TOKEN) {
      return json({ error: "Worker secret is not configured" }, 500);
    }

    if (url.pathname === "/admin") {
      if (!isAuthorized(request, env.SECRET_TOKEN)) {
        return basicAuthRequired();
      }
      return new Response(adminPage(), { status: 200, headers: HTML_HEADERS });
    }

    if (url.pathname === "/api/config") {
      if (!isAuthorized(request, env.SECRET_TOKEN)) {
        return json({ error: "Unauthorized" }, 401);
      }
      if (request.method === "GET") {
        return json(await loadConfig(env), 200);
      }
      if (request.method === "PUT") {
        const nextConfig = await readConfigRequest(request);
        await saveConfig(env, nextConfig);
        return json(nextConfig, 200);
      }
      return json({ error: "Method not allowed" }, 405);
    }

    if (url.pathname === "/api/refresh") {
      if (request.method !== "POST") {
        return json({ error: "Method not allowed" }, 405);
      }
      if (!isAuthorized(request, env.SECRET_TOKEN)) {
        return json({ error: "Unauthorized" }, 401);
      }
      return json(await refreshCache(env), 200);
    }

    if (request.method !== "GET") {
      return json({ error: "Method not allowed" }, 405);
    }

    if (!isAuthorized(request, env.SECRET_TOKEN)) {
      return json({ error: "Unauthorized" }, 401);
    }

    const cached = await loadLatest(env);
    return json(cached?.items ?? [], 200);
  },

  async scheduled(
    _controller: ScheduledController,
    env: Env,
    _ctx: ExecutionContext,
  ): Promise<void> {
    if (!env.SECRET_TOKEN) {
      console.warn("Worker secret is not configured; scheduled refresh skipped");
      return;
    }
    await refreshCache(env);
  },
};

async function refreshCache(env: Env): Promise<CachePayload> {
  const config = await loadConfig(env);
  const enabledSources = config.sources.filter((source) => source.enabled);
  const results = await Promise.allSettled(enabledSources.map(fetchSource));
  const merged = results.flatMap((result) =>
    result.status === "fulfilled" ? result.value : [],
  );
  const items = normalizeItems(merged).map(({ title, url, source, summary }) => {
    const item: PublicNewsItem = { title, url, source };
    if (summary) item.summary = summary;
    return item;
  });
  const payload: CachePayload = {
    items,
    fetchedAt: new Date().toISOString(),
    sourceCount: enabledSources.length,
  };
  await env.PULSE_KV.put(CACHE_KEY, JSON.stringify(payload));
  return payload;
}

async function fetchSource(source: DataSource): Promise<NewsItem[]> {
  switch (source.type) {
    case "huggingface_daily_papers":
      return fetchHuggingFaceDailyPapers(source);
    case "rss":
      return fetchRssSource(source);
    case "rss_fallback":
      return fetchRssFallbackSource(source);
    case "arxiv":
      return fetchArxivSource(source);
    default:
      return [];
  }
}

async function fetchHuggingFaceDailyPapers(source: DataSource): Promise<NewsItem[]> {
  try {
    const data = await fetchJson<unknown>(
      "https://huggingface.co/api/daily_papers",
    );
    if (!Array.isArray(data)) return [];

    return data.slice(0, source.limit ?? 20).map((entry) => {
      const record = asRecord(entry) ?? {};
      const paper = asRecord(record.paper) ?? record;
      const id = text(paper.id);
      const title = cleanText(text(paper.title) || text(record.title));
      const summary = summarize(
        text(paper.ai_summary) || text(record.ai_summary) || text(paper.summary),
      );
      const publishedAt = parseDate(
        text(record.publishedAt) ||
          text(paper.submittedOnDailyAt) ||
          text(paper.publishedAt),
      );

      return {
        title,
        url: id ? `https://huggingface.co/papers/${id}` : "",
        source: source.name,
        summary,
        publishedAt,
        score: number(paper.upvotes) + number(record.numComments),
      };
    });
  } catch {
    return [];
  }
}

async function fetchRssSource(source: DataSource): Promise<NewsItem[]> {
  if (!source.url) return [];
  try {
    const xml = await fetchText(source.url);
    return parseRss(xml, source.name).slice(0, source.limit ?? 12);
  } catch {
    return [];
  }
}

async function fetchRssFallbackSource(source: DataSource): Promise<NewsItem[]> {
  for (const url of source.urls ?? []) {
    try {
      const xml = await fetchText(url);
      const items = parseRss(xml, source.name).slice(0, source.limit ?? 10);
      if (items.length > 0) return items;
    } catch {
      // Try the next mirror.
    }
  }

  return [];
}

async function fetchArxivSource(source: DataSource): Promise<NewsItem[]> {
  const category = (source.category ?? "").trim();
  if (!category) return [];

  try {
    const query = encodeURIComponent(`cat:${category}`);
    const maxResults = Math.max(1, source.limit ?? 10);
    const xml = await fetchText(
      `https://export.arxiv.org/api/query?search_query=${query}&sortBy=submittedDate&sortOrder=descending&max_results=${maxResults}`,
    );
    return parseRss(xml, source.name)
      .filter((item) => isRelevantAiItem(item))
      .slice(0, maxResults);
  } catch {
    return [];
  }
}

async function loadConfig(env: Env): Promise<WorkerConfig> {
  const stored = await env.PULSE_KV.get(CONFIG_KEY, "json");
  const config = asRecord(stored);
  if (!config || !Array.isArray(config.sources)) {
    return { sources: DEFAULT_SOURCES };
  }
  return sanitizeConfig(config);
}

async function saveConfig(env: Env, config: WorkerConfig): Promise<void> {
  await env.PULSE_KV.put(CONFIG_KEY, JSON.stringify(sanitizeConfig(config)));
}

async function loadLatest(env: Env): Promise<CachePayload | null> {
  const stored = await env.PULSE_KV.get(CACHE_KEY, "json");
  const payload = asRecord(stored);
  if (!payload || !Array.isArray(payload.items)) return null;
  return {
    items: payload.items
      .map((item) => sanitizePublicItem(item))
      .filter((item): item is PublicNewsItem => item !== null),
    fetchedAt: text(payload.fetchedAt),
    sourceCount: number(payload.sourceCount),
  };
}

async function readConfigRequest(request: Request): Promise<WorkerConfig> {
  const body = asRecord(await request.json());
  if (!body) throw new Error("Invalid config payload");
  return sanitizeConfig(body);
}

function sanitizeConfig(value: Record<string, unknown>): WorkerConfig {
  const sources = Array.isArray(value.sources) ? value.sources : [];
  return {
    sources: sources
      .map((source) => sanitizeSource(source))
      .filter((source): source is DataSource => source !== null),
  };
}

function sanitizeSource(value: unknown): DataSource | null {
  const record = asRecord(value);
  if (!record) return null;

  const type = text(record.type) as SourceType;
  if (!["huggingface_daily_papers", "rss", "rss_fallback", "arxiv"].includes(type)) {
    return null;
  }

  const source: DataSource = {
    id: slug(text(record.id) || crypto.randomUUID()),
    name: cleanText(text(record.name)).slice(0, 80) || "Untitled Source",
    type,
    enabled: typeof record.enabled === "boolean" ? record.enabled : true,
  };
  const url = cleanUrl(text(record.url));
  const category = cleanText(text(record.category));
  const limit = Math.trunc(number(record.limit));
  const urls = Array.isArray(record.urls)
    ? record.urls.map((item) => cleanUrl(text(item))).filter(Boolean)
    : [];

  if (url) source.url = url;
  if (urls.length > 0) source.urls = urls.slice(0, 5);
  if (category) source.category = category.slice(0, 32);
  if (limit > 0) source.limit = Math.min(limit, 50);

  return source;
}

function sanitizePublicItem(value: unknown): PublicNewsItem | null {
  const record = asRecord(value);
  if (!record) return null;
  const title = cleanText(text(record.title));
  const url = cleanUrl(text(record.url));
  const source = cleanText(text(record.source));
  if (!title || !url || !source) return null;
  const summary = summarize(text(record.summary));
  const item: PublicNewsItem = { title, url, source };
  if (summary) item.summary = summary;
  return item;
}

async function fetchText(url: string): Promise<string> {
  const response = await fetchWithTimeout(url, {
    headers: {
      accept: "application/rss+xml, application/xml, text/xml, text/plain;q=0.9",
      "user-agent": "astrbot-plugin-pulse-worker/1.0",
    },
    cf: {
      cacheEverything: true,
      cacheTtl: 300,
    },
  });
  if (!response.ok) {
    throw new Error(`Fetch failed: ${url} ${response.status}`);
  }
  return response.text();
}

async function fetchJson<T>(url: string): Promise<T> {
  const response = await fetchWithTimeout(url, {
    headers: {
      accept: "application/json",
      "user-agent": "astrbot-plugin-pulse-worker/1.0",
    },
    cf: {
      cacheEverything: true,
      cacheTtl: 300,
    },
  });
  if (!response.ok) {
    throw new Error(`Fetch failed: ${url} ${response.status}`);
  }
  return (await response.json()) as T;
}

async function fetchWithTimeout(
  url: string,
  init: WorkerRequestInit,
): Promise<Response> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timeout);
  }
}

function parseRss(xml: string, source: string): NewsItem[] {
  const rssBlocks = xml.match(/<item\b[\s\S]*?<\/item>/gi);
  const blocks = rssBlocks ?? xml.match(/<entry\b[\s\S]*?<\/entry>/gi) ?? [];

  return blocks
    .map((block) => {
      const title = cleanText(tagText(block, "title"));
      const url = cleanUrl(tagText(block, "link") || atomHref(block));
      const summary = summarize(
        tagText(block, "description") ||
          tagText(block, "summary") ||
          tagText(block, "content:encoded") ||
          tagText(block, "content"),
      );
      const publishedAt = parseDate(
        tagText(block, "pubDate") ||
          tagText(block, "published") ||
          tagText(block, "updated") ||
          tagText(block, "dc:date"),
      );

      return {
        title,
        url,
        source,
        summary,
        publishedAt,
        score: relevanceScore(title, summary),
      };
    })
    .filter((item) => item.title && item.url);
}

function normalizeItems(items: NewsItem[]): NewsItem[] {
  const seen = new Set<string>();
  const deduped: NewsItem[] = [];

  for (const item of items) {
    const url = cleanUrl(item.url);
    if (!item.title || !url || seen.has(url)) continue;
    seen.add(url);
    deduped.push({ ...item, url });
  }

  const sorted = deduped.sort(compareNewsItems);
  const selected: NewsItem[] = [];
  const selectedUrls = new Set<string>();
  const bySource = new Map<string, NewsItem[]>();

  for (const item of sorted) {
    const list = bySource.get(item.source) ?? [];
    list.push(item);
    bySource.set(item.source, list);
  }

  for (const list of bySource.values()) {
    for (const item of list.slice(0, PER_SOURCE_BASE_LIMIT)) {
      if (selected.length >= MAX_ITEMS) return selected;
      selected.push(item);
      selectedUrls.add(item.url);
    }
  }

  for (const item of sorted) {
    if (selected.length >= MAX_ITEMS) break;
    if (selectedUrls.has(item.url)) continue;
    selected.push(item);
    selectedUrls.add(item.url);
  }

  return selected.sort(compareNewsItems);
}

function compareNewsItems(a: NewsItem, b: NewsItem): number {
  const timeDelta = (b.publishedAt ?? 0) - (a.publishedAt ?? 0);
  if (timeDelta !== 0) return timeDelta;
  return (b.score ?? 0) - (a.score ?? 0);
}

function isAuthorized(request: Request, token: string): boolean {
  const auth = request.headers.get("authorization") ?? "";
  if (auth === `Bearer ${token}`) return true;
  if (!auth.startsWith("Basic ")) return false;

  try {
    const decoded = atob(auth.slice("Basic ".length));
    const separatorIndex = decoded.indexOf(":");
    if (separatorIndex < 0) return decoded === token;
    const username = decoded.slice(0, separatorIndex);
    const password = decoded.slice(separatorIndex + 1);
    return password === token || username === token;
  } catch {
    return false;
  }
}

function basicAuthRequired(): Response {
  return new Response("Authentication required", {
    status: 401,
    headers: {
      "www-authenticate": 'Basic realm="Pulse Worker Admin", charset="UTF-8"',
      ...JSON_HEADERS,
    },
  });
}

function isRelevantAiItem(item: NewsItem): boolean {
  return relevanceScore(item.title, item.summary) > 0;
}

function relevanceScore(title: string, summary = ""): number {
  const haystack = `${title} ${summary}`.toLowerCase();
  let score = 0;
  for (const keyword of AI_KEYWORDS) {
    if (haystack.includes(keyword.toLowerCase())) score += 1;
  }
  return score;
}

function tagText(xml: string, tagName: string): string {
  const escapedTag = tagName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = xml.match(
    new RegExp(`<${escapedTag}\\b[^>]*>([\\s\\S]*?)<\\/${escapedTag}>`, "i"),
  );
  return match ? decodeXml(stripCdata(match[1])) : "";
}

function atomHref(xml: string): string {
  const match = xml.match(/<link\b[^>]*\bhref=["']([^"']+)["'][^>]*>/i);
  return match ? decodeXml(match[1]) : "";
}

function stripCdata(value: string): string {
  return value.replace(/^\s*<!\[CDATA\[/, "").replace(/\]\]>\s*$/, "");
}

function cleanText(value: string): string {
  return decodeXml(value)
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function cleanUrl(value: string): string {
  return decodeXml(value).trim();
}

function summarize(value: string): string | undefined {
  const cleaned = cleanText(value);
  if (!cleaned) return undefined;
  return cleaned.length > 280 ? `${cleaned.slice(0, 277)}...` : cleaned;
}

function decodeXml(value: string): string {
  return value
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&apos;/g, "'")
    .replace(/&#(\d+);/g, (_, code: string) =>
      String.fromCharCode(Number(code)),
    )
    .replace(/&#x([0-9a-f]+);/gi, (_, code: string) =>
      String.fromCharCode(Number.parseInt(code, 16)),
    );
}

function parseDate(value: string): number | undefined {
  if (!value) return undefined;
  const timestamp = Date.parse(cleanText(value));
  return Number.isFinite(timestamp) ? timestamp : undefined;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function number(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object"
    ? (value as Record<string, unknown>)
    : null;
}

function slug(value: string): string {
  return (
    value
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 64) || crypto.randomUUID()
  );
}

function json(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: JSON_HEADERS,
  });
}

function adminPage(): string {
  return String.raw`<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Pulse Worker</title>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: #f6f7f9;
      color: #1f2937;
      font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    main { max-width: 1100px; margin: 0 auto; padding: 28px 18px 42px; }
    header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 18px; }
    h1 { margin: 0; font-size: 24px; line-height: 1.2; }
    .toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
    input, select {
      width: 100%;
      height: 36px;
      border: 1px solid #d1d5db;
      border-radius: 6px;
      padding: 6px 9px;
      background: #fff;
      color: #111827;
      font: inherit;
    }
    input[type="checkbox"] { width: 18px; height: 18px; }
    input:disabled { background: #f3f4f6; color: #6b7280; }
    button {
      height: 36px;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      padding: 0 12px;
      background: #fff;
      color: #111827;
      font: inherit;
      cursor: pointer;
    }
    button.primary { border-color: #2563eb; background: #2563eb; color: #fff; }
    button.danger { border-color: #fecaca; color: #b91c1c; }
    button:disabled { cursor: not-allowed; opacity: .55; }
    .status { min-height: 22px; margin: 8px 0 14px; color: #4b5563; }
    table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; overflow: hidden; }
    th, td { padding: 10px; border-bottom: 1px solid #e5e7eb; text-align: left; vertical-align: middle; }
    th { background: #f9fafb; color: #374151; font-weight: 650; }
    tr:last-child td { border-bottom: 0; }
    .enabled { width: 48px; text-align: center; }
    .actions { width: 86px; }
    .limit { width: 88px; }
    .type { width: 180px; }
    .empty { padding: 28px; text-align: center; color: #6b7280; background: #fff; border: 1px dashed #cbd5e1; border-radius: 8px; }
    .help {
      margin-top: 22px;
      padding: 18px;
      background: #fff;
      border: 1px solid #e5e7eb;
      border-radius: 8px;
    }
    .help h2 { margin: 0 0 10px; font-size: 18px; }
    .help h3 { margin: 16px 0 8px; font-size: 15px; }
    .help ul, .help ol { margin: 8px 0 0; padding-left: 22px; }
    .help li { margin: 6px 0; }
    code {
      padding: 1px 5px;
      border-radius: 4px;
      background: #f3f4f6;
      color: #111827;
      font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
      font-size: 13px;
    }
    @media (max-width: 820px) {
      header { display: block; }
      .toolbar { margin-top: 8px; }
      table, thead, tbody, tr, th, td { display: block; width: 100%; }
      thead { display: none; }
      tr { border-bottom: 1px solid #e5e7eb; padding: 8px 0; }
      td { border: 0; padding: 6px 10px; }
      td::before { display: block; margin-bottom: 4px; color: #6b7280; font-size: 12px; content: attr(data-label); }
      .enabled, .actions, .limit, .type { width: 100%; text-align: left; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>Pulse Worker 数据源</h1>
      <div class="toolbar">
        <button id="load">重新加载</button>
        <button id="add">新增数据源</button>
        <button id="refresh">立即抓取</button>
        <button class="primary" id="save">保存配置</button>
      </div>
    </header>
    <div class="status" id="status"></div>
    <div id="content"></div>
    <section class="help">
      <h2>新增数据源教程</h2>
      <ol>
        <li>点击“新增数据源”，填写一个容易识别的名称。</li>
        <li>选择类型：普通 RSS 选 <code>RSS</code>；同一媒体有多个镜像地址时选 <code>RSS 备用源</code>；arXiv 分类选 <code>arXiv 分类</code>；Hugging Face Daily Papers 是内置源。</li>
        <li>按类型填写目标字段：RSS 填完整订阅地址；RSS 备用源用英文逗号分隔多个订阅地址；arXiv 填分类号，例如 <code>cs.CL</code>、<code>cs.LG</code>、<code>cs.AI</code>；内置源无需填写。</li>
        <li>设置数量上限后保存配置。需要马上验证时，点击“立即抓取”。</li>
      </ol>
      <h3>注意事项</h3>
      <ul>
        <li>管理页使用浏览器 Basic Auth 登录。用户名可任意填写，密码填写 Worker 的 <code>SECRET_TOKEN</code>。</li>
        <li>AstrBot 调用接口仍使用 Bearer Token，不受管理页登录方式影响。</li>
        <li>新增 RSS 源必须能被 Cloudflare Worker 直接访问，并返回 RSS、Atom 或常规 XML 内容。</li>
        <li>arXiv 类型只需要分类号，不要填写完整 URL。</li>
        <li>保存后不会自动抓取，下一次 Cron 会使用新配置；也可以点击“立即抓取”刷新缓存。</li>
      </ul>
    </section>
  </main>
  <script>
    const typeOptions = [
      ["rss", "RSS"],
      ["rss_fallback", "RSS 备用源"],
      ["arxiv", "arXiv 分类"],
      ["huggingface_daily_papers", "Hugging Face Daily Papers"],
    ];
    let sources = [];

    const $ = (id) => document.getElementById(id);
    $("load").onclick = loadConfig;
    $("save").onclick = saveConfig;
    $("add").onclick = () => {
      sources.push({
        id: crypto.randomUUID(),
        name: "New RSS Source",
        type: "rss",
        enabled: true,
        url: "",
        limit: 10,
      });
      render();
    };
    $("refresh").onclick = refreshNow;

    function jsonHeaders() {
      return { "content-type": "application/json" };
    }

    function setStatus(text, isError = false) {
      $("status").textContent = text;
      $("status").style.color = isError ? "#b91c1c" : "#4b5563";
    }

    async function loadConfig() {
      setStatus("正在加载配置...");
      try {
        const response = await fetch("/api/config", { credentials: "same-origin" });
        if (!response.ok) throw new Error(await response.text());
        const config = await response.json();
        sources = Array.isArray(config.sources) ? config.sources : [];
        render();
        setStatus("配置已加载。");
      } catch (error) {
        setStatus("加载失败：" + error.message, true);
      }
    }

    async function saveConfig() {
      collect();
      setStatus("正在保存配置...");
      try {
        const response = await fetch("/api/config", {
          method: "PUT",
          headers: jsonHeaders(),
          credentials: "same-origin",
          body: JSON.stringify({ sources }),
        });
        if (!response.ok) throw new Error(await response.text());
        const config = await response.json();
        sources = Array.isArray(config.sources) ? config.sources : [];
        render();
        setStatus("配置已保存，下一次定时抓取会使用新的数据源。");
      } catch (error) {
        setStatus("保存失败：" + error.message, true);
      }
    }

    async function refreshNow() {
      setStatus("正在触发抓取...");
      try {
        const response = await fetch("/api/refresh", {
          method: "POST",
          credentials: "same-origin",
        });
        if (!response.ok) throw new Error(await response.text());
        const payload = await response.json();
        setStatus("抓取完成：" + payload.items.length + " 条，时间 " + payload.fetchedAt);
      } catch (error) {
        setStatus("抓取失败：" + error.message, true);
      }
    }

    function render() {
      if (sources.length === 0) {
        $("content").innerHTML = '<div class="empty">暂无数据源</div>';
        return;
      }
      $("content").innerHTML = '<table><thead><tr><th class="enabled">启用</th><th>名称</th><th class="type">类型</th><th>数据源目标</th><th class="limit">数量</th><th class="actions">操作</th></tr></thead><tbody>' +
        sources.map((source, index) => rowHtml(source, index)).join("") +
        '</tbody></table>';
      bindRows();
    }

    function rowHtml(source, index) {
      const options = typeOptions.map(([value, label]) =>
        '<option value="' + value + '"' + (source.type === value ? " selected" : "") + '>' + label + '</option>'
      ).join("");
      const target = source.type === "arxiv"
        ? (source.category || "")
        : source.type === "rss_fallback"
          ? (source.urls || []).join(", ")
          : (source.url || "");
      const meta = targetMeta(source.type);
      return '<tr data-index="' + index + '">' +
        '<td class="enabled" data-label="启用"><input data-field="enabled" type="checkbox"' + (source.enabled ? " checked" : "") + ' /></td>' +
        '<td data-label="名称"><input data-field="name" value="' + escapeAttr(source.name || "") + '" /></td>' +
        '<td class="type" data-label="类型"><select data-field="type">' + options + '</select></td>' +
        '<td data-label="' + escapeAttr(meta.label) + '"><input data-field="target" value="' + escapeAttr(target) + '" placeholder="' + escapeAttr(meta.placeholder) + '"' + (meta.disabled ? " disabled" : "") + ' /></td>' +
        '<td class="limit" data-label="数量"><input data-field="limit" type="number" min="1" max="50" value="' + (source.limit || 10) + '" /></td>' +
        '<td class="actions" data-label="操作"><button class="danger" data-remove="' + index + '">删除</button></td>' +
        '</tr>';
    }

    function targetMeta(type) {
      if (type === "rss") {
        return { label: "RSS URL", placeholder: "https://example.com/feed.xml", disabled: false };
      }
      if (type === "rss_fallback") {
        return { label: "备用 RSS URL", placeholder: "多个 URL 用英文逗号分隔", disabled: false };
      }
      if (type === "arxiv") {
        return { label: "arXiv 分类", placeholder: "例如 cs.CL、cs.LG、cs.AI", disabled: false };
      }
      return { label: "内置源无需填写", placeholder: "内置源无需填写 URL", disabled: true };
    }

    function bindRows() {
      document.querySelectorAll("button[data-remove]").forEach((button) => {
        button.onclick = () => {
          collect();
          sources.splice(Number(button.dataset.remove), 1);
          render();
        };
      });
      document.querySelectorAll("select[data-field='type']").forEach((select) => {
        select.onchange = () => {
          collect();
          render();
        };
      });
    }

    function collect() {
      document.querySelectorAll("tbody tr").forEach((row) => {
        const index = Number(row.dataset.index);
        const source = sources[index] || {};
        source.enabled = row.querySelector("[data-field='enabled']").checked;
        source.name = row.querySelector("[data-field='name']").value.trim();
        source.type = row.querySelector("[data-field='type']").value;
        source.limit = Number(row.querySelector("[data-field='limit']").value) || 10;
        const targetInput = row.querySelector("[data-field='target']");
        const target = targetInput ? targetInput.value.trim() : "";
        delete source.url;
        delete source.urls;
        delete source.category;
        if (source.type === "arxiv") {
          source.category = target;
        } else if (source.type === "rss_fallback") {
          source.urls = target.split(/[\n,]+/).map((item) => item.trim()).filter(Boolean);
        } else if (source.type === "rss") {
          source.url = target;
        }
        sources[index] = source;
      });
    }

    function escapeAttr(value) {
      return String(value).replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[char]));
    }

    render();
    loadConfig();
  </script>
</body>
</html>`;
}