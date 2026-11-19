// aitrack brauseriaeg: iga minut, kui brauser on fookuses ja kasutaja pole jõude,
// salvestatakse aktiivse vahelehe aadress (ilma päringu ja fragmendita) lokaalsele aitrackile
// native messaging'u kaudu. Midagi ei saadeta internetti; projektiga mitteseotud aadressid
// jätab aitrack ise arvestusest välja.
const api = globalThis.browser ?? globalThis.chrome;
const HOST = "com.aitrack.browser";
const ALARM = "aitrack-minute";
const IDLE_SECONDS = 120;
const MAX_QUEUE = 24 * 60;

function browserName() {
  const ua = navigator.userAgent || "";
  if (navigator.brave) return "Brave";
  if (ua.includes("Firefox/")) return "Firefox";
  if (ua.includes("Edg/")) return "Edge";
  if (ua.includes("OPR/")) return "Opera";
  if (ua.includes("Vivaldi")) return "Vivaldi";
  if (ua.includes("Chrome/")) return "Chrome";
  return "Brauser";
}

function stripUrl(raw) {
  try {
    const u = new URL(raw);
    if (u.protocol !== "http:" && u.protocol !== "https:") return "";
    return `${u.protocol}//${u.host}${u.pathname}`;
  } catch (_) {
    return "";
  }
}

async function activeUrl() {
  const state = await api.idle.queryState(IDLE_SECONDS);
  if (state !== "active") return "";
  const win = await api.windows.getLastFocused({ populate: true });
  if (!win || !win.focused || win.incognito) return "";
  const tab = (win.tabs || []).find((t) => t.active);
  return tab ? stripUrl(tab.url || "") : "";
}

async function loadQueue() {
  const got = await api.storage.local.get("queue");
  return Array.isArray(got.queue) ? got.queue : [];
}

async function sample() {
  const url = await activeUrl();
  const queue = await loadQueue();
  if (url) {
    const minute = new Date();
    minute.setSeconds(0, 0);
    queue.push({ ts: minute.toISOString(), url, browser: browserName() });
  }
  if (!queue.length) return;
  try {
    const reply = await api.runtime.sendNativeMessage(HOST, { type: "samples", samples: queue });
    if (reply && reply.ok) {
      await api.storage.local.set({ queue: [] });
      return;
    }
  } catch (_) {
    // aitrack pole paigaldatud või ei vasta → hoia järjekorras ja proovi järgmisel minutil
  }
  await api.storage.local.set({ queue: queue.slice(-MAX_QUEUE) });
}

async function ensureAlarm() {
  const existing = await api.alarms.get(ALARM);
  if (!existing) api.alarms.create(ALARM, { periodInMinutes: 1 });
}

api.runtime.onInstalled.addListener(ensureAlarm);
api.runtime.onStartup.addListener(ensureAlarm);
api.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) sample();
});
ensureAlarm();
