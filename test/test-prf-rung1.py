"""
test-prf-rung1.py — headless rung-1 (PRF) test for the SSD PWA.

Covers: PRF-derived AES key from virtual authenticator, O: + D: key mint,
        sign round-trip, and keyring persistence (re-unlock after lock).

Uses Chrome's CDP virtual authenticator API (hasPrf: true) via Playwright.
Targets Brave or Chrome depending on what's found. Falls back to bundled Chromium.
Firefox cannot run this test (CDP virtual authenticator is Chromium-only).

Run from repo root:
  py ssd.signed-sealed-delivered/test/test-prf-rung1.py

Requires:
  - Python playwright  (pip install playwright)
  - Brave or Chrome installed (or bundled Chromium via `playwright install chromium`)
  - No external server needed — starts its own on port 8097.
"""
import asyncio, json, subprocess, sys, os, time, signal
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')
from pathlib import Path
from playwright.async_api import async_playwright

PWA_DIR  = Path(__file__).parent.parent
PORT     = 8097
PWA_URL  = f"http://localhost:{PORT}/?mock"

BRAVE_PATH  = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

def find_browser():
    for path in [BRAVE_PATH, CHROME_PATH]:
        if os.path.exists(path):
            return path
    return None  # falls back to Playwright bundled Chromium

# ── result tracking ───────────────────────────────────────────────────────────

passed = failed = 0

def ok(msg):
    global passed
    passed += 1
    print(f"  ✓ {msg}")

def fail(msg):
    global failed
    failed += 1
    print(f"  ✗ {msg}", file=sys.stderr)

# ── test sequence (runs inside the PWA page context) ─────────────────────────

TEST_JS = """
async () => {
  // Step 1: register passkey (virtual authenticator, hasPrf:true)
  const reg = await passkey.register('test-harness');
  if (!reg.prfSupported) throw new Error('PRF not supported by virtual authenticator');

  // Step 2: unlock keyring — PRF authenticate → HKDF → AES-GCM session key
  await keyring.unlock();
  if (!keyring._sessionKey) throw new Error('keyring._sessionKey not set after unlock');

  // Step 3: mint O: and D: keys (the O:/D: mint pair)
  const oKey = await keyring.createKey('O:test-owner');
  const dKey = await keyring.createKey('D:test-device');

  // Step 4: sign test content with O: key
  const payload = new TextEncoder().encode('SSD rung-1 headless test payload — 2026-06-21');
  const oPriv   = await keyring.getPrivateKey(oKey.id);
  const sig     = await cryptoOps.sign(oPriv, payload);

  // Step 5: verify signature against stored public key
  const oPub  = await cryptoOps.importPublicKeyB64(oKey.public_key_b64);
  const valid = await cryptoOps.verify(oPub, sig, payload);

  // Step 6: lock and re-unlock (persistence: keys survive re-auth)
  keyring._sessionKey = null;
  await keyring.unlock();
  const oPriv2 = await keyring.getPrivateKey(oKey.id);
  const sig2   = await cryptoOps.sign(oPriv2, payload);
  const valid2 = await cryptoOps.verify(oPub, sig2, payload);

  return {
    prfSupported:    reg.prfSupported,
    credId:          reg.credIdB64.slice(0, 8),
    oKeyHash8:       oKey.hash8,
    dKeyHash8:       dKey.hash8,
    signatureValid:  valid,
    reAuthValid:     valid2,
  };
}
"""

# ── main ─────────────────────────────────────────────────────────────────────

async def main():
    # Start the PWA HTTP server
    server = subprocess.Popen(
        [sys.executable, '-m', 'http.server', str(PORT), '--directory', str(PWA_DIR)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)  # brief wait for server to bind

    exe = find_browser()
    browser_name = "Brave" if exe and "Brave" in exe else \
                   "Chrome" if exe else "Playwright bundled Chromium"
    print(f"\nSSD rung-1 PRF headless test — {browser_name}\n")

    try:
        async with async_playwright() as p:
            launch_kwargs = dict(executable_path=exe) if exe else {}
            browser  = await p.chromium.launch(**launch_kwargs)
            context  = await browser.new_context()
            page     = await context.new_page()

            # Enable CDP virtual authenticator before navigating
            cdp = await context.new_cdp_session(page)
            await cdp.send("WebAuthn.enable", {"enableUI": False})
            auth = await cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
                "protocol":           "ctap2",
                "transport":          "internal",
                "hasResidentKey":     True,
                "hasUserVerification": True,
                "isUserVerified":     True,
                "hasPrf":             True,
            }})
            auth_id = auth["authenticatorId"]

            await page.goto(PWA_URL)
            await page.wait_for_load_state("domcontentloaded")

            # Run the test sequence inside the PWA
            print("1. Passkey register + PRF")
            print("2. Keyring unlock (PRF -> HKDF -> AES)")
            print("3. O: + D: key mint")
            print("4. Sign round-trip")
            print("5. Lock + re-unlock + re-sign")
            print()

            try:
                r = await page.evaluate(TEST_JS)
            except Exception as e:
                fail(f"Test sequence threw: {e}")
                r = None

            if r:
                ok(f"PRF supported by virtual authenticator (cred ...{r['credId']})")
                ok(f"Keyring unlocked via PRF → AES-GCM session key")
                ok(f"O: key minted (hash8={r['oKeyHash8']})")
                ok(f"D: key minted (hash8={r['dKeyHash8']})")
                if r['signatureValid']:  ok("Sign + verify round-trip valid")
                else:                   fail("Sign + verify round-trip FAILED")
                if r['reAuthValid']:    ok("Re-unlock + re-sign valid (persistence)")
                else:                   fail("Re-unlock + re-sign FAILED")

            await browser.close()
    finally:
        server.terminate()

    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)

asyncio.run(main())
