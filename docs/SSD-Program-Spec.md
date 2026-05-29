# Signed, Sealed, Delivered (SSD)
## PWA Program Specification v0.1
### For Claude Code

---

## 0. How to Read This Document

This spec is written phase by phase. **Implement Phase 1 completely before touching Phase 2.** Each phase has a stated goal and a gate — do not proceed past the gate until it passes. Where this spec says "TBD" or "future phase", leave a comment stub and move on.

The reference PWA in this project (`index.html` — Bitcoin Block Verifier) shows the established coding style. Follow it: single HTML file per phase boundary, named JS objects for logical groupings, CSS custom properties for all theming, `showSection()` navigation pattern. No framework, no build step, no bundler.

---

## 1. Project Overview

SSD is a cryptographic signing and verification tool. Users generate keypairs, sign plain-text documents with a biometric passkey confirmation, and produce `.ssd` artifacts that anyone can verify. All cryptography runs in the browser. Nothing sensitive leaves the device unencrypted.

**Core guarantee:** what the signer confirmed is exactly what the verifier sees. The render is the contract.

**Non-goals for this implementation:**
- Server-side components (none — fully client-side)
- Real-time sync (sync is explicit user action)
- Rich text / styled rendering (Phase 1 uses plain text only)

---

## 2. File Structure

```
ssd/
├── index.html          ← entire application (grows phase by phase)
├── manifest.json       ← PWA manifest
├── service-worker.js   ← cache + file handler routing
├── favicon.ico
├── icon-192.png
├── icon-512.png
└── README.md
```

Single-file application. All HTML, CSS, and JS live in `index.html`. No separate JS or CSS files. No node_modules. No build step.

---

## 3. Tech Stack

| Concern | Solution | Notes |
|---|---|---|
| Language | Vanilla JS (ES2022) | No framework |
| Crypto | WebCrypto API | Native — no library |
| Passkeys | WebAuthn API | Native — no library |
| Storage | IndexedDB | Via thin wrapper (see §5) |
| QR generation | `qrcode` library (CDN) | Generation only, Phase 2+ |
| QR scanning | `jsQR` library (CDN) | Scanning only, Phase 2+ |
| ZIP packaging | `fflate` (CDN) | `.ssd` container, Phase 1 |
| Markdown | None — Phase 1 is plain text | Deferred |
| Canvas | None — Phase 1 is plain text render | Deferred |

CDN base: `https://cdnjs.cloudflare.com` — consistent with existing PWAs.

---

## 4. PWA Manifest (`manifest.json`)

```json
{
  "name": "Signed, Sealed, Delivered",
  "short_name": "SSD",
  "description": "Cryptographic signing and verification for digital documents",
  "icons": [
    { "src": "icon-192.png", "sizes": "192x192", "type": "image/png" },
    { "src": "icon-512.png", "sizes": "512x512", "type": "image/png" }
  ],
  "start_url": "./index.html",
  "scope": "./",
  "display": "standalone",
  "background_color": "#0a0a0a",
  "theme_color": "#0a0a0a",
  "file_handlers": [
    {
      "action": "./index.html",
      "accept": { "application/x-ssd": [".ssd"] }
    }
  ],
  "share_target": {
    "action": "./index.html",
    "method": "POST",
    "enctype": "multipart/form-data",
    "params": {
      "files": [
        { "name": "artifact", "accept": ["application/x-ssd", ".ssd"] }
      ]
    }
  }
}
```

---

## 5. Service Worker (`service-worker.js`)

Follow the established pattern exactly. Increment the cache version number on any change.

```javascript
const CACHE_NAME = 'ssd-v1';
const SHELL = [
  './',
  './index.html',
  './manifest.json',
  './favicon.ico',
  './icon-192.png',
  './icon-512.png',
];

// install: cache shell
// activate: delete old caches, claim clients
// fetch: cache-first for shell, network-first for CDN libs,
//        pass through everything else (no RPC calls to intercept)
```

The service worker must **not** intercept WebAuthn or WebCrypto operations — those are handled by the browser natively and do not go through fetch.

---

## 6. IndexedDB Schema

Database name: `ssd-keyring`
Database version: `1`

All DB access goes through `db.js`-style object (inline in `index.html`) — a thin async wrapper. No direct `indexedDB.open()` calls outside this wrapper.

