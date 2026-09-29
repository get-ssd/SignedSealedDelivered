'use strict';
// ─── KEYRING ─────────────────────────────────────────────────────────────────
// Requires globals: db, cryptoOps, passkey, log
//
// Key hierarchy (SPEC-KEYRING.md §4):
//
//   unlock credential  (passkey PRF, or PIN)
//         │  derive
//         ▼
//    unlock key (UK)   — ephemeral, never stored. `_sessionKey` below.
//         │  AES-GCM wrap
//         ▼
//    master key (MK)   — random 256-bit, generated once, wrapped in `settings`
//         │  AES-GCM wrap
//         ▼
//   private material   — my_keys.private_key_encrypted, encryption_keys.enc
//
// The unlock key wraps the master key and nothing else (MK-1). This is what
// makes a credential change a rewrap of one small record instead of an
// impossible rewrap of everything — see `beginCredentialChange` below.
//
// The master key does NOT raise at-rest strength (MK-10): that is still the
// strength of the current rung's unlock key. Do not describe it as if it does.
const keyring = {
  _sessionKey: null,
  _rung2GatePassed: false,
  _pinCanary: null,
  // Outcome of the last migration run, for honest reporting to the holder.
  _migrationReport: null,

  MK_RECORD: 'master_key',
  UK_ID:     'uk_id',

  async _getRung() {
    const rec = await db.get('settings', 'keyring_rung');
    return rec ? rec.value : 1;
  },

  async setRung(n) {
    await db.put('settings', { key: 'keyring_rung', value: n });
  },

  // unlock([pin]) — omit pin for rung-1 (PRF) and the rung-2 WebAuthn gate step.
  // Throws { pinRequired: true } when a PIN is needed but not supplied.
  // Rung 2: first call triggers WebAuthn gate, then throws pinRequired; second call (with pin) unwraps.
  async unlock(pin = null) {
    const rung = await this._getRung();

    if (rung === 3) {
      if (pin === null) {
        const err = new Error('PIN required'); err.pinRequired = true; throw err;
      }
      this._sessionKey = await this._checkedPinKey(pin);
      this._pinCanary = await cryptoOps.encryptBytes(
        new TextEncoder().encode('ssd-pin-canary-v1'), this._sessionKey
      );
      await this._afterUnlock();
      return this._sessionKey;
    }

    if (rung === 2) {
      if (!this._rung2GatePassed) {
        await passkey.authenticate(); // gate only — PRF result ignored
        this._rung2GatePassed = true;
      }
      if (pin === null) {
        const err = new Error('PIN required'); err.pinRequired = true; throw err;
      }
      this._rung2GatePassed = false;
      this._sessionKey = await this._checkedPinKey(pin);
      this._pinCanary = await cryptoOps.encryptBytes(
        new TextEncoder().encode('ssd-pin-canary-v1'), this._sessionKey
      );
      await this._afterUnlock();
      return this._sessionKey;
    }

    // rung 1 — PRF
    const { aesKey, prfUnsupported } = await passkey.authenticate();
    if (prfUnsupported) {
      throw new Error('PRF not available on this credential — re-register at the correct rung.');
    }
    this._sessionKey = aesKey;
    await this._afterUnlock();
    return this._sessionKey;
  },

  // PIN → unlock key, refused unless it opens the stored master key. Without this
  // a wrong PIN left the keyring "unlocked" with a key that decrypts nothing, and
  // later unlocks never prompted again. No master-key record yet (fresh install,
  // pre-MK migration) means there is nothing to check against.
  async _checkedPinKey(pin) {
    const key = await this._pinToAesKey(pin);
    if (await this._getMasterKeyRecord()) {
      try { await this._masterKey(key); }
      catch { const err = new Error('Incorrect PIN.'); err.wrongPin = true; throw err; }
    }
    return key;
  },

  // ── Master key (SPEC-KEYRING §5, §6) ───────────────────────────────────────

  // Runs once per unlock, immediately after the unlock key is derived. Three
  // outcomes: initialise (fresh install), migrate (pre-MK install), or detect
  // an orphan (MK-9). Never returns a value — it either completes or throws.
  async _afterUnlock() {
    const mkRec = await this._getMasterKeyRecord();
    let ukId = await this._getUkId();

    if (!mkRec) {
      // No master-key record: either a fresh install with nothing to carry, or
      // a pre-MK install whose material is still wrapped under the unlock key.
      // §8 step 2 — a pre-migration install has no uk_id either; mint one now.
      if (!ukId) ukId = await this.newUnlockKeyId();
      await this._migrate(ukId);
      return;
    }

    // MK-9 — vintage comparison, not a decrypt attempt. Costs nothing, and
    // reports the condition instead of surfacing WebCrypto's OperationError,
    // whose .message is empty (the ssd-v77 finding).
    if (ukId !== mkRec.uk_id) {
      this._sessionKey = null;   // never leave a half-unlocked keyring behind
      this._pinCanary  = null;
      const err = new Error(
        'This keyring was locked with a different passkey or PIN. The stored keys ' +
        'cannot be opened with the credential this device now has.'
      );
      err.orphaned = true;
      throw err;
    }
  },

  async _getMasterKeyRecord() {
    const rec = await db.get('settings', this.MK_RECORD);
    return rec ? rec.value : null;
  },

  async _getUkId() {
    const rec = await db.get('settings', this.UK_ID);
    return rec ? rec.value : null;
  },

  // MK-6 — called wherever an unlock key is newly established: passkey
  // registration, and PIN set at rung 2/3 setup. Deliberately NOT credential_id,
  // which is never set at rung 3 (B8) and so cannot serve as a uniform marker.
  // Not secret, carries no key material.
  async newUnlockKeyId() {
    const id = cryptoOps.b64enc(window.crypto.getRandomValues(new Uint8Array(16)));
    await db.put('settings', { key: this.UK_ID, value: id });
    return id;
  },

  // MK-2 — extractable, because it must be re-exportable in order to be rewrapped.
  async _generateMasterKey() {
    return window.crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, true, ['encrypt', 'decrypt']);
  },

  async _writeMasterKeyRecord(mk, unlockKey, ukId) {
    const raw = new Uint8Array(await window.crypto.subtle.exportKey('raw', mk));
    const { iv_b64, ct_b64 } = await cryptoOps.encryptBytes(raw, unlockKey);
    await db.put('settings', {
      key: this.MK_RECORD,
      value: { mk_wrapped: ct_b64, iv: iv_b64, uk_id: ukId, created: new Date().toISOString() },
    });
  },

  // MK-4 — the master key is unwrapped for the operation that needs it and
  // dropped when that operation returns. No module- or closure-scoped retention:
  // there is deliberately no `_masterKey` field on this object.
  async _masterKey(unlockKey = this._sessionKey) {
    if (!unlockKey) throw new Error('Keyring locked.');
    const rec = await this._getMasterKeyRecord();
    if (!rec) throw new Error('Keyring not initialised — no master key record.');
    const raw = await cryptoOps.decryptBytes(rec.iv, rec.mk_wrapped, unlockKey);
    return window.crypto.subtle.importKey('raw', raw, { name: 'AES-GCM', length: 256 }, true, ['encrypt', 'decrypt']);
  },

  // MK-7 step 1 — unwrap the master key with the CURRENT (old) unlock key,
  // before the new credential is registered. If this throws, the caller MUST
  // abort the credential change: registering over the old wrap orphans
  // everything under it, and rewrapping afterwards is not a thing that exists.
  async beginCredentialChange() {
    if (!this._sessionKey) throw new Error('Unlock with the current credential first.');
    return this._masterKey(this._sessionKey);
  },

  // MK-7 steps 2–4 — the new credential is registered and its unlock key
  // derived; rewrap the master key under it and stamp a fresh uk_id. Private
  // material is not touched at any point (step 4).
  async completeCredentialChange(mk, newUnlockKey) {
    const ukId = await this.newUnlockKeyId();
    await this._writeMasterKeyRecord(mk, newUnlockKey, ukId);
    this._sessionKey = newUnlockKey;
    const rung = await this._getRung();
    this._pinCanary = rung === 1 ? null : await cryptoOps.encryptBytes(
      new TextEncoder().encode('ssd-pin-canary-v1'), newUnlockKey
    );
  },

  // §8 / MK-12 — runs once, at first unlock after upgrade. All-or-nothing per
  // record: nothing is replaced until its master-key-wrapped form is written
  // and verified readable. A record that will not unwrap under the unlock key
  // is ALREADY orphaned — it is left byte-for-byte untouched and reported, and
  // it does not fail the migration for the healthy cohort.
  async _migrate(ukId) {
    const mk = await this._generateMasterKey();
    await this._writeMasterKeyRecord(mk, this._sessionKey, ukId);

    const [myKeys, encKeys] = await Promise.all([
      db.getAll('my_keys'), db.getAll('encryption_keys'),
    ]);
    if (!myKeys.length && !encKeys.length) { this._migrationReport = null; return; }

    const migrated = [], orphaned = [];

    for (const rec of myKeys) {
      try {
        const privB64 = await cryptoOps.decryptPrivateKey(rec.private_key_encrypted, rec.private_key_iv, this._sessionKey);
        const { ciphertext_b64, iv_b64 } = await cryptoOps.encryptPrivateKey(privB64, mk);
        await cryptoOps.decryptPrivateKey(ciphertext_b64, iv_b64, mk);   // verify before replacing
        await db.put('my_keys', { ...rec, private_key_encrypted: ciphertext_b64, private_key_iv: iv_b64 });
        migrated.push({ store: 'my_keys', id: rec.id, name: rec.name, hash8: rec.hash8 });
      } catch {
        orphaned.push({ store: 'my_keys', id: rec.id, name: rec.name, hash8: rec.hash8 });
      }
    }

    for (const rec of encKeys) {
      try {
        const privB64 = await cryptoOps.decryptPrivateKey(rec.enc, rec.iv, this._sessionKey);
        const { ciphertext_b64, iv_b64 } = await cryptoOps.encryptPrivateKey(privB64, mk);
        await cryptoOps.decryptPrivateKey(ciphertext_b64, iv_b64, mk);   // verify before replacing
        await db.put('encryption_keys', { ...rec, enc: ciphertext_b64, iv: iv_b64 });
        migrated.push({ store: 'encryption_keys', pub: rec.pub, is_current: !!rec.is_current });
      } catch {
        orphaned.push({ store: 'encryption_keys', pub: rec.pub, is_current: !!rec.is_current });
      }
    }

    // The mark for an orphaned record lives here rather than on the record —
    // §8 says leave it untouched, §5.3 says do not change its shape.
    this._migrationReport = { at: new Date().toISOString(), migrated, orphaned };
    await db.put('settings', { key: 'mk_migration_report', value: this._migrationReport });
    log(`keyring migrated to master key: ${migrated.length} carried, ${orphaned.length} already orphaned`);
  },

  async getMigrationReport() {
    const rec = await db.get('settings', 'mk_migration_report');
    return rec ? rec.value : null;
  },

  async ensureUnlocked(pin = null) {
    if (!this._sessionKey) await this.unlock(pin);
    return this._sessionKey;
  },

  async verifyPin(pin) {
    if (!this._pinCanary) throw new Error('No PIN canary — unlock the keyring first.');
    const testKey = await this._pinToAesKey(pin);
    try {
      const plain = await cryptoOps.decryptBytes(this._pinCanary.iv_b64, this._pinCanary.ct_b64, testKey);
      return new TextDecoder().decode(plain) === 'ssd-pin-canary-v1';
    } catch {
      return false;
    }
  },

  async _pinToAesKey(pin) {
    const saltRec = await db.get('settings', 'pin_salt');
    if (!saltRec?.value) throw new Error('No PIN configured — enable PIN fallback in Settings first.');
    const keyMaterial = await crypto.subtle.importKey(
      'raw', new TextEncoder().encode(pin), 'PBKDF2', false, ['deriveKey']
    );
    return crypto.subtle.deriveKey(
      { name: 'PBKDF2', salt: cryptoOps.b64dec(saltRec.value), iterations: 200000, hash: 'SHA-256' },
      keyMaterial, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']
    );
  },

  async getEncryptionPrivateKey(pubB64) {
    if (!this._sessionKey) throw new Error('Keyring locked.');
    const rec = await db.get('encryption_keys', pubB64);
    if (!rec) throw new Error('No matching encryption key found.');
    const privB64 = await cryptoOps.decryptPrivateKey(rec.enc, rec.iv, await this._masterKey());
    return cryptoOps.importX25519PrivateKeyB64(privB64);
  },

  async ensureEncryptionKey() {
    const allKeys = await db.getAll('encryption_keys');
    const current = allKeys.find(k => k.is_current);
    if (current) {
      await db.put('settings', { key: 'my_encryption_key_pub', value: current.pub });
      return current.pub;
    }
    const kp = await cryptoOps.generateX25519Keypair();
    const pubB64  = await cryptoOps.exportX25519PublicKeyB64(kp.publicKey);
    const privB64 = await cryptoOps.exportX25519PrivateKeyB64(kp.privateKey);
    const { ciphertext_b64, iv_b64 } = await cryptoOps.encryptPrivateKey(privB64, await this._masterKey());
    await db.put('encryption_keys', { pub: pubB64, enc: ciphertext_b64, iv: iv_b64, created: new Date().toISOString(), is_current: true });
    await db.put('settings', { key: 'my_encryption_key_pub', value: pubB64 });
    log('encryption key generated');
    return pubB64;
  },

  async createKey(name) {
    await this.ensureUnlocked();
    const { publicKey, privateKey } = await cryptoOps.generateKeypair();
    const pubB64 = await cryptoOps.exportPublicKeyB64(publicKey);
    const privB64 = await cryptoOps.exportPrivateKeyB64(privateKey);
    const { ciphertext_b64, iv_b64 } = await cryptoOps.encryptPrivateKey(privB64, await this._masterKey());
    const hash8 = await cryptoOps.hash8(pubB64);
    const existing = await db.getAll('my_keys');
    // Ensure global encryption key exists (generates it if this is the first key)
    await this.ensureEncryptionKey();
    const record = {
      id: window.crypto.randomUUID(), name, hash8,
      public_key_b64: pubB64,
      private_key_encrypted: ciphertext_b64,
      private_key_iv: iv_b64,
      identicon_algorithm: 'ssd-identicon-1.0',
      self_image_b64: null,
      created: new Date().toISOString(),
      expires: null, recheck_interval_days: null, revocation_hint: null,
      is_default: existing.length === 0,
      is_revoked: false,
    };
    await db.put('my_keys', record);
    return record;
  },

  async getPrivateKey(keyId) {
    if (!this._sessionKey) throw new Error('Keyring locked — authenticate first.');
    const rec = await db.get('my_keys', keyId);
    if (!rec) throw new Error('Key not found.');
    const privB64 = await cryptoOps.decryptPrivateKey(rec.private_key_encrypted, rec.private_key_iv, await this._masterKey());
    return cryptoOps.importPrivateKeyB64(privB64);
  },

  async exportKeyB64(keyId) {
    if (!this._sessionKey) throw new Error('Keyring locked — authenticate first.');
    const rec = await db.get('my_keys', keyId);
    if (!rec) throw new Error('Key not found.');
    return cryptoOps.decryptPrivateKey(rec.private_key_encrypted, rec.private_key_iv, await this._masterKey());
  },

  async exportEncryptionPrivateKeyB64(pubB64) {
    if (!this._sessionKey) throw new Error('Keyring locked — authenticate first.');
    const rec = await db.get('encryption_keys', pubB64);
    if (!rec) throw new Error('No matching encryption key found.');
    return cryptoOps.decryptPrivateKey(rec.enc, rec.iv, await this._masterKey());
  },

  async importEncryptionKey(pubB64, privB64) {
    if (!this._sessionKey) throw new Error('Keyring locked — authenticate first.');
    const { ciphertext_b64, iv_b64 } = await cryptoOps.encryptPrivateKey(privB64, await this._masterKey());
    await db.put('encryption_keys', { pub: pubB64, enc: ciphertext_b64, iv: iv_b64, created: new Date().toISOString(), is_current: true });
    await db.put('settings', { key: 'my_encryption_key_pub', value: pubB64 });
  },

  async importKey(name, pubB64, privB64) {
    if (!this._sessionKey) throw new Error('Keyring locked — authenticate first.');
    const hash8 = await cryptoOps.hash8(pubB64);
    const existing = await db.getAll('my_keys');
    if (existing.some(k => k.hash8 === hash8))
      throw new Error(`Key ${hash8} is already in your keyring.`);
    const { ciphertext_b64, iv_b64 } = await cryptoOps.encryptPrivateKey(privB64, await this._masterKey());
    const record = {
      id: window.crypto.randomUUID(), name, hash8,
      public_key_b64: pubB64,
      private_key_encrypted: ciphertext_b64, private_key_iv: iv_b64,
      identicon_algorithm: 'ssd-identicon-1.0', self_image_b64: null,
      created: new Date().toISOString(),
      expires: null, recheck_interval_days: null, revocation_hint: null,
      is_default: existing.length === 0, is_revoked: false,
    };
    await db.put('my_keys', record);
    return record;
  },

  async exportKeyCard(keyId) {
    const rec = await db.get('my_keys', keyId);
    if (!rec) throw new Error('Key not found.');
    const encKeySetting = await db.get('settings', 'my_encryption_key_pub');
    const cardData = {
      chain: [],
      encryption_public_key: encKeySetting
        ? cryptoOps.b64urlenc(cryptoOps.b64dec(encKeySetting.value))
        : null,
      expires: rec.expires,
      hash8: rec.hash8,
      identicon_algorithm: rec.identicon_algorithm,
      issued: rec.created,
      name: rec.name,
      recheck_interval_days: rec.recheck_interval_days ?? 90,
      revocation_hint: rec.revocation_hint,
      self_image: rec.self_image_b64,
      signing_algorithm: 'Ed25519',
      signing_public_key: rec.public_key_b64,
      version: '1.0',
    };
    const sortedKeys = Object.keys(cardData).sort();
    const canonical = JSON.stringify(Object.fromEntries(sortedKeys.map(k => [k, cardData[k]])));
    const dataBytes = new TextEncoder().encode(canonical);
    const privateKey = await this.getPrivateKey(keyId);
    const selfSigned = await cryptoOps.sign(privateKey, dataBytes);
    return { ...cardData, self_signed: selfSigned };
  },

  async verifyKeyCard(card) {
    const { self_signed, ...data } = card;
    const sortedKeys = Object.keys(data).sort();
    const canonical = JSON.stringify(Object.fromEntries(sortedKeys.map(k => [k, data[k]])));
    const dataBytes = new TextEncoder().encode(canonical);
    const publicKey = await cryptoOps.importPublicKeyB64(card.signing_public_key);
    return cryptoOps.verify(publicKey, self_signed, dataBytes);
  },
};
