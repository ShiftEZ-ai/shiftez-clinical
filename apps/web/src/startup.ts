// Run before the deferred app module executes, even when an import fails to load. The build versions the bundle's
// path (/assets/<version>/app.js, packages/web_assets.py), so the page's one module script is found by its type.
document.querySelector('script[type="module"]')?.addEventListener('error', () => {
  const boot = document.querySelector('#main.boot');
  if (!boot) return;
  // The same words as the app's own start-up failure (app.ts bootFailure, N-12).
  const heading = boot.querySelector('h1');
  heading?.replaceChildren('Rota can’t be reached');
  // The bundle failed, not necessarily the server: the deployment's configured short name replaces the default when
  // the public branding still answers (the same field brand.ts paints into [data-brand-short]).
  void fetch('/api/branding', {credentials: 'same-origin'}).then(r => r.ok ? r.json() : null).then((b: {short_name?: unknown} | null) => {
    const short = typeof b?.short_name === 'string' ? b.short_name.trim() : '';
    if (short && short.length <= 40 && heading?.isConnected) heading.textContent = `${short} can’t be reached`;
  }, () => {});
  const message = document.createElement('p');
  message.setAttribute('role', 'alert');
  message.textContent = 'This is not an empty rota. No schedule could be retrieved. Use the approved departmental fallback and contact support.';
  const reload = document.createElement('button');
  reload.type = 'button';
  reload.className = 'button primary';
  reload.textContent = 'Reload';
  reload.addEventListener('click', () => location.reload());
  boot.querySelector('p, .loading')?.replaceWith(message);
  boot.append(reload);
  reload.focus();
}, {once: true});

// A module that never answers (a server restarting, F-L1) fires no error: the static start-up wait follows the same
// timing and words as every skeleton (loading.ts RETRY_AFTER, READ_TIMEOUT, WAIT_WORDS, clock; kept equal by
// tests/test_web_loading.py: this classic script cannot import): the moving line alone, then after 15 s one quiet line
// under it, "Still loading · 0:15", ticking; past READ_TIMEOUT "Didn’t load in 1:00." with Retry (a reload). Once the app
// runs it puts its own wait in place and this stops.
const RETRY_AFTER = 15000, READ_TIMEOUT = 60_000;
const SLOW_WORDS = 'Still loading', TIMED_OUT_WORDS = 'Didn’t load in';
// shortcut: loading.ts clock (m:ss) copied, since nothing can be imported here; the test runs both on the same values.
const startupClock = (ms: number) => { const s = Math.max(0, Math.floor(ms / 1000)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
const startupWait = () => document.querySelector('#main.boot > .sk-boot:not([data-born])');
setTimeout(() => {
  const wait = startupWait();
  if (!wait) return;
  const line = document.createElement('p'), time = document.createElement('span');
  line.className = 'sk-slow';
  time.className = 'elapsed-time';
  time.setAttribute('aria-hidden', 'true');
  line.append(`${SLOW_WORDS} · `, time);
  wait.append(line);
  const live = document.createElement('p');
  live.className = 'sr-only';
  live.setAttribute('role', 'status');
  wait.append(live);
  live.textContent = SLOW_WORDS;  // said once; the clock is not re-announced
  const tick = () => {
    if (!wait.isConnected) { clearInterval(timer); return; }
    const ms = performance.now();
    if (ms < READ_TIMEOUT) { time.textContent = startupClock(ms); return; }
    clearInterval(timer);
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'link-button';
    retry.textContent = 'Retry';
    retry.addEventListener('click', () => location.reload());
    line.replaceChildren(`${TIMED_OUT_WORDS} ${startupClock(READ_TIMEOUT)}. `, retry);
    live.textContent = line.textContent || '';
    wait.removeAttribute('aria-busy');
  };
  const timer = setInterval(tick, 1000);
  tick();
}, RETRY_AFTER);

// The stored theme applies before the first paint (LIVE-15): this classic script runs as <head> is parsed. It reads the
// same key and values as theme.ts (rota.prefs), which applies it again once the app loads; anything unreadable leaves the
// CSS system theme.
try {
  const stored = JSON.parse(localStorage.getItem('rota.prefs') || '{}').theme;
  if (stored === 'light' || stored === 'dark') document.documentElement.setAttribute('data-theme', stored);
} catch { /* nothing stored, or storage blocked: system theme */ }