### 6.1 Object Stores

#### `my_keys`
Stores the user's own keypairs.

```javascript
{
  id: "uuid-v4",                    // keyPath
  name: "Personal",                 // user label
  fingerprint: "3F2A9C1D",         // first 8 hex chars of SHA-256(pubkey)
  public_key_b64: "...",            // base64 Ed25519 public key
  private_key_encrypted: "...",     // base64 — AES-256-GCM encrypted private key
  private_key_iv: "...",            // base64 — IV for above
  identicon_algorithm: "ssd-identicon-1.0",
  self_image_b64: null,             // optional base64 image
  created: "2026-05-02T10:30:00Z",
  expires: null,                    // ISO date string or null
  recheck_interval_days: null,
  revocation_hint: null,
  is_default: true,
  is_revoked: false
}
```

#### `contact_keys`
Stores contacts' public keys.

```javascript
{
  id: "uuid-v4",                    // keyPath
  name: "Alice",                    // user-assigned label
  fingerprint: "A1B2C3D4",
  public_key_b64: "...",
  identicon_algorithm: "ssd-identicon-1.0",
  self_image_b64: null,
  received_via: "qr_scan",         // "qr_scan"|"paste"|"file"|"well_known"|"manual"
  received_at: "2026-05-02T10:30:00Z",
  expires: null,
  recheck_interval_days: 90,
  last_checked: "2026-05-02T10:30:00Z",
  revocation_hint: null,
  trust_type: "peer",               // "peer"|"circle"|"org"|"notarial"|"institutional"
  local_label: null,                // recipient's override display name
  local_identicon_algorithm: null,  // recipient's preferred identicon algorithm (null = use issuer's)
  countersignatures: [],
  is_revoked: false
}
```

#### `artifacts`
Stores sent and received artifacts (metadata only — full `.ssd` blobs in OPFS).

```javascript
{
  id: "uuid-v4",                    // keyPath
  direction: "sent",                // "sent"|"received"
  signed_by_key_id: "uuid-v4",     // ref to my_keys or contact_keys
  fingerprint: "3F2A9C1D",
  artifact_hash: "sha256:...",
  render_spec: "ssd-render-1.0",
  created: "2026-05-02T10:30:00Z",
  encrypted: false,
  encrypted_for: [],
  opfs_path: "artifacts/uuid-v4.ssd",
  summary: "First 80 chars of source text..."
}
```

#### `settings`
Key-value store for non-sensitive app preferences.

```javascript
{
  key: "default_key_id",           // keyPath
  value: "uuid-v4"
}
```

### 6.2 Keyring Encryption at Rest

The private key material in `my_keys` is encrypted with AES-256-GCM using a key derived from the WebAuthn PRF extension. This means:

- The database is readable (for public key material, metadata) without unlocking
- Private key bytes are never in memory without a passkey authentication event
- On cold start, the app can display the keyring structure but cannot sign until unlocked

**Derivation flow:**
```
WebAuthn authentication with PRF extension
  → PRF output (32 bytes, deterministic per credential + salt)
  → HKDF-SHA256(prfOutput, salt="ssd-keyring-v1", info="private-key-encryption")
  → AES-256-GCM key
  → decrypt private_key_encrypted for the session
```

If the browser does not support PRF (older Safari), fall back to prompting passkey auth per signing operation and keeping the private key in memory only for the duration of that operation. Flag the absence of PRF in the UI.

---

## 7. Module Structure (inside `index.html`)

Organised as named const objects, consistent with the Bitcoin Verifier pattern.

```javascript
const db = { ... }           // IndexedDB wrapper
const opfsStore = { ... }    // OPFS wrapper for artifact blobs
const crypto = { ... }       // WebCrypto operations (namespace — don't shadow window.crypto)
const passkey = { ... }      // WebAuthn operations
const keyring = { ... }      // Key management business logic
const signer = { ... }       // Document signing
const verifier = { ... }     // Document verification
const artifact = { ... }     // .ssd package creation and parsing
const identicon = { ... }    // Identicon rendering
const ui = { ... }           // showSection, displayResult, clearResult, etc.
const app = { ... }          // Init and top-level orchestration
```

Each object is a plain object literal with async methods. No classes except where a class is clearly cleaner (e.g. a streaming parser). No `this` binding issues — methods that need to call siblings reference them by name.

