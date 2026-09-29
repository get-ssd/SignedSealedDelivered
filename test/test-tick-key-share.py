"""Key shares between the SSD app and Tick's popup, both directions.

Tick's real popup (../ssd.tick-2/popup) runs as a plain page with chrome.storage
stubbed in memory, so this checks the code and file format, not the installed
extension.

Run: py -3 test/test-tick-key-share.py
"""
import asyncio
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from playwright.async_api import async_playwright, expect
from server_ready import wait_for_server

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = Path(__file__).resolve().parent.parent
TICK = ROOT.parent / 'ssd.tick-2'
PORT, TICK_PORT = 8106, 8107
URL = f'http://localhost:{PORT}'
TICK_URL = f'http://localhost:{TICK_PORT}/popup/popup.html'
CHROME = r'C:\Program Files\Google\Chrome\Application\chrome.exe'
passed = 0

STORAGE_STUB = r'''
(() => {
  const store = {};
  const pick = keys => {
    if (keys == null) return { ...store };
    const list = typeof keys === 'string' ? [keys] : Array.isArray(keys) ? keys : Object.keys(keys);
    const out = {};
    for (const k of list) if (k in store) out[k] = structuredClone(store[k]);
    return out;
  };
  globalThis.chrome = { storage: { local: {
    get: async keys => pick(keys),
    set: async obj => { Object.assign(store, structuredClone(obj)); },
  } } };
})();
'''


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
    return page, card


async def import_card(page, card):
    await page.click('#tab-keys-btn')
    await page.locator('#keys-import-text').fill(json.dumps(card))
    await page.get_by_role('button', name='Import key', exact=True).click()
    await page.wait_for_function("document.getElementById('msg').textContent.includes('Added')")


async def ssd_share(page):
    """Export the SSD device's public key share through the Keys tab."""
    await page.click('#tab-keys-btn')
    async with page.expect_download() as dl:
        await page.get_by_role('button', name='Share public keys…').click()
        pin = page.locator('#_pin-overlay-input')
        try:
            await pin.wait_for(state='visible', timeout=1500)
            await pin.fill('1234')
            await page.click('#_pin-overlay-ok')
        except Exception:
            pass
    return Path(await (await dl.value).path()).read_bytes()


async def ssd_import(page, data):
    await page.click('#tab-keys-btn')
    await page.evaluate("document.getElementById('msg').textContent = ''")
    await page.locator('#keys-share-import-file').set_input_files(
        {'name': 'share.ssd', 'mimeType': 'application/octet-stream', 'buffer': data})
    await page.wait_for_function("/imported|failed|not from|mismatch|invalid|cancelled/i.test(document.getElementById('msg').textContent)")
    return await page.locator('#msg').inner_text()


async def tick_import(tick, data, owner):
    async with tick.expect_file_chooser() as fc:
        await tick.click('#owner-import-btn' if owner else '#share-import-btn')
    await (await fc.value).set_files({'name': 'share.ssd', 'mimeType': 'application/octet-stream', 'buffer': data})
    await tick.wait_for_function("document.getElementById('message').textContent !== ''")
    text = await tick.locator('#message').inner_text()
    await tick.evaluate("document.getElementById('message').textContent = ''")
    return text


def rezip(data, edit):
    """Rewrite an .ssd zip with source.json changed by edit(dict) and nothing else."""
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for name in src.namelist():
            body = src.read(name)
            if name == 'source.json':
                c = json.loads(body)
                edit(c)
                body = json.dumps(c, separators=(',', ':'), ensure_ascii=False).encode()
            z.writestr(name, body)
    return out.getvalue()


