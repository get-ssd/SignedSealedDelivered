# Signed, Sealed, Delivered

Cryptographic document signing and verification that runs entirely in the browser. No server, no accounts, no cloud.

Part of the SSD ecosystem:
- **Signed, Sealed, Delivered** (this repo) — the signing tool. Author-side: key management, signing, sealing, verification.
- **ssd-tick** — browser extension for readers. Detects and verifies SSD-signed content on social media.
- **ssd-vault** — publishing hub (planned). One signed artifact, many platform renditions.

## Current State

**v45** — full identity model in place: device keys, owner keys, contact pairing, and secure cross-device key transfer via sealed documents.

## How It Works

### Identity model

Two levels of signing identity:

- **Device key** (`D:` prefix) — auto-generated when you name a device. Tied to one browser instance. Contacts know you by this key.
- **Owner key** (`O:` prefix) — a personal key representing you across all your devices. Created manually, then transferred to other devices via sealed document.

Each browser instance manages its own keyring, protected by a passkey (WebAuthn PRF). Private keys never leave the device unencrypted.

### Key naming convention

| Format | Meaning | Example |
|--------|---------|---------|
| `D:name` | Device key — for this browser/device | `D:Alice's iPhone` |
| `O:name` | Owner key — personal identity across devices | `O:Alice` |
| (plain) | Legacy or manually named key | `Work` |

The keyring displays a **Device** or **Owner** badge based on the prefix.

### Setup on a new device

1. Register a passkey (Settings → Passkey)
2. Name the device (Settings → Devices) — the app offers to generate `D:<name>` automatically
3. Generate an encryption key (Settings → Encryption Key)
4. Share your key card QR with contacts so they can send you sealed documents

### Cross-device pairing

Each device shows its key card as a QR code or exported JSON. Scan another device's key card to add it as a contact — you can then seal documents addressed to it, and it can seal documents addressed to you. No central authority.

### Owner key transfer

To use the same owner key on multiple devices:

1. **Pair first** — both devices must have scanned each other's key card
2. On the source device: Me → Keys → tap the `O:` key → **Transfer to device…** → pick the destination
3. The app signs the transfer with your device key (`D:`), seals it for the destination's encryption key, and delivers a `.ssd` file
4. On the destination device: open the file — the app verifies five security checks before showing the import prompt:
   - Outer envelope signature valid
   - Inner document signature valid
   - Content hash intact
   - Signer is a known, non-quarantined contact
   - Outer and inner signers match
5. All checks pass → **Import key** button appears, naming the sender

A tampered or unsigned transfer is hard-rejected — no import UI is shown.

### Sealed documents

Documents can be encrypted to one or more recipients by their public encryption key. Tap the **Sealed ▾** badge on any document in the list to see who it was addressed to.

Recipients can include yourself (to keep a readable copy), contacts, or other devices you own.

## Features

- **Passkey registration** — WebAuthn with PRF extension for keyring encryption
- **Ed25519 keypairs** — named signing identities (`D:` / `O:` convention), stored encrypted in IndexedDB
- **Key card** — self-signed JSON exportable as QR or file; verifiable without the app
- **Device naming** — naming a device auto-offers a matching `D:` key; rename offers rename-or-fresh-keypair
- **Drafts** — compose and save documents before signing
- **Sign** — compose → canonical render → biometric confirmation → `.ssd` artifact
- **Seal / Reseal** — encrypt artifacts to one or more recipients by public key
- **Sealed recipients** — tap the badge on any sealed document to see who it was addressed to
- **Owner key transfer** — securely copy an `O:` key to another device via a signed, sealed transfer document
- **Deliver** — download or share `.ssd` files from the Documents list
- **Verify** — drag-and-drop `.ssd` files; checks hashes, Ed25519 signature, and handles key transfer imports
- **Contacts** — import key cards via QR scan or paste; quarantine management
- **Identicons** — deterministic 5×5 pixel grid from SHA-256 of public key
- **Pluggable render engines** — `ssd-render-1.0` (text), `ssd-json-1.0` (JSON), `ssd-key-transfer-1.0` (key transfer)
- **PWA** — installs offline, service worker caches shell

## Running

Served from GitHub Pages. To run locally — no build step, just serve over HTTP/HTTPS:

```
py -m http.server 8080
```

Open `http://localhost:8080`. **WebAuthn requires localhost or HTTPS** — `file://` URLs will not work.

## File Structure

```
index.html          — entire application (HTML + CSS + JS)
manifest.json       — PWA manifest
service-worker.js   — cache-first shell, network-first CDN
favicon.ico
icon-192.png
icon-512.png
generate-icons.py   — run once to regenerate icons if needed
docs/               — specification
```

## Artifact Format (`.ssd` files)

A ZIP archive containing:
- `manifest.json`  — metadata, render spec, SHA-256 hashes of content files
- `signature.json` — Ed25519 signature over `manifest.json` bytes
- Engine-specific content files (e.g. `source.txt` + `render.txt`, or `source.json`)

Encrypted artifacts are wrapped in a `sealed-enc-v1` JSON envelope with per-recipient X25519 key encapsulation slots before the ZIP payload.

Key transfer artifacts use `render_spec: ssd-key-transfer-1.0` and carry the owner key's private bytes inside the encryption envelope.

## Architecture

All JavaScript lives in `index.html` as named const objects:
`db` · `opfsStore` · `cryptoOps` · `passkey` · `keyring` · `signer` · `renderEngines` · `artifact` · `identicon` · `ui` · `app`

All cryptography uses the native WebCrypto API. Storage is IndexedDB (metadata) + OPFS (artifact blobs). Nothing sensitive is stored unencrypted or in localStorage.

## Roadmap

| Phase | Goal | Status |
|-------|------|--------|
| 1 | Core loop — sign, verify, key management | ✅ Complete |
| 2 | Key exchange — QR codes, paste import, contact keyring | ✅ Complete |
| 3 | Encryption — per-recipient sealed artifacts | ✅ Complete |
| 4 | Identity model — D:/O: keys, device naming, owner key transfer | ✅ Complete |
| 5 | Social layer — compact inline token format for social media posts, key bundle documents for Tick import | Planned |
| 6 | Vault integration — generate platform renditions from a single signed artifact | Planned |

## Browser Requirements

- Chrome 113+ / Edge 113+
- Firefox 130+
- Safari 17+

Ed25519 and WebAuthn PRF are required for full functionality.