---

## 8. Cryptographic Operations (`crypto` object)

All operations use `window.crypto.subtle` directly. No wrapper library.

### 8.1 Key Generation
```javascript
crypto.generateKeypair()
// Returns { publicKey: CryptoKey, privateKey: CryptoKey }
// Ed25519, extractable: true (we need to export for storage and key cards)

crypto.exportPublicKeyB64(cryptoKey)
// Returns base64 string

crypto.exportPrivateKeyB64(cryptoKey)
// Returns base64 string — only called when encrypting for storage

crypto.importPublicKeyB64(b64)
// Returns CryptoKey for Ed25519 verify operations

crypto.importPrivateKeyB64(b64)
// Returns CryptoKey for Ed25519 sign operations
```

### 8.2 Signing and Verification
```javascript
crypto.sign(privateKey, dataBytes)
// Returns base64 signature string

crypto.verify(publicKey, signatureB64, dataBytes)
// Returns boolean
```

### 8.3 Hashing
```javascript
crypto.sha256(dataBytes)
// Returns hex string

crypto.sha256B64(dataBytes)
// Returns base64 string
```

### 8.4 Fingerprint and Identicon Seed
```javascript
crypto.fingerprint(publicKeyB64)
// Returns 8-char uppercase hex string (first 8 chars of SHA-256 of raw public key bytes)

crypto.identiconSeed(publicKeyB64)
// Returns full 32-byte SHA-256 of raw public key bytes as Uint8Array
// — passed to identicon.render()
```

### 8.5 Symmetric Encryption (for keyring at rest)
```javascript
crypto.encryptPrivateKey(privateKeyB64, aesKey)
// Returns { ciphertext_b64, iv_b64 }

crypto.decryptPrivateKey(ciphertextB64, ivB64, aesKey)
// Returns privateKey base64 string
```

### 8.6 Key Exchange Encryption (Phase 3)
```javascript
crypto.generateEphemeralX25519()
// Returns { publicKey: CryptoKey, privateKey: CryptoKey }

crypto.deriveSharedKey(myPrivateKey, theirPublicKey)
// ECDH + HKDF → AES-256-GCM key

crypto.encryptForRecipient(contentBytes, recipientPublicKeyB64)
// Returns { ciphertext_b64, iv_b64, ephemeral_public_key_b64 }

crypto.decryptFromSender(ciphertextB64, ivB64, ephemeralPublicKeyB64, myPrivateKey)
// Returns Uint8Array plaintext
```

---

## 9. Passkey Operations (`passkey` object)

### 9.1 Registration (creating a new passkey)
```javascript
passkey.register(label)
// Creates a new WebAuthn credential
// label: shown to user in authenticator UI (e.g. "SSD Keyring")
// Returns { credentialId_b64, publicKey_b64 }
// Stores credentialId in settings store for future auth calls
```

### 9.2 Authentication + PRF key derivation
```javascript
passkey.authenticate()
// Triggers WebAuthn assertion with PRF extension
// Returns { aesKey: CryptoKey } — derived from PRF output via HKDF
// aesKey is used to decrypt private keys from IndexedDB for the session
// If PRF not supported: returns { aesKey: null, prfUnsupported: true }
```

### 9.3 Sign gesture
```javascript
passkey.confirmAndSign(privateKey, dataBytes)
// Triggers WebAuthn assertion (biometric prompt) as the deliberate signing act
// On success: calls crypto.sign(privateKey, dataBytes)
// Returns base64 signature
// This is the UX moment — the user consciously confirms "I sign this"
```

**Note:** The biometric prompt for `confirmAndSign` is the legal signing act. The UI must make this explicit — not just a loading spinner. The user must have seen the rendered document *before* this prompt appears.

---

## 10. The Render Pipeline (`signer` object)

For Phase 1, ssd-render-1.0 only.

