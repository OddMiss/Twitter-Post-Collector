const DEFAULT_SERVER = "http://127.0.0.1:8765";
const BACKGROUND_TICK_ALARM = "x-collector-background-tick";

async function registerCollector(sender) {
  const tabId = sender.tab?.id;
  if (!tabId) throw new Error("无法识别采集标签页");
  const tab = await chrome.tabs.get(tabId);
  await chrome.storage.local.set({
    collectorTabId: tabId,
    originalAutoDiscardable: tab.autoDiscardable !== false,
  });
  try {
    await chrome.tabs.update(tabId, { autoDiscardable: false });
  } catch (_) {
    // 某些 Edge 策略可能不允许修改；后台闹钟仍可继续使用。
  }
  await chrome.alarms.clear(BACKGROUND_TICK_ALARM);
  chrome.alarms.create(BACKGROUND_TICK_ALARM, { periodInMinutes: 0.5 });
}

async function unregisterCollector() {
  await chrome.alarms.clear(BACKGROUND_TICK_ALARM);
  const value = await chrome.storage.local.get(["collectorTabId", "originalAutoDiscardable"]);
  if (value.collectorTabId && value.originalAutoDiscardable) {
    try {
      await chrome.tabs.update(value.collectorTabId, { autoDiscardable: true });
    } catch (_) {
      // 标签页可能已关闭。
    }
  }
  await chrome.storage.local.remove(["collectorTabId", "originalAutoDiscardable"]);
}

async function settings() {
  const value = await chrome.storage.local.get(["serverUrl", "token"]);
  return {
    serverUrl: (value.serverUrl || DEFAULT_SERVER).replace(/\/$/, ""),
    token: value.token || "",
  };
}

async function callServer(path, method = "POST", body = undefined) {
  const { serverUrl, token } = await settings();
  const options = {
    method,
    headers: { "X-X-Collector-Token": token },
  };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(`${serverUrl}${path}`, options);
  const value = await response.json();
  if (!response.ok || !value.ok) {
    throw new Error(value.error || `HTTP ${response.status}`);
  }
  return value;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "collector:register") {
    registerCollector(sender)
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message.type === "collector:unregister") {
    unregisterCollector()
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message.type === "collector:posts") {
    callServer("/posts", "POST", { posts: message.posts })
      .then(async (result) => {
        await chrome.storage.local.set({
          savedCount: result.total,
          lastError: "",
          collectorState: "采集中",
        });
        await chrome.action.setBadgeText({ text: String(result.total) });
        sendResponse({ ok: true, ...result });
      })
      .catch(async (error) => {
        await chrome.storage.local.set({ lastError: error.message, collectorState: "连接失败" });
        sendResponse({ ok: false, error: error.message });
      });
    return true;
  }

  if (message.type === "collector:finish") {
    callServer("/finish", "POST", { reason: message.reason || "manual" })
      .then(async (result) => {
        await chrome.storage.local.set({
          collectorState: "摘要已完成，可继续采集",
          lastError: "",
          summaryPath: result.summary_path,
          savedCount: 0,
        });
        await chrome.action.setBadgeBackgroundColor({ color: "#16833a" });
        sendResponse({ ok: true, ...result });
      })
      .catch(async (error) => {
        await chrome.storage.local.set({ lastError: error.message, collectorState: "总结失败" });
        sendResponse({ ok: false, error: error.message });
      });
    return true;
  }

  if (message.type === "collector:health") {
    callServer("/health", "GET")
      .then((result) => sendResponse({ ok: true, ...result }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== BACKGROUND_TICK_ALARM) return;
  const { collectorTabId } = await chrome.storage.local.get("collectorTabId");
  if (!collectorTabId) return;
  try {
    const response = await chrome.tabs.sendMessage(collectorTabId, { type: "collector:backgroundTick" });
    if (!response?.ok) throw new Error(response?.error || "采集页面未运行");
  } catch (error) {
    await chrome.storage.local.set({
      lastError: "采集页面已关闭或重新加载，请重新点击开始采集",
      collectorState: "采集已停止",
    });
    await unregisterCollector();
  }
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const { collectorTabId } = await chrome.storage.local.get("collectorTabId");
  if (tabId === collectorTabId) await unregisterCollector();
});
