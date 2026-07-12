"""
test-wizard-rungs.py — full Home-wizard walk at every rung (SSD PWA, ssd-v78).

The wizard is phone-first (rung 1, PRF passkey), but it must stay walkable on
devices that land at rung 2 or rung 3. This test drives the real UI — it clicks
the actual wizard buttons and fills the actual PIN overlay — from a clean DB
through all three steps, once per rung:

  Rung 1  virtual authenticator with hasPrf: true   → passkey, no PIN
  Rung 2  virtual authenticator with hasPrf: false  → passkey + PIN
  Rung 3  WebAuthn removed from the page            → PIN only

At each rung it asserts the ssd-v78 behaviour that the rung-3-only test
(test-home-wizard-gate.py) cannot cover through the passkey path:

  1. Step 1 completes and step 2 becomes available
  2. Step 2 mints O: + D: and — the defect-1 regression — the wizard STAYS OPEN
     with step 3 current and enabled, rather than collapsing on "identity exists"
  3. Step 3 (share beacon) completes, sets home_wizard_done, and collapses Home
     to the status view

Uses Chrome's CDP virtual authenticator API via Playwright (Chromium-only), the
same mechanism as test-prf-rung1.py. Each rung runs in its own browser context
against a fresh ?mock DB.

Run from repo root:
  py ssd.signed-sealed-delivered/test/test-wizard-rungs.py
"""
import asyncio, subprocess, sys, os, time
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')
from pathlib import Path
from playwright.async_api import async_playwright

PWA_DIR = Path(__file__).parent.parent
PORT    = 8095
PWA_URL = f"http://localhost:{PORT}/?mock"
PIN     = '1234'

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

# Rung 3: no WebAuthn at all. passkey.register() throws, app.registerPasskey()
# catches anything that is not a user cancellation and drops to rung 3.
NO_WEBAUTHN_JS = """
delete window.PublicKeyCredential;
Object.defineProperty(navigator, 'credentials', {
  configurable: true,
  value: { create: () => Promise.reject(new Error('WebAuthn unavailable')),
           get:    () => Promise.reject(new Error('WebAuthn unavailable')) },
});
"""

STATE_JS = """
async () => {
  const vis = id => getComputedStyle(document.getElementById(id)).display !== 'none';
  const cls = id => document.getElementById(id).className;
  const dis = id => document.getElementById(id).disabled;
  const rung = await db.get('settings', 'keyring_rung');
  const flag = await db.get('settings', 'home_wizard_done');
  const keys = await db.getAll('my_keys');
  return {
    wizardVisible: vis('home-wizard'), statusVisible: vis('home-status'),
    wz3: cls('wz-3'), btn2Disabled: dis('wz-btn-2'), btn3Disabled: dis('wz-btn-3'),
    rung: rung ? rung.value : null,
    flag: flag ? flag.value : null,
    oKeys: keys.filter(k => k.name.startsWith('O:') && !k.is_revoked).length,
    dKeys: keys.filter(k => k.name.startsWith('D:') && !k.is_revoked).length,
  };
}
"""

async def fill_pin_if_prompted(page):
    """Rungs 2 and 3 raise the PIN overlay; rung 1 never does."""
    try:
        await page.wait_for_selector('#_pin-overlay-input', timeout=3000)
    except Exception:
        return False
    await page.fill('#_pin-overlay-input', PIN)
    await page.click('#_pin-overlay-ok')
    await page.wait_for_selector('#_pin-overlay-input', state='detached')
    return True

