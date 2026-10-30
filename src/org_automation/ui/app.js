/* PLAIN ENGLISH: The demo screen's brain. It signs a person in, then shows only the screens their role allows:
     automation account ("flow")  -> submit a document, run the agent, search policies
     reviewer ("alice", "bob")    -> review queue, search policies, audit log
   It talks to the same web API as every other caller, using the person's key. Nothing here can do anything the
   API would refuse. SAFETY: text that came from documents or the AI is only ever shown as plain text (never as
   HTML), so a hostile document can't run code in a reviewer's browser. The key lives in this tab's session
   storage and disappears when the tab closes. */
'use strict';

/* ---------------------------------------------------------------- small helpers */
const root = document.getElementById('app');

/** Build an element. Strings and numbers become TEXT, never HTML. */
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === false || kid == null) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const uid = () => (window.crypto && crypto.randomUUID ? crypto.randomUUID()
  : `id-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`);

const fmtTime = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'medium' });
};

const FIELD_LABELS = { client_id: 'Client ID', effective_date: 'Effective date', email: 'Email' };
const ROLE_LABELS = { service: 'Automation account', reviewer: 'Reviewer' };
const STATE_LABELS = {
  not_started: ['Not started', ''],
  awaiting_review: ['Waiting for a reviewer', 'ok'],
  awaiting_exception_review: ['Flagged for investigation', 'warn'],
  approved_not_yet_applied: ['Approved, not yet applied', 'warn'],
  completed_applied: ['Applied to the CRM', 'ok'],
  approved_shadow_not_written: ['Approved, shadow mode (nothing written)', ''],
  rejected: ['Rejected', ''],
  failed_needs_attention: ['Failed, needs attention', 'bad'],
  stale_needs_resubmission: ['Record changed after review; submit again', 'warn'],
};

function pill(text, tone = '') { return h('span', { class: `pill ${tone}`.trim() }, text); }

function show(box, kind, text) { box.replaceChildren(h('div', { class: `notice ${kind}` }, text)); }
function clear(box) { box.replaceChildren(); }

function pageHead(title, text) {
  return h('header', { class: 'page-head' }, h('h1', {}, title), text && h('p', {}, text));
}

function valueOrNone(v) { return v ? v : h('span', { class: 'muted' }, '(not found)'); }

/* ---------------------------------------------------------------- session + API */
let session = null;
try { session = JSON.parse(sessionStorage.getItem('session')); } catch { session = null; }
let config = { app_name: 'Org Automation demo', modes: {}, demo_logins: [] };
let tab = null;
let signInNotice = null;

function setSession(s) {
  session = s;
  if (s) sessionStorage.setItem('session', JSON.stringify(s)); else sessionStorage.removeItem('session');
}

/* ---------------------------------------------------------------- Microsoft Entra ID sign-in
   Only active when the server says so (config.auth.entra). The sign-in itself happens on Microsoft's own page
   (authorization-code flow with PKCE, via Microsoft's MSAL library); this page never sees a password. Tokens live in
   sessionStorage (cleared when the tab closes) and are attached to API calls as a Bearer header. */
let msalApp = null;
const entraCfg = () => (config.auth && config.auth.entra) || null;
const authMode = () => (config.auth && config.auth.mode) || 'api_key';

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const el = document.createElement('script');
    el.src = src; el.onload = resolve;
    el.onerror = () => reject(new Error('Could not load the Microsoft sign-in library.'));
    document.head.appendChild(el);
  });
}

async function initEntra() {
  const e = entraCfg();
  if (!e) return;
  try {
    await loadScript('msal-browser.min.js?v=4.30.0');
    msalApp = new window.msal.PublicClientApplication({
      auth: { clientId: e.client_id, authority: e.authority, redirectUri: `${window.location.origin}/ui/`,
        postLogoutRedirectUri: `${window.location.origin}/ui/`, navigateToLoginRequestUrl: false },
      cache: { cacheLocation: 'sessionStorage' },
    });
    await msalApp.initialize();
    const result = await msalApp.handleRedirectPromise();
    const account = (result && result.account) || msalApp.getActiveAccount() || msalApp.getAllAccounts()[0];
    if (!account) { if (session && session.auth === 'entra') setSession(null); return; }
    msalApp.setActiveAccount(account);
    if (!session || session.auth !== 'entra') {
      const me = await api('/me', { entra: true });
      setSession({ auth: 'entra', name: me.name, role: me.role });
    }
  } catch (err) {
    setSession(null);
    signInNotice = err.status === 401 || err.status === 403
      ? 'You signed in to Microsoft, but this account has no role in this application. Ask an administrator to assign you the Reviewer role, or choose another account.'
      : err.message;
  }
}

async function accessToken() {
  const e = entraCfg();
  const account = msalApp && msalApp.getActiveAccount();
  if (!e || !account) throw Object.assign(new Error('Please sign in with Microsoft.'), { status: 401 });
  try {
    return (await msalApp.acquireTokenSilent({ scopes: [e.api_scope], account })).accessToken;
  } catch (err) {
    if (err instanceof window.msal.InteractionRequiredAuthError) {
      await msalApp.acquireTokenRedirect({ scopes: [e.api_scope], account });   // leaves the page; it comes back signed in
      throw Object.assign(new Error('Redirecting to Microsoft sign-in...'), { status: 0 });
    }
    throw Object.assign(new Error('Could not get a sign-in token from Microsoft. Try signing in again.'), { status: 401 });
  }
}

function explain(status, data) {
  const d = data && data.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) return d.map((e) => `${(e.loc || []).slice(1).join('.')}: ${e.msg}`).join('; ');
  return `The request failed (HTTP ${status}).`;
}

async function api(path, { method = 'GET', json, form, key, entra = false } = {}) {
  const useEntra = entra || (session && session.auth === 'entra');
  const headers = useEntra ? { Authorization: `Bearer ${await accessToken()}` }
    : { 'X-API-Key': key ?? (session ? session.key : '') };
  let body;
  if (json !== undefined) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(json); }
  else if (form) body = form;
  let res;
  try { res = await fetch(path, { method, headers, body }); }
  catch { throw Object.assign(new Error('Could not reach the server. Is it still running?'), { status: 0 }); }
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) {
    if (res.status === 401 && session && key === undefined && !entra) {
      signInNotice = session.auth === 'entra' ? 'Your Microsoft session has expired. Please sign in again.'
        : 'Your previous session key is no longer valid. Choose a demo sign-in below or enter a valid API key.';
      signOut({ local: true });
    }
    throw Object.assign(new Error(explain(res.status, data)), { status: res.status });
  }
  return data;
}

function signOut({ local = false } = {}) {
  const wasEntra = session && session.auth === 'entra';
  setSession(null); tab = null;
  if (wasEntra && msalApp && !local) {   // also end the Microsoft session, then come back to this page
    msalApp.logoutRedirect({ account: msalApp.getActiveAccount(), postLogoutRedirectUri: `${window.location.origin}/ui/` });
    return;
  }
  render();
}

async function signIn(key, box) {
  try {
    const me = await api('/me', { key });
    setSession({ key, name: me.name, role: me.role, auth: 'api_key' });
    tab = null;
    signInNotice = null;
    render();
  } catch (e) {
    show(box, 'problem', e.status === 401 ? 'That key was not recognized. Check it and try again.' : e.message);
  }
}

/* ---------------------------------------------------------------- shell */
const tabsFor = (role) => {
  const policy = { id: 'policy', label: 'Document search', view: policyView };
  // Model settings are available to both demo roles in development. Keeping this in the
  // shared navigation avoids hiding provider configuration when signed in as `flow`.
  const common = [{ id: 'architecture', label: 'System flow', view: architectureView }, policy,
    ...(config.environment === 'development' ? [{ id: 'settings', label: 'LLM settings', view: settingsView }] : [])];
  return role === 'service'
    ? [{ id: 'submit', label: 'Submit a document', view: submitView },
       { id: 'agent', label: 'Run the agent', view: agentView },
       ...(config.environment === 'development' ? [{ id: 'webhook', label: 'Webhook demo', view: webhookView }] : []), ...common]
    : [{ id: 'review', label: 'Review queue', view: reviewView },
       { id: 'scorecard', label: 'Scorecard', view: scorecardView }, ...common,
       { id: 'audit', label: 'Audit log', view: auditView }];
};

function refreshModeBadges() {
  const m = config.modes || {};
  const labels = [
    `Reader: ${m.reader === 'llm' ? 'LLM' : (m.reader || 'unknown')}`,
    `Agent: ${m.agent === 'llm' ? 'LLM-driven' : m.agent === 'scripted' ? 'Scripted' : (m.agent || 'unknown')}`,
    `LLM: ${config.llm?.provider || 'unknown'} / ${config.llm?.model || 'unknown'}`,
    `CRM: ${m.crm || 'unknown'}`, `Writes: ${m.writes || 'unknown'}`, 'Synthetic data only'
  ];
  const nodes = document.querySelectorAll('.rail-foot .modes span');
  labels.forEach((label, i) => { if (nodes[i]) nodes[i].textContent = label; });
}

