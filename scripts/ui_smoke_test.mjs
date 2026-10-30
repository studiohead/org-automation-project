// PLAIN ENGLISH: A headless check of the sign-in screen logic (no browser, no Microsoft). It loads the real index.html and
// app.js in a simulated page, fakes the server and Microsoft's sign-in library, and checks what the page does in each mode.
// Run:  npm install jsdom && node scripts/ui_smoke_test.mjs      (not part of CI: it needs node; pytest covers the API)
import { readFileSync } from 'node:fs';
import { JSDOM } from 'jsdom';

const UI = new URL('../src/org_automation/ui/', import.meta.url);
const appJs = readFileSync(new URL('app.js', UI), 'utf8');
let failures = 0;
const check = (name, ok) => { console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}`); if (!ok) failures += 1; };
const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));

async function page({ config, me = { name: 'alice@contoso.com', role: 'reviewer', auth: 'entra_user' }, meStatus = 200,
  account = null, stored = null }) {
  const dom = new JSDOM('<!doctype html><body><div id="app"></div></body>', { url: 'https://app.example/ui/', runScripts: 'outside-only' });
  const w = dom.window;
  const calls = [];
  if (stored) w.sessionStorage.setItem('session', JSON.stringify(stored));
  w.fetch = async (path, init = {}) => {
    calls.push({ path, headers: init.headers || {} });
    if (path === '/ui/config') return { ok: true, status: 200, json: async () => config };
    if (path === '/me') return { ok: meStatus === 200, status: meStatus, json: async () => (meStatus === 200 ? me : { detail: 'nope' }) };
    return { ok: true, status: 200, json: async () => ({ items: [] }) };
  };
  const msalCalls = [];
  w.msal = {
    InteractionRequiredAuthError: class extends Error {},
    PublicClientApplication: class {
      constructor(cfg) { msalCalls.push(['config', cfg]); this.acct = account; }
      async initialize() {}
      async handleRedirectPromise() { return account ? { account } : null; }
      getActiveAccount() { return this.acct; }
      getAllAccounts() { return this.acct ? [this.acct] : []; }
      setActiveAccount(a) { this.acct = a; }
      async acquireTokenSilent(req) { msalCalls.push(['silent', req.scopes]); return { accessToken: 'TOKEN-123' }; }
      async loginRedirect(req) { msalCalls.push(['login', req]); }
      async logoutRedirect(req) { msalCalls.push(['logout', req]); }
    },
  };
  w.document.head.appendChild = (el) => { if (el.tagName === 'SCRIPT') setTimeout(() => el.onload && el.onload(), 0); };
  w.eval(appJs);
  await tick(80);
  return { w, calls, msalCalls, text: () => w.document.body.textContent };
}

const entra = { mode: 'entra', entra: { tenant_id: 't', client_id: 'spa', authority: 'https://login.microsoftonline.com/t', api_scope: 'api://api/access_as_user' } };
const base = { app_name: 'Org', environment: 'production', modes: {}, demo_logins: [{ name: 'flow', role: 'service', key: 'demo-x' }] };

// 1. entra-only, signed out: Microsoft button, no key box, no demo logins
let p = await page({ config: { ...base, auth: entra } });
check('entra mode shows the Microsoft button', p.text().includes('Sign in with Microsoft'));
check('entra mode hides the API key box and demo logins', !p.w.document.getElementById('key') && !p.text().includes('Demo sign-in'));
const btn = [...p.w.document.querySelectorAll('button')].find((b) => b.textContent === 'Sign in with Microsoft');
btn.click(); await tick();
check('button starts a redirect sign-in for the API scope', p.msalCalls.some((c) => c[0] === 'login' && c[1].scopes[0] === 'api://api/access_as_user'));
check('MSAL uses sessionStorage and this origin', p.msalCalls[0][1].cache.cacheLocation === 'sessionStorage' && p.msalCalls[0][1].auth.redirectUri === 'https://app.example/ui/');

// 2. coming back from Microsoft with an account: calls /me with a Bearer token, shows the role's screen
p = await page({ config: { ...base, auth: entra }, account: { username: 'alice@contoso.com' } });
const me = p.calls.find((c) => c.path === '/me');
check('after redirect the page calls /me with a Bearer token and no API key', me && me.headers.Authorization === 'Bearer TOKEN-123' && !me.headers['X-API-Key']);
check('signed-in reviewer sees the review queue', p.text().includes('Review queue') && p.text().includes('alice@contoso.com'));
check('session stored without any token or key', !/TOKEN-123|key/.test(p.w.sessionStorage.getItem('session')));
const out = [...p.w.document.querySelectorAll('button')].find((b) => b.textContent === 'Sign out');
out.click(); await tick();
check('sign-out also ends the Microsoft session', p.msalCalls.some((c) => c[0] === 'logout'));

// 3. signed in to Microsoft but no role here: clear message, not a broken page
p = await page({ config: { ...base, auth: entra }, account: { username: 'bob@contoso.com' }, meStatus: 401 });
check('no-role account gets a plain explanation and the sign-in screen', p.text().includes('no role in this application') && p.text().includes('Sign in with Microsoft'));

// 4. hybrid keeps keys and demo logins next to Microsoft
p = await page({ config: { ...base, environment: 'development', auth: { ...entra, mode: 'hybrid' } } });
check('hybrid shows Microsoft, demo logins and the key box', p.text().includes('Sign in with Microsoft') && p.text().includes('Demo sign-in') && !!p.w.document.getElementById('key'));

// 5. plain api_key mode: unchanged, and MSAL is never touched
p = await page({ config: { ...base, environment: 'development', auth: { mode: 'api_key', entra: null } } });
check('api_key mode has no Microsoft button and never loads MSAL', !p.text().includes('Sign in with Microsoft') && p.msalCalls.length === 0 && !!p.w.document.getElementById('key'));

// 6. stale entra session with no Microsoft account is dropped, not trusted
p = await page({ config: { ...base, auth: entra }, stored: { auth: 'entra', name: 'ghost', role: 'reviewer' } });
check('a leftover entra session without an MSAL account is discarded', !p.text().includes('ghost') && p.text().includes('Sign in with Microsoft'));

console.log(failures ? `\n${failures} check(s) failed` : '\nAll UI checks passed');
process.exit(failures ? 1 : 0);
