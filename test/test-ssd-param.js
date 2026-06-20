// test/test-ssd-param.js — unit test for ?ssd= base64url decode logic.
// Run with: node test/test-ssd-param.js
// Requires Node 16+ (atob and Uint8Array available globally).
//
// The ?ssd= handler in index.html is browser-only inline code; it cannot be
// imported in Node. This test extracts the 4-line decode logic verbatim from
// the handler (lines ~1071-1075 in index.html) and exercises it in Node.
// The three structural assertions (replaceState called sync before await;
// verifyArtifact called with decoded bytes; malformed input caught by
// the existing upload-error path) are verified by code review — they are
// linear, trivially correct, and not extractable without a full DOM mock.

'use strict';

if (!globalThis.atob) globalThis.atob = (b) => Buffer.from(b, 'base64').toString('binary');

// Extracted verbatim from the ?ssd= handler in index.html
function decodeSsdParam(param) {
  const base64 = param.replace(/-/g, '+').replace(/_/g, '/')
    + '=='.slice(0, (4 - param.length % 4) % 4);
  const bin = atob(base64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function toBase64url(bytes) {
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return Buffer.from(bin, 'binary').toString('base64')
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

let passed = 0;
let failed = 0;

function assert(label, condition) {
  if (condition) { console.log(`  ✓ ${label}`); passed++; }
  else           { console.error(`  ✗ ${label}`); failed++; }
}

console.log('\n?ssd= base64url decode');

// Round-trip: known byte array survives encode → decode intact
{
  const original = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x00, 0x01, 0xff, 0x80, 0x00]);
  const encoded  = toBase64url(original);
  const decoded  = decodeSsdParam(encoded);
  assert('round-trip: length matches',  decoded.length === original.length);
  let allMatch = true;
  for (let i = 0; i < original.length; i++) {
    if (decoded[i] !== original[i]) { allMatch = false; break; }
  }
  assert('round-trip: bytes match', allMatch);
}

// All-zero bytes (ensures no padding issue)
{
  const zeros   = new Uint8Array(32);
  const decoded = decodeSsdParam(toBase64url(zeros));
  assert('32-zero-byte round-trip', decoded.length === 32 && decoded.every(b => b === 0));
}

// Single byte (length not divisible by 3 — exercises padding branch)
{
  const one     = new Uint8Array([0xab]);
  const decoded = decodeSsdParam(toBase64url(one));
  assert('single-byte round-trip', decoded.length === 1 && decoded[0] === 0xab);
}

// Two bytes (another padding case)
{
  const two     = new Uint8Array([0xde, 0xad]);
  const decoded = decodeSsdParam(toBase64url(two));
  assert('two-byte round-trip', decoded.length === 2 && decoded[0] === 0xde && decoded[1] === 0xad);
}

// Malformed input causes atob to throw — caught by the try/catch in the handler
{
  let threw = false;
  try { decodeSsdParam('not!!base64url'); }
  catch (_) { threw = true; }
  assert('malformed param throws (caught by handler try/catch)', threw);
}

// URL-safe chars: '-' and '_' must survive (standard base64 '+' and '/' are
// not present in a well-formed param and would be converted by the handler)
{
  const bytes   = new Uint8Array([0xfb, 0xff, 0xfe]);   // encodes to base64 +//+ region
  const encoded = toBase64url(bytes);                    // uses - and _ instead
  assert('param uses - not +', !encoded.includes('+'));
  assert('param uses _ not /', !encoded.includes('/'));
  const decoded = decodeSsdParam(encoded);
  assert('url-safe chars decoded correctly',
    decoded[0] === 0xfb && decoded[1] === 0xff && decoded[2] === 0xfe);
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