function render() {
  if (!session) { root.replaceChildren(signInView()); return; }
  const tabs = tabsFor(session.role);
  if (!tabs.some((t) => t.id === tab)) tab = tabs[0].id;
  const m = config.modes || {};
  const rail = h('aside', { class: 'rail' },
    h('div', { class: 'brand' }, config.app_name || 'Org Automation demo'),
    h('div', { class: 'who' }, h('strong', {}, session.name), h('span', { class: 'muted' }, ROLE_LABELS[session.role] || session.role)),
    h('nav', { class: 'nav', 'aria-label': 'Screens' },
      tabs.map((t) => h('button', {
        type: 'button', 'aria-current': t.id === tab ? 'page' : false,
        onclick: () => { tab = t.id; render(); },
      }, t.label))),
    h('div', { class: 'rail-foot' },
      h('div', { class: 'modes' },
        h('span', {}, `Reader: ${m.reader === 'llm' ? 'LLM' : (m.reader || 'unknown')}`),
        h('span', {}, `Agent: ${m.agent === 'llm' ? 'LLM-driven' : m.agent === 'scripted' ? 'Scripted' : (m.agent || 'unknown')}`),
        h('span', {}, `LLM: ${config.llm?.provider || 'unknown'} / ${config.llm?.model || 'unknown'}`),
        h('span', {}, `CRM: ${m.crm || 'unknown'}`), h('span', {}, `Writes: ${m.writes || 'unknown'}`), h('span', {}, 'Synthetic data only')),
      h('button', { class: 'btn', type: 'button', onclick: signOut }, 'Sign out')));
  const shadow = m.writes === 'shadow' && h('div', { class: 'banner', role: 'note' },
    h('strong', {}, 'Shadow mode.'), ' Approvals are recorded and checked, but nothing is written to the CRM. The audit log shows what would have been written.');
  const main = h('main', { class: 'main' }, shadow, tabs.find((t) => t.id === tab).view());
  root.replaceChildren(h('div', { class: 'shell' }, rail, main));
  window.scrollTo(0, 0);
}

/* ---------------------------------------------------------------- sign in */
function signInView() {
  const box = h('div', { role: 'status', 'aria-live': 'polite' });
  if (signInNotice) show(box, 'info', signInNotice);
  const keyInput = h('input', { type: 'password', id: 'key', autocomplete: 'off', spellcheck: 'false' });
  const go = () => { if (keyInput.value.trim()) signIn(keyInput.value.trim(), box); };
  keyInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });

  const microsoft = entraCfg() && h('section', { class: 'stack' },
    h('h2', {}, 'Sign in with Microsoft'),
    msalApp
      ? h('p', {}, 'Use your work account. What you can do here comes from the role your organization assigned you; this page never sees your password.')
      : h('p', { class: 'muted' }, 'Microsoft sign-in is turned on but could not start. Reload the page, or ask an administrator.'),
    msalApp && h('div', { class: 'row' }, h('button', {
      class: 'btn primary', type: 'button',
      onclick: () => msalApp.loginRedirect({ scopes: [entraCfg().api_scope], prompt: 'select_account' }),
    }, 'Sign in with Microsoft')));
  const keysAllowed = authMode() !== 'entra';

  const personaText = { service: 'Submits documents and runs the agent', reviewer: 'Approves or rejects changes' };
  const personas = keysAllowed && (config.demo_logins || []).length && h('section', { class: 'stack' },
    h('h2', {}, 'Demo sign-in'),
    h('div', { class: 'persona-list' }, config.demo_logins.map((p) => h('button', {
      class: 'btn persona', type: 'button', onclick: () => signIn(p.key, box),
    }, p.name, h('span', {}, personaText[p.role] || p.role)))));

  return h('main', { class: 'signin' },
    h('header', { class: 'page-head' },
      h('h1', {}, config.app_name || 'Org Automation demo'),
      h('p', {}, 'A signed form goes in, a person approves the change, and every step is written to a tamper-evident log. Everything here is synthetic.')),
    h('section', { class: 'stack' },
      h('h2', {}, 'Try it in three steps'),
      h('ol', { class: 'steps' },
        h('li', {}, 'Sign in as flow and submit a document, or let the agent process a signed envelope.'),
        h('li', {}, 'Sign out, then sign in as alice or bob and approve or reject what flow sent.'),
        h('li', {}, 'Open the audit log and verify that nothing was altered.'))),
    box,
    microsoft,
    personas,
    keysAllowed && h('section', { class: 'stack' },
      h('h2', {}, 'Sign in with your own key'),
      h('label', {}, 'API key', keyInput),
      h('div', { class: 'row' }, h('button', { class: 'btn primary', type: 'button', onclick: go }, 'Sign in'))));
}

/* ---------------------------------------------------------------- submit a document (automation account) */
const SAMPLES = [
  { label: 'Clean update', text: 'Client ID: C10482\nEffective date: 2026-10-12\nEmail: new.client@example.com', hint: 'Should wait for a reviewer, showing the old and new email.' },
  { label: 'Unknown client', text: 'Client ID: C99999\nEffective date: 2026-10-12\nEmail: someone@example.com', hint: 'There is no such client, so it should be flagged for investigation.' },
  { label: 'Missing email', text: 'Client ID: C10482\nEffective date: 2026-10-12', hint: 'A required field is missing, so it should be flagged.' },
  { label: 'Old and new email', text: 'Client ID: C10482\nEffective date: 2026-10-12\nOld email: old.client@example.com\nNew email: new.client@example.com', hint: 'A known weak spot of the simple demo reader: it picks the first address, the old one. Real Claude mode is meant to do better.' },
];

function submitView() {
  const docId = h('input', { type: 'text', id: 'doc-id', value: `doc-${uid().slice(0, 6)}`, maxlength: '100', autocomplete: 'off', spellcheck: 'false' });
  const text = h('textarea', { id: 'doc-text', rows: '7', spellcheck: 'false' });
  text.value = SAMPLES[0].text;
  const hint = h('p', { class: 'muted' }, SAMPLES[0].hint);
  const file = h('input', { type: 'file', id: 'doc-file', accept: 'application/pdf,.pdf' });
  const clearFile = h('button', { class: 'btn link', type: 'button', hidden: true, onclick: () => { file.value = ''; clearFile.hidden = true; } }, 'Remove PDF');
  file.addEventListener('change', () => { clearFile.hidden = !file.files.length; });
  const out = h('div', { class: 'stack', 'aria-live': 'polite' });
  const go = h('button', { class: 'btn primary', type: 'button', onclick: () => send(uid(), go) }, 'Process document');

  async function send(eventId, button) {
    const id = docId.value.trim();
    if (!/^[A-Za-z0-9._-]{1,100}$/.test(id)) return show(out, 'problem', 'The document ID can use letters, numbers, dots, dashes and underscores.');
    if (!file.files.length && !text.value.trim()) return show(out, 'problem', 'Paste the document text or choose a PDF.');
    button.disabled = true;
    const originalLabel = 'Process document';
    button.textContent = 'Processing…';
    button.setAttribute('aria-busy', 'true');
    show(out, 'info', 'Processing document with the configured model. This may take a minute for PDFs; please keep this page open.');
    try {
      let result;
      if (file.files.length) {
        const fd = new FormData();
        fd.append('file', file.files[0]); fd.append('document_type', 'client_update'); fd.append('event_id', eventId);
        result = await api(`/documents/${id}/process-pdf`, { method: 'POST', form: fd });
      } else {
        result = await api(`/documents/${id}/process`, { method: 'POST', json: { document_type: 'client_update', event_id: eventId, text: text.value } });
      }
      out.replaceChildren(resultView(result, () => send(eventId, button)));
    } catch (e) {
      show(out, 'problem', e.message);
    } finally {
      button.disabled = false;
      button.textContent = originalLabel;
      button.removeAttribute('aria-busy');
    }
  }

  return h('div', { class: 'page' },
    pageHead('Submit a document', 'This is what a Power Automate flow would do: send in the text of a signed form. Nothing changes in the CRM until a person approves it.'),
    h('section', { class: 'panel' },
      h('div', { class: 'row' }, h('span', { class: 'muted' }, 'Fill in a sample:'),
        SAMPLES.map((s) => h('button', { class: 'btn', type: 'button', onclick: () => { text.value = s.text; hint.textContent = s.hint; file.value = ''; clearFile.hidden = true; } }, s.label))),
      hint,
      h('label', {}, 'Document text', text),
      h('label', {}, h('span', {}, 'Or upload a PDF ', h('span', { class: 'hint' }, '(it replaces the text above)')), h('div', { class: 'row' }, file, clearFile)),
      h('label', {}, 'Document ID', docId),
      h('div', { class: 'row' }, go)),
    out);
}

