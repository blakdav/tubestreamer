// Send to TubeStreamer: queues URLs on the server's /api/queue endpoint.
//
// - Toolbar button / Alt+Shift+Y: sends the current tab's URL.
// - Right-click a link: "Send link to TubeStreamer" (e.g. thumbnails in a
//   feed, without opening them).
// - Right-click a page or video: sends the page URL.
//
// The result shows as a badge on the toolbar icon (✓ queued, ! failed); hover
// the icon to see the error. The server address is set on the options page,
// which also grants host permission for just that one origin.

const MENU_LINK = 'send-link';
const MENU_PAGE = 'send-page';

chrome.runtime.onInstalled.addListener(async ({ reason }) => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: MENU_LINK, title: 'Send link to TubeStreamer', contexts: ['link'] });
    chrome.contextMenus.create({ id: MENU_PAGE, title: 'Send this page to TubeStreamer', contexts: ['page', 'video'] });
  });
  if (reason === 'install') chrome.runtime.openOptionsPage();
});

chrome.action.onClicked.addListener(tab => send(tab.url));

chrome.contextMenus.onClicked.addListener(info => {
  send(info.menuItemId === MENU_LINK ? info.linkUrl : info.pageUrl);
});

async function send(url) {
  if (!url || !/^https?:\/\//.test(url)) {
    return flash('!', '#ff4747', 'Not a web page URL');
  }

  const { server } = await chrome.storage.local.get('server');
  if (!server) {
    chrome.runtime.openOptionsPage();
    return flash('!', '#ff4747', 'Set your TubeStreamer address first');
  }
  const granted = await chrome.permissions.contains({ origins: [server + '/*'] });
  if (!granted) {
    chrome.runtime.openOptionsPage();
    return flash('!', '#ff4747', 'Permission for the server was not granted. Save it again in options');
  }

  setBadge('…', '#666666', 'Sending...');
  try {
    const r = await fetch(server + '/api/queue', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || `Server returned ${r.status}`);
    flash('✓', '#2fbf4a', `Queued: ${url}`);
  } catch (e) {
    flash('!', '#ff4747', `Failed to queue: ${e.message}`);
  }
}

function setBadge(text, color, title) {
  chrome.action.setBadgeBackgroundColor({ color });
  chrome.action.setBadgeText({ text });
  chrome.action.setTitle({ title: `Send to TubeStreamer\n${title}` });
}

// Errors stay visible longer so you have time to hover for the reason
let clearTimer;
function flash(text, color, title) {
  setBadge(text, color, title);
  clearTimeout(clearTimer);
  clearTimer = setTimeout(() => {
    chrome.action.setBadgeText({ text: '' });
    chrome.action.setTitle({ title: 'Send to TubeStreamer' });
  }, text === '✓' ? 3000 : 10000);
}
