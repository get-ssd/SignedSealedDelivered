// test/test-opfs-identicon-extract.js — verify opfsStore + identicon extraction
// Run with: node test/test-opfs-identicon-extract.js
//
// Tests:
//   1. opfs-identicon.js is valid JS containing both globals
//   2. Both shells no longer define them inline (const opfsStore / const identicon)
//   3. Both shells load <script src="opfs-identicon.js">
//   4. service-worker.js includes opfs-identicon.js in SHELL precache
//   5. identicon.algorithmId() returns expected string
//   6. identicon.render() produces a filled grid (canvas mock)
//   7. identicon.renderToDataURL() returns a data URL string

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');

let pass = 0;
let fail = 0;

function ok(label, cond) {
  if (cond) { console.log(`  PASS  ${label}`); pass++; }
  else       { console.error(`  FAIL  ${label}`); fail++; }
}

// ─── Load source texts ───────────────────────────────────────────────────────

const sharedSrc    = fs.readFileSync(path.join(ROOT, 'opfs-identicon.js'), 'utf8');
const advancedSrc  = fs.readFileSync(path.join(ROOT, 'advanced.html'), 'utf8');
const indexSrc     = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');
const swSrc        = fs.readFileSync(path.join(ROOT, 'service-worker.js'), 'utf8');

// ─── 1. Shared file structure ────────────────────────────────────────────────

console.log('\n1. opfs-identicon.js structure');
ok('contains opfsStore definition',  sharedSrc.includes('const opfsStore ='));
ok('contains identicon definition',  sharedSrc.includes('const identicon ='));
ok('identicon has algorithmId',      sharedSrc.includes("algorithmId: () => 'ssd-identicon-1.0'"));
ok("starts with 'use strict'",       sharedSrc.trimStart().startsWith("'use strict'"));

// ─── 2. Shells no longer define them inline ──────────────────────────────────

console.log('\n2. Inline definitions removed from shells');
ok('advanced.html: no inline opfsStore',  !advancedSrc.includes('const opfsStore ='));
ok('advanced.html: no inline identicon',  !advancedSrc.includes('const identicon ='));
ok('index.html: no inline opfsStore',     !indexSrc.includes('const opfsStore ='));
ok('index.html: no inline identicon',     !indexSrc.includes('const identicon ='));

// ─── 3. Shells load the shared script ────────────────────────────────────────

console.log('\n3. Shells load opfs-identicon.js');
ok('advanced.html: has <script src="opfs-identicon.js">',
   advancedSrc.includes('<script src="opfs-identicon.js"></script>'));
ok('index.html: has <script src="opfs-identicon.js">',
   indexSrc.includes('<script src="opfs-identicon.js"></script>'));
ok('advanced.html: opfs-identicon.js loaded after signer-engines.js',
   advancedSrc.indexOf('<script src="opfs-identicon.js">') >
   advancedSrc.indexOf('<script src="signer-engines.js">'));
ok('index.html: opfs-identicon.js loaded after signer-engines.js',
   indexSrc.indexOf('<script src="opfs-identicon.js">') >
   indexSrc.indexOf('<script src="signer-engines.js">'));

// ─── 4. Service worker precache ──────────────────────────────────────────────

console.log('\n4. Service worker precache');
ok("service-worker.js: SHELL contains './opfs-identicon.js'",
   swSrc.includes("'./opfs-identicon.js'"));

// ─── 5-7. Execute identicon in Node (canvas mock) ────────────────────────────

console.log('\n5-7. identicon runtime behaviour (canvas mock)');

// Minimal canvas mock that records fillRect calls
function makeCanvas() {
  const fills = [];
  return {
    _fills: fills,
    width: 0, height: 0,
    getContext() {
      return {
        clearRect() {},
        set fillStyle(_) {},
        fillRect(x, y, w, h) { fills.push({ x, y, w, h }); },
      };
    },
    toDataURL() { return 'data:image/png;base64,MOCK'; },
  };
}

// Minimal document mock for renderToDataURL
const sandbox = {
  document: { createElement(tag) { return tag === 'canvas' ? makeCanvas() : {}; } },
};

// vm.runInNewContext: const/let don't become properties of the sandbox, so we
// append an assignment to expose them via globalThis (which is the sandbox).
vm.runInNewContext(
  sharedSrc + '\nglobalThis.opfsStore = opfsStore; globalThis.identicon = identicon;',
  sandbox
);

const { identicon } = sandbox;

ok('identicon is defined', typeof identicon === 'object' && identicon !== null);
ok("algorithmId() returns 'ssd-identicon-1.0'", identicon.algorithmId() === 'ssd-identicon-1.0');

// render() should fill some cells for a non-zero seed
const canvas = makeCanvas();
const seed = new Uint8Array(32).fill(0x5A); // known non-trivial pattern
identicon.render(seed, canvas, 64);
ok('render() produces filled cells', canvas._fills.length > 0);
ok('render() sets canvas size to 64', canvas.width === 64 && canvas.height === 64);

// renderToDataURL returns a string starting with 'data:'
const dataUrl = identicon.renderToDataURL(seed, 64);
ok("renderToDataURL() returns data URL", typeof dataUrl === 'string' && dataUrl.startsWith('data:'));

// Determinism: same seed produces same fill count
const canvas2 = makeCanvas();
identicon.render(seed, canvas2, 64);
ok('render() is deterministic', canvas._fills.length === canvas2._fills.length);

// ─── Summary ─────────────────────────────────────────────────────────────────

console.log(`\n${pass + fail} checks: ${pass} passed, ${fail} failed`);
if (fail > 0) process.exit(1);