async def walk_wizard(context, rung_label, has_prf, expected_rung, expect_pin):
    page = await context.new_page()

    if has_prf is None:
        await page.add_init_script(NO_WEBAUTHN_JS)
    else:
        # The virtual authenticator is scoped to the page its CDP session is
        # attached to — it must be this page, and it must exist before navigation.
        cdp = await context.new_cdp_session(page)
        await cdp.send("WebAuthn.enable", {"enableUI": False})
        await cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
            "protocol":            "ctap2",
            "transport":           "internal",
            "hasResidentKey":      True,
            "hasUserVerification": True,
            "isUserVerified":      True,
            "hasPrf":              has_prf,
        }})

    await page.goto(PWA_URL)
    await page.wait_for_function(
        "() => document.getElementById('home-loading').style.display === 'none'")

    print(f"\n{rung_label}")

    # ── Step 1 — register passkey (or fall back to PIN) ──────────────────────
    await page.click('#wz-btn-1')
    pin_seen_step1 = await fill_pin_if_prompted(page)
    await page.wait_for_function("() => !document.getElementById('wz-btn-2').disabled",
                                 timeout=10000)
    s = await page.evaluate(STATE_JS)

    if s['rung'] == expected_rung:
        ok(f"Step 1 complete — landed at rung {s['rung']}")
    else:
        fail(f"Expected rung {expected_rung}, got {s['rung']}")
    if pin_seen_step1 == expect_pin:
        ok(f"PIN {'set during step 1' if expect_pin else 'correctly not requested'}")
    else:
        fail(f"PIN prompt mismatch: expected {expect_pin}, saw {pin_seen_step1}")

    # ── Step 2 — create identity ─────────────────────────────────────────────
    await page.click('#wz-btn-2')
    await fill_pin_if_prompted(page)      # rungs 2 and 3 need the PIN to unlock
    await page.wait_for_function("() => !document.getElementById('wz-btn-3').disabled",
                                 timeout=15000)
    s = await page.evaluate(STATE_JS)

    if s['oKeys'] == 1 and s['dKeys'] == 1:
        ok("Step 2 complete — O: and D: keys minted")
    else:
        fail(f"Identity wrong: {s['oKeys']} O: key(s), {s['dKeys']} D: key(s)")

    # The defect-1 regression: identity now exists, and the wizard must NOT have
    # collapsed on it. Step 3 has to be sitting there, current and clickable.
    if s['wizardVisible'] and not s['statusVisible'] and 'current' in s['wz3'] and not s['btn3Disabled']:
        ok("Wizard still open at step 3 (did not collapse on identity)")
    else:
        fail(f"Step 3 not reachable: wizard={s['wizardVisible']} status={s['statusVisible']} "
             f"wz-3='{s['wz3']}' btn3Disabled={s['btn3Disabled']}")
    if s['flag'] is None:
        ok("Completion flag still unset before step 3 is run")
    else:
        fail(f"home_wizard_done set too early: {s['flag']!r}")

    # ── Step 3 — share beacon ────────────────────────────────────────────────
    await page.click('#wz-btn-3')
    await page.wait_for_function(
        "() => getComputedStyle(document.getElementById('home-status')).display !== 'none'",
        timeout=10000)
    s = await page.evaluate(STATE_JS)

    if s['flag'] is True:
        ok("Step 3 complete — home_wizard_done set")
    else:
        fail(f"home_wizard_done not set: {s['flag']!r}")
    if s['statusVisible'] and not s['wizardVisible']:
        ok("Wizard collapsed to the status view")
    else:
        fail(f"Not collapsed: wizard={s['wizardVisible']} status={s['statusVisible']}")

    await page.close()

async def main():
    server = subprocess.Popen(
        [sys.executable, '-m', 'http.server', str(PORT), '--directory', str(PWA_DIR)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)

    exe = find_browser()
    browser_name = "Brave" if exe and "Brave" in exe else \
                   "Chrome" if exe else "Playwright bundled Chromium"
    print(f"\nSSD Home wizard — full walk at every rung — {browser_name}")
    print("Each rung: register → create identity → share beacon, driving the real UI.")

    try:
        async with async_playwright() as p:
            launch_kwargs = dict(executable_path=exe) if exe else {}
            browser = await p.chromium.launch(**launch_kwargs)

            for rung_label, has_prf, expected_rung, expect_pin in [
                ("Rung 1 — passkey with PRF (phone case)",        True,  1, False),
                ("Rung 2 — passkey without PRF, PIN fallback",    False, 2, True),
                ("Rung 3 — no WebAuthn, PIN only",                None,  3, True),
            ]:
                context = await browser.new_context()
                await context.grant_permissions(['clipboard-read', 'clipboard-write'])
                try:
                    await walk_wizard(context, rung_label, has_prf, expected_rung, expect_pin)
                except Exception as e:
                    fail(f"{rung_label}: walk threw — {e}")
                await context.close()

            await browser.close()
    finally:
        server.terminate()

    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)

asyncio.run(main())
