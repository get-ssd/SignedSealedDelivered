"""
test-master-key.py — headless tests for the keyring master key (SPEC-KEYRING.md).

Covers the six checks named in the build brief:
  1. Round-trip      — mint → wrap under MK → unlock → unwrap → sign
  2. Credential change — rung 3 → rung 1 through advanced.html's real
                         registerPasskey() flow, keys still readable after
  3. Rung transition   — same flow (MK-11); in this codebase the rung-3
                         re-registration escape IS the only in-app credential
                         change, so checks 2 and 3 coincide by construction
  4. Migration         — pre-MK fixture (records wrapped directly under the
                         unlock key, no master-key record) migrates and reads
  5. Partial orphan    — healthy cohort migrates, orphaned record left untouched
                         and reported, migration does NOT fail
  6. Detection         — uk_id mismatch caught at keyring.unlock() and surfaced
                         as a flagged, readable error rather than OperationError

Uses Chrome's CDP virtual authenticator API (hasPrf: true) via Playwright,
following test-prf-rung1.py. Firefox cannot run this (CDP is Chromium-only).

Run from repo root:
  py ssd.signed-sealed-delivered/test/test-master-key.py

Requires:
  - Python playwright  (pip install playwright)
  - Brave or Chrome installed (or bundled Chromium via `playwright install chromium`)
  - No external server needed — starts its own on port 8099.
"""
import asyncio, subprocess, sys, os, time
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')
from pathlib import Path
from playwright.async_api import async_playwright

PWA_DIR = Path(__file__).parent.parent
PORT    = 8099
BASIC   = f"http://localhost:{PORT}/?mock"
ADV     = f"http://localhost:{PORT}/advanced.html?mock"

BRAVE_PATH  = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def find_browser():
    for path in [BRAVE_PATH, CHROME_PATH]:
        if os.path.exists(path):
            return path
    return None


passed = failed = 0


def ok(msg):
    global passed
    passed += 1
    print(f"  \u2713 {msg}")


def fail(msg):
    global failed
    failed += 1
    print(f"  \u2717 {msg}", file=sys.stderr)


def check(cond, good, bad):
    ok(good) if cond else fail(bad)


# ── 1. Round-trip: mint → wrap under MK → unlock → unwrap → sign ──────────────

ROUNDTRIP_JS = """
async () => {
  await passkey.register('test-harness');
  await keyring.newUnlockKeyId();
  await keyring.unlock();

  const mkRec = await keyring._getMasterKeyRecord();
  const ukId  = await keyring._getUkId();

  const oKey = await keyring.createKey('O:test-owner');

  // The stored private key must NOT open under the unlock key any more (MK-1/MK-3):
  // if it does, the wrapping never moved off the unlock key.
  const rec = await db.get('my_keys', oKey.id);
  let opensUnderUnlockKey = false;
  try {
    await cryptoOps.decryptPrivateKey(rec.private_key_encrypted, rec.private_key_iv, keyring._sessionKey);
    opensUnderUnlockKey = true;
  } catch (_) {}

  // ...and must open under the master key.
  const mk = await keyring._masterKey();
  let opensUnderMasterKey = true;
  try {
    await cryptoOps.decryptPrivateKey(rec.private_key_encrypted, rec.private_key_iv, mk);
  } catch (_) { opensUnderMasterKey = false; }

  // Lock, re-unlock, sign.
  keyring._sessionKey = null;
  await keyring.unlock();
  const payload = new TextEncoder().encode('SSD master-key round-trip');
  const priv    = await keyring.getPrivateKey(oKey.id);
  const sig     = await cryptoOps.sign(priv, payload);
  const pub     = await cryptoOps.importPublicKeyB64(oKey.public_key_b64);
  const valid   = await cryptoOps.verify(pub, sig, payload);

  return {
    hasMkRecord: !!mkRec,
    mkStampMatchesUkId: !!mkRec && mkRec.uk_id === ukId,
    noMasterKeyField: !('_masterKeyCache' in keyring) && keyring._masterKey.constructor.name === 'AsyncFunction',
    opensUnderUnlockKey, opensUnderMasterKey, valid,
    hash8: oKey.hash8,
  };
}
"""

