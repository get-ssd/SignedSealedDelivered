"""Fresh-browser exchange, crypto, persistence and authentication-policy tests.

Run: py -3 test/test-basic-exchange.py
PIN-only fixtures use ?mock; normal onboarding also supports explicit PIN fallback.
"""
import asyncio
import base64
import json
import subprocess
import sys
from pathlib import Path
from playwright.async_api import async_playwright, expect
from server_ready import wait_for_server

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = Path(__file__).resolve().parent.parent
PORT = 8104
URL = f'http://localhost:{PORT}'
CHROME = r'C:\Program Files\Google\Chrome\Application\chrome.exe'
passed = 0


def check(label, condition):
    global passed
    if not condition:
        raise AssertionError(label)
    passed += 1
    print(f'PASS {label}', flush=True)


async def pin(page):
    await page.locator('#_pin-overlay-input').fill('1234')
    await page.click('#_pin-overlay-ok')


async def setup(browser, name):
    context = await browser.new_context(service_workers='block')
    page = await context.new_page()
    page.on('dialog', lambda d: d.accept(d.default_value or 'Test contact'))
    await page.goto(URL + '/?mock')
    await page.wait_for_function('typeof basicExchange !== "undefined"')
    await page.evaluate('''() => {
      window.authCalls = 0;
      Object.defineProperty(navigator, 'credentials', {configurable:true, value:{
        create:()=>{window.authCalls++; throw new Error('No authenticator in test')},
        get:()=>{window.authCalls++; throw new Error('No authenticator in test')}
      }});
    }''')
    await page.click('#wz-btn-pin')
    await expect(page.locator('#_pin-overlay-input')).to_be_visible()
    check(name + ' sees rung-3 disclosure', 'Anyone with access' in await page.locator('#msg').inner_text())
    await pin(page)
    await page.locator('#wz-identity-name').fill(name)
    await page.click('#wz-btn-2')
    await page.wait_for_function("!document.getElementById('wz-btn-3').disabled")
    await page.click('#wz-btn-3')
    await page.locator('#home-status').wait_for(state='visible')
    check(name + ' test setup calls no WebAuthn', await page.evaluate('authCalls') == 0)
    check(name + ' test rung is labelled secret', 'secret' in await page.locator('#home-passkey-status').inner_text())
    await page.click('#tab-keys-btn')
    await page.locator('#keys-beacon-list button').filter(has_text='Show QR').click()
    await page.locator('#qr-modal').wait_for(state='visible')
    await page.wait_for_function("document.getElementById('qr-modal-payload').textContent.startsWith('{')")
    card = json.loads(await page.locator('#qr-modal-payload').inner_text())
    await page.get_by_role('button', name='Close', exact=True).click()
    return context, page, card


async def import_card(page, card):
    await page.click('#tab-keys-btn')
    await page.locator('#keys-import-text').fill(json.dumps(card))
    await page.get_by_role('button', name='Import key', exact=True).click()
    await expect(page.locator('#keys-import-text')).to_have_value('')
    await page.wait_for_function("document.getElementById('msg').textContent.includes('Added') || document.getElementById('msg').textContent.includes('Already')")


async def upload(page, data):
    await page.click('#tab-docs-btn')
    await page.locator('#docs-open-file').set_input_files({'name':'agreement.ssd', 'mimeType':'application/octet-stream', 'buffer':bytes(data)})
    await page.locator('#docs-verification [data-state^="V"]').wait_for()
    return await page.locator('#docs-verification').inner_text()


