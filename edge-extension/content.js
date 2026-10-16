let running = false;
let scanning = false;
let autoScrollTimer = null;
let scrollDelay = 2200;
let captured = 0;
const seen = new Set();
const pending = new Set();
const seenContent = new Set();
const pendingContent = new Set();
let indicator = null;

function showIndicator(message, kind = "active") {
  if (!indicator || !document.documentElement.contains(indicator)) {
    indicator = document.createElement("div");
    indicator.id = "x-collector-indicator";
    Object.assign(indicator.style, {
      position: "fixed",
      right: "18px",
      bottom: "18px",
      zIndex: "2147483647",
      maxWidth: "320px",
      padding: "10px 14px",
      borderRadius: "9px",
      color: "white",
      font: "600 13px/1.4 Segoe UI, sans-serif",
      boxShadow: "0 4px 18px rgba(0,0,0,.28)",
      pointerEvents: "none",
    });
    document.documentElement.appendChild(indicator);
  }
  indicator.style.background = kind === "error" ? "#b42318" : kind === "done" ? "#16833a" : "#1261a0";
  indicator.textContent = message;
}

function metric(article, testIds) {
  for (const testId of testIds) {
    const element = article.querySelector(`[data-testid="${testId}"]`);
    if (element) return element.getAttribute("aria-label") || null;
  }
  return null;
}

function mediaMarkers(article) {
  const markers = [];
  if (article.querySelector('video, [data-testid="videoPlayer"], [data-testid="videoComponent"]')) {
    markers.push("[视频]");
  }
  if (article.querySelector('[data-testid="tweetPhoto"], [data-testid="tweetPhoto"] img')) {
    markers.push("[图片]");
  }
  if (article.querySelector(
    'a[href*="/i/article/"], a[href*="/article/"], ' +
    '[data-testid="articlePreview"], [data-testid="article-cover"], ' +
    '[data-testid="twitterArticleRichTextView"]'
  )) {
    markers.push("[文章]");
  }
  return markers;
}

async function expandTruncatedText(article) {
  const controls = [...article.querySelectorAll('[data-testid="tweet-text-show-more-link"]')];
  let clicked = false;
  for (const control of controls) {
    if (!(control instanceof HTMLElement) || control.offsetParent === null) continue;
    control.click();
    clicked = true;
  }
  if (clicked) {
    await new Promise((resolve) => setTimeout(resolve, 350));
  }
}

function contentKey(post) {
  const author = (post.handle || post.author || "").trim().toLocaleLowerCase();
  const text = post.text.replace(/\s+/g, " ").trim().toLocaleLowerCase();
  return `${author}\n${text}`;
}

function extractPost(article) {
  const textElement = article.querySelector('[data-testid="tweetText"]');
  const statusLinks = [...article.querySelectorAll('a[href*="/status/"]')];
  if (statusLinks.length === 0) return null;
  const statusLink = statusLinks.find((link) => /\/status\/\d+/.test(link.getAttribute("href") || ""));
  if (!statusLink) return null;
  const href = statusLink.getAttribute("href") || "";
  const idMatch = href.match(/\/status\/(\d+)/);
  if (!idMatch) return null;

  const userText = article.querySelector('[data-testid="User-Name"]')?.innerText?.trim() || "";
  const handle = userText.match(/@[A-Za-z0-9_]{1,15}/)?.[0] || "";
  const author = (handle ? userText
    .split("\n")
    .map((part) => part.trim())
    .find((part) => part && !part.includes(handle)) : userText.split("\n")[0]?.trim()) || handle;
  const time = article.querySelector("time")?.getAttribute("datetime") || null;
  const cleanHref = href.split("?")[0];
  const text = textElement?.innerText?.trim() || "";
  const markers = mediaMarkers(article);
  if (!text && markers.length === 0) return null;
  return {
    id: idMatch[1],
    url: cleanHref.startsWith("/") ? `https://x.com${cleanHref}` : cleanHref,
    author,
    handle,
    text: [text, ...markers].filter(Boolean).join("\n"),
    created_at: time,
    reply_count: metric(article, ["reply"]),
    repost_count: metric(article, ["retweet", "unretweet"]),
    like_count: metric(article, ["like", "unlike"]),
  };
}

