'use strict';
// ─── KEYRING ─────────────────────────────────────────────────────────────────
// Requires globals: db, cryptoOps, passkey, log
const keyring = {
  _sessionKey: null,
  _rung2GatePassed: false,
  _pinCanary: null,

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
      this._sessionKey = await this._pinToAesKey(pin);
      this._pinCanary = await cryptoOps.encryptBytes(
        new TextEncoder().encode('ssd-pin-canary-v1'), this._sessionKey
      );
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
      this._sessionKey = await this._pinToAesKey(pin);
      this._pinCanary = await cryptoOps.encryptBytes(
        new TextEncoder().encode('ssd-pin-canary-v1'), this._sessionKey
      );
      return this._sessionKey;
    }

    // rung 1 — PRF
    const { aesKey, prfUnsupported } = await passkey.authenticate();
    if (prfUnsupported) {
      throw new Error('PRF not available on this credential — re-register at the correct rung.');
    }
    this._sessionKey = aesKey;
    return this._sessionKey;
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
    const privB64 = await cryptoOps.decryptPrivateKey(rec.enc, rec.iv, this._sessionKey);
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
    const { ciphertext_b64, iv_b64 } = await cryptoOps.encryptPrivateKey(privB64, this._sessionKey);
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
    const { ciphertext_b64, iv_b64 } = await cryptoOps.encryptPrivateKey(privB64, this._sessionKey);
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
    const privB64 = await cryptoOps.decryptPrivateKey(rec.private_key_encrypted, rec.private_key_iv, this._sessionKey);
    return cryptoOps.importPrivateKeyB64(privB64);
  },

  async exportKeyB64(keyId) {
    if (!this._sessionKey) throw new Error('Keyring locked — authenticate first.');
    const rec = await db.get('my_keys', keyId);
    if (!rec) throw new Error('Key not found.');
    return cryptoOps.decryptPrivateKey(rec.private_key_encrypted, rec.private_key_iv, this._sessionKey);
  },

  async importKey(name, pubB64, privB64) {
    if (!this._sessionKey) throw new Error('Keyring locked — authenticate first.');
    const hash8 = await cryptoOps.hash8(pubB64);
    const existing = await db.getAll('my_keys');
    if (existing.some(k => k.hash8 === hash8))
      throw new Error(`Key ${hash8} is already in your keyring.`);
    const { ciphertext_b64, iv_b64 } = await cryptoOps.encryptPrivateKey(privB64, this._sessionKey);
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
