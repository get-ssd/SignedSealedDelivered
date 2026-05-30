# SSD Tick — Test Guide

## 1. Start the PWA

Double-click `00-startup.bat` in the `SignedSealedDelivered\` directory.

Open **http://localhost:8080** in your normal browser (Chrome/Brave — not Chromium, WebAuthn works better here for setup).

---

## 2. First-time PWA setup

**Me → Passkey & Devices**
- Click **Register Passkey** — Windows Hello / PIN prompt will appear

**Me → Keys → + (fab button)**
- Name: `O:Your Name` (Owner key)
- Click **Generate Keypair** → biometric prompt → key appears in list

**Export your key card**
- Click **Export Key Card** on your key
- It downloads a `.json` file AND shows the JSON in the page — **click the JSON block to copy it**
- Keep this JSON — you need it for Tick

---

## 3. Load Tick into Chromium

In Chromium: `chrome://extensions/` → Developer mode → **Reload** the SSD Tick extension (refresh icon).

Click the puzzle piece in the toolbar → pin SSD Tick.

**Import your key into Tick:**
- Click the SSD Tick icon → popup opens
- Paste the key card JSON → **Import key card**
- Your key should appear listed in the popup

---

## 4. Sign a test post

In the PWA at **http://localhost:8080**:

- **Documents → Social post**
- **Signing key:** select your key
- **Identity hint:** `https://example.com/key.json` *(placeholder — doesn't need to resolve for the known-key test)*
- **Post text:** type a test message, e.g.:

  ```
  This is a test of SSD signed posting. The content is exactly what I confirmed before signing.
  ```

- Click **Review →** — confirm the canonical form of your text
- Click **Sign** → biometric prompt → token generated
- Click **Copy to clipboard**

You now have something like:

```
This is a test of SSD signed posting. The content is exactly what I confirmed before signing.

—SSD·AB12CD34·https://example.com/key.json·MEQCIBx...88chars...·2026-05-30T10:00Z—
```

---

## 5. Post to Facebook

Open Chromium (with Tick installed and pinned).

Go to a Facebook private message thread or your own timeline.

Paste the copied text into the message/post box and send it.

---

## 6. Validate in Tick

Once the post appears on the page:

- The token text should have a **green box** around it (key is in Tick's store)
- Click the green box → inline card appears
- Card shows signer name, then **Verifying…** → resolves to **✓ Signed** (sig valid)

**If ✗ appears (sig invalid):** Facebook modified the text — likely whitespace or emoji normalisation. Check `F12 → Console` for `[SSD Tick]` logs.

---

## 7. Test the unknown-signer flow

To test the amber → fetch flow, the key card JSON needs a real public URL:

1. Go to [gist.github.com](https://gist.github.com) → new public gist → paste key card JSON → save
2. Click **Raw** → copy that URL
3. In the PWA, sign a new post using the raw gist URL as the identity hint
4. Clear Tick's keystore (popup → delete the key)
5. Paste the new post into Facebook
6. Token shows **amber** → click → **Fetch & verify key** → fetches from gist, verifies self-sig → **Trust this signer**
7. Token turns green, sig verified

---

## Troubleshooting

| Symptom | Likely cause |
|---------|-------------|
| Token not highlighted | Extension not reloaded — refresh extension then reload the tab |
| Sig shows ✗ | Facebook modified text (whitespace, emoji) — check console |
| Fetch fails on amber token | URL not publicly accessible or CORS issue |
| Card shows ? (can't verify) | Tick couldn't find post text in DOM — check console |