async function send(message) {
  return await chrome.runtime.sendMessage(message);
}

async function scan() {
  if (!running || scanning) return;
  scanning = true;
  try {
    const batch = [];
    const batchKeys = new Map();
    for (const article of document.querySelectorAll('article[data-testid="tweet"], article[role="article"]')) {
      await expandTruncatedText(article);
      const post = extractPost(article);
      if (!post) continue;
      const key = contentKey(post);
      if (seen.has(post.id) || pending.has(post.id) || seenContent.has(key) || pendingContent.has(key)) continue;
      pending.add(post.id);
      pendingContent.add(key);
      batch.push(post);
      batchKeys.set(post.id, key);
    }
    if (batch.length === 0) return;
    const response = await send({ type: "collector:posts", posts: batch });
    if (!response?.ok) throw new Error(response?.error || "Python 服务没有响应");
    for (const post of batch) {
      pending.delete(post.id);
      pendingContent.delete(batchKeys.get(post.id));
      seen.add(post.id);
      seenContent.add(batchKeys.get(post.id));
    }
    captured = Number(response.total) || captured;
    showIndicator(`X 采集运行中：已保存 ${captured} 条（手动停止）`);
    await chrome.storage.local.set({ capturedCount: captured, collectorState: "采集中", lastError: "" });
  } catch (error) {
    pending.clear();
    pendingContent.clear();
    showIndicator(`X 采集错误：${error.message}`, "error");
    await chrome.storage.local.set({ lastError: error.message, collectorState: "连接失败" });
  } finally {
    scanning = false;
  }
}

function scheduleAutoScroll() {
  clearInterval(autoScrollTimer);
  autoScrollTimer = setInterval(() => {
    if (!running) return;
    window.scrollBy(0, Math.max(650, window.innerHeight * 0.82));
    scan();
  }, scrollDelay);
}

async function start(config) {
  running = true;
  scrollDelay = Math.max(1000, Math.min(10000, Number(config.scrollDelay) || 2200));
  captured = 0;
  seen.clear();
  pending.clear();
  seenContent.clear();
  pendingContent.clear();
  const registration = await send({ type: "collector:register" });
  if (!registration?.ok) throw new Error(registration?.error || "无法注册后台采集任务");
  showIndicator("X 采集已启动：不限数量，点击“停止并总结”结束");
  await chrome.storage.local.set({
    capturedCount: 0,
    savedCount: 0,
    collectorState: "采集中",
    lastError: "",
    summaryPath: "",
  });
  if (config.autoScroll) scheduleAutoScroll();
  else clearInterval(autoScrollTimer);
  await scan();
}

async function stopAndFinish(reason = "manual") {
  if (!running && reason !== "manual") return;
  running = false;
  clearInterval(autoScrollTimer);
  await chrome.storage.local.set({ collectorState: "正在生成摘要" });
  try {
    const response = await send({ type: "collector:finish", reason });
    if (!response?.ok) {
      showIndicator(`生成摘要失败：${response?.error || "未知错误"}`, "error");
      await chrome.storage.local.set({ collectorState: "总结失败", lastError: response?.error || "未知错误" });
    } else {
      showIndicator(`采集完成：已保存 ${response.total || captured} 条；可再次开始`, "done");
    }
  } finally {
    await send({ type: "collector:unregister" });
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "collector:start") {
    start(message.config).then(() => sendResponse({ ok: true })).catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }
  if (message.type === "collector:stop") {
    stopAndFinish("manual").then(() => sendResponse({ ok: true })).catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }
  if (message.type === "collector:backgroundTick") {
    if (!running) {
      sendResponse({ ok: false, error: "采集未运行" });
      return;
    }
    window.scrollBy(0, Math.max(650, window.innerHeight * 0.82));
    scan().then(() => sendResponse({ ok: true })).catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }
  if (message.type === "collector:status") {
    sendResponse({ ok: true, running, captured });
  }
});

const observer = new MutationObserver(() => {
  if (running) scan();
});
observer.observe(document.documentElement, { childList: true, subtree: true });
