const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');

function fixture() {
  const create = (init) => {
    let state;
    const listeners = [];
    const store = {
      getState: () => state,
      setState: (patch) => { const old = state; state = { ...state, ...(typeof patch === 'function' ? patch(state) : patch) }; listeners.forEach(fn => fn(state, old)); },
      subscribe: fn => listeners.push(fn),
    };
    state = init(store.setState);
    return store;
  };
  const app = create(() => ({ backendUrl: 'https://backend.example', secrets: { api: 'original' } }));
  const calls = [], errors = [];
  let get = async () => [], invoke = async () => ({});
  const mocks = {
    zustand: { create },
    './actions': { agentList: x => x, refreshAgents: () => {} },
    './api': { backendUrl: () => app.getState().backendUrl, apiGet: (...a) => get(...a), apiMut: async (...a) => calls.push(a) },
    './github': {}, './store': { useApp: app },
    './tauri': { tauriInvoke: (...a) => invoke(...a), errText: String },
    './toast': { toastErr: (...a) => errors.push(a), toastOk: () => {} },
  };
  const exports = {};
  const stripped = stripTypeScriptTypes(fs.readFileSync('src/lib/oauth.ts', 'utf8'));
  const names = [...stripped.matchAll(/export (?:async )?(?:function|const) (\w+)/g)].map(m => m[1]);
  const source = stripped.replace(/import \{([^}]+)\} from "([^"]+)";/g, 'const {$1} = require("$2");')
    .replace(/export (?=(?:async )?(?:function|const) )/g, '') + `\nObject.assign(exports, {${names.join(',')}});`;
  vm.runInNewContext(source, { exports, require: name => mocks[name], URL, console });
  return { oauth: exports, app, calls, errors, setGet: fn => get = fn, setInvoke: fn => invoke = fn };
}

test('public webhook URLs are canonical and reject private mapped IPv4', () => {
  const { oauth } = fixture();
  for (const host of ['::ffff:7f00:1', '::ffff:a00:1', '::ffff:c0a8:101', '::ffff:127.0.0.1']) {
    assert.equal(oauth.validatePublicHttpsUrl(`https://[${host}]`), '');
  }
  assert.equal(oauth.validatePublicHttpsUrl('HTTPS://EXAMPLE.COM:443/base///'), 'https://example.com/base');
  assert.equal(oauth.validatePublicHttpsUrl('https://[2001:4860:4860::8888]/'), 'https://[2001:4860:4860::8888]');
  assert.equal(oauth.validatePublicHttpsUrl('https://[::ffff:808:808]'), 'https://[::ffff:808:808]');
});

for (const existing of [[], [{ id: 'existing' }]]) test(`changed API credential invalidates agent lookup (${existing.length} agents)`, async () => {
  const f = fixture();
  f.setGet(async () => { f.app.setState({ secrets: { api: 'changed' } }); return existing; });
  await assert.rejects(f.oauth.ensureDefaultBackendAgent(), /connection changed/);
  assert.equal(f.calls.length, 0);
});

test('teams require a confirmed save to the selected backend', async () => {
  const f = fixture();
  f.setInvoke(async () => ({ teams: [{ id: 't' }], cloud_saved: false, env_path: '/local', cloud_error: 'cloud failed', local_error: 'local warning' }));
  await f.oauth.importVercelCli();
  assert.equal(f.oauth.useOAuth.getState().vercelTeams.length, 0);
  assert.equal(f.oauth.useOAuth.getState().vercelTeamsFromSavedToken, false);
  assert.match(f.errors[0][1], /cloud failed.*local warning/);
});

test('saved teams are invalidated on backend credential changes', async () => {
  const f = fixture();
  f.setInvoke(async () => ({ teams: [{ id: 't' }], cloud_saved: true }));
  await f.oauth.importVercelCli();
  assert.equal(f.oauth.useOAuth.getState().vercelTeamsFromSavedToken, true);
  f.app.setState({ secrets: { api: 'next' } });
  assert.equal(f.oauth.useOAuth.getState().vercelTeams.length, 0);
});

test('team lookup cannot apply a response from a previous backend', async () => {
  const f = fixture();
  f.setInvoke(async () => { f.app.setState({ backendUrl: 'https://other.example' }); return { teams: [{ id: 't' }] }; });
  await assert.rejects(f.oauth.fetchVercelTeams('manual'), /connection changed/);
  assert.equal(f.oauth.useOAuth.getState().vercelTeams.length, 0);
});

test('replacing a Vercel connection invalidates an in-flight team lookup', async () => {
  const f = fixture();
  let resolve;
  f.setInvoke(() => new Promise(done => { resolve = done; }));
  const pending = f.oauth.fetchVercelTeams('old-token');
  f.oauth.invalidateVercelTeams();
  resolve({ teams: [{ id: 'old-team' }] });
  await assert.rejects(pending, /connection changed/);
  assert.equal(f.oauth.useOAuth.getState().vercelTeams.length, 0);
});

test('saved teams record whether the token also reached the local destination', async () => {
  const f = fixture();
  f.setInvoke(async () => ({ teams: [{ id: 't' }], cloud_saved: true, local_error: 'file unavailable' }));
  assert.equal(await f.oauth.importVercelCli(), true);
  assert.equal(f.oauth.useOAuth.getState().vercelTeamsTarget.localSaved, false);
});