# ── 2/3. Credential change: rung 3 → rung 1 via the real in-app flow ──────────
# Set up a rung-3 install (registration fails → PIN only), mint keys, then let
# registerPasskey() succeed on a second run — the escape route — and confirm
# every private key still unwraps afterwards.

CRED_CHANGE_SETUP_JS = """
async (PIN) => {
  // Force rung 3: registration throws, which the flow grades as rung 3.
  const realRegister = passkey.register;
  passkey.register = async () => { throw new Error('no authenticator'); };
  app._promptPin = async () => PIN;
  await app.registerPasskey();
  passkey.register = realRegister;

  const rung = await keyring._getRung();
  await keyring.unlock(PIN);
  const oKey = await keyring.createKey('O:carried-owner');
  const dKey = await keyring.createKey('D:carried-device');
  const encPub = await keyring.ensureEncryptionKey();

  const mkRec = await keyring._getMasterKeyRecord();
  return { rung, oId: oKey.id, dId: dKey.id, encPub,
           hash8: oKey.hash8, ukIdBefore: mkRec.uk_id };
}
"""

CRED_CHANGE_RUN_JS = """
async ({ PIN, oId, dId, encPub, ukIdBefore }) => {
  // The credential change itself, through the real UI entry point. Registration
  // now succeeds with PRF, so the install flips rung 3 → rung 1 (MK-11).
  app._promptPin = async () => PIN;
  keyring._sessionKey = null;
  await app.registerPasskey();

  const rungAfter = await keyring._getRung();
  const mkRec = await keyring._getMasterKeyRecord();

  // Fully re-unlock with the NEW credential and read everything back.
  keyring._sessionKey = null;
  await keyring.unlock();

  const payload = new TextEncoder().encode('after credential change');
  let oSigns = false, dReads = false, encReads = false;
  try {
    const priv = await keyring.getPrivateKey(oId);
    const sig  = await cryptoOps.sign(priv, payload);
    const rec  = await db.get('my_keys', oId);
    const pub  = await cryptoOps.importPublicKeyB64(rec.public_key_b64);
    oSigns = await cryptoOps.verify(pub, sig, payload);
  } catch (e) { oSigns = 'threw: ' + e.message; }
  try { await keyring.getPrivateKey(dId); dReads = true; } catch (e) { dReads = 'threw: ' + e.message; }
  try { await keyring.getEncryptionPrivateKey(encPub); encReads = true; } catch (e) { encReads = 'threw: ' + e.message; }

  return { rungAfter, ukIdChanged: mkRec.uk_id !== ukIdBefore, oSigns, dReads, encReads };
}
"""

# ── 2b. Abort branch: old unlock key will not open the MK → do not register ───

ABORT_JS = """
async (PIN) => {
  const realRegister = passkey.register;
  passkey.register = async () => { throw new Error('no authenticator'); };
  app._promptPin = async () => PIN;
  await app.registerPasskey();
  passkey.register = realRegister;
  await keyring.unlock(PIN);
  await keyring.createKey('O:abort-owner');

  const mkBefore   = await keyring._getMasterKeyRecord();
  const credBefore = await db.get('settings', 'credential_id');
  const rungBefore = await keyring._getRung();

  // Corrupt the wrap so the old unlock key cannot open the master key. MK-7
  // step 1 must abort the whole flow rather than registering over it.
  const bad = { ...mkBefore, mk_wrapped: cryptoOps.b64enc(crypto.getRandomValues(new Uint8Array(48))) };
  await db.put('settings', { key: keyring.MK_RECORD, value: bad });

  let registerCalled = false;
  passkey.register = async (...a) => { registerCalled = true; return realRegister.apply(passkey, a); };
  keyring._sessionKey = null;
  await app.registerPasskey();
  passkey.register = realRegister;

  const mkAfter   = await keyring._getMasterKeyRecord();
  const credAfter = await db.get('settings', 'credential_id');
  const rungAfter = await keyring._getRung();

  return {
    registerCalled,
    recordUntouched: mkAfter.mk_wrapped === bad.mk_wrapped && mkAfter.uk_id === bad.uk_id,
    credUnchanged: (credBefore?.value ?? null) === (credAfter?.value ?? null),
    rungUnchanged: rungBefore === rungAfter,
  };
}
"""

