const token = document.querySelector("#token");
const delay = document.querySelector("#delay");
const autoScroll = document.querySelector("#autoScroll");
const startButton = document.querySelector("#start");
const stopButton = document.querySelector("#stop");
const status = document.querySelector("#status");
const error = document.querySelector("#error");

function withTimeout(promise, milliseconds, label) {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error(`${label}超时`)), milliseconds)),
  ]);
}

async function activeXTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !/^https:\/\/(x|www\.x|twitter|www\.twitter)\.com\//.test(tab.url || "")) {
    throw new Error("请先打开一个已登录的 x.com 页面");
  }
  return tab;
}

async function updateStatus() {
  const value = await chrome.storage.local.get(["collectorState", "savedCount", "lastError", "summaryPath"]);
  status.textContent = `${value.collectorState || "等待启动"} · 已保存 ${value.savedCount || 0} 条`;
  error.textContent = value.lastError || "";
  if (value.summaryPath) status.title = value.summaryPath;
}

async function saveSettings() {
  await chrome.storage.local.set({
    token: token.value.trim(),
    scrollDelay: Number(delay.value),
    autoScroll: autoScroll.checked,
  });
}

async function sendToContent(tabId, message) {
  try {
    return await withTimeout(chrome.tabs.sendMessage(tabId, message), 8000, "启动采集");
  } catch (reason) {
    const messageText = String(reason?.message || reason);
    if (!messageText.includes("Receiving end does not exist") &&
        !messageText.includes("Could not establish connection")) {
      throw reason;
    }
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
    return await withTimeout(chrome.tabs.sendMessage(tabId, message), 8000, "注入采集脚本");
  }
}

startButton.addEventListener("click", async () => {
  error.textContent = "";
  startButton.disabled = true;
  status.textContent = "正在检查 Python 服务……";
  try {
    if (!token.value.trim()) throw new Error("请填写 Python 服务显示的连接令牌");
    await saveSettings();
    const health = await withTimeout(
      chrome.runtime.sendMessage({ type: "collector:health" }),
      6000,
      "连接 Python 服务",
    );
    if (!health?.ok) throw new Error(health?.error || "无法连接 Python 服务");
    const tab = await activeXTab();
    status.textContent = "正在启动页面采集……";
    const response = await sendToContent(tab.id, {
      type: "collector:start",
      config: {
        scrollDelay: Number(delay.value),
        autoScroll: autoScroll.checked,
      },
    });
    if (!response?.ok) throw new Error(response?.error || "无法启动页面采集");
    await chrome.storage.local.set({ collectorState: "采集中", lastError: "" });
    status.textContent = "采集已启动 · 可关闭此弹窗";
  } catch (reason) {
    const message = reason?.message || String(reason);
    await chrome.storage.local.set({ collectorState: "启动失败", lastError: message });
    error.textContent = message;
  } finally {
    startButton.disabled = false;
    await updateStatus();
  }
});

stopButton.addEventListener("click", async () => {
  error.textContent = "";
  stopButton.disabled = true;
  try {
    const tab = await activeXTab();
    const response = await chrome.tabs.sendMessage(tab.id, { type: "collector:stop" });
    if (!response?.ok) throw new Error(response?.error || "无法停止采集");
  } catch (reason) {
    const message = reason?.message || String(reason);
    await chrome.storage.local.set({ collectorState: "停止失败", lastError: message });
    error.textContent = message;
  } finally {
    stopButton.disabled = false;
    updateStatus();
  }
});

chrome.storage.onChanged.addListener(updateStatus);
chrome.storage.local.get(["token", "scrollDelay", "autoScroll"]).then((value) => {
  token.value = value.token || "";
  delay.value = value.scrollDelay || 2200;
  autoScroll.checked = value.autoScroll !== false;
  updateStatus();
});
