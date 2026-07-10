"""
test-revocation-smoke.py — self-signed key revocation smoke test (ssd-v76).

Covers: emit (Revoke this key -> signed ssd:revocation artifact via
        passkey.confirmAndSign), reader-side verify (self-signed revocation
        marks a held my_keys record AND a held contact_keys record revoked),
        and a subsequent verify of an ordinary signature under the revoked
        key surfacing key_revoked / "Signing key is revoked.".

No virtual authenticator is injected — MOCK_MODE installs a mock passkey
(passkey.authenticate / passkey.confirmAndSign) so the flow runs headless.

Run from repo root:
  py ssd.signed-sealed-delivered/test/test-revocation-smoke.py
"""
import asyncio, subprocess, sys, os, time
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')
from pathlib import Path
from playwright.async_api import async_playwright

PWA_DIR = Path(__file__).parent.parent
PORT    = 8098
PWA_URL = f"http://localhost:{PORT}/advanced.html?mock"

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
  // Own identity to be revoked.
  const oKey = await keyring.createKey('O:test-revoke');

  // Simulate a second person holding the same public key as a contact.
  const contactId = crypto.randomUUID();
  await db.put('contact_keys', {
    id: contactId, person_id: crypto.randomUUID(),
    name: oKey.name, hash8: oKey.hash8, public_key_b64: oKey.public_key_b64,
    received_via: 'test-harness', received_at: new Date().toISOString(),
    expires: null, recheck_interval_days: null, last_checked: null,
    revocation_hint: null, trust_type: 'peer', local_label: null,
    is_revoked: false, is_quarantined: false,
  });

  // Capture the Blob revokeKey() would otherwise hand to the browser download UI.
  let capturedBlob = null;
  const origCreateObjectURL = URL.createObjectURL;
  URL.createObjectURL = function(blob) { capturedBlob = blob; return origCreateObjectURL.call(URL, blob); };
  await app.revokeKey(oKey.id, `I revoke key ${oKey.hash8} for testing.`);
  URL.createObjectURL = origCreateObjectURL;
  if (!capturedBlob) throw new Error('revokeKey produced no downloadable artifact');
  const zipBytes = new Uint8Array(await capturedBlob.arrayBuffer());

  // Parse + verify the produced .ssd directly (same checks verifyArtifact runs).
  const unpacked = await artifact.unpack(zipBytes);
  const revocationHasCapacity = unpacked.signature.capacity.includes('ssd:revocation');
  const revocationVerify = await artifact.verify(unpacked);

  // Drive it through the real UI import path (verifyArtifact), which is what
  // actually applies the reader-side revocation marker.
  const file = new File([zipBytes], 'revocation-test.ssd', { type: 'application/octet-stream' });
  await app.verifyArtifact(file);

  const myRecAfter      = await db.get('my_keys', oKey.id);
  const contactRecAfter = await db.get('contact_keys', contactId);

  // A fresh ordinary signature under the now-revoked key must surface key_revoked.
  const enc = new TextEncoder();
  const payload = enc.encode('SSD revocation smoke test — an ordinary signed document');
  const manifestObj2 = {
    version: '1.0', render_spec: 'ssd-render-1.0', signed_at: new Date().toISOString(),
    signer_hash8: oKey.hash8, signer_name: oKey.name,
    files: { 'source.txt': 'sha256:' + await cryptoOps.sha256(payload), 'render.txt': 'sha256:' + await cryptoOps.sha256(payload) },
  };
  const manifestBytes2 = enc.encode(JSON.stringify(manifestObj2, null, 2));
  const priv2 = await keyring.getPrivateKey(oKey.id);
  const sig2  = await cryptoOps.sign(priv2, manifestBytes2);
  const signatureObj2 = {
    algorithm: 'Ed25519', signer_hash8: oKey.hash8, capacity: ['ssd:author'],
    signed_at: manifestObj2.signed_at,
    manifest_hash: 'sha256:' + await cryptoOps.sha256(manifestBytes2), signature: sig2,
  };
  const unpacked2 = { manifest: manifestObj2, content: { source: 'x', render: 'x' }, signature: signatureObj2, engine: renderEngines['ssd-render-1.0'] };
  // Bypass hash-target mismatch (payload isn't real source/render text) — we only care about key_revoked here.
  const origHashTargets = renderEngines['ssd-render-1.0'].hashTargets;
  renderEngines['ssd-render-1.0'].hashTargets = () => ({ 'source.txt': payload, 'render.txt': payload });
  const laterResult = await artifact.verify(unpacked2);
  renderEngines['ssd-render-1.0'].hashTargets = origHashTargets;

  return {
    oHash8: oKey.hash8,
    revocationHasCapacity,
    revocationSignatureValid: revocationVerify.signature_valid,
    myRecRevokedBefore: false,
    myRecRevokedAfter: !!myRecAfter?.is_revoked,
    contactRecRevokedAfter: !!contactRecAfter?.is_revoked,
    laterKeyRevoked: laterResult.key_revoked,
    laterErrors: laterResult.errors,
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
    print(f"\nSSD self-signed revocation smoke test (ssd-v76) — {browser_name}\n")
    print("1. Revoke a held test identity (emit, via passkey.confirmAndSign)")
    print("2. Produced .ssd has capacity ssd:revocation and verifies")
    print("3. Importing it (verifyArtifact) flips my_keys + contact_keys is_revoked")
    print("4. A subsequent verify of that key surfaces key_revoked")
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
                ok(f"Identity minted (hash8={r['oHash8']})")
                if r['revocationHasCapacity']: ok("Produced artifact carries capacity ssd:revocation")
                else:                          fail("Produced artifact missing capacity ssd:revocation")
                if r['revocationSignatureValid']: ok("Revocation artifact self-signature verifies")
                else:                             fail("Revocation artifact signature INVALID")
                if r['myRecRevokedAfter']: ok("my_keys record flipped to is_revoked after import")
                else:                      fail("my_keys record NOT marked revoked")
                if r['contactRecRevokedAfter']: ok("contact_keys record flipped to is_revoked after import")
                else:                           fail("contact_keys record NOT marked revoked")
                if r['laterKeyRevoked']: ok("Subsequent verify of the key surfaces key_revoked")
                else:                    fail("Subsequent verify did NOT surface key_revoked")
                if any('revoked' in e.lower() for e in r['laterErrors']):
                    ok(f"Verify error text present: {[e for e in r['laterErrors'] if 'revoked' in e.lower()]}")
                else:
                    fail(f"No 'revoked' error text in verify result: {r['laterErrors']}")

            await browser.close()
    finally:
        server.terminate()

    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)

asyncio.run(main())