# ── 4/5. Migration, clean and partially orphaned ──────────────────────────────
# Build a pre-MK fixture the way a real ssd-v79 install looks: private material
# wrapped DIRECTLY under the unlock key, no master-key record, no uk_id. Then
# unlock and let migration run.

MIGRATION_JS = """
async ({ PIN, poison }) => {
  const realRegister = passkey.register;
  passkey.register = async () => { throw new Error('no authenticator'); };
  app._promptPin = async () => PIN;
  await app.registerPasskey();
  passkey.register = realRegister;

  // Tear the install back down to the pre-MK shape.
  await db.del('settings', keyring.MK_RECORD);
  await db.del('settings', keyring.UK_ID);
  keyring._sessionKey = null;
  keyring._migrationReport = null;

  const uk = await keyring._pinToAesKey(PIN);

  // Two signing keys + one encryption key, wrapped directly under the unlock key.
  const mk1 = await cryptoOps.generateKeypair();
  const mk2 = await cryptoOps.generateKeypair();
  const made = [];
  for (const [name, kp] of [['O:legacy-owner', mk1], ['D:legacy-device', mk2]]) {
    const pubB64  = await cryptoOps.exportPublicKeyB64(kp.publicKey);
    const privB64 = await cryptoOps.exportPrivateKeyB64(kp.privateKey);
    const { ciphertext_b64, iv_b64 } = await cryptoOps.encryptPrivateKey(privB64, uk);
    const id = crypto.randomUUID();
    await db.put('my_keys', {
      id, name, hash8: await cryptoOps.hash8(pubB64), public_key_b64: pubB64,
      private_key_encrypted: ciphertext_b64, private_key_iv: iv_b64,
      identicon_algorithm: 'ssd-identicon-1.0', self_image_b64: null,
      created: new Date().toISOString(), expires: null, recheck_interval_days: null,
      revocation_hint: null, is_default: name.startsWith('O:'), is_revoked: false,
    });
    made.push({ id, name });
  }
  const ekp     = await cryptoOps.generateX25519Keypair();
  const encPub  = await cryptoOps.exportX25519PublicKeyB64(ekp.publicKey);
  const encPriv = await cryptoOps.exportX25519PrivateKeyB64(ekp.privateKey);
  const encWrap = await cryptoOps.encryptPrivateKey(encPriv, uk);
  await db.put('encryption_keys', { pub: encPub, enc: encWrap.ciphertext_b64, iv: encWrap.iv_b64,
                                    created: new Date().toISOString(), is_current: true });

  // The partially-orphaned case: rewrap ONE record under a foreign key, exactly
  // as a record wrapped by a previous credential looks.
  let poisonedId = null, poisonedBefore = null;
  if (poison) {
    const foreign = await crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
    const victim  = made[1];                       // the D: key
    const privB64 = await cryptoOps.exportPrivateKeyB64(mk2.privateKey);
    const w       = await cryptoOps.encryptPrivateKey(privB64, foreign);
    const rec     = await db.get('my_keys', victim.id);
    await db.put('my_keys', { ...rec, private_key_encrypted: w.ciphertext_b64, private_key_iv: w.iv_b64 });
    poisonedId     = victim.id;
    poisonedBefore = { ct: w.ciphertext_b64, iv: w.iv_b64 };
  }

  // First unlock after upgrade — migration runs here.
  await keyring.unlock(PIN);

  const rep   = keyring._migrationReport;
  const mkRec = await keyring._getMasterKeyRecord();
  const ukId  = await keyring._getUkId();

  // Everything healthy must now read back under the master key.
  const reads = {};
  for (const m of made) {
    try { await keyring.getPrivateKey(m.id); reads[m.name] = true; }
    catch (e) { reads[m.name] = 'threw'; }
  }
  try { await keyring.getEncryptionPrivateKey(encPub); reads.enc = true; }
  catch (e) { reads.enc = 'threw'; }

  // The orphaned record must be byte-identical to how it was left.
  let orphanUntouched = null;
  if (poison) {
    const now = await db.get('my_keys', poisonedId);
    orphanUntouched = now.private_key_encrypted === poisonedBefore.ct && now.private_key_iv === poisonedBefore.iv;
  }

  return {
    hasMkRecord: !!mkRec, ukIdMinted: !!ukId, stampMatches: mkRec.uk_id === ukId,
    migratedCount: rep ? rep.migrated.length : 0,
    orphanedCount: rep ? rep.orphaned.length : 0,
    orphanedNames: rep ? rep.orphaned.map(o => o.name || o.pub) : [],
    reads, orphanUntouched,
    persisted: !!(await keyring.getMigrationReport()),
  };
}
"""

