'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const source = fs.readFileSync(path.join(__dirname, '../passkey.js'), 'utf8');
let passed = 0;
function check(name, fn) { fn(); passed++; console.log('PASS ' + name); }
function fixture({ flags = 5, prf = true, attachment = 'platform', missingData = false } = {}) {
  const records = new Map();
  const calls = { signed: 0 };
  const data = new Uint8Array(37); data[32] = flags;
  const credential = {
    rawId: new Uint8Array([1, 2, 3]).buffer,
    authenticatorAttachment: attachment,
    response: {getAuthenticatorData: () => missingData ? undefined : data.buffer, authenticatorData: missingData ? undefined : data.buffer},
    getClientExtensionResults: () => ({prf: {enabled: prf}}),
  };
  const context = vm.createContext({
    ArrayBuffer, Uint8Array, TextEncoder, AbortController, setTimeout, clearTimeout,
    window: {crypto: webcrypto, location: {hostname: 'localhost'}},
    navigator: {credentials: {
      create: async options => { calls.create = options; return credential; },
      get: async options => { calls.get = options; return credential; },
    }},
    db: {put: async (_, row) => records.set(row.key, row), get: async (_, key) => records.get(key)},
    cryptoOps: {
      b64enc: bytes => Buffer.from(bytes).toString('base64'), b64dec: value => Buffer.from(value, 'base64'),
      sign: async () => { calls.signed++; return 'signature'; },
    },
  });
  vm.runInContext(source + '\nglobalThis.subject = passkey;', context);
  return {subject: context.subject, records, calls};
}
async function main() {
  const local = fixture();
  await local.subject.register('local', {requirePrf: true});
  check('registration requests non-discoverable platform credential with required verification', () => {
    const selection = local.calls.create.publicKey.authenticatorSelection;
    assert.equal(selection.residentKey, 'discouraged');
    assert.equal(selection.authenticatorAttachment, 'platform');
    assert.equal(selection.userVerification, 'required');
  });
  check('accepted local credential is persisted', () => assert.ok(local.records.has('credential_id')));
  for (const [name, options] of [
    ['backup eligible', {flags: 13}], ['backed up', {flags: 29}],
    ['invalid backup state', {flags: 21}], ['no user verification', {flags: 1}],
    ['no user presence', {flags: 4}], ['no auth data', {missingData: true}],
    ['external authenticator', {attachment: 'cross-platform'}],
    ['missing attachment evidence', {attachment: null}], ['no PRF', {prf: false}],
  ]) {
    const f = fixture(options);
    await assert.rejects(f.subject.register('reject', {requirePrf: true}), e => e.code === 'SSD_AUTH_POLICY');
    check(name + ' rejected before any settings write', () => assert.equal(f.records.size, 0));
  }
  const fallback = fixture({prf: false});
  const registered = await fallback.subject.register('explicit fallback');
  check('low-level explicit non-PRF fallback remains distinguishable', () => assert.equal(registered.prfSupported, false));
  for (const [name, options] of [['syncable assertion', {flags: 13}], ['unverified assertion', {flags: 1}], ['missing assertion data', {missingData: true}]]) {
    const f = fixture(options);
    f.records.set('credential_id', {value: 'AQID'});
    await assert.rejects(f.subject.authenticate(), e => e.code === 'SSD_AUTH_POLICY');
    await assert.rejects(f.subject.confirmAndSign({}, new Uint8Array()), e => e.code === 'SSD_AUTH_POLICY');
    check(name + ' cannot unlock or sign', () => assert.equal(f.calls.signed, 0));
  }
  const signed = await local.subject.confirmAndSign({}, new Uint8Array());
  check('verified device-bound assertion permits signing', () => assert.equal(signed, 'signature'));
  check('assertion selects stored credential and requires verification', () => {
    assert.equal(local.calls.get.publicKey.userVerification, 'required');
    assert.equal(Buffer.from(local.calls.get.publicKey.allowCredentials[0].id).toString('base64'), 'AQID');
  });
  console.log(`${passed} passed`);
}
main().catch(error => { console.error(error); process.exitCode = 1; });
