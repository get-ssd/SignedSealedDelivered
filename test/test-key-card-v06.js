// test/test-key-card-v06.js — verify SPEC-PROTO v0.6 key card format changes
// Run with: node test/test-key-card-v06.js
//
// Tests:
//   1. keyring.js exportKeyCard uses signing_public_key + encryption_public_key
//   2. keyring.js verifyKeyCard reads signing_public_key (not public_key)
//   3. Normalized objects in both shells include self_signed + new field names
//   4. Canonical JSON keys match between emit and verify paths
//   5. Tick background files use signing_public_key in verifyKeyCard + storeFetchedKey
//   6. Encoding: exportKeyCard transcodes encryption key (b64dec → b64urlenc)
//   7. Import side in advanced.html transcodes back (b64urldec → b64enc)

'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const TICK = path.join(ROOT, '..', 'ssd.tick-2');

let pass = 0;
let fail = 0;

function ok(label, cond) {
  if (cond) { console.log(`  PASS  ${label}`); pass++; }
  else       { console.error(`  FAIL  ${label}`); fail++; }
}

const keyring   = fs.readFileSync(path.join(ROOT, 'keyring.js'), 'utf8');
const advanced  = fs.readFileSync(path.join(ROOT, 'advanced.html'), 'utf8');
const indexHtml = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');
const tickSW    = fs.readFileSync(path.join(TICK, 'background', 'service-worker.js'), 'utf8');
const tickBG    = fs.readFileSync(path.join(TICK, 'background', 'background.js'), 'utf8');
const tickSWDist = fs.readFileSync(path.join(TICK, 'dist', 'chrome', 'background', 'service-worker.js'), 'utf8');

// ─── 1. keyring.js emit (exportKeyCard) ─────────────────────────────────────

console.log('\n1. keyring.js exportKeyCard — field names');
ok('uses signing_public_key',             keyring.includes('signing_public_key: rec.public_key_b64'));
ok('uses encryption_public_key',          keyring.includes('encryption_public_key:'));
ok('does NOT use old public_key field',   !/^\s+public_key: rec\.public_key_b64/m.test(keyring));
ok('does NOT use old encryption_key',     !keyring.includes("encryption_key:"));

// ─── 2. keyring.js emit — encoding ──────────────────────────────────────────

console.log('\n2. keyring.js exportKeyCard — encryption key encoding');
ok('transcodes enc key b64dec→b64urlenc', keyring.includes('cryptoOps.b64urlenc(cryptoOps.b64dec(encKeySetting.value))'));

// ─── 3. keyring.js verifyKeyCard ────────────────────────────────────────────

console.log('\n3. keyring.js verifyKeyCard');
ok('reads signing_public_key for pubKey', keyring.includes('cryptoOps.importPublicKeyB64(card.signing_public_key)'));
ok('does NOT read card.public_key',       !keyring.includes('importPublicKeyB64(card.public_key)'));

// ─── 4. Canonical JSON key set ───────────────────────────────────────────────
// Extract the cardData field list from keyring.js and verify it matches what
// the normalized objects in both shells set up.

console.log('\n4. Canonical JSON field alignment');

// Fields that must be in the signed payload (same in emit and both verify paths)
const SIGNED_FIELDS = [
  'chain', 'encryption_public_key', 'expires', 'hash8', 'identicon_algorithm',
  'issued', 'name', 'recheck_interval_days', 'revocation_hint', 'self_image',
  'signing_algorithm', 'signing_public_key', 'version',
];

for (const field of SIGNED_FIELDS) {
  ok(`keyring.js cardData has '${field}'`, keyring.includes(`${field}:`));
}

// advanced.html normalized object (inside importContactCard)
const advNormMatch = advanced.match(/const normalized = \{([^}]+)\}/);
ok('advanced.html has normalized object', !!advNormMatch);
if (advNormMatch) {
  const normBody = advNormMatch[1];
  ok('advanced normalized: signing_public_key',    normBody.includes('signing_public_key'));
  ok('advanced normalized: encryption_public_key', normBody.includes('encryption_public_key'));
  ok('advanced normalized: self_signed',           normBody.includes('self_signed'));
  ok('advanced normalized: no old public_key',     !normBody.includes("public_key:            card.public_key"));
  ok('advanced normalized: no old encryption_key', !normBody.includes("encryption_key:"));
}

// index.html normalized object (inside _importKeyCard)
const idxNormMatch = indexHtml.match(/const normalized = \{([^}]+)\}/);
ok('index.html has normalized object', !!idxNormMatch);
if (idxNormMatch) {
  const normBody = idxNormMatch[1];
  ok('index normalized: signing_public_key',    normBody.includes('signing_public_key'));
  ok('index normalized: encryption_public_key', normBody.includes('encryption_public_key'));
  ok('index normalized: self_signed',           normBody.includes('self_signed'));
  ok('index normalized: no old public_key',     !normBody.includes("public_key:            card.public_key"));
  ok('index normalized: no old encryption_key', !normBody.includes("encryption_key:"));
}

// ─── 5. Import paths: field reads ────────────────────────────────────────────

console.log('\n5. Import paths — field reads');
ok('advanced: guard reads signing_public_key', advanced.includes('card.signing_public_key || !card.name'));
ok('advanced: hash8 from signing_public_key',  advanced.includes('cryptoOps.hash8(card.signing_public_key)'));
ok('advanced: DB stores signing_public_key',   advanced.includes('public_key_b64: card.signing_public_key'));
ok('advanced: enc key guard uses new field',   advanced.includes('card.encryption_public_key)'));
ok('advanced: transcodes enc key back',        advanced.includes('cryptoOps.b64enc(cryptoOps.b64urldec(card.encryption_public_key))'));

ok('index: guard reads signing_public_key',    indexHtml.includes("card.signing_public_key || !card.hash8"));
ok('index: hash8 from signing_public_key',     indexHtml.includes('cryptoOps.hash8(card.signing_public_key)'));
ok('index: DB stores signing_public_key',      indexHtml.includes('public_key_b64: card.signing_public_key'));

// ─── 6. Tick background files ────────────────────────────────────────────────

console.log('\n6. Tick background files');
for (const [label, src] of [['service-worker.js', tickSW], ['background.js', tickBG], ['dist/service-worker.js', tickSWDist]]) {
  ok(`${label}: verifyKeyCard reads signing_public_key`, src.includes('b64ToBytes(card.signing_public_key)'));
  ok(`${label}: required field is signing_public_key`,  src.includes("'signing_public_key'"));
  ok(`${label}: storeFetchedKey maps to internal public_key`, src.includes('public_key:        card.signing_public_key'));
}

// ─── Summary ─────────────────────────────────────────────────────────────────

console.log(`\n${pass + fail} checks: ${pass} passed, ${fail} failed`);
if (fail > 0) process.exit(1);
