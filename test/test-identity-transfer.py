"""Smoke test for identity transfer to a fresh device (SPEC-IDENTITY-TRANSFER).

Alice (A) sends her identity to Bob's freshly set-up device (B). Carol is an
unrelated device. Full cross-device/hardware testing is manual.

Run: py -3 test/test-identity-transfer.py
"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from playwright.async_api import async_playwright, expect
from server_ready import wait_for_server

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = Path(__file__).resolve().parent.parent
PORT = 8105
URL = f'http://localhost:{PORT}'
CHROME = r'C:\Program Files\Google\Chrome\Application\chrome.exe'
passed = 0


def check(label, condition):
    global passed
    if not condition:
        raise AssertionError(label)
    passed += 1
    print(f'PASS {label}', flush=True)


async def setup(browser, name):
    """PIN-rung setup through the Home wizard; returns the device's key card."""
    context = await browser.new_context(service_workers='block')
    page = await context.new_page()
    page.on('dialog', lambda d: d.accept(d.default_value or 'Test contact'))
    await page.goto(URL + '/?mock')
    await page.wait_for_function('typeof basicExchange !== "undefined"')
    await page.click('#wz-btn-pin')
    await page.locator('#_pin-overlay-input').fill('1234')
    await page.click('#_pin-overlay-ok')
    await page.locator('#wz-identity-name').fill(name)
    await page.click('#wz-btn-2')
    await page.wait_for_function("!document.getElementById('wz-btn-3').disabled")
    await page.click('#wz-btn-3')
    await page.locator('#home-status').wait_for(state='visible')
    await page.click('#tab-keys-btn')
    await page.locator('#keys-beacon-list button').filter(has_text='Show QR').click()
    await page.wait_for_function("document.getElementById('qr-modal-payload').textContent.startsWith('{')")
    card = json.loads(await page.locator('#qr-modal-payload').inner_text())
    await page.get_by_role('button', name='Close', exact=True).click()
    return context, page, card


async def import_card(page, card):
    await page.click('#tab-keys-btn')
    await page.locator('#keys-import-text').fill(json.dumps(card))
    await page.get_by_role('button', name='Import key', exact=True).click()
    await page.wait_for_function("document.getElementById('msg').textContent.includes('Added')")


EXPORT = r'''async (destEncUrl) => {
  const destPub = cryptoOps.b64enc(cryptoOps.b64urldec(destEncUrl));
  const oKey = (await db.getAll('my_keys')).find(k => k.name.startsWith('O:'));
  const bytes = await basicExchange.buildIdentityBackup(oKey, destPub);
  const env = JSON.parse(new TextDecoder().decode(bytes));
  const own = (await db.get('settings', 'my_encryption_key_pub')).value;
  return { bytes: Array.from(bytes), oPub: oKey.public_key_b64, encPub: own,
           slots: env.recipients.map(r => r.for_key), destPub,
           text: new TextDecoder().decode(bytes) };
}'''

TRY_OPEN = r'''async (bytes) => {
  try { await basicExchange.openIdentityBackup(Uint8Array.from(bytes), () => app._unlock()); return 'opened'; }
  catch (e) { return e.message; }
}'''


