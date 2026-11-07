// Send to TubeStreamer: queues URLs on the server's /api/queue endpoint.
//
// - Toolbar button / Alt+Shift+Y: sends the current tab's URL.
// - Right-click a link: "Send link to TubeStreamer" (e.g. thumbnails in a
//   feed, without opening them). Right-click a page or video: sends the page.
//
// Feedback:
// - The toolbar icon shows the state of the video in *this* tab, looked up on
//   the server whenever you switch tabs or videos: ✓ already downloaded,
//   ↓ queued or downloading, ! the last attempt failed, nothing otherwise.
//   Needs permission for youtube.com (asked on the options page); without it
//   the badge only appears right after sending.
// - Desktop notifications when a link is sent, and when that download
//   finishes or fails. While anything sent is unfinished, an alarm checks the
//   queue every 30 seconds (the background worker sleeps between events, so
//   plain timers aren't reliable).

const MENU_LINK = 'send-link';
const MENU_PAGE = 'send-page';
const YOUTUBE = ['https://www.youtube.com/*', 'https://m.youtube.com/*'];

const BADGES = {
  downloaded: { text: '✓', color: '#2fbf4a', title: 'Already in TubeStreamer' },
  queued: { text: '↓', color: '#3b82f6', title: 'Queued in TubeStreamer' },
  downloading: { text: '↓', color: '#3b82f6', title: 'Downloading in TubeStreamer' },
  error: { text: '!', color: '#ff4747', title: 'TubeStreamer download failed' },
};

chrome.runtime.onInstalled.addListener(async ({ reason }) => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: MENU_LINK, title: 'Send link to TubeStreamer', contexts: ['link'] });
    chrome.contextMenus.create({ id: MENU_PAGE, title: 'Send this page to TubeStreamer', contexts: ['page', 'video'] });
  });
  // Older versions set one badge for every tab; clear it.
  chrome.action.setBadgeText({ text: '' });
  chrome.action.setTitle({ title: 'Send to TubeStreamer' });
  const hasYouTube = await chrome.permissions.contains({ origins: YOUTUBE });
  if (reason === 'install' || !hasYouTube) chrome.runtime.openOptionsPage();
});

chrome.action.onClicked.addListener(tab => send(tab.url, tab.id, tab.title));

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === MENU_LINK) send(info.linkUrl, null, null);
  else send(info.pageUrl, tab && tab.id, tab && tab.title);
});

// ---- Per-tab badge ----------------------------------------------------------

function isVideo(url) {
  return /^https:\/\/(www\.|m\.)?youtube\.com\/(watch\?|shorts\/|live\/)|^https:\/\/youtu\.be\//.test(url || '');
}

function cleanTitle(title) {
  return (title || '').replace(/^\(\d+\)\s*/, '').replace(/ - YouTube$/, '');
}

function setTabBadge(tabId, badge, detail) {
  if (tabId == null) return;
  if (!badge) {
    chrome.action.setBadgeText({ tabId, text: '' });
    chrome.action.setTitle({ tabId, title: 'Send to TubeStreamer' });
    return;
  }
  chrome.action.setBadgeText({ tabId, text: badge.text });
  chrome.action.setBadgeBackgroundColor({ tabId, color: badge.color });
  chrome.action.setTitle({ tabId, title: badge.title + (detail ? `\n${detail}` : '') });
}

async function server() {
  const { server } = await chrome.storage.local.get('server');
  return server || null;
}

async function refreshTab(tabId, url) {
  if (!isVideo(url)) return setTabBadge(tabId, null);
  const base = await server();
  if (!base) return;
  try {
    const r = await fetch(`${base}/api/status?url=${encodeURIComponent(url)}`);
    if (!r.ok) return setTabBadge(tabId, null);
    const d = await r.json();
    setTabBadge(tabId, BADGES[d.state], d.state === 'error' ? d.msg : d.title);
  } catch {
    setTabBadge(tabId, null);  // server unreachable (off the VPN): show nothing
  }
}

chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  // YouTube changes videos without reloading the page, so watch URL changes
  if (info.url || info.status === 'complete') refreshTab(tabId, tab.url);
});

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  try {
    const tab = await chrome.tabs.get(tabId);
    refreshTab(tabId, tab.url);
  } catch { /* tab already closed */ }
});

async function refreshActiveTabs() {
  for (const tab of await chrome.tabs.query({ active: true })) refreshTab(tab.id, tab.url);
}

// ---- Sending ------------------------------------------------------------------

function notify(title, message) {
  chrome.notifications.create({ type: 'basic', iconUrl: 'icons/icon128.png', title, message: message || '' });
}

async function send(url, tabId, tabTitle) {
  const label = cleanTitle(tabTitle) || url;
  if (!url || !/^https?:\/\//.test(url)) {
    return notify("Can't send this page", 'Not a web page URL');
  }
  const base = await server();
  if (!base) {
    chrome.runtime.openOptionsPage();
    return notify('Set up TubeStreamer first', 'Enter your TubeStreamer address in the extension options.');
  }
  if (!(await chrome.permissions.contains({ origins: [base + '/*'] }))) {
    chrome.runtime.openOptionsPage();
    return notify('TubeStreamer access not allowed', 'Click Save & test in the extension options and allow access.');
  }

  setTabBadge(tabId, { text: '…', color: '#666666', title: 'Sending...' });
  try {
    const r = await fetch(base + '/api/queue', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || `Server returned ${r.status}`);

    // Remember what we sent, to report when it finishes
    const { pending = {} } = await chrome.storage.session.get('pending');
    for (const job of body) pending[job.id] = { label, url };
    await chrome.storage.session.set({ pending });
    chrome.alarms.create('poll', { periodInMinutes: 0.5 });

    notify('Sent to TubeStreamer', label);
    setTabBadge(tabId, BADGES.queued, label);
  } catch (e) {
    notify("Couldn't send to TubeStreamer", `${e.message}\n${label}`);
    setTabBadge(tabId, BADGES.error, e.message);
  }
}

// ---- Completion notifications -----------------------------------------------

chrome.alarms.onAlarm.addListener(a => { if (a.name === 'poll') poll(); });

async function poll() {
  const { pending = {} } = await chrome.storage.session.get('pending');
  const base = await server();
  if (!Object.keys(pending).length || !base) return chrome.alarms.clear('poll');
  let jobs;
  try {
    jobs = await (await fetch(base + '/api/queue')).json();
  } catch {
    return;  // unreachable for now; try again on the next alarm
  }
  for (const [id, sent] of Object.entries(pending)) {
    const job = jobs.find(j => j.id === id);
    const name = (job && job.title) || sent.label;
    if (!job || job.status === 'cancelled') delete pending[id];
    else if (job.status === 'done') { notify('Downloaded', name); delete pending[id]; }
    else if (job.status === 'error') { notify('Download failed', `${name}\n${job.msg}`); delete pending[id]; }
  }
  await chrome.storage.session.set({ pending });
  if (!Object.keys(pending).length) chrome.alarms.clear('poll');
  refreshActiveTabs();
}
