"""
test-home-wizard-gate.py — Home wizard first-run state test for the SSD PWA (ssd-v78).

Covers the two defects fixed in ssd-v78:
  Defect 1 — step 3 (share beacon) was unreachable: the wizard collapsed on
             "identity exists", which is step 2's own product.
  Defect 2 — flash-of-wizard: the wizard was the static default paint and was
             collapsed only after the async identity read resolved.

  1. Seeded DB (identity present, home_wizard_done absent) → wizard renders,
     resumed at step 3: steps 1 and 2 show done and their buttons stay disabled
     (not re-runnable), step 3 is current and its button is enabled.
  2. Resuming re-mints nothing — the my_keys set is identical after the reload.
  3. shareBeacon() sets home_wizard_done and collapses Home to the status view.
  4. With the flag set, a fresh load renders the status view and the wizard
     element never paints — sampled on every animation frame from before the
     first page script runs (this is the flash assertion).

Runs against the real DB name (not ?mock) because the state must survive a
reload; the Playwright browser context is ephemeral, so the DB is still isolated.

Passkey registration (rung 1/2) needs a real WebAuthn round-trip and is a
user-testing handoff. This test seeds rung 3 (PIN-only), the same approach as
test-rung3-smoke.py.

Run from repo root:
  py ssd.signed-sealed-delivered/test/test-home-wizard-gate.py
"""
import asyncio, subprocess, sys, os, time
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')
from pathlib import Path
from playwright.async_api import async_playwright

PWA_DIR = Path(__file__).parent.parent
PORT    = 8096
PWA_URL = f"http://localhost:{PORT}/"

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

# Sample #home-wizard visibility on every animation frame, starting before any
# page script has run — so a wizard painted for even one frame is caught.
FRAME_SAMPLER_JS = """
window.__wizardVisibleFrames = 0;
window.__framesSampled = 0;
(function sample() {
  const w = document.getElementById('home-wizard');
  if (w) {
    window.__framesSampled++;
    if (getComputedStyle(w).display !== 'none') window.__wizardVisibleFrames++;
  }
  requestAnimationFrame(sample);
})();
"""

# Seed: rung-3 keyring + O: and D: keys. Deliberately does NOT set
# home_wizard_done — this is the "signed up but never shared a beacon" user.
SEED_JS = """
async () => {
  await keyring.setRung(3);
  const salt = crypto.getRandomValues(new Uint8Array(32));
  await db.put('settings', { key: 'pin_salt', value: cryptoOps.b64enc(salt) });
  await keyring.unlock('1234');

  const oKey = await keyring.createKey('O:test-owner');
  const dKey = await keyring.createKey('D:test-device');
  await db.put('my_keys', { ...oKey, signed_card: await keyring.exportKeyCard(oKey.id) });
  await db.put('my_keys', { ...dKey, signed_card: await keyring.exportKeyCard(dKey.id) });

  const flag = await db.get('settings', 'home_wizard_done');
  return { keyIds: (await db.getAll('my_keys')).map(k => k.id).sort(), flagSeeded: !!flag };
}
"""