function resultView(r, resend) {
  const [label, tone] = {
    needs_review: ['Waiting for a reviewer', 'ok'],
    needs_exception_review: ['Flagged for investigation', 'warn'],
    duplicate_ignored: ['Duplicate request ignored', ''],
  }[r.status] || [r.status, ''];
  const next = {
    needs_review: 'Next: sign out, then sign in as alice or bob to review it.',
    needs_exception_review: 'Next: a reviewer will see it in the queue. Exceptions can be rejected but never approved as they are.',
    duplicate_ignored: 'The same request ID was already handled, so nothing new happened. This is the protection against double processing.',
  }[r.status];
  const fields = r.extracted && h('div', { class: 'scroll' }, h('table', { class: 'plain' },
    h('thead', {}, h('tr', {}, ['Field', 'Value read', 'Page', 'Confidence', 'Quoted from the document'].map((t) => h('th', {}, t)))),
    h('tbody', {}, ['client_id', 'effective_date', 'email'].map((k) => {
      const f = r.extracted[k];
      return h('tr', {}, h('td', {}, FIELD_LABELS[k]), h('td', { class: 'mono' }, valueOrNone(f.value)),
        h('td', { class: 'num' }, f.source_page ?? '-'), h('td', { class: 'num' }, f.confidence == null ? '-' : f.confidence.toFixed(2)),
        h('td', { class: 'mono' }, f.source_excerpt || '-'));
    }))));
  return h('section', { class: 'panel' },
    h('div', { class: 'sheet-head' }, h('h2', {}, 'Result'), pill(label, tone)),
    next && h('p', {}, next),
    fields,
    r.issues && r.issues.length > 0 && h('div', { class: 'stack' }, h('h3', {}, 'Problems found'),
      h('ul', { class: 'issues' }, r.issues.map((i) => h('li', {}, h('strong', {}, FIELD_LABELS[i.field] || i.field), `: ${i.message} `, h('span', { class: 'mono' }, `(${i.code})`))))),
    h('div', { class: 'sheet-meta' }, h('span', {}, 'Document ', h('span', { class: 'mono' }, r.document_id)),
      r.approval_id && h('span', {}, 'Review item ', h('span', { class: 'mono' }, r.approval_id.slice(0, 8)))),
    h('div', {}, h('button', { class: 'btn link', type: 'button', onclick: resend }, 'Send the exact same request again')));
}

/* ---------------------------------------------------------------- run the agent (automation account) */
const ENVELOPES = [
  { id: 'env-1001', title: 'Signed form for a known client', note: 'The clean path. It should end as a change waiting for a reviewer.' },
  { id: 'env-1002', title: 'Signed form for a client that does not exist', note: 'It should be flagged as an exception, not silently dropped.' },
  { id: 'env-1003', title: 'Not signed yet', note: 'The agent must refuse to read the document at all.' },
  { id: 'env-1004', title: 'Signed form with a hidden instruction aimed at the AI', note: 'The text tells the agent to ignore its rules.' },
];

const OUTCOME_LABELS = {
  ok: 'Allowed', denied: 'Refused', unknown_tool: 'Refused',
  invalid_args: 'Refused: bad arguments', error: 'Failed',
};