async def main():
    server = subprocess.Popen([sys.executable, '-m', 'http.server', str(PORT), '--directory', str(ROOT)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for_server(server, PORT)
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME)
            _, alice, _ = await setup(browser, 'Alice')
            _, bob, bob_card = await setup(browser, 'Bob')
            _, carol, carol_card = await setup(browser, 'Carol')

            # A learns B's sealing key through the key-card exchange, then exports.
            await import_card(alice, bob_card)
            await import_card(alice, carol_card)
            out = await alice.evaluate(EXPORT, bob_card['encryption_public_key'])
            check('file sealed to exactly destination and self',
                  sorted(out['slots']) == sorted([out['destPub'], out['encPub']]))
            check('no private material visible outside the seal', 'priv' not in out['text'])
            check('A still holds its identity',
                  await alice.evaluate("async () => (await db.getAll('my_keys')).some(k => k.name.startsWith('O:'))"))

            # Carol is not a recipient.
            check('unrelated device cannot open',
                  'not sealed for this device' in await carol.evaluate(TRY_OPEN, out['bytes']))

            # Tampered ciphertext / public share are refused.
            tampered = json.loads(out['text'])
            tampered['ct'] = tampered['ct'][:-8] + 'AAAAAAA='
            check('tampered file refused',
                  await bob.evaluate(TRY_OPEN, list(json.dumps(tampered).encode())) != 'opened')

            # A second file for Carol, blocked by a draft on Carol.
            out_c = await alice.evaluate(EXPORT, carol_card['encryption_public_key'])
            await carol.evaluate("() => db.put('drafts', {id:'d1', text:'x'})")
            blocked = await carol.evaluate(r'''async (bytes) => {
              const c = await basicExchange.openIdentityBackup(Uint8Array.from(bytes), () => app._unlock());
              const before = (await db.getAll('my_keys')).length;
              try { await basicExchange.adoptIdentityBackup(c); return 'adopted'; }
              catch (e) { return e.message + '|' + ((await db.getAll('my_keys')).length === before); }
            }''', out_c['bytes'])
            check('device with a draft is refused without changes', 'draft' in blocked and blocked.endswith('|true'))

            # B adopts through the Keys UI.
            before = await bob.evaluate('''async () => {
              const k = await db.getAll('my_keys');
              return { d: k.find(x => x.name.startsWith('D:')), mk: (await db.get('settings','master_key')).value };
            }''')
            await bob.click('#tab-keys-btn')
            await bob.locator('#keys-restore-file').set_input_files(
                {'name': 'id.ssd', 'mimeType': 'application/octet-stream', 'buffer': bytes(out['bytes'])})
            await expect(bob.locator('#msg')).to_contain_text('now holds', timeout=15000)

            after = await bob.evaluate(r'''async ([oPub, bytes]) => {
              const keys = await db.getAll('my_keys');
              const enc = await db.getAll('encryption_keys');
              const o = keys.find(k => k.name.startsWith('O:'));
              const d = keys.find(k => k.name.startsWith('D:'));
              const data = new TextEncoder().encode('hello');
              const sig = await cryptoOps.sign(await keyring.getPrivateKey(o.id), data);
              const ok = await cryptoOps.verify(await cryptoOps.importPublicKeyB64(oPub), sig, data);
              // Self slot: reopen the same file after the setup sealing key is gone.
              const reopened = await basicExchange.open(Uint8Array.from(bytes), () => app._unlock());
              return { oPub: o.public_key_b64, count: keys.length, d, enc: enc.map(e => e.pub),
                       signs: ok, reopened: reopened.seal.state,
                       mk: (await db.get('settings','master_key')).value,
                       cardEnc: d.signed_card.encryption_public_key };
            }''', [out['oPub'], out['bytes']])
            check('B holds A\'s O: key, one D:, nothing else', after['oPub'] == out['oPub'] and after['count'] == 2)
            check('B\'s own D: key unchanged', after['d']['public_key_b64'] == before['d']['public_key_b64'])
            check('B\'s master-key record unchanged', after['mk'] == before['mk'])
            check('B\'s only sealing key is the owner\'s', after['enc'] == [out['encPub']])
            check('B signs as the owner', after['signs'])
            check('B reopens the same file via the self slot', after['reopened'] == 'S2')
            check('B\'s device card advertises the owner sealing key',
                  after['cardEnc'] == json.loads(json.dumps(out['encPub'])).replace('+', '-').replace('/', '_').rstrip('='))
            check('second adoption refused (already has identity)',
                  'already has this identity' in await bob.evaluate(r'''async (bytes) => {
                    const c = await basicExchange.openIdentityBackup(Uint8Array.from(bytes), () => app._unlock());
                    return (await basicExchange.identityAdoptionBlocker(c)) || 'none';
                  }''', out['bytes']))

            # Advanced shell loads with the shared functions.
            adv_ctx = await browser.new_context(service_workers='block')
            adv = await adv_ctx.new_page()
            errors = []
            adv.on('pageerror', lambda e: errors.append(str(e)))
            await adv.goto(URL + '/advanced.html?mock')
            await adv.wait_for_function('typeof basicExchange !== "undefined" && typeof app !== "undefined"')
            check('advanced.html loads with identity transfer wired',
                  not errors and await adv.evaluate('typeof app.backupIdentity === "function" && typeof app._esc === "function"'))
            await browser.close()
    finally:
        server.terminate()
    print(f'\n{passed} passed')


asyncio.run(main())