# What Home actually rendered, plus the current DB state.
READ_HOME_JS = """
async () => {
  const vis = id => getComputedStyle(document.getElementById(id)).display !== 'none';
  const cls = id => document.getElementById(id).className;
  const dis = id => document.getElementById(id).disabled;
  const flag = await db.get('settings', 'home_wizard_done');
  return {
    loadingVisible: vis('home-loading'),
    wizardVisible:  vis('home-wizard'),
    statusVisible:  vis('home-status'),
    wz1: cls('wz-1'), wz2: cls('wz-2'), wz3: cls('wz-3'),
    btn1Disabled: dis('wz-btn-1'), btn2Disabled: dis('wz-btn-2'), btn3Disabled: dis('wz-btn-3'),
    flag: flag ? flag.value : null,
    keyIds: (await db.getAll('my_keys')).map(k => k.id).sort(),
    wizardVisibleFrames: window.__wizardVisibleFrames,
    framesSampled: window.__framesSampled,
  };
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
    print(f"\nSSD Home wizard first-run state test — {browser_name}\n")
    print("1. Identity present, completion flag absent → wizard resumes at step 3")
    print("2. Steps 1 and 2 render done and stay disabled (no re-mint on resume)")
    print("3. shareBeacon() sets the completion flag and collapses to status")
    print("4. Completed user: status view renders, wizard never paints a frame")
    print()

    try:
        async with async_playwright() as p:
            launch_kwargs = dict(executable_path=exe) if exe else {}
            browser = await p.chromium.launch(**launch_kwargs)
            context = await browser.new_context()
            await context.grant_permissions(['clipboard-read', 'clipboard-write'])
            await context.add_init_script(FRAME_SAMPLER_JS)
            page = await context.new_page()

            # ── Seed identity, no completion flag ────────────────────────────
            await page.goto(PWA_URL)
            await page.wait_for_load_state("domcontentloaded")
            seed = await page.evaluate(SEED_JS)
            if seed['flagSeeded']:
                fail("Seed error: home_wizard_done was already set")
            seeded_keys = seed['keyIds']

            # ── Reload: identity present, flag absent ────────────────────────
            await page.reload()
            await page.wait_for_function("() => document.getElementById('home-loading').style.display === 'none'")
            r = await page.evaluate(READ_HOME_JS)

            if r['wizardVisible'] and not r['statusVisible']:
                ok("Identity present + flag absent → wizard shown, status hidden")
            else:
                fail(f"Expected wizard shown / status hidden, got wizard={r['wizardVisible']} status={r['statusVisible']}")

            if 'done' in r['wz1'] and 'done' in r['wz2']:
                ok("Steps 1 and 2 render as already-done")
            else:
                fail(f"Steps 1/2 not done: wz-1='{r['wz1']}' wz-2='{r['wz2']}'")

            if 'current' in r['wz3'] and not r['btn3Disabled']:
                ok("Step 3 is current and its button is enabled (reachable)")
            else:
                fail(f"Step 3 not reachable: wz-3='{r['wz3']}' btn3Disabled={r['btn3Disabled']}")

            if r['btn1Disabled'] and r['btn2Disabled']:
                ok("Step 1 and 2 buttons disabled — not re-runnable on resume")
            else:
                fail(f"Completed steps re-runnable: btn1={r['btn1Disabled']} btn2={r['btn2Disabled']}")

            if r['keyIds'] == seeded_keys:
                ok(f"No re-mint on resume ({len(seeded_keys)} keys, unchanged)")
            else:
                fail(f"Key set changed on resume: {seeded_keys} → {r['keyIds']}")

            # ── Complete step 3 via the real shareBeacon() path ──────────────
            await page.evaluate("async () => { await app.shareBeacon(); }")
            r2 = await page.evaluate(READ_HOME_JS)

            if r2['flag'] is True:
                ok("shareBeacon() set home_wizard_done")
            else:
                fail(f"home_wizard_done not set by shareBeacon(): {r2['flag']!r}")

            if r2['statusVisible'] and not r2['wizardVisible']:
                ok("Wizard collapsed to the status view on step-3 completion")
            else:
                fail(f"Not collapsed: wizard={r2['wizardVisible']} status={r2['statusVisible']}")

            # ── Fresh load as a completed user: no flash of wizard ───────────
            await page.reload()
            await page.wait_for_function("() => document.getElementById('home-loading').style.display === 'none'")
            r3 = await page.evaluate(READ_HOME_JS)

            if r3['statusVisible'] and not r3['wizardVisible'] and not r3['loadingVisible']:
                ok("Returning completed user lands on the status view")
            else:
                fail(f"Wrong view: wizard={r3['wizardVisible']} status={r3['statusVisible']} loading={r3['loadingVisible']}")

            if r3['framesSampled'] < 1:
                fail("Frame sampler recorded no frames — flash assertion is vacuous")
            elif r3['wizardVisibleFrames'] == 0:
                ok(f"Wizard never painted — 0 of {r3['framesSampled']} sampled frames")
            else:
                fail(f"Flash of wizard: visible in {r3['wizardVisibleFrames']} of {r3['framesSampled']} frames")

            await browser.close()
    finally:
        server.terminate()

    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)

asyncio.run(main())