/** Arguments as short readable text, e.g. document_id=env-1001. Plain text only. */
function argsText(args) {
  const entries = Object.entries(args || {});
  return entries.length ? entries.map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`).join('\n') : '(none)';
}

function runView(r, envelopeId) {
  const isLlm = r.driver === 'llm' || r.driver === 'claude';
  const isSim = r.driver === 'simulated';
  const [stateLabel, stateTone] = STATE_LABELS[(r.workflow || {}).state] || [r.status, ''];
  const completed = r.run_status === 'completed';
  const runOutcome = isSim
    ? ['Simulation replay; no CRM update performed', 'warn']
    : r.run_status === 'incomplete_no_tool_call'
      ? ['Incomplete: model stopped without a tool call', 'warn']
      : r.run_status === 'incomplete_turn_limit'
        ? ['Incomplete: tool-call limit reached', 'warn']
        : isLlm
          ? (completed ? ['LLM workflow completed', 'ok'] : ['LLM run incomplete', 'warn'])
          : (completed ? ['Scripted workflow completed', ''] : ['Scripted run incomplete', 'warn']);
  const asked = isLlm ? 'LLM asked for' : isSim ? 'Simulated agent asked for' : 'Script asked for';
  const label = isLlm ? `Driver: LLM (${config.llm?.provider || 'configured provider'} · ${r.model || config.llm?.model || 'model'})` : isSim ? 'Driver: simulated hijacked agent (not a real model)' : 'Driver: scripted';
  const explain = isLlm
    ? 'The configured LLM chose each step. The gateway decided whether each one was allowed, and logged all of them.'
    : isSim
      ? 'This is a replay written in advance, with no AI involved. It pretends the hidden instruction fooled the agent, so you can see the gateway refuse each forbidden request. Tools the agent should never see (applying updates, writing audit entries) answer "No such tool" on purpose. It does not show how a real model behaves.'
      : 'A fixed checklist chose each step, with no AI involved. The gateway still checked and logged every one.';
  const refused = r.tool_calls.filter((t) => !t.ok).length;
  return h('section', { class: 'panel run' },
    h('div', { class: 'run-head' },
      h('span', { class: `driver ${isLlm ? 'claude' : isSim ? 'sim' : ''}`.trim() }, label),
      h('h2', {}, `Envelope ${envelopeId}`), pill(runOutcome[0], runOutcome[1])),
    h('p', {}, explain),
    h('p', { class: 'muted' }, `Historical document workflow state (may include earlier runs): ${stateLabel}`),
    h('p', { class: 'muted' }, `Current run outcome: ${r.run_status || 'not reported'}. A run outcome does not imply a CRM write.`),
    (!isLlm && !isSim && envelopeId === 'env-1004') && h('p', { class: 'notice info' },
      'The checklist never reads the document text, so the hidden instruction had nothing to act on. This run shows the gateway logging every step, not an attack being resisted.'),
    (r.model_notes || []).length > 0 && h('div', { class: 'stack' }, h('h3', {}, isLlm ? 'What the model said' : 'What the simulated agent said'),
      r.model_notes.map((n) => h('blockquote', { class: 'said' }, n))),
    r.tool_calls.length
      ? h('div', { class: 'scroll' }, h('table', { class: 'plain calls' },
        h('thead', {}, h('tr', {}, ['Step', asked, 'With', 'Gateway decision'].map((t) => h('th', {}, t)))),
        h('tbody', {}, r.tool_calls.map((t) => h('tr', { class: t.ok ? '' : 'refused' },
          h('td', { class: 'num' }, t.step ?? ''), h('td', { class: 'tool' }, t.tool), h('td', { class: 'args' }, argsText(t.args)),
          h('td', {}, h('div', { class: 'decision' }, pill(OUTCOME_LABELS[t.outcome] || (t.ok ? 'Allowed' : 'Refused'), t.ok ? 'ok' : 'bad'),
            t.why && h('span', { class: 'muted' }, t.why))))))))
      : h('p', {}, 'No tool requests were made.'),
    h('div', { class: 'sheet-meta' }, h('span', {}, `${r.tool_calls.length} requests`), h('span', {}, `${refused} refused`),
      r.run_id && h('span', {}, 'Run ', h('span', { class: 'mono' }, r.run_id.slice(0, 8)))));
}

function agentView() {
  const mode = (config.modes || {}).agent;
  const out = h('div', { class: 'stack', 'aria-live': 'polite' });
  const choices = ENVELOPES.map((e, i) => h('label', { class: 'choice' },
    h('input', { type: 'radio', name: 'envelope', value: e.id, checked: i === 0 }), h('strong', {}, e.title), h('span', { class: 'muted' }, `${e.note}${e.id === 'env-1004' ? (mode === 'llm' || mode === 'claude' ? ' The configured LLM reads it, so this is the real test.' : ' The scripted checklist never reads it; use the simulation below to see the gateway refuse an attack.') : ''} (${e.id})`)));
  const go = h('button', { class: 'btn primary', type: 'button', onclick: () => run('configured') }, 'Run the agent');
  const sim = (config.environment === 'development') && h('button', { class: 'btn', type: 'button', onclick: () => run('simulated') }, 'Replay the simulated attack (no AI)');

  async function run(kind) {
    const id = kind === 'configured' ? (document.querySelector('input[name="envelope"]:checked') || {}).value : 'env-1004';
    if (!id) return;
    go.disabled = true; if (sim) sim.disabled = true;
    const startedAt = Date.now();
    const progress = h('div', { class: 'notice info run-progress', role: 'status', 'aria-live': 'polite' },
      h('span', { class: 'spinner', 'aria-hidden': 'true' }),
      h('div', { class: 'run-progress-copy' },
        h('strong', {}, kind === 'simulated' ? 'Replaying simulated attack…' : 'Agent is processing…'),
        h('span', { class: 'run-progress-detail' }, 'The server is running the workflow and checking each tool request. This may take 20–60 seconds with a local model.'),
        h('span', { class: 'run-progress-time' }, 'Elapsed: 0 seconds')));
    out.prepend(progress);
    const timeLabel = progress.querySelector('.run-progress-time');
    const timer = window.setInterval(() => {
      if (timeLabel) timeLabel.textContent = `Elapsed: ${Math.floor((Date.now() - startedAt) / 1000)} seconds`;
    }, 1000);
    try {
      const r = await api(`/agent/runs/${id}`, { method: 'POST', json: { event_id: uid(), ...(kind === 'simulated' ? { simulate_attack: true } : {}) } });
      progress.remove();
      out.prepend(runView(r, id));                       // newest run on top; earlier runs stay for comparison
      while (out.children.length > 5) out.lastChild.remove();
    } catch (e) { progress.remove(); show(out, 'problem', e.message); } finally {
      window.clearInterval(timer);
      go.disabled = false; if (sim) sim.disabled = false;
    }
  }

  return h('div', { class: 'page' },
    pageHead('Run the agent', 'The agent can only ask the gateway to run approved tools, one at a time. Pick a pretend DocuSign envelope and compare what was asked for with what the gateway allowed.'),
    h('section', { class: 'panel' },
      h('p', {}, h('span', { class: `driver ${mode === 'llm' || mode === 'claude' ? 'claude' : ''}`.trim() }, mode === 'llm' || mode === 'claude' ? 'Driver: LLM-driven' : 'Driver: scripted'),
        ' is how the server is set up right now (AGENT_MODE in .env or the in-memory setting). The LLM provider/model is configured separately.'),
      h('fieldset', { class: 'fieldset' }, h('legend', {}, 'Signed envelope'), h('div', { class: 'choices' }, choices)),
      h('div', { class: 'row' }, go, sim),
      h('p', { class: 'muted' }, 'Run the agent follows AGENT_MODE from the server configuration. With AGENT_MODE=llm, the selected envelope is processed by the configured model; with AGENT_MODE=scripted, it follows the scripted workflow.'),
      sim && h('p', { class: 'muted' }, 'The simulation uses no AI. It replays an attack written in advance, through the real gateway, so you can see what gets refused.')), 
    out);
}

/* ---------------------------------------------------------------- webhook demo (automation account, development only) */
const WEBHOOK_NOTES = {
  'env-1001': 'Signed. The agent should end with a change waiting for a reviewer.',
  'env-1002': 'Signed, but the client does not exist, so it should be flagged as an exception.',
  'env-1003': 'The message will say "completed", but DocuSign still says "sent". The agent re-checks and must refuse to read it.',
  'env-1004': 'Signed, with the hidden instruction in the text.',
};

function webhookView() {
  const out = h('div', { class: 'stack', 'aria-live': 'polite' });
  const choices = ENVELOPES.map((e, i) => h('label', { class: 'choice' },
    h('input', { type: 'radio', name: 'wh-envelope', value: e.id, checked: i === 0 }), h('strong', {}, e.title),
    h('span', { class: 'muted' }, `${WEBHOOK_NOTES[e.id]} (${e.id})`)));
  const event = h('select', { id: 'wh-event' },
    h('option', { value: 'envelope-completed' }, 'Envelope completed (the client finished signing)'),
    h('option', { value: 'envelope-sent' }, 'Envelope sent (only recorded, nothing to process yet)'));
  const send = h('button', { class: 'btn primary', type: 'button', onclick: () => fire(false) }, 'Send signed event');
  const forge = h('button', { class: 'btn', type: 'button', onclick: () => fire(true) }, 'Send forged event (wrong signature)');

  async function fire(forged) {
    const envelope_id = (document.querySelector('input[name="wh-envelope"]:checked') || {}).value;
    if (!envelope_id) return;
    send.disabled = forge.disabled = true;
    try {
      const r = await api('/ui/demo/webhook', { method: 'POST', json: { envelope_id, event: event.value, forge: forged } });
      const note = out.querySelector(':scope > .notice');
      if (note) note.remove();
      out.prepend(webhookResult(r));
      while (out.children.length > 4) out.lastChild.remove();
    } catch (e) { show(out, 'problem', e.message); } finally { send.disabled = forge.disabled = false; }
  }

  return h('div', { class: 'page' },
    pageHead('Webhook demo', 'DocuSign calls the app when a document changes. The app checks the signature, ignores repeats, and only then lets the agent process the signed document.'),
    h('section', { class: 'panel' },
      h('p', { class: 'muted' }, 'In this demo the server plays DocuSign and signs each message with the shared secret. The secret never reaches your browser. Send the same event twice to see the duplicate protection.'),
      h('fieldset', { class: 'fieldset' }, h('legend', {}, 'Envelope'), h('div', { class: 'choices' }, choices)),
      h('label', {}, 'Event', event),
      h('div', { class: 'row' }, send, forge)),
    out);
}

function webhookResult(r) {
  const rec = r.received; const body = rec.body || {}; const run = body.agent_run;
  const envelope = r.sent.body.data.envelopeId;
  let verdict; let tone; let text;
  if (rec.http_status === 401) { verdict = 'Rejected (401)'; tone = 'bad'; text = 'The signature did not match the shared secret, so the message was thrown away. Nothing was recorded or processed.'; }
  else if (rec.http_status !== 202) { verdict = `Refused (${rec.http_status})`; tone = 'bad'; text = String(body.detail || 'The app refused the message.'); }
  else if (body.status === 'duplicate_ignored') { verdict = 'Duplicate ignored'; tone = ''; text = 'This exact event was already handled, so nothing new happened. DocuSign retries deliveries, so this protection matters.'; }
  else if (run === 'not_needed') { verdict = 'Accepted (202)'; tone = 'ok'; text = 'Recorded in the audit log. This kind of event does not start any work.'; }
  else if (run && run.status === 'failed') { verdict = 'Accepted, then failed'; tone = 'bad'; text = `The agent run failed (${run.error}). The claim was released so a retry can run.`; }
  else { verdict = 'Accepted (202)'; tone = 'ok'; text = 'The signature checked out, the event was recorded, and the agent was started. It asks DocuSign itself whether the document is really signed instead of trusting the message.'; }
  const ran = run && typeof run === 'object' && run.tool_calls;
  const panel = h('section', { class: 'panel run' },
    h('div', { class: 'run-head' },
      h('span', { class: `driver ${r.sent.forged ? 'sim' : ''}`.trim() }, r.sent.forged ? 'Forged signature' : 'Signed with the shared secret'),
      h('h2', {}, `${r.sent.body.event} for ${envelope}`)),
    h('div', { class: 'stack' }, h('h3', {}, '1. What was sent'),
      h('pre', { class: 'sent' }, `${r.sent.header}: ${r.sent.signature_start}...\n${JSON.stringify(r.sent.body, null, 2)}`)),
    h('div', { class: 'stack' }, h('h3', {}, '2. What the app answered'), h('div', {}, pill(verdict, tone)), h('p', {}, text)),
    ran && h('h3', {}, '3. What the agent did next'),
    ran && run.tool_calls.length === 1 && run.workflow && run.workflow.state === 'not_started' && h('p', {},
      'DocuSign itself says this envelope is not fully signed, so the agent stopped after one request. The message\u2019s claim was not trusted.'),
    ran && run.workflow && run.workflow.state === 'awaiting_review' && h('p', {}, 'It is now waiting for a person. Sign out and sign in as alice or bob to review it; the audit log shows the webhook entries.'));
  return h('div', { class: 'stack' }, panel, ran && runView(run, envelope));
}

/* ---------------------------------------------------------------- review queue (reviewer) */
function reviewView() {
  const flash = h('div', { role: 'status', 'aria-live': 'polite' });
  const queue = h('div', { class: 'stack' });
  const history = h('div', { class: 'stack' });

  async function load() {
    try {
      const [pending, all] = await Promise.all([api('/approvals'), api('/approvals?status=all&limit=15')]);
      queue.replaceChildren(...(pending.items.length
        ? pending.items.map((it) => approvalSheet(it, flash, load))
        : [h('p', { class: 'empty' }, 'Nothing is waiting. Sign in as flow, submit a document, then come back.')]));
      const done = all.items.filter((i) => i.status !== 'pending');
      history.replaceChildren(...(done.length ? [h('h2', {}, 'Recent decisions'), decisionsTable(done)] : []));
    } catch (e) { show(flash, 'problem', e.message); }
  }
  load();

  return h('div', { class: 'page' },
    pageHead('Review queue', 'Check the before and after values against the signed form. Approving updates the CRM once; rejecting changes nothing.'),
    flash, h('div', { class: 'row' }, h('button', { class: 'btn', type: 'button', onclick: load }, 'Refresh')), queue, history);
}

function approvalSheet(item, flash, reload) {
  const p = item.proposal || {};
  const isException = item.kind === 'exception';
  const note = h('textarea', { rows: '2', maxlength: '2000', placeholder: 'Required to approve. For example: verified by phone to the number on file.' });
  const msg = h('div', { role: 'status', 'aria-live': 'polite' });
  const approve = h('button', {
    class: 'btn primary', type: 'button', disabled: isException,
    title: isException ? 'Exceptions cannot be approved. Reject it and fix the source document.' : false,
    onclick: () => decide('approve'),
  }, 'Approve and update CRM');
  const reject = h('button', { class: 'btn', type: 'button', onclick: () => decide('reject') }, 'Reject');

  async function decide(decision) {
    if (decision === 'approve' && !note.value.trim()) return show(msg, 'problem', 'Add a note saying how the change was verified. The email-change policy asks for this before approval.');
    approve.disabled = reject.disabled = true;
    try {
      const r = await api(`/approvals/${item.approval_id}/decision`, { method: 'POST', json: { decision, note: note.value.trim() } });
      show(flash, 'good', decision === 'reject' ? 'Rejected. Nothing was changed.'
        : (r.crm_update && r.crm_update.shadow) ? (r.crm_update.would_apply
          ? 'Approved. Shadow mode: nothing was written to the CRM. The audit log records that it would have been.'
          : 'Approved. Shadow mode: nothing was written, and the CRM already held these values.')
        : (r.crm_update && r.crm_update.applied) ? 'Approved. The CRM now holds the new values.'
          : 'Approved. The CRM already had these values, so nothing was written.');
      reload();
    } catch (e) {
      show(msg, 'problem', e.message);
      if (e.status === 502 || (e.status === 409 && /recorded/i.test(e.message))) { show(flash, 'problem', e.message); reload(); return; }
      approve.disabled = isException; reject.disabled = false;
    }
  }

  const rowsFor = () => ['email', 'effective_date'].map((k) => {
    const before = (p.crm_current || {})[k]; const after = (p.proposed_changes || {})[k];
    const changed = before !== after;
    return h('div', { class: 'diff-row' }, h('div', { class: 'diff-name' }, FIELD_LABELS[k]),
      h('div', { class: `diff-old ${changed ? 'changed' : ''}` }, before || '(empty)'),
      h('div', { class: `diff-new ${changed ? 'changed' : ''}` }, after || '(empty)'));
  });
  const body = isException
    ? h('div', { class: 'stack' }, h('h3', {}, 'Why this was flagged'),
      h('ul', { class: 'issues' }, (p.issues || []).map((i) => h('li', {}, h('strong', {}, FIELD_LABELS[i.field] || i.field), `: ${i.message} `, h('span', { class: 'mono' }, `(${i.code})`)))))
    : h('div', { class: 'stack' },
      h('div', { class: 'diff' }, h('div', { class: 'diff-row diff-head' }, h('div', {}, 'Field'), h('div', {}, 'In the CRM now'), h('div', {}, 'From the signed form')), rowsFor()),
      (p.crm_current && p.proposed_changes && ['email', 'effective_date'].every((k) => p.crm_current[k] === p.proposed_changes[k]))
        && h('p', { class: 'muted' }, 'Both values already match the CRM, so approving writes nothing.'));

  return h('article', { class: `sheet ${isException ? 'exception' : ''}`.trim() },
    h('div', { class: 'sheet-head' },
      h('h2', {}, isException ? `Document ${item.document_id}` : `Client ${p.record_id}`),
      isException ? pill('Needs investigation', 'warn') : pill('Change to review')),
    h('div', { class: 'sheet-meta' }, h('span', {}, 'Document ', h('span', { class: 'mono' }, item.document_id)),
      h('span', {}, 'Submitted by ', h('span', { class: 'mono' }, item.submitted_by)), h('span', {}, fmtTime(item.created_at))),
    body,
    p.agent_note_untrusted && h('div', {}, h('div', { class: 'untrusted-label' }, 'Note from the agent (plain text, treat as untrusted)'),
      h('blockquote', { class: 'untrusted' }, p.agent_note_untrusted)),
    h('label', {}, 'Your note', note),
    msg,
    h('div', { class: 'row' }, approve, reject));
}

function decisionsTable(items) {
  const tone = { approved: 'ok', rejected: '' };
  return h('div', { class: 'scroll panel' }, h('table', { class: 'plain' },
    h('thead', {}, h('tr', {}, ['Document', 'Decision', 'Decided by', 'Note', 'When'].map((t) => h('th', {}, t)))),
    h('tbody', {}, items.map((i) => h('tr', {}, h('td', { class: 'mono' }, i.document_id), h('td', {}, pill(i.status, tone[i.status] || '')),
      h('td', {}, i.decided_by || '-'), h('td', {}, i.decision_note || '-'), h('td', { class: 'num' }, i.decided_at ? fmtTime(i.decided_at) : '-'))))));
}

/* ---------------------------------------------------------------- scorecard (reviewer) */
const AGREEMENT_ROWS = [
  ['agreed', 'Same change made in the CRM', 'Staff made exactly the change the system proposed. The system was right.'],
  ['untouched', 'Not made yet', 'The record is still as it was. This is the backlog the automation would clear.'],
  ['different', 'A different change', 'Staff changed a field to something other than what the system proposed. Look at these first.'],
  ['partial', 'Partly made', 'Staff made some of the change but not all of it.'],
  ['applied_by_system', 'Written by this system', 'Set aside: of course these match, so they prove nothing.'],
  ['unavailable', 'Could not check', 'The CRM record could not be read.'],
];

function figure(value, label, note) {
  return h('div', { class: 'figure' }, h('div', { class: 'figure-num' }, value), h('div', { class: 'figure-label' }, label), note && h('div', { class: 'muted' }, note));
}

function scorecardView() {
  const out = h('div', { class: 'stack', 'aria-live': 'polite' });
  const days = h('select', { id: 'sc-days' }, [7, 30, 90].map((d) => h('option', { value: d, selected: d === 30 }, `Last ${d} days`)));
  days.addEventListener('change', load);
  const dev = config.environment === 'development' && (config.modes || {}).crm === 'mock';

  async function load() {
    try { out.replaceChildren(render(await api(`/scorecard?days=${days.value}`))); }
    catch (e) { show(out, 'problem', e.message); }
  }

  function render(r) {
    const ag = r.agreement;
    const pct = ag.rate == null ? 'none yet' : `${Math.round(ag.rate * 100)}%`;
    return h('div', { class: 'stack' },
      h('div', { class: 'figures' },
        figure(r.volume.documents, 'Documents handled', `${r.volume.changes_proposed} changes, ${r.volume.exceptions} exceptions`),
        figure(ag.untouched, 'Changes nobody has made yet', 'the backlog'),
        figure(pct, 'Agreement with staff', ag.judged ? `${ag.agreed} of ${ag.judged} where staff acted` : 'needs staff to act first'),
        figure(r.agent.refused, 'Agent requests refused', `of ${r.agent.tool_requests} by the gateway`)),
      h('section', { class: 'panel' }, h('h2', {}, 'Staff and the system, compared'),
        h('p', { class: 'muted' }, 'Each change the system proposed is compared with the client record in the CRM now, and with what the record held at the time.'),
        h('div', { class: 'scroll' }, h('table', { class: 'plain' },
          h('thead', {}, h('tr', {}, ['Outcome', 'Count', 'What it means'].map((t) => h('th', {}, t)))),
          h('tbody', {}, AGREEMENT_ROWS.map(([k, name, text]) => h('tr', {}, h('td', {}, name), h('td', { class: 'num' }, ag[k]), h('td', { class: 'muted' }, text))))))),
      r.disagreements.length > 0 && h('section', { class: 'panel' }, h('h2', {}, 'Differences to look at'),
        h('div', { class: 'scroll' }, h('table', { class: 'plain' },
          h('thead', {}, h('tr', {}, ['Document', 'Client', 'Field', 'System proposed', 'CRM now'].map((t) => h('th', {}, t)))),
          h('tbody', {}, r.disagreements.flatMap((d) => d.differences.map((x) => h('tr', {},
            h('td', { class: 'mono' }, d.document_id), h('td', { class: 'mono' }, d.record_id), h('td', {}, FIELD_LABELS[x.field] || x.field),
            h('td', { class: 'mono' }, x.proposed), h('td', { class: 'mono' }, x.crm_now ?? '(empty)')))))))),
      h('section', { class: 'panel' }, h('h2', {}, 'People and process'),
        h('div', { class: 'scroll' }, h('table', { class: 'plain' }, h('tbody', {},
          [['Approved by a reviewer', r.reviews.approved], ['Rejected by a reviewer', r.reviews.rejected], ['Waiting for a reviewer', r.reviews.pending],
            ['Median time from proposal to decision', r.reviews.median_minutes_to_decision == null ? 'no decisions yet' : `${r.reviews.median_minutes_to_decision} minutes`],
            ['Agent runs', r.agent.runs], ['Duplicate events ignored', r.volume.duplicates_ignored],
            ['Shadow mode: would have been written', r.shadow.would_apply], ['Shadow mode: record already held the values', r.shadow.already_current],
          ].map(([a, b]) => h('tr', {}, h('td', {}, a), h('td', { class: 'num' }, b)))))),
        Object.keys(r.exceptions_by_reason).length > 0 && h('div', { class: 'stack' }, h('h3', {}, 'Why documents became exceptions'),
          h('ul', { class: 'issues' }, Object.entries(r.exceptions_by_reason).map(([code, n]) => h('li', {}, `${n} \u00d7 `, h('span', { class: 'mono' }, code)))))),
      h('p', { class: 'muted' }, r.estimate
        ? `Estimate: ${r.estimate.hours_of_manual_work_covered} hours of manual work covered, assuming each manual update takes ${r.estimate.manual_minutes_per_update} minutes (your own setting, MANUAL_MINUTES_PER_UPDATE).`
        : 'No time-saved estimate is shown. Time a few manual updates, then set MANUAL_MINUTES_PER_UPDATE in .env to see one. A made-up number would undermine the rest of this page.'));
  }

  const email = h('input', { type: 'text', id: 'se-email', value: 'new.client@example.com', maxlength: '200', autocomplete: 'off' });
  const date = h('input', { type: 'text', id: 'se-date', value: '2026-10-12', maxlength: '10', autocomplete: 'off' });
  const rec = h('select', { id: 'se-rec' }, ['C10482', 'C10001', 'C20005'].map((c) => h('option', { value: c }, c)));
  const helperMsg = h('div', { role: 'status', 'aria-live': 'polite' });
  const edit = h('button', { class: 'btn', type: 'button', onclick: async () => {
    edit.disabled = true;
    try {
      await api('/ui/demo/staff-edit', { method: 'POST', json: { record_id: rec.value, email: email.value.trim(), effective_date: date.value.trim() } });
      show(helperMsg, 'good', `Saved as if a colleague had edited ${rec.value} in the CRM. The scorecard has been refreshed.`);
      load();
    } catch (e) { show(helperMsg, 'problem', e.message); } finally { edit.disabled = false; }
  } }, 'Apply as a staff edit');

  load();
  return h('div', { class: 'page' },
    pageHead('Scorecard', 'How the automation is doing on real work: what it handled, where people stepped in, and whether staff and the system reached the same answer in the CRM.'),
    h('div', { class: 'row' }, h('label', {}, 'Period', days), h('button', { class: 'btn', type: 'button', onclick: load }, 'Refresh')),
    out,
    dev && h('section', { class: 'panel' }, h('h2', {}, 'Demo helper'),
      h('p', { class: 'muted' }, 'Stands in for a colleague editing a client in Salesforce during a shadow-mode trial, so there is something to compare with. It exists only in development with the pretend CRM. Enter the proposed values to create an agreement, or others to create a difference.'),
      h('div', { class: 'row' }, h('label', {}, 'Client', rec), h('label', {}, 'Email', email), h('label', {}, 'Effective date', date)),
      h('div', { class: 'row' }, edit), helperMsg));
}

/* ---------------------------------------------------------------- policy search (either role) */
const EXAMPLE_QUESTIONS = ['How do we change a client email?', 'How long do we keep signed documents?', 'When can automation make a change?'];

function architectureView() {
  const node = (title, detail, kind = '') => h('div', { class: `flow-node ${kind}`.trim() },
    h('strong', {}, title), h('p', {}, detail));
  const link = (label) => h('div', { class: 'flow-link' }, h('span', {}, '↓'), h('small', {}, label));
  const step = (n, title, detail) => h('li', {}, h('strong', {}, `${n}. ${title}`), h('p', {}, detail));
  return h('div', { class: 'page' },
    pageHead('System flow', 'Follow the event, API calls, document movement, and writes across the systems. This scaffold uses mock DocuSign and CRM adapters by default; SharePoint is currently a connector stub.'),
    h('section', { class: 'panel' },
      h('div', { class: 'flow-grid' },
        node('DocuSign envelope', 'Client signs advisory / account form', 'external'),
        node('Webhook receiver', 'POST /webhooks/docusign · verify HMAC · claim event idempotently', 'trigger'),
        node('Agent + gateway', 'Re-check envelope status; retrieve document; extract and validate fields', 'core'),
        node('Human review', 'Approve / reject proposed changes; exceptions remain reviewable', 'human'),
        node('Salesforce', 'Read client record; compare current values; conditionally write approved fields', 'external'),
        node('SharePoint / Graph', 'Target for signed PDF + metadata; real adapter still needs OAuth / Graph wiring', 'external'),
        node('SQLite audit chain', 'Event history, idempotency, approval state, hash-chained audit records', 'storage')),
      h('p', { class: 'muted' }, 'Lines below describe logical calls, not a claim that every external connector is live.')),
    h('section', { class: 'panel' }, h('h2', {}, 'Who talks to whom'),
      h('div', { class: 'scroll' }, h('table', { class: 'plain' },
        h('thead', {}, h('tr', {}, ['From', 'To', 'Interaction', 'Data / purpose'].map((x) => h('th', {}, x)))),
        h('tbody', {},
          [
            ['DocuSign', 'Webhook endpoint', 'POST event (push)', 'Envelope ID, event type, HMAC signature'],
            ['Webhook endpoint', 'Idempotency + audit store', 'Verify / claim / append', 'Reject forged events; suppress duplicate deliveries'],
            ['Agent gateway', 'DocuSign adapter', 'GET status + retrieve pages', 'Re-check signed status; obtain signed content'],
            ['Agent tools', 'Knowledge search', 'Query (read)', 'Role-filtered policy passages + citations'],
            ['Agent workflow', 'Salesforce adapter', 'OAuth + SOQL read', 'Find exactly one client and fetch mapped fields'],
            ['Reviewer', 'Approval service', 'Approve / reject (write)', 'Human decision and reviewer identity'],
            ['Approval workflow', 'Salesforce adapter', 'Re-read + PATCH (conditional write)', 'Only approved mapped fields; conflict check; shadow mode supported'],
            ['Document workflow', 'SharePoint / Graph', 'Planned upload / metadata write', 'Not wired yet: adapter currently raises NotImplementedError'],
            ['Indexer', 'Ollama embedding endpoint', 'POST embeddings', 'Embed each child chunk locally during indexing'],
            ['Search API', 'Ollama embedding endpoint', 'POST query embedding', 'Rank indexed vectors by cosine similarity; fallback to BM25'],
          ].map((r) => h('tr', {}, r.map((v) => h('td', {}, v)))))))),
    h('section', { class: 'panel' }, h('h2', {}, 'Webhook: envelope completed'),
      h('ol', { class: 'flow-steps' },
        step(1, 'DocuSign → webhook', 'A Connect event includes the envelope ID and event type. In the demo, the server simulates this sender.'),
        step(2, 'Verify before trust', 'Validate the HMAC against the raw request body. A bad signature is rejected before processing.'),
        step(3, 'Deduplicate event', 'Claim the event ID so retries do not launch duplicate work.'),
        step(4, 'Re-query DocuSign', 'The agent asks the adapter for the envelope status instead of trusting the webhook alone.'),
        step(5, 'Retrieve and extract', 'Read signed pages, redact sensitive values, extract fields, then run deterministic validation.'),
        step(6, 'Salesforce read', 'Look up exactly one client using the configured client identifier and only the mapped fields.'),
        step(7, 'Approval gate', 'Create a proposal; a reviewer approves or rejects it. The model cannot directly write to the CRM.'),
        step(8, 'Approved update + audit', 'Re-read current values, refuse stale conflicts, write only allowed fields (or remain in shadow mode), and append audit events.'),
        step(9, 'SharePoint document archive', 'Intended next connector: store signed PDF and completion certificate with metadata and least-privilege access.'))),
    h('section', { class: 'panel' }, h('h2', {}, 'Semantic search: document → chunks → vectors → answer'),
      h('div', { class: 'flow-grid search-flow' },
        node('Policy document', 'Extract text + redact sensitive identifiers'),
        node('Parent / child chunks', 'Deterministic chunk IDs preserve section context'),
        node('Embedding model', `Configured: ${config.llm?.embedding_mode || 'none'} / ${config.llm?.embedding_model || 'nomic-embed-text'}`, 'trigger'),
        node('Vector store', 'SQLite kb_embeddings table: child ID, model, vector JSON', 'storage'),
        node('Question embedding', 'Embed query with the same model'),
        node('Similarity ranking', 'Cosine similarity; role filter is applied before results are returned'),
        node('Parent context + citation', 'Return the matching child excerpt and its full parent section'),
        node('Generation model', `Current: ${config.llm?.provider || 'claude'} · ${config.llm?.model || 'configured model'}`, 'external')),
      h('p', { class: 'muted' }, 'If embeddings are disabled or a document has not been re-indexed with vectors, retrieval falls back to SQLite FTS5/BM25 keyword search. Enable Ollama embeddings in .env and re-index to create vectors.')));
}

function settingsView() {
  const current = config.llm || {};
  const provider = h('select', {},
    h('option', { value: 'claude', selected: current.provider === 'claude' }, 'Anthropic Claude API'),
    h('option', { value: 'openai-compatible', selected: current.provider === 'openai-compatible' }, 'OpenAI-compatible API / local model (Ollama)'));
  const model = h('input', { type: 'text', maxlength: '200', value: current.model || 'llama3.1', autocomplete: 'off' });
  const endpoint = h('input', { type: 'url', maxlength: '500', value: current.base_url || 'http://localhost:11434/v1', autocomplete: 'off' });
  const key = h('input', { type: 'password', maxlength: '2000', autocomplete: 'new-password', placeholder: 'Optional for local Ollama' });
  const endpointWrap = h('div', { class: 'stack' }, h('label', {}, 'Generation API base URL', endpoint));
  const keyLabel = h('label', {}, 'Generation API key (optional for local Ollama)', key);
  const genOut = h('div', { role: 'status', 'aria-live': 'polite' });
  const embedMode = h('select', {},
    h('option', { value: 'none', selected: (current.embedding_mode || 'none') === 'none' }, 'Disabled (keyword search only)'),
    h('option', { value: 'ollama', selected: current.embedding_mode === 'ollama' }, 'Ollama embeddings (semantic search)'));
  const embedModel = h('input', { type: 'text', maxlength: '200', value: current.embedding_model || 'nomic-embed-text', autocomplete: 'off' });
  const embedEndpoint = h('input', { type: 'url', maxlength: '500', value: current.embedding_base_url || 'http://localhost:11434', autocomplete: 'off' });
  const embedEndpointWrap = h('div', { class: 'stack' }, h('label', {}, 'Embedding API base URL', embedEndpoint));
  const embedOut = h('div', { role: 'status', 'aria-live': 'polite' });
  const note = h('p', { class: 'muted' }, 'Settings apply to this running server process only. API keys are not returned to the browser or written to disk. Restarting resets choices to .env defaults.');
  function updateFields() {
    endpointWrap.hidden = provider.value === 'claude';
    keyLabel.firstChild.textContent = provider.value === 'claude' ? 'Anthropic API key (blank keeps the current in-memory key)' : 'Generation API key (optional for local Ollama)';
    if (provider.value === 'claude' && model.value === 'llama3.1') model.value = 'claude-sonnet-5-5';
    if (provider.value === 'openai-compatible' && model.value === 'claude-sonnet-5-5') model.value = 'llama3.1';
  }
  function updateEmbeddingFields() { embedEndpointWrap.hidden = embedMode.value !== 'ollama'; }
  provider.addEventListener('change', updateFields);
  embedMode.addEventListener('change', updateEmbeddingFields);
  updateFields(); updateEmbeddingFields();

  const save = h('button', { class: 'btn primary', type: 'button', onclick: apply }, 'Apply settings');
  const testGen = h('button', { class: 'btn', type: 'button', onclick: () => testConnection('generation', testGen, genOut) }, 'Test generation model');
  const testEmbed = h('button', { class: 'btn', type: 'button', onclick: () => testConnection('embedding', testEmbed, embedOut) }, 'Test embedding model');
  const applyEmbed = h('button', { class: 'btn primary', type: 'button', onclick: applyEmbeddingSettings }, 'Apply embedding settings');
  const reindexEmbed = h('button', { class: 'btn', type: 'button', onclick: reindexPolicyChunks }, 'Re-embed indexed policy chunks');
  async function applyEmbeddingSettings() {
    applyEmbed.disabled = true;
    try {
      const r = await api('/ui/settings/embeddings', { method: 'POST', json: {
        embedding_mode: embedMode.value,
        embedding_model: embedModel.value.trim() || 'nomic-embed-text',
        embedding_base_url: embedEndpoint.value.trim() || 'http://localhost:11434'
      }});
      config.llm = { ...(config.llm || {}), embedding_mode: r.embedding_mode,
        embedding_model: r.embedding_model, embedding_base_url: r.embedding_base_url };
      show(embedOut, 'good', `${r.note} Model: ${r.embedding_model}.`);
    } catch (e) { show(embedOut, 'problem', e.message); }
    finally { applyEmbed.disabled = false; }
  }
  async function apply() {
    save.disabled = true;
    try {
      const r = await api('/ui/settings/llm', { method: 'POST', json: {
        provider: provider.value, model: model.value.trim(),
        base_url: endpoint.value.trim() || 'http://localhost:11434/v1', api_key: key.value
      }});
      config.modes = { ...(config.modes || {}), agent: r.agent_mode || 'llm', reader: 'llm' };
      config.llm = { ...(config.llm || {}), provider: r.provider, model: r.model,
        base_url: provider.value === 'claude' ? '' : endpoint.value.trim(),
        embedding_mode: r.embedding_mode, embedding_model: r.embedding_model,
        embedding_base_url: r.embedding_base_url || current.embedding_base_url || 'http://localhost:11434' };
      refreshModeBadges();
      key.value = '';
      show(genOut, 'good', `${r.note} Generation and embedding settings are separate. Test each connection below.`);
    } catch (e) { show(genOut, 'problem', e.message); } finally { save.disabled = false; }
  }
  async function reindexPolicyChunks() {
    reindexEmbed.disabled = true;
    try {
      const r = await api('/ui/settings/reindex-embeddings', { method: 'POST', json: {} });
      show(embedOut, 'good', `${r.message} Documents: ${r.documents}; chunks: ${r.chunks}; model: ${r.model}${r.dimensions ? `; vector dimensions: ${r.dimensions}` : ''}.`);
    } catch (e) { show(embedOut, 'problem', e.message); } finally { reindexEmbed.disabled = false; }
  }
  async function testConnection(target, button, output) {
    button.disabled = true;
    try {
      const r = await api('/ui/settings/test', { method: 'POST', json: { target } });
      show(output, 'good', `${r.message}${r.dimensions ? ` Vector dimensions: ${r.dimensions}.` : ''} Model: ${r.model || ''}`);
    } catch (e) { show(output, 'problem', e.message); } finally { button.disabled = false; }
  }
  return h('div', { class: 'page' },
    pageHead('Model settings', 'Configure and test the model that reads client documents separately from the model that powers semantic policy search.'),
    h('section', { class: 'panel stack' },
      h('h2', {}, '1. Generation model — PDF processing and agent'),
      h('p', {}, 'Use a chat/instruct model here, such as llama3.1. Do NOT enter nomic-embed-text in this field; it only creates embeddings and cannot perform document extraction.'),
      h('label', {}, 'Generation provider', provider),
      h('label', {}, 'Generation model name', model),
      endpointWrap, keyLabel,
      h('div', { class: 'actions' }, save, testGen), genOut,
      h('hr'),
      h('h2', {}, '2. Embedding model — semantic search'),
      h('p', {}, 'Embedding models turn policy chunks and search questions into vectors. This is used by Document search, not by the Process document extraction step.'),
      h('label', {}, 'Embedding provider', embedMode),
      h('label', {}, 'Embedding model name', embedModel),
      embedEndpointWrap,
      h('div', { class: 'actions' }, applyEmbed, testEmbed, reindexEmbed), embedOut,
      h('p', { class: 'muted' }, 'For local Ollama, install and start Ollama, then run: ollama pull llama3.1 and ollama pull nomic-embed-text. If this app runs in Docker, localhost refers to the container; try http://host.docker.internal:11434 instead. Apply settings before testing.'),
      h('p', { class: 'muted' }, 'After enabling embeddings, test the model and click Re-embed indexed policy chunks. This applies the current in-memory settings to existing chunks; newly indexed policy documents will use the active embedding settings.')));
}

function policyView() {
  const q = h('input', { type: 'search', id: 'q', maxlength: '200', autocomplete: 'off', placeholder: 'Ask a question across indexed documents…' });
  const out = h('div', { class: 'stack', 'aria-live': 'polite' });
  const scope = h('p', { class: 'muted' });
  const go = h('button', { class: 'btn primary', type: 'button', onclick: () => search(q.value) }, 'Ask and search');
  q.addEventListener('keydown', (e) => { if (e.key === 'Enter') search(q.value); });
  let visibleDocs = [];

  api('/knowledge/documents').then((r) => {
    visibleDocs = r.items || [];
    scope.textContent = session.role === 'reviewer'
      ? `Searching ${visibleDocs.length} indexed documents, including advisor and compliance documents.`
      : `Searching ${visibleDocs.length} indexed documents available to all staff.`;
    if (!visibleDocs.length) show(out, 'info', 'No documents are indexed yet. Reviewers can add text-based policy PDFs below, or run python scripts/index_policies.py to index the bundled examples.');
  }).catch((e) => show(out, 'problem', e.message));

  async function search(text) {
    const query = text.trim();
    if (query.length < 3) return show(out, 'problem', 'Type at least three characters.');
    go.disabled = true;
    go.textContent = 'Searching and generating answer…';
    show(out, 'info', 'Searching the indexed documents and asking the configured LLM to synthesize an answer. This may take a little while.');
    try {
      const r = await api(`/knowledge/answer?q=${encodeURIComponent(query)}&top_k=5`);
      const answerCard = h('article', { class: 'panel' },
        h('h2', {}, 'Answer from indexed documents'),
        h('p', { class: 'untrusted' }, r.answer),
        h('p', { class: 'muted' }, r.model_used ? 'Generated from retrieved passages. Verify the cited source sections below.' : 'No LLM synthesis was needed because no passages matched.'));
      const cards = (r.results || []).map((x, i) => h('article', { class: 'panel' },
        h('div', {}, h('h2', {}, `[${i + 1}] ${x.citation.title}`), h('p', { class: 'muted' }, `Document: ${x.citation.document_id} · Section: ${x.citation.section}`),
          h('p', { class: 'hint' }, x.retrieval?.method === 'semantic'
            ? `Semantic match · cosine similarity ${x.retrieval.cosine_similarity}`
            : 'Keyword match · SQLite BM25')),
        h('div', {}, h('div', { class: 'untrusted-label' }, 'Matched passage'), h('blockquote', { class: 'untrusted' }, x.matched_excerpt_untrusted)),
        h('details', {}, h('summary', {}, 'Show the full retrieved section'), h('blockquote', { class: 'untrusted' }, x.context_untrusted))));
      out.replaceChildren(answerCard, ...cards);
    } catch (e) {
      // Keep retrieval useful even if the generation provider is down or misconfigured.
      try {
        const r = await api(`/knowledge/search?q=${encodeURIComponent(query)}&top_k=5`);
        out.replaceChildren(h('div', { class: 'notice info' }, `The answer model could not respond (${e.message}). Showing retrieved passages without a synthesized answer.`),
          ...(r.results || []).map((x) => h('article', { class: 'panel' },
            h('h2', {}, x.citation.title), h('p', { class: 'muted' }, `Document: ${x.citation.document_id} · Section: ${x.citation.section}`),
            h('blockquote', { class: 'untrusted' }, x.matched_excerpt_untrusted),
            h('details', {}, h('summary', {}, 'Show the full retrieved section'), h('blockquote', { class: 'untrusted' }, x.context_untrusted)))));
      } catch (fallbackError) { show(out, 'problem', `${e.message} Retrieval fallback also failed: ${fallbackError.message}`); }
    } finally { go.disabled = false; go.textContent = 'Ask and search'; }
  }

  const panels = [h('section', { class: 'panel stack' },
    h('h2', {}, 'Search the indexed corpus'),
    h('p', {}, 'Ask a natural-language question. The app retrieves relevant sections across all documents you are allowed to see, then asks the configured LLM to answer from those sources with numbered citations.'),
    h('label', {}, 'Your question', q),
    h('div', { class: 'row' }, go, EXAMPLE_QUESTIONS.map((s) => h('button', { class: 'btn link', type: 'button', onclick: () => { q.value = s; search(s); } }, s))), scope)];

  if (session.role === 'reviewer') {
    const docId = h('input', { type: 'text', maxlength: '100', required: true, placeholder: 'e.g. wire-transfer-policy' });
    const title = h('input', { type: 'text', maxlength: '200', required: true, placeholder: 'Document title' });
    const audience = h('select', {}, h('option', { value: 'all_staff' }, 'All staff'), h('option', { value: 'advisors' }, 'Advisors'), h('option', { value: 'compliance' }, 'Compliance'));
    const file = h('input', { type: 'file', accept: 'application/pdf,.pdf', required: true });
    const uploadOut = h('div', { role: 'status', 'aria-live': 'polite' });
    const upload = h('button', { class: 'btn primary', type: 'button', onclick: async () => {
      if (!docId.value.trim() || !title.value.trim() || !file.files?.[0]) return show(uploadOut, 'problem', 'Enter a document ID and title, then choose a PDF.');
      const fd = new FormData(); fd.append('document_id', docId.value.trim()); fd.append('title', title.value.trim());
      fd.append('audience', audience.value); fd.append('file', file.files[0]);
      upload.disabled = true; upload.textContent = 'Extracting, chunking, and indexing…';
      show(uploadOut, 'info', 'Reading the PDF, creating parent/child chunks, and generating embeddings if enabled. Keep this page open.');
      try {
        const r = await api('/knowledge/documents/upload', { method: 'POST', form: fd });
        show(uploadOut, 'good', `Document ${r.status}: ${r.title || title.value} · ${r.parents ?? '—'} parent sections · ${r.children ?? '—'} child chunks. ${r.note || ''}`);
        const d = await api('/knowledge/documents'); visibleDocs = d.items || [];
        scope.textContent = `Searching ${visibleDocs.length} indexed documents visible to your role.`;
      } catch (e) { show(uploadOut, 'problem', e.message); }
      finally { upload.disabled = false; upload.textContent = 'Add PDF to searchable library'; }
    } }, 'Add PDF to searchable library');
    panels.push(h('section', { class: 'panel stack' }, h('h2', {}, 'Add a document to the searchable library'),
      h('p', {}, 'Use this for internal policies and reference documents. This is separate from “Process document,” which starts the client-change approval workflow. Do not add client-specific records or sensitive client documents to the shared policy library.'),
      h('label', {}, 'Document ID', docId), h('label', {}, 'Title', title), h('label', {}, 'Who may search this document?', audience),
      h('label', {}, 'Text-based PDF', file), upload, uploadOut));
  }

  return h('div', { class: 'page' },
    pageHead('Document search', 'Search across the indexed document library, get an LLM-generated answer grounded in retrieved passages, and inspect the cited source sections.'),
    ...panels, out);
}

/* ---------------------------------------------------------------- audit log (reviewer) */
function auditView() {
  const verifyBox = h('div', { role: 'status', 'aria-live': 'polite' });
  const list = h('div', { class: 'stack' });
  const filter = h('input', { type: 'text', maxlength: '100', autocomplete: 'off', spellcheck: 'false', placeholder: 'All documents' });
  filter.addEventListener('keydown', (e) => { if (e.key === 'Enter') load(); });
  const verify = h('button', { class: 'btn primary', type: 'button', onclick: runVerify }, 'Verify audit log');

  async function runVerify() {
    verify.disabled = true;
    try {
      const r = await api('/audit/verify');
      if (r.ok) show(verifyBox, 'good', `The log is intact: ${r.events} entries, none altered or removed.`);
      else show(verifyBox, 'problem', `The log was altered. The first bad entry is number ${r.first_bad_seq} of ${r.events}.`);
    } catch (e) { show(verifyBox, 'problem', e.message); } finally { verify.disabled = false; }
  }

  async function load() {
    try {
      const doc = filter.value.trim();
      const r = await api(`/audit/events?limit=50${doc ? `&document_id=${encodeURIComponent(doc)}` : ''}`);
      list.replaceChildren(r.items.length
        ? h('div', { class: 'scroll panel' }, h('table', { class: 'plain' },
          h('thead', {}, h('tr', {}, ['#', 'When', 'Event', 'Document', 'Who', 'Result', 'Details'].map((t) => h('th', {}, t)))),
          h('tbody', {}, r.items.map((e) => h('tr', {}, h('td', { class: 'num' }, e.seq), h('td', { class: 'num' }, fmtTime(e.timestamp)),
            h('td', { class: 'mono' }, e.event_type), h('td', { class: 'mono' }, e.document_id), h('td', {}, e.actor), h('td', {}, e.outcome),
            h('td', {}, h('details', {}, h('summary', {}, 'Show'), h('pre', {}, JSON.stringify(e.details, null, 2)))))))))
        : h('p', { class: 'empty' }, 'No entries yet.'));
    } catch (e) { show(list, 'problem', e.message); }
  }
  load();

  return h('div', { class: 'page' },
    pageHead('Audit log', 'Every step is written down with who did it and when. Each entry is chained to the one before, so an edited or deleted entry shows up when you verify.'),
    h('section', { class: 'panel' }, h('div', { class: 'row' }, verify), verifyBox),
    h('section', { class: 'stack' }, h('div', { class: 'row' }, h('h2', {}, 'Latest entries'),
      h('label', {}, h('span', { class: 'hint' }, 'Show only one document'), filter),
      h('button', { class: 'btn', type: 'button', onclick: load }, 'Show entries')), list));
}

/* ---------------------------------------------------------------- start */
(async function start() {
  try { const r = await fetch('/ui/config'); if (r.ok) config = { ...config, ...(await r.json()) }; } catch { /* keep defaults */ }
  await initEntra();
  render();
}());
