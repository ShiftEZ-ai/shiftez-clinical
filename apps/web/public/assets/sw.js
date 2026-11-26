// Installable app: only the app's own files are kept for offline start. Rota, people and account data (/api, /auth,
// /calendar) are never stored here; they always come from the server.
// Build-time inputs from scripts/preload_web.py: the complete bundle, never a second hand-maintained list.
const SHELL = 'rota-shell-__BUNDLE_VERSION__';
const FILES = __SHELL_FILES__;
// Each file is kept as it arrives, and an install fetches only what this version's cache lacks. An install that did not
// finish (one failed file, or the browser discarding it) is retried from scratch by the next page load, so a single
// addAll would download the whole bundle again; the install still fails until every file is stored.
self.addEventListener('install', event => {
  event.waitUntil(caches.open(SHELL).then(async cache => {
    const kept = new Set((await cache.keys()).map(r => new URL(r.url).pathname));
    const failed = (await Promise.allSettled(FILES.filter(f => !kept.has(f)).map(f => cache.add(f)))).find(r => r.status === 'rejected');
    if (failed) throw failed.reason;  // only after every other file is stored
  }).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k.startsWith('rota-shell-') && k !== SHELL).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== location.origin) return;
  if (!FILES.includes(url.pathname) || url.search) return;  // data and stale asset requests go straight to the network
  const cached = () => caches.open(SHELL).then(cache => cache.match(event.request));
  // Answer with the network response at once; the cache copy is written in the background, so a slow cache write
  // never holds a page (or the next navigation) behind it.
  const network = () => fetch(event.request).then(response => {
    if (response.ok) event.waitUntil(caches.open(SHELL).then(cache => cache.put(event.request, response.clone())).catch(() => {}));
    return response;
  });
  // Versioned files are immutable. HTML revalidates; offline misses never masquerade as HTML modules.
  if (url.pathname !== '/') {
    event.respondWith(cached().then(hit => hit || network()));
    return;
  }
  event.respondWith(network().catch(() => cached().then(hit => hit || Response.error())));
});

// Push (build/contracts/PWA-BE.md › payload v1: {v,notification_id,delivery_id,title,body,url,tag,unread}): every push shows a visible alert at once, even with the app open (iOS revokes
// permission after silent pushes), and never waits on a signed-in read. The payload is Rota's envelope or Declarative
// Web Push ({web_push: 8030, notification: {...}}); anything unreadable still shows the app's name and a generic line.
// The app's icon and name come from the branding-generated manifest (public, application-wide; never cached here).
const ICON = '/api/pwa/apple-touch-icon.png';
/** Only this app's own pages open from an alert; anything else falls back to the inbox. */
const inApp = target => {
  try { const url = new URL(target, location.origin); if (target && url.origin === location.origin && !url.pathname.startsWith('/api/')) return url.href; } catch { /* not a URL */ }
  return location.origin + '/#inbox';
};
const appName = () => fetch('/api/pwa/manifest.webmanifest').then(r => r.ok ? r.json() : {}).then(m => m.short_name || m.name).catch(() => '')
  .then(name => name || new URL(location.origin).hostname);
async function badge(count) {
  if (typeof count !== 'number' || !self.navigator || !('setAppBadge' in self.navigator)) return;
  await (count > 0 ? self.navigator.setAppBadge(count) : self.navigator.clearAppBadge()).catch(() => {});
}
self.addEventListener('push', event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = {}; }
  const shown = data && typeof data.notification === 'object' && data.notification ? data.notification : (data || {});
  event.waitUntil((async () => {
    const title = typeof shown.title === 'string' && shown.title ? shown.title : await appName();
    // shortcut: the generic line for an unreadable payload is fixed here (a worker has no signed-in read); the server's
    // configured template is used whenever the payload can be read.
    const body = typeof shown.body === 'string' ? shown.body : 'You have a rota update';
    const id = typeof data.notification_id === 'string' ? data.notification_id : typeof data.id === 'string' ? data.id : '';
    const tag = typeof data.tag === 'string' && data.tag ? data.tag : id ? 'rota-' + id : undefined;
    const actions = Array.isArray(data.actions) ? data.actions.filter(a => a && typeof a.action === 'string' && typeof a.title === 'string').slice(0, 2) : [];
    await Promise.all([
      self.registration.showNotification(title, {body, icon: ICON, tag,
        data: {url: inApp(shown.navigate || data.url), actions: Object.fromEntries(actions.map(a => [a.action, inApp(a.url)]))},
        actions: actions.map(({action, title}) => ({action, title}))}),
      badge(typeof shown.app_badge === 'number' ? shown.app_badge : data.unread && typeof data.unread === 'object' ? data.unread.count : data.unread)]);
  })());
});
// An alert opens its item. An open window is handed the address (its own unsaved-changes guard decides); otherwise a
// new window opens it. Nothing is acknowledged, accepted or approved by the tap.
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const data = event.notification.data || {};
  const url = (event.action && data.actions && data.actions[event.action]) || data.url || inApp('');
  event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(tabs => {
    const tab = tabs.find(t => new URL(t.url).origin === location.origin);
    if (tab) { tab.postMessage({type: 'fmuc-open', url}); return tab.focus(); }
    return self.clients.openWindow(url);
  }));
});
// A browser-replaced subscription is registered again by the next signed-in visit (the worker holds no session).
self.addEventListener('pushsubscriptionchange', event => {
  event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(tabs => tabs.forEach(t => t.postMessage({type: 'fmuc-resubscribe'}))));
});