CRYPTO_CHECKS = r'''async ({sealed, publicBytes, aliceCard}) => {
  const checks = [];
  const check = (name, value) => { if(!value) throw new Error(name); checks.push(name); };
  const bytes = Uint8Array.from(sealed);
  let opened = await basicExchange.open(bytes, ()=>app._unlock());
  check('addressed Bob opens valid unknown signer S2/V2', opened.seal.state==='S2' && opened.signature==='V2');
  const id = await basicExchange.persistReceived(bytes, opened);
  check('valid unknown document stored', !!id);
  check('duplicate bytes resolve to the same record', await basicExchange.persistReceived(bytes, opened)===id && (await db.getAll('artifacts')).length===1);
  await basicExchange.saveContact(aliceCard,'test','Alice');
  opened = await basicExchange.open(bytes, ()=>app._unlock());
  check('import signer then reopen yields V1', opened.signature==='V1');
  const original = await db.getAll('encryption_keys');
  for(const k of original) await db.put('encryption_keys',{...k,is_current:false});
  check('retired encryption key still opens', (await basicExchange.open(bytes,()=>app._unlock())).seal.state==='S2');
  for(const k of original) await db.del('encryption_keys',k.pub);
  const other = await basicExchange.open(bytes,()=>app._unlock());
  check('non-recipient S3 verifies known outer metadata', other.seal.state==='S3' && other.signature==='V1' && other.outerOnly);
  const unaddressed = JSON.parse(new TextDecoder().decode(bytes));
  unaddressed.recipients[0].for_key='different';
  const unaddressedBytes=new TextEncoder().encode(JSON.stringify(unaddressed));
  check('new S3 file not stored', await basicExchange.persistReceived(unaddressedBytes,await basicExchange.open(unaddressedBytes,()=>app._unlock()))===null);
  const newEnvelope = JSON.parse(new TextDecoder().decode(bytes));
  newEnvelope.signed_at = 'tampered';
  check('outer metadata tamper V5', (await basicExchange.open(new TextEncoder().encode(JSON.stringify(newEnvelope)),()=>app._unlock())).signature==='V5');
  for(const k of original) await db.put('encryption_keys',k);
  const broken = JSON.parse(new TextDecoder().decode(bytes));
  broken.ct = cryptoOps.b64enc(new Uint8Array(32));
  const badBytes = new TextEncoder().encode(JSON.stringify(broken));
  const badOpen = await basicExchange.open(badBytes,()=>app._unlock());
  check('matching slot with bad ciphertext is S4', badOpen.seal.state==='S4');
  check('S4 not persisted', await basicExchange.persistReceived(badBytes,badOpen)===null);
  const files = fflate.unzipSync(Uint8Array.from(publicBytes));
  const changed = {...files, 'source.txt':new TextEncoder().encode('Changed content')};
  check('content modification V4', (await basicExchange.open(fflate.zipSync(changed),()=>app._unlock())).signature==='V4');
  const sig = JSON.parse(new TextDecoder().decode(files['signature.json']));
  sig.signature = cryptoOps.b64urlenc(new Uint8Array(64));
  const invalid = fflate.zipSync({...files,'signature.json':new TextEncoder().encode(JSON.stringify(sig))});
  const invalidResult = await basicExchange.open(invalid,()=>app._unlock());
  check('signature tamper V5', invalidResult.signature==='V5');
  check('V5 not persisted', await basicExchange.persistReceived(invalid,invalidResult)===null);
  check('invalid file V7', (await basicExchange.open(new Uint8Array([1,2,3]),()=>app._unlock())).signature==='V7');
  const signer = (await db.getAll('contact_keys')).find(k=>k.hash8===aliceCard.hash8);
  await db.put('contact_keys',{...signer,is_revoked:true});
  check('revoked valid signer V3', (await basicExchange.open(bytes,()=>app._unlock())).signature==='V3');
  await db.put('contact_keys',signer);
  return checks;
}'''