```javascript
signer.canonicalise(rawText)
// 1. Normalise to NFC
// 2. Normalise line endings to LF
// 3. Strip trailing whitespace from each line
// 4. Ensure single trailing newline
// Returns canonical UTF-8 string

signer.wrap(canonicalText, width = 80)
// Hard wrap at 80 chars, preserving existing newlines
// Returns wrapped string

signer.buildSignatureBlock(fingerprint, name, timestamp)
// Returns the plain text signature block (without the sig bytes — those come after hashing)
// Format:
// ---
// Signed by: {name} [{fingerprint}]
// Timestamp: {timestamp}
// Render spec: ssd-render-1.0
// I rendered this document using the above spec, confirmed the output, and signed it.
// Signature: {placeholder — replaced after signing}
// ---

signer.buildRender(canonicalText, signatureBlockWithoutSig)
// Concatenates canonical text + "\n" + signatureBlock
// Returns full render string (this is what gets hashed and signed)

signer.signDocument(myKeyId, rawText)
// Full signing flow:
// 1. canonicalise(rawText)
// 2. wrap(canonical)
// 3. build signature block placeholder
// 4. buildRender(wrapped, block)
// 5. Display rendered text to user — WAIT for explicit user confirmation
// 6. sha256(render) → manifest hashes
// 7. Build manifest JSON
// 8. passkey.confirmAndSign(privateKey, manifestBytes) → signature
// 9. Insert signature into signature block
// 10. Package into .ssd via artifact.pack()
// 11. Store artifact record in IndexedDB + blob in OPFS
// Returns artifact record
```

---

## 11. Artifact Packaging (`artifact` object)

```javascript
artifact.pack(sourceText, renderText, manifestObj, signatureObj)
// Builds a .ssd ZIP containing:
//   manifest.json
//   source.txt
//   render.txt
//   signature.json
// Returns Uint8Array (ZIP bytes)
// Uses fflate for ZIP creation

artifact.unpack(ssdBytes)
// Parses .ssd ZIP
// Returns { manifest, source, render, signature, chain }

artifact.verify(unpacked)
// Full verification flow:
// 1. Hash manifest.json, render.txt, source.txt → compare to manifest.files
// 2. Verify Ed25519 signature over manifest hash
// 3. Check timestamp within key validity period
// 4. Check key not revoked (via revocation_hint if present)
// 5. Check recheck interval
// Returns verification result object (see §14)
```

---

## 12. OPFS Storage (`opfsStore` object)

Follows the exact same pattern as the Bitcoin Verifier's `opfs` object.

```javascript
opfsStore.write(path, bytes)    // e.g. "artifacts/uuid.ssd"
opfsStore.read(path)            // Returns Uint8Array
opfsStore.delete(path)
opfsStore.list(dir)             // Returns array of filenames
opfsStore.export(path, filename) // Triggers browser download
```

Directories used:
- `artifacts/` — `.ssd` blobs for sent/received artifacts
- `keycards/` — exported key card JSON files
- `imports/` — received artifacts pending verification

---

## 13. Identicon Rendering (`identicon` object)

Phase 1 implementation: a simple deterministic pixel grid rendered to a `<canvas>`. The algorithm is `ssd-identicon-1.0` — defined here, stable, declared in all key cards.

```javascript
identicon.render(seed, canvas, size = 64)
// seed: Uint8Array (32 bytes — SHA-256 of public key)
// canvas: HTMLCanvasElement
// size: pixel dimensions (square)
// Algorithm ssd-identicon-1.0:
//   1. Take seed bytes
//   2. Build a 5x5 boolean grid (mirrored horizontally, so 15 unique bits needed)
//   3. Derive foreground colour from bytes 16–18 (HSL with fixed saturation/lightness)
//   4. Background: always transparent / surface colour
//   5. Render each cell as a square, scaled to fit canvas
// Returns nothing — draws directly to canvas

identicon.renderToDataURL(seed, size = 64)
// Creates offscreen canvas, renders, returns data URL for use in <img> tags

identicon.algorithmId()
// Returns "ssd-identicon-1.0"
```

The algorithm is intentionally simple for v1. The `identicon_algorithm` field in key cards means a future algorithm can be declared without breaking existing keys.

---

## 14. Verification Result Object

Returned by `artifact.verify()` and displayed by the UI.

```javascript
{
  signature_valid: true,
  content_unmodified: true,
  signed_at: "2026-05-02T10:30:00Z",
  render_spec: "ssd-render-1.0",
  signer_fingerprint: "3F2A9C1D",
  signer_name: "J. Smith",           // from key card if known, else null
  signer_known: true,                // in contact_keys
  trust_types: ["peer"],             // types present in chain
  key_expired: false,
  key_revoked: false,
  recheck_due: false,
  recheck_overdue: false,            // interval elapsed
  decryption_logged: false,
  errors: [],
  warnings: ["Key recheck due — last verified 92 days ago"]
}
```