# ── 6. Detection: uk_id mismatch at unlock ────────────────────────────────────

DETECTION_JS = """
async (PIN) => {
  const realRegister = passkey.register;
  passkey.register = async () => { throw new Error('no authenticator'); };
  app._promptPin = async () => PIN;
  await app.registerPasskey();
  passkey.register = realRegister;
  await keyring.unlock(PIN);
  const oKey = await keyring.createKey('O:doomed-owner');

  // Simulate a credential established without a rewrap: new uk_id, same record.
  await keyring.newUnlockKeyId();
  keyring._sessionKey = null;

  let flagged = false, message = '', name = '', leftUnlocked = true;
  try {
    await keyring.unlock(PIN);
  } catch (e) {
    flagged = e.orphaned === true;
    message = e.message || '';
    name    = e.name;
    leftUnlocked = !!keyring._sessionKey;
  }

  // The accept-loss path must leave a usable keyring behind.
  window.confirm = () => true;
  await app.acceptKeyringLoss();
  const mkRec = await keyring._getMasterKeyRecord();
  const ukId  = await keyring._getUkId();
  let mintsAgain = false;
  try { await keyring.createKey('D:fresh-start'); mintsAgain = true; } catch (_) {}
  const keysLeft = (await db.getAll('my_keys')).map(k => k.name);

  return { flagged, message, name, leftUnlocked, hadKey: !!oKey,
           recovered: !!mkRec && mkRec.uk_id === ukId, mintsAgain, keysLeft };
}
"""


# ── 7. Backup/restore round-trip (SPEC-KEYRING §9 — must be unaffected) ───────
# backupIdentity()/restore go through exportKeyB64, importKey and
# importEncryptionKey, all of which now route through the master key. §9 says
# backup continues to export UNWRAPPED keys and carries no master key.

BACKUP_JS = """
async () => {
  await passkey.register('test-harness');
  await keyring.newUnlockKeyId();
  await keyring.unlock();
  const oKey   = await keyring.createKey('O:backup-owner');
  const encPub = await keyring.ensureEncryptionKey();

  // What a backup payload carries.
  const signingPriv = await keyring.exportKeyB64(oKey.id);
  const encPriv     = await keyring.exportEncryptionPrivateKeyB64(encPub);
  const payload     = { key_name: oKey.name, signing_pub_b64: oKey.public_key_b64,
                        signing_priv_b64: signingPriv, encryption_pub_b64: encPub,
                        encryption_priv_b64: encPriv };
  const carriesMasterKey = JSON.stringify(payload).includes(
    (await keyring._getMasterKeyRecord()).mk_wrapped);

  // Wipe the keyring material and restore from the payload, as the restore path does.
  for (const k of await db.getAll('my_keys')) await db.del('my_keys', k.id);
  for (const e of await db.getAll('encryption_keys')) await db.del('encryption_keys', e.pub);
  const restored = await keyring.importKey(payload.key_name, payload.signing_pub_b64, payload.signing_priv_b64);
  await keyring.importEncryptionKey(payload.encryption_pub_b64, payload.encryption_priv_b64);

  // Re-unlock and use the restored material.
  keyring._sessionKey = null;
  await keyring.unlock();
  const data  = new TextEncoder().encode('restored identity signs');
  const priv  = await keyring.getPrivateKey(restored.id);
  const sig   = await cryptoOps.sign(priv, data);
  const pub   = await cryptoOps.importPublicKeyB64(payload.signing_pub_b64);
  const valid = await cryptoOps.verify(pub, sig, data);
  let encReads = false;
  try { await keyring.getEncryptionPrivateKey(encPub); encReads = true; } catch (_) {}

  return { sameKey: restored.hash8 === oKey.hash8, valid, encReads, carriesMasterKey };
}
"""


