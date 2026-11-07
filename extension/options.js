const input = document.getElementById('server');
const result = document.getElementById('result');

chrome.storage.local.get('server').then(({ server }) => {
  if (server) input.value = server;
});

function show(cls, msg) {
  result.className = cls;
  result.textContent = msg;
}

function normalize(raw) {
  let s = raw.trim();
  if (!s) return '';
  if (!/^https?:\/\//i.test(s)) s = 'https://' + s;
  return new URL(s).origin + new URL(s).pathname.replace(/\/+$/, '');
}

async function save() {
  let server;
  try {
    server = normalize(input.value);
  } catch {
    return show('err', 'That doesn\'t look like a valid address.');
  }
  if (!server) return show('err', 'Enter an address.');
  input.value = server;

  // Must be called directly from the click handler (user gesture)
  // The server, plus YouTube so the icon can show whether the video in a tab
  // is already in TubeStreamer (it reads YouTube tab addresses, nothing else).
  const granted = await chrome.permissions.request({
    origins: [new URL(server).origin + '/*', 'https://www.youtube.com/*', 'https://m.youtube.com/*']
  });
  if (!granted) return show('err', 'Access was not allowed, so the extension can\'t reach TubeStreamer.');

  await chrome.storage.local.set({ server });
  show('', 'Saved. Testing connection...');
  try {
    const r = await fetch(server + '/api/queue');
    if (!r.ok) throw new Error(`Server returned ${r.status}`);
    const jobs = await r.json();
    if (!Array.isArray(jobs)) throw new Error('Unexpected response. Is this the TubeStreamer address?');
    const active = jobs.filter(j => j.status === 'queued' || j.status === 'downloading').length;
    show('ok', `✓ Connected. ${active} item(s) currently in the queue.`);
  } catch (e) {
    show('err', `Saved, but the test failed: ${e.message}`);
  }
}

document.getElementById('save').addEventListener('click', save);
input.addEventListener('keydown', e => { if (e.key === 'Enter') save(); });