async def main():
    server = subprocess.Popen([sys.executable, '-m', 'http.server', str(PORT), '--directory', str(ROOT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for_server(server, PORT)
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME)
            # PIN protection is a normal explicit choice, not a demo exception.
            normal = await browser.new_page()
            await normal.goto(URL)
            await normal.locator('#home-wizard').wait_for(state='visible')
            check('normal onboarding exposes explicit weaker PIN option', await normal.locator('#wz-btn-pin').is_visible())
            await normal.evaluate("() => {passkey.register=async()=>{throw new Error('Unavailable')}}")
            await normal.click('#wz-btn-1')
            await normal.wait_for_function("document.getElementById('msg').textContent.includes('did not complete')")
            check('normal passkey error does not create PIN identity', await normal.evaluate("async()=>!(await db.get('settings','keyring_rung'))"))
            await normal.click('#wz-btn-pin')
            await expect(normal.locator('#_pin-overlay-input')).to_be_visible()
            check('normal PIN-only choice discloses offline guessing', 'guessing the PIN offline' in await normal.locator('#msg').inner_text())
            await pin(normal)
            await normal.wait_for_function("!document.getElementById('wz-btn-2').disabled")
            check('normal explicit PIN setup creates rung 3 without a credential', await normal.evaluate("async()=>(await db.get('settings','keyring_rung')).value===3 && !(await db.get('settings','credential_id'))"))
            await normal.close()

            # Real WebAuthn responses must not admit cloud-syncable credentials
            # or misrepresent non-PRF PIN protection in normal onboarding.
            for label, has_prf, backup_eligible, expected in [
                ('local non-PRF', False, False, None),
                ('syncable PRF', True, True, 'backup-eligible'),
            ]:
                rejected_context = await browser.new_context(service_workers='block')
                rejected = await rejected_context.new_page()
                auth = await rejected_context.new_cdp_session(rejected)
                await auth.send('WebAuthn.enable')
                await auth.send('WebAuthn.addVirtualAuthenticator', {'options': {
                    'protocol': 'ctap2', 'ctap2Version': 'ctap2_1', 'transport': 'internal',
                    'hasResidentKey': True, 'hasUserVerification': True, 'isUserVerified': True,
                    'automaticPresenceSimulation': True, 'hasPrf': has_prf,
                    'defaultBackupEligibility': backup_eligible,
                }})
                await rejected.goto(URL)
                await rejected.click('#wz-btn-1')
                if not has_prf:
                    await expect(rejected.locator('#_pin-overlay-input')).to_be_visible()
                    check('normal non-PRF choice discloses offline attack', 'device verification does not protect the copied data' in await rejected.locator('#msg').inner_text())
                    await rejected.click('#_pin-overlay-cancel')
                    await expect(rejected.locator('#msg')).to_contain_text('Setup cancelled')
                    check('cancelling non-PRF PIN choice leaves setup unconfigured', await rejected.evaluate("async()=>!(await db.get('settings','credential_id')) && !(await db.get('settings','keyring_rung'))"))
                    await rejected.click('#wz-btn-1')
                    await expect(rejected.locator('#_pin-overlay-input')).to_be_visible()
                    await pin(rejected)
                    await rejected.wait_for_function("!document.getElementById('wz-btn-2').disabled")
                    check('normal non-PRF setup retains credential and uses rung 2', await rejected.evaluate("async()=>(await db.get('settings','keyring_rung')).value===2 && !!(await db.get('settings','credential_id'))"))
                    await rejected_context.close()
                    continue
                await expect(rejected.locator('#msg .fail')).to_contain_text(expected)
                check(label + ' leaves normal setup unconfigured', await rejected.evaluate("async()=>!(await db.get('settings','credential_id')) && !(await db.get('settings','keyring_rung'))"))
                check(label + ' does not open a PIN fallback', await rejected.locator('#_pin-overlay-input').count() == 0)
                await rejected_context.close()

            # A normal (non-mock) product session with an actual CDP virtual
            # authenticator exercises credential APIs, PRF, and sign approval.
            pc = await browser.new_context(service_workers='block')
            pp = await pc.new_page()
            cdp = await pc.new_cdp_session(pp)
            await cdp.send('WebAuthn.enable')
            await cdp.send('WebAuthn.addVirtualAuthenticator', {'options': {
                'protocol':'ctap2', 'ctap2Version':'ctap2_1', 'transport':'internal',
                'hasResidentKey':True, 'hasUserVerification':True, 'isUserVerified':True,
                'automaticPresenceSimulation':True, 'hasPrf':True,
            }})
            assertions = []
            cdp.on('WebAuthn.credentialAsserted', lambda event: assertions.append(event))
            await pp.goto(URL)
            await pp.click('#wz-btn-1')
            await pp.wait_for_function("!document.getElementById('wz-btn-2').disabled")
            await pp.locator('#wz-identity-name').fill('Passkey Alice')
            await pp.click('#wz-btn-2')
            await pp.wait_for_function("!document.getElementById('wz-btn-3').disabled")
            check('normal setup creates PRF passkey identity', await pp.evaluate("async()=>(await db.get('settings','keyring_rung')).value") == 1)
            await pp.click('#tab-docs-btn')
            await pp.get_by_role('button', name='+ New', exact=True).click()
            await pp.locator('#docs-text').fill('Signed with device verification')
            await pp.get_by_role('button', name='Preview & Sign', exact=True).click()
            await pp.locator('[name="docs-access"][value="public"]').check()
            prior_assertions = len(assertions)
            await pp.click('#docs-sign-btn')
            await pp.locator('#docs-deliver').wait_for(state='visible')
            check('normal signing invokes passkey verification', len(assertions) > prior_assertions)
            check('public choice produces an unsealed artifact', await pp.evaluate("async()=>(await db.getAll('artifacts'))[0].encrypted") is False)
            await pc.close()

            ac, a, ca = await setup(browser, 'Alice')
            bc, b, cb = await setup(browser, 'Bob')
            cc, c, ccard = await setup(browser, 'Carol')
            await import_card(a, cb)
            await import_card(a, ccard)
            check('full card imports show Can seal to', await a.get_by_text('Can seal to', exact=True).count() == 2)
            # Paste a signing-only code then upgrade the same signing key.
            await c.click('#tab-keys-btn')
            await c.locator('#keys-import-text').fill(f"[SSDKEY:{cb['hash8']}:{cb['signing_public_key']}]")
            await c.get_by_role('button', name='Import key', exact=True).click()
            await c.get_by_text('Verify only', exact=True).wait_for()
            check('signing-only import shows Verify only', True)
            await c.locator('#keys-import-text').fill(json.dumps(cb))
            await c.get_by_role('button', name='Import key', exact=True).click()
            await c.get_by_text('Can seal to', exact=True).wait_for()
            check('reimport adds sealing capability', 'can now receive' in await c.locator('#msg').inner_text())
            states = await c.evaluate('''async card=>{
              const known=await basicExchange.saveContact(card,'test','Bob');
              const kp=await cryptoOps.generateX25519Keypair();
              const incoming={...card,encryption_public_key:cryptoOps.b64urlenc(await crypto.subtle.exportKey('raw',kp.publicKey))};
              const conflict=await basicExchange.saveContact(incoming,'test','Bob');
              const person=(await db.getAll('contacts'))[0];
              return [known.state,conflict.state,person.encryption_key_pub===cryptoOps.b64enc(cryptoOps.b64urldec(card.encryption_public_key))];
            }''', cb)
            check('known key skipped; conflicting sealing key kept', states == ['known', 'conflict', True])

            signing_requests = []
            a.on('request', lambda request: signing_requests.append(request.url))
            await ac.set_offline(True)
            await a.click('#tab-docs-btn')
            await a.get_by_role('button', name='+ New', exact=True).click()
            await a.locator('#docs-text').fill('Alice, Bob and Carol agree to exchange this document.')
            await a.get_by_role('button', name='Preview & Sign', exact=True).click()
            await a.locator('#docs-preview').wait_for(state='visible')
            visible_preview = await a.locator('#docs-preview-text').text_content()
            check('review defaults sealed for Me', await a.locator('.docs-recipient:checked').count() == 1)
            await a.locator('.docs-recipient').nth(0).uncheck()
            check('no recipients disables sign', await a.locator('#docs-sign-btn').is_disabled())
            await a.locator('.docs-recipient').nth(1).check()
            check('no self recipient warns', "won't be able" in await a.locator('#docs-recipient-note').inner_text())
            await a.locator('.docs-recipient').nth(0).check()
            await a.locator('.docs-recipient').nth(2).check()
            check('button describes three recipients', await a.locator('#docs-sign-btn').inner_text() == 'Sign and seal for 3')
            await a.evaluate("() => {window.confirmCalls=0; const confirm=app._confirmSign.bind(app); app._confirmSign=(...args)=>{confirmCalls++;return confirm(...args)}}")
            await a.evaluate('''()=>{
              Object.defineProperty(navigator,'canShare',{configurable:true,value:({files})=>files.length===1 && files[0].type==='application/octet-stream'});
              Object.defineProperty(navigator,'share',{configurable:true,value:async({files})=>{window.sharedFile=files[0];throw new DOMException('Cancelled','AbortError')}});
            }''')
            await a.click('#docs-sign-btn')
            await pin(a)
            await a.locator('#docs-deliver').wait_for(state='visible')
            check('one confirmation per signing act', await a.evaluate('confirmCalls') == 1)
            check('outgoing stored before delivery', await a.evaluate("async()=>(await db.getAll('artifacts')).length") == 1)
            check('download MIME unchanged', await a.evaluate('app._deliverFile.type') == 'application/octet-stream')
            sealed = await a.evaluate('Array.from(app._deliverBytes)')
            check('compose, sign and seal work offline without network requests', signing_requests == [])
            check('signing does not automatically share', await a.evaluate('typeof window.sharedFile === "undefined"'))
            signed_render = await a.evaluate("async()=>new TextDecoder().decode(fflate.unzipSync((await basicExchange.unwrap(app._deliverBytes,()=>app._unlock())).zipBytes)['render.txt'])")
            check('signed document exactly matches the displayed preview', signed_render == visible_preview)
            await ac.set_offline(False)
            await a.click('#docs-share-btn')
            await a.wait_for_function('window.sharedFile instanceof File')
            check('share sends actual file and cancellation stays on delivery', await a.locator('#docs-deliver').is_visible() and await a.evaluate('sharedFile.size') == len(sealed))
            async with a.expect_download() as download_event:
                await a.get_by_role('button',name='Download',exact=True).click()
            download = await download_event.value
            check('download contains the same sealed bytes', Path(await download.path()).read_bytes() == bytes(sealed))
            public_bytes = await a.evaluate("async()=>Array.from((await basicExchange.unwrap(app._deliverBytes,()=>app._unlock())).zipBytes)")
            fixture = await a.evaluate("async()=>Object.fromEntries(await Promise.all(['settings','my_keys','encryption_keys'].map(async s=>[s,await db.getAll(s)])))")
            for label in await b.evaluate(CRYPTO_CHECKS, {'sealed': sealed, 'publicBytes': public_bytes, 'aliceCard': ca}):
                check(label, True)
            await import_card(c, ca)
            for recipient in [b, c]:
                text = await upload(recipient, sealed)
                check('file input opens S2/V1 from Alice', 'Signed by O:Alice' in text and await recipient.locator('[data-state="S2"]').count() == 1)
            # Public artifact can be verified without an identity or contact.
            urlpage = await browser.new_page()
            encoded = base64.urlsafe_b64encode(bytes(public_bytes)).decode().rstrip('=')
            await urlpage.goto(URL + '/?ssd=' + encoded)
            await urlpage.locator('[data-state="V2"]').wait_for()
            check('URL input verifies unknown signature and removes param', '?ssd=' not in urlpage.url)
            await urlpage.evaluate('navigator.serviceWorker.ready')
            await urlpage.wait_for_function('navigator.serviceWorker.controller !== null')
            await urlpage.context.set_offline(True)
            await urlpage.goto(URL + '/?ssd=' + encoded)
            await urlpage.locator('[data-state="V2"]').wait_for()
            check('installed shell receives dispatcher URL offline', True)
            check('cache keys do not retain document URL bytes', await urlpage.evaluate("async()=>{for(const name of await caches.keys())for(const r of await (await caches.open(name)).keys())if(new URL(r.url).searchParams.has('ssd'))return false;return true}"))
            await urlpage.context.set_offline(False)
            await urlpage.close()

            # Advanced is an independent current producer/consumer, not mocked.
            adv = await ac.new_page()
            await adv.goto(URL + '/advanced.html?mock')
            await adv.wait_for_function('typeof app.verifyArtifact === "function"')
            result = await adv.evaluate('''async({sealed,card,bob,fixture})=>{
              for (const [store,rows] of Object.entries(fixture)) for (const row of rows) await db.put(store,row);
              await keyring.unlock('1234');
              const author=await keyring.createKey('O:Advanced');
              const pub=await db.get('settings','my_encryption_key_pub');
              const basic=await artifact.unpack(Uint8Array.from(bob));
              await db.put('contact_keys',{id:crypto.randomUUID(),hash8:card.hash8,name:card.name,public_key_b64:card.signing_public_key});
              const verified=await artifact.verify(basic);
              await app.verifyArtifact(new Blob([Uint8Array.from(sealed)]));
              const verifiedUi=document.getElementById('results').textContent;
              // Import Alice's encryption key so Advanced can independently seal for Alice.
              const personId=crypto.randomUUID();
              await db.put('contacts',{id:personId,local_name:'Alice',encryption_key_pub:cryptoOps.b64enc(cryptoOps.b64urldec(card.encryption_public_key))});
              document.getElementById('compose-key-select').innerHTML=`<option value="${author.id}">Advanced</option>`;
              document.getElementById('compose-text').value='A document from Advanced';
              await app.previewSign(); await app.confirmSign();
              await app.loadSealRecipients();
              [...document.querySelectorAll('.seal-recipient-cb')].find(cb=>cb.value===personId).checked=true;
              await app.doDeliver();
              const outgoing=Array.from(app._deliverCtx.outputBytes);
              let revocation;
              const create=URL.createObjectURL;
              URL.createObjectURL=blob=>{revocation=blob;return create(blob)};
              try {await app.revokeKey(author.id,'Revoke the test author');} finally {URL.createObjectURL=create;}
              return {valid:verified.signature_valid && verified.content_unmodified,verifiedUi,bytes:outgoing,card:await keyring.exportKeyCard(author.id),revocation:Array.from(new Uint8Array(await revocation.arrayBuffer()))};
            }''', {'sealed': sealed, 'card': ca, 'bob': public_bytes, 'fixture': fixture})
            check('Advanced opens and verifies Basic sealed bytes', result['valid'] and 'Signature valid' in result['verifiedUi'] and 'Encrypted to 3' in result['verifiedUi'])
            await import_card(a, result['card'])
            check('Basic opens Advanced sealed output', 'Signed by O:Advanced' in await upload(a, result['bytes']))
            check('Basic applies received Advanced revocation', 'marked revoked' in await upload(a,result['revocation']))
            check('reopening stored file observes revocation', 'revoked' in await upload(a,result['bytes']))
            await browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)
    print(f'\n{passed} passed, 0 failed', flush=True)


asyncio.run(main())