async def new_page(browser, url, with_prf=True):
    context = await browser.new_context()
    page    = await context.new_page()
    cdp     = await context.new_cdp_session(page)
    await cdp.send("WebAuthn.enable", {"enableUI": False})
    await cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
        "protocol": "ctap2", "transport": "internal",
        "hasResidentKey": True, "hasUserVerification": True,
        "isUserVerified": True, "hasPrf": with_prf,
    }})
    await page.goto(url)
    await page.wait_for_load_state("domcontentloaded")
    return page


async def main():
    server = subprocess.Popen(
        [sys.executable, '-m', 'http.server', str(PORT), '--directory', str(PWA_DIR)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)

    exe = find_browser()
    browser_name = "Brave" if exe and "Brave" in exe else "Chrome" if exe else "Playwright bundled Chromium"
    print(f"\nSSD master-key (SPEC-KEYRING) tests — {browser_name}\n")

    PIN = "1234"
    try:
        async with async_playwright() as p:
            launch_kwargs = dict(executable_path=exe) if exe else {}
            browser = await p.chromium.launch(**launch_kwargs)

            # 1 ── round-trip
            print("1. Round-trip: mint \u2192 wrap under MK \u2192 unlock \u2192 unwrap \u2192 sign")
            try:
                page = await new_page(browser, BASIC)
                r = await page.evaluate(ROUNDTRIP_JS)
                check(r['hasMkRecord'], "Master-key record written at mint", "No master-key record after mint")
                check(r['mkStampMatchesUkId'], "Record stamped with the current uk_id", "uk_id stamp does not match")
                check(not r['opensUnderUnlockKey'],
                      "Private key does NOT open under the unlock key (MK-1/MK-3)",
                      "Private key still opens under the unlock key — wrapping did not move")
                check(r['opensUnderMasterKey'], "Private key opens under the master key", "Private key will not open under the master key")
                check(r['valid'], f"Sign + verify after re-unlock (hash8={r['hash8']})", "Sign + verify FAILED")
                await page.context.close()
            except Exception as e:
                fail(f"Round-trip threw: {e}")

            # 2/3 ── credential change through the real in-app flow
            print("\n2. Credential change (rung 3 \u2192 rung 1) through advanced.html registerPasskey()")
            try:
                page = await new_page(browser, ADV)
                s = await page.evaluate(CRED_CHANGE_SETUP_JS, PIN)
                check(s['rung'] == 3, "Fixture install is rung 3 (PIN only)", f"Fixture rung is {s['rung']}, expected 3")
                r = await page.evaluate(CRED_CHANGE_RUN_JS, {
                    "PIN": PIN, "oId": s['oId'], "dId": s['dId'],
                    "encPub": s['encPub'], "ukIdBefore": s['ukIdBefore'],
                })
                check(r['rungAfter'] == 1, "Install moved to rung 1 (MK-11 rung transition)", f"Rung after change is {r['rungAfter']}")
                check(r['ukIdChanged'], "Master-key record rewrapped with a fresh uk_id", "uk_id did not change — no rewrap happened")
                check(r['oSigns'] is True, "O: key still signs after the credential change", f"O: key lost: {r['oSigns']}")
                check(r['dReads'] is True, "D: key still unwraps after the credential change", f"D: key lost: {r['dReads']}")
                check(r['encReads'] is True, "X25519 encryption key still unwraps", f"Encryption key lost: {r['encReads']}")
                await page.context.close()
            except Exception as e:
                fail(f"Credential change threw: {e}")

            print("\n2b. Abort branch: old unlock key cannot open the master key")
            try:
                page = await new_page(browser, ADV)
                r = await page.evaluate(ABORT_JS, PIN)
                check(not r['registerCalled'], "credentials.create() was never reached (MK-7 step 1 abort)",
                      "Registration proceeded over material that could not be carried")
                check(r['recordUntouched'], "Master-key record left untouched by the aborted flow", "Aborted flow modified the master-key record")
                check(r['credUnchanged'], "credential_id unchanged by the aborted flow", "Aborted flow changed credential_id")
                check(r['rungUnchanged'], "Rung unchanged by the aborted flow", "Aborted flow changed the rung")
                await page.context.close()
            except Exception as e:
                fail(f"Abort branch threw: {e}")

            # 4 ── migration, clean
            print("\n3. Migration on a pre-existing install (directly-wrapped records)")
            try:
                page = await new_page(browser, ADV)
                r = await page.evaluate(MIGRATION_JS, {"PIN": PIN, "poison": False})
                check(r['hasMkRecord'] and r['ukIdMinted'] and r['stampMatches'],
                      "Master-key record + uk_id created at first unlock (\u00a78 step 2)",
                      "Migration did not create a stamped master-key record")
                check(r['migratedCount'] == 3, "All 3 directly-wrapped records migrated", f"Migrated {r['migratedCount']} of 3")
                check(r['orphanedCount'] == 0, "Nothing reported orphaned on a healthy install", f"{r['orphanedCount']} spuriously orphaned")
                check(all(v is True for v in r['reads'].values()),
                      "Every migrated record reads back under the master key", f"Post-migration reads: {r['reads']}")
                await page.context.close()
            except Exception as e:
                fail(f"Migration threw: {e}")

            # 5 ── migration, partially orphaned
            print("\n4. Migration on a partially-orphaned install")
            try:
                page = await new_page(browser, ADV)
                r = await page.evaluate(MIGRATION_JS, {"PIN": PIN, "poison": True})
                check(r['hasMkRecord'], "Migration completed rather than failing (MK-12)", "Migration aborted on the orphaned record")
                check(r['migratedCount'] == 2, "Healthy cohort (2 records) migrated", f"Migrated {r['migratedCount']} of 2 healthy")
                check(r['orphanedCount'] == 1, f"Orphan reported: {r['orphanedNames']}", f"{r['orphanedCount']} orphans reported, expected 1")
                check(r['orphanUntouched'] is True, "Orphaned record left byte-identical (\u00a78 step 4)", "Orphaned record was modified")
                check(r['reads'].get('O:legacy-owner') is True and r['reads'].get('enc') is True,
                      "Healthy records read back under the master key", f"Post-migration reads: {r['reads']}")
                check(r['reads'].get('D:legacy-device') == 'threw', "Orphaned record still unreadable, as expected",
                      "Orphaned record unexpectedly readable")
                check(r['persisted'], "Migration report persisted for the holder", "Migration report not persisted")
                await page.context.close()
            except Exception as e:
                fail(f"Partial-orphan migration threw: {e}")

            # 6 ── detection + accept-loss recovery
            print("\n5. Detection at unlock (uk_id mismatch) and the accept-loss route")
            try:
                page = await new_page(browser, ADV)
                r = await page.evaluate(DETECTION_JS, PIN)
                check(r['flagged'], "Mismatch caught at keyring.unlock() and flagged orphaned (MK-9)", "Mismatch not detected")
                check(r['name'] != 'OperationError' and len(r['message']) > 0,
                      f"Readable error, not an empty OperationError: \u201c{r['message'][:60]}\u2026\u201d",
                      f"Unreadable failure: {r['name']} / {r['message']!r}")
                check(not r['leftUnlocked'], "No half-unlocked keyring left behind", "Session key survived a detected orphan")
                check(r['recovered'], "Accept-loss re-initialised a usable keyring", "Accept-loss left the keyring unusable")
                check(r['mintsAgain'], "New keys can be minted after accept-loss", "Cannot mint after accept-loss")
                check(r['keysLeft'] == ['D:fresh-start'], "Orphaned keys cleared, only the new key remains", f"Keys left: {r['keysLeft']}")
                await page.context.close()
            except Exception as e:
                fail(f"Detection threw: {e}")

            # 7 ── backup/restore unaffected (§9)
            print("\n6. Backup/restore round-trip (§9 — unaffected by the master key)")
            try:
                page = await new_page(browser, BASIC)
                r = await page.evaluate(BACKUP_JS)
                check(not r['carriesMasterKey'], "Backup payload carries no master key (MK-5/§9)", "Backup payload contains the master key")
                check(r['sameKey'], "Restored key is the same keypair", "Restored key differs")
                check(r['valid'], "Restored O: key signs and verifies", "Restored O: key will not sign")
                check(r['encReads'], "Restored encryption key unwraps", "Restored encryption key will not unwrap")
                await page.context.close()
            except Exception as e:
                fail(f"Backup round-trip threw: {e}")

            await browser.close()
    finally:
        server.terminate()

    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)

asyncio.run(main())