---

## 15. UI Structure and Conventions

Follow the Bitcoin Verifier pattern exactly for navigation and results display.

### 15.1 CSS Custom Properties

```css
:root {
  --bg: #0a0a0a;
  --surface: #141414;
  --surface-2: #1e1e1e;
  --text: #e8e8e8;
  --text-muted: #666;
  --border: #2a2a2a;
  --primary: #c8a96e;        /* wax seal gold */
  --primary-hover: #b8935a;
  --success: #4caf77;
  --warning: #e8a020;
  --danger: #e05050;
  --mono: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
  --shadow: 0 2px 12px rgba(0,0,0,0.4);
  --radius: 6px;
}
```

Dark theme. The gold primary colour references the wax seal metaphor. Clean, utilitarian — this is a security tool, not a consumer app.

### 15.2 Navigation

Top nav buttons, same as Bitcoin Verifier. Sections hidden/shown by `showSection(id)`.

**Phase 1 sections:**
- `keyring` — view and manage own keys (default landing)
- `newKey` — generate a new keypair
- `compose` — write and sign a document
- `verify` — verify a received artifact
- `settings` — passkey setup, app preferences

### 15.3 Results Display

Same `#results` div pattern. `ui.display(text)` appends, `ui.clear()` clears. For verification results, `ui.displayVerification(resultObj)` renders a structured view with icons.

```
✓ Signature valid
✓ Content unmodified
✓ Signed: 2026-05-02 10:30 UTC  [ssd-render-1.0]
◎ Key: 3F2A9C1D — J. Smith (known contact)
◎ Trust: peer (verified via QR scan, 2026-01-15)
⚠ Key recheck due — last verified 92 days ago
```

### 15.4 The Signing Confirmation Screen

This is the most important UX moment in the application. When a user signs a document:

1. The compose section is replaced by a full-screen render preview — the exact text that will be signed, rendered in a monospace font, with the signature block appended (signature field shown as `[pending]`)
2. A prominent message: **"This is exactly what you are signing. Once confirmed, this cannot be changed."**
3. A single large button: **"Confirm and Sign"** — which triggers the biometric passkey prompt
4. A **"Cancel"** link in small text
5. After biometric confirmation: a brief "Signing..." state, then the result screen showing the completed artifact

This flow must not be shortcuttable. No auto-signing. The user reads, then acts.

### 15.5 Key Card Display

Each key in the keyring is displayed as a card:
- Identicon (canvas, 48×48)
- Name and fingerprint (8 chars)
- Key type badge (Personal / Work / etc.)
- Expiry / recheck status
- Action buttons: Export Key Card, Sign Document, [Revoke]

---

## 16. Key Card Export Format

A key card is a JSON object, self-signed by the key it describes. It is the unit of key exchange.

```json
{
  "version": "1.0",
  "name": "J. Smith",
  "fingerprint": "3F2A9C1D",
  "public_key": "<base64 Ed25519 public key>",
  "signing_algorithm": "Ed25519",
  "identicon_algorithm": "ssd-identicon-1.0",
  "self_image": null,
  "expires": null,
  "recheck_interval_days": 90,
  "revocation_hint": null,
  "issued": "2026-05-02T10:30:00Z",
  "self_signed": "<base64 Ed25519 signature over the above fields canonicalised as sorted JSON>",
  "chain": []
}
```

The `self_signed` signature covers the JSON of all fields above it, keys sorted alphabetically, no whitespace. This makes the signature deterministic and verifiable without ambiguity.

Export options (Phase 1):
- Download as `.json` file
- Copy as text (for pasting into any channel)
- QR code display (Phase 2)

---

## 17. Phase Gates

### Phase 1 Gate — Core Loop ✓

Before Phase 2 begins, all of the following must work end-to-end:

- [ ] Passkey registration completes and stores `credentialId` in settings
- [ ] Keypair generates, private key encrypts to IndexedDB, public key and metadata stored
- [ ] Identicon renders correctly and deterministically for a given fingerprint
- [ ] Key card exports as valid self-signed JSON
- [ ] Key card self-signature verifies correctly
- [ ] Compose → preview → biometric confirm → signed artifact flow completes
- [ ] `.ssd` ZIP is produced and contains the correct files
- [ ] Hashes in manifest match actual file contents
- [ ] Verify section accepts a `.ssd` file and returns a correct verification result
- [ ] App installs as a PWA and works offline after first load