def ssd_files(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    return {n: z.read(n) for n in z.namelist()}


async def main():
    servers = [subprocess.Popen([sys.executable, '-m', 'http.server', str(port), '--directory', str(d)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
               for port, d in ((PORT, ROOT), (TICK_PORT, TICK))]
    try:
        wait_for_server(servers[0], PORT)
        wait_for_server(servers[1], TICK_PORT)
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME)
            alice, _ = await setup(browser, 'Alice')
            bob, bob_card = await setup(browser, 'Bob')
            await import_card(alice, bob_card)
            ssd_keys = await alice.evaluate('''async () => ({
              mine: (await db.getAll('my_keys')).find(k => k.name.startsWith('O:')),
              contacts: await db.getAll('contact_keys') })''')
            a_hash = ssd_keys['mine']['hash8']
            share = await ssd_share(alice)
            check('SSD share is signed', 'signature.json' in ssd_files(share))

            tick_ctx = await browser.new_context()
            await tick_ctx.add_init_script(STORAGE_STUB)
            tick = await tick_ctx.new_page()
            errors = []
            tick.on('pageerror', lambda e: errors.append(str(e)))
            await tick.goto(TICK_URL)
            await tick.wait_for_function("document.getElementById('owner-info').textContent.includes('Not set')")

            # Export is refused until the owner is set.
            await tick.click('#share-export-btn')
            await expect(tick.locator('#message')).to_contain_text('Import your own SSD key share first')
            check('Tick refuses export with no owner', True)

            # SSD -> Tick, as the owner's own share.
            msg = await tick_import(tick, share, owner=True)
            check(f'Tick imports SSD share as owner ({msg})', f'my key set to {a_hash}' in msg)
            ks = await tick.evaluate("async () => (await chrome.storage.local.get(['keystore','owner']))")
            check('Tick owner is Alice\'s O: key', ks['owner']['hash8'] == a_hash
                  and ks['owner']['public_key'] == ssd_keys['mine']['public_key_b64'])
            want = {a_hash: ssd_keys['mine']['public_key_b64'],
                    **{c['hash8']: c['public_key_b64'] for c in ssd_keys['contacts']}}
            check('Tick holds the same hash8s and keys as SSD',
                  {h: k['public_key'] for h, k in ks['keystore'].items()} == want)
            await expect(tick.locator('#owner-info')).to_contain_text(a_hash)

            # Tampered SSD share is refused by Tick.
            bad = rezip(share, lambda c: c['contacts'][0].update(name='Mallory'))
            msg = await tick_import(tick, bad, owner=False)
            check(f'Tick refuses tampered SSD share ({msg})', 'mismatch' in msg.lower())

            # Tick finds a new key (as a beacon import would), then exports.
            carol = await tick.evaluate(r'''async () => {
              const kp = await crypto.subtle.generateKey({ name: 'Ed25519' }, true, ['sign', 'verify']);
              const raw = new Uint8Array(await crypto.subtle.exportKey('raw', kp.publicKey));
              const pub = btoa(String.fromCharCode(...raw)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
              const hash8 = await _hash8(pub);
              const ks = await getKeystore();
              ks[hash8] = { hash8, name: 'Carol', public_key: pub, signing_algorithm: 'Ed25519',
                            imported_at: new Date().toISOString(), source: 'profile' };
              await saveKeystore(ks);
              return { hash8, pub };
            }''')
            async with tick.expect_download() as dl:
                await tick.click('#share-export-btn')
            tick_share = Path(await (await dl.value).path()).read_bytes()
            files = ssd_files(tick_share)
            content = json.loads(files['source.json'])
            check('Tick share is unsigned and names its owner',
                  'signature.json' not in files and content['owner_hash8'] == a_hash)

            # Tick -> SSD: the owner's device takes it, marked unverified.
            msg = await ssd_import(alice, tick_share)
            check(f'SSD imports own Tick share ({msg})', '1 new key added, 2 already known' in msg)
            rec = await alice.evaluate("async h => (await db.getAll('contact_keys')).find(k => k.hash8 === h)", carol['hash8'])
            check('imported key matches Tick\'s', rec and rec['public_key_b64'] == carol['pub'])
            check('imported key is unverified, from Tick, provenance kept',
                  rec['trust_type'] == 'unverified' and rec['received_via'] == 'tick-share' and rec['source'] == 'profile')
            existing = await alice.evaluate("async h => (await db.getAll('contact_keys')).find(k => k.hash8 === h)",
                                            ssd_keys['contacts'][0]['hash8'])
            check('existing peer key left as peer', existing['trust_type'] == 'peer')

            # Another owner's device refuses it.
            msg = await ssd_import(bob, tick_share)
            check(f'other device refuses Tick share ({msg})', 'not from your own Tick' in msg)
            bob_has = await bob.evaluate("async h => (await db.getAll('contact_keys')).some(k => k.hash8 === h)", carol['hash8'])
            check('nothing imported on the other device', not bob_has)

            # Tampered Tick share is refused by SSD.
            bad = rezip(tick_share, lambda c: c['contacts'][-1].update(name='Mallory'))
            msg = await ssd_import(alice, bad)
            check(f'SSD refuses tampered Tick share ({msg})', 'mismatch' in msg.lower())

            # Advanced shell still loads.
            adv_ctx = await browser.new_context(service_workers='block')
            adv = await adv_ctx.new_page()
            adv.on('pageerror', lambda e: errors.append('advanced: ' + str(e)))
            await adv.goto(URL + '/advanced.html?mock')
            await adv.wait_for_function('typeof app !== "undefined" && typeof app.importPublicShare === "function"')
            check(f'no page errors ({errors})', not errors)
            await browser.close()
    finally:
        for s in servers:
            s.terminate()
    print(f'\n{passed} passed')


asyncio.run(main())
