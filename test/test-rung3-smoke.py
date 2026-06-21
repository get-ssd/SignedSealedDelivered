"""
test-rung3-smoke.py — rung-3 (PIN-only) smoke test for the SSD PWA.

Covers: rung-3 setup (no WebAuthn), PIN-derived KEK via PBKDF2, O: + D: mint,
        sign round-trip, and re-unlock with PIN after lock.

No virtual authenticator is injected. The rung is set directly to 3 (simulating
the grade-detection path where WebAuthn is unavailable).

Run from repo root:
  py ssd.signed-sealed-delivered/test/test-rung3-smoke.py
"""
import asyncio, subprocess, sys, os, time
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')
from pathlib import Path
from playwright.async_api import async_playwright

PWA_DIR = Path(__file__).parent.parent
PORT    = 8098
PWA_URL = f"http://localhost:{PORT}/?mock"

BRAVE_PATH  = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

def find_browser():
    for path in [BRAVE_PATH, CHROME_PATH]:
        if os.path.exists(path):
            return path
    return None

passed = failed = 0

def ok(msg):
    global passed; passed += 1; print(f"  ✓ {msg}")

def fail(msg):
    global failed; failed += 1; print(f"  ✗ {msg}", file=sys.stderr)

TEST_JS = """
async () => {
  const PIN = '1234';

  // Simulate rung-3 registration: no WebAuthn call, just store rung + salt
  await keyring.setRung(3);
  const salt = crypto.getRandomValues(new Uint8Array(32));
  await db.put('settings', { key: 'pin_salt', value: cryptoOps.b64enc(salt) });

  // Unlock with PIN (rung 3: PBKDF2 only, no WebAuthn)
  await keyring.unlock(PIN);
  if (!keyring._sessionKey) throw new Error('_sessionKey not set after rung-3 unlock');

  // Mint O: and D: keys
  const oKey = await keyring.createKey('O:test-owner-rung3');
  const dKey = await keyring.createKey('D:test-device-rung3');

  // Sign + verify
  const payload = new TextEncoder().encode('SSD rung-3 smoke test payload — 2026-06-22');
  const oPriv   = await keyring.getPrivateKey(oKey.id);
  const sig     = await cryptoOps.sign(oPriv, payload);
  const oPub    = await cryptoOps.importPublicKeyB64(oKey.public_key_b64);
  const valid   = await cryptoOps.verify(oPub, sig, payload);

  // Lock + re-unlock + re-sign (persistence)
  keyring._sessionKey = null;
  await keyring.unlock(PIN);
  const oPriv2 = await keyring.getPrivateKey(oKey.id);
  const sig2   = await cryptoOps.sign(oPriv2, payload);
  const valid2 = await cryptoOps.verify(oPub, sig2, payload);

  // Wrong PIN must fail
  keyring._sessionKey = null;
  let wrongPinFailed = false;
  try {
    await keyring.unlock('9999');
    await keyring.getPrivateKey(oKey.id); // should throw (wrong key)
  } catch (_) {
    wrongPinFailed = true;
  }
  keyring._sessionKey = null;

  return { rung: 3, oHash8: oKey.hash8, dHash8: dKey.hash8, valid, valid2, wrongPinFailed };
}
"""

async def main():
    server = subprocess.Popen(
        [sys.executable, '-m', 'http.server', str(PORT), '--directory', str(PWA_DIR)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)

    exe = find_browser()
    browser_name = "Brave" if exe and "Brave" in exe else \
                   "Chrome" if exe else "Playwright bundled Chromium"
    print(f"\nSSD rung-3 PIN-only smoke test — {browser_name}\n")
    print("1. Rung-3 setup (no WebAuthn, PIN-derived KEK)")
    print("2. Keyring unlock via PBKDF2 PIN")
    print("3. O: + D: key mint")
    print("4. Sign round-trip")
    print("5. Lock + re-unlock + re-sign")
    print("6. Wrong PIN rejection")
    print()

    try:
        async with async_playwright() as p:
            launch_kwargs = dict(executable_path=exe) if exe else {}
            browser = await p.chromium.launch(**launch_kwargs)
            context = await browser.new_context()
            page    = await context.new_page()

            await page.goto(PWA_URL)
            await page.wait_for_load_state("domcontentloaded")

            try:
                r = await page.evaluate(TEST_JS)
            except Exception as e:
                fail(f"Test sequence threw: {e}")
                r = None

            if r:
                ok(f"Rung 3 registered (no WebAuthn)")
                ok(f"Keyring unlocked via PBKDF2 PIN")
                ok(f"O: key minted (hash8={r['oHash8']})")
                ok(f"D: key minted (hash8={r['dHash8']})")
                if r['valid']:         ok("Sign + verify round-trip valid")
                else:                  fail("Sign + verify round-trip FAILED")
                if r['valid2']:        ok("Re-unlock + re-sign valid (persistence)")
                else:                  fail("Re-unlock + re-sign FAILED")
                if r['wrongPinFailed']: ok("Wrong PIN correctly rejected")
                else:                  fail("Wrong PIN was NOT rejected")

            await browser.close()
    finally:
        server.terminate()

    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)

asyncio.run(main())