### Phase 2 Gate — Key Exchange ✓

- [ ] QR code generates from key card JSON
- [ ] QR code scans and imports a contact key correctly
- [ ] Paste import parses and validates a key card
- [ ] Recheck interval tracking works — overdue keys flagged
- [ ] Revocation notice (signed statement) is parsed and marks a key revoked
- [ ] GETKEY response generation produces a valid key card
- [ ] Device sync export produces an encrypted blob; import on a new session restores keyring

### Phase 3 Gate — Encryption ✓

- [ ] Single-recipient encryption and decryption round-trip correctly
- [ ] Multi-recipient works — each recipient can independently decrypt
- [ ] Author can re-read their own encrypted artifact
- [ ] Logged access mode declared in manifest and communicated to recipient before open
- [ ] Encrypted `.ssd` artifacts verify correctly (signature covers encrypted content)

### Phase 4 Gate — Integration ✓

- [ ] OS file handler routes `.ssd` files to the app
- [ ] Share target receives `.ssd` artifacts from other apps
- [ ] Receipt countersignature appends to `chain/` without breaking original signature
- [ ] Full verify + decrypt + display flow works for all encryption modes

---

## 18. Error Handling

Follow the Bitcoin Verifier pattern: catch at the operation level, display human-readable messages via `ui.display()`, log full errors to console. Never let an unhandled rejection surface to the user as a blank screen.

Specific cases to handle gracefully:
- WebAuthn not available (HTTPS required — show setup instructions)
- PRF extension not supported (fall back, note in UI)
- IndexedDB quota exceeded (prompt export before proceeding)
- `.ssd` file unreadable / corrupt (show specific parse error)
- Signature verification failure (show clearly — do not soft-fail)
- Key not found for fingerprint (show fingerprint, offer to import key card)

---

## 19. Security Constraints

These are non-negotiable. Do not work around them even if it seems convenient.

- Private key bytes must never appear in `localStorage` or unencrypted IndexedDB
- Private key must not remain in memory across sessions — re-derive from PRF on each session start
- The signing confirmation screen cannot be bypassed programmatically
- Verification failures must be shown prominently — not logged and ignored
- The `self_signed` field in a key card must be verified before any key is trusted
- OPFS artifact blobs are the canonical record — IndexedDB holds metadata only; the two must not get out of sync

---

## 20. What Claude Code Should NOT Do

- Do not add a backend, API, or server component
- Do not use React, Vue, or any component framework
- Do not use npm or a package manager — CDN only
- Do not store anything sensitive in `localStorage` — IndexedDB only under encryption
- Do not auto-sign anything — every signing operation requires explicit biometric confirmation
- Do not implement EUDI / government identity — the slot exists in the data model but leave it empty
- Do not implement CommonMark rendering — Phase 1 is plain text only
- Do not implement Canvas-based PNG rendering — deferred to a future render spec version
- Do not add analytics, telemetry, or any network call other than CDN library loads and explicit user-initiated key/artifact retrieval

---

## 21. First Prompt for Claude Code

> Implement Phase 1 of the SSD PWA as specified in SSD-Program-Spec.md. Produce a single `index.html` file containing all HTML, CSS, and JavaScript. Follow the coding style of the reference PWA (index.html — Bitcoin Block Verifier) already in this project.
>
> Phase 1 deliverables:
> - Passkey registration and authentication (WebAuthn, with PRF extension for keyring encryption)
> - Ed25519 keypair generation via WebCrypto
> - IndexedDB keyring with encrypted private key storage
> - Identicon rendering (ssd-identicon-1.0 — 5×5 mirrored pixel grid from SHA-256 of public key)
> - Key card export (self-signed JSON)
> - Plain text document compose → canonical render preview → biometric confirm → .ssd artifact
> - .ssd artifact verification (signature, hash, render spec check)
> - Dark theme UI with gold accent (#c8a96e), sections: keyring / newKey / compose / verify / settings
> - Service worker and manifest as specified
>
> Stop at the Phase 1 gate. Do not begin Phase 2 work.
