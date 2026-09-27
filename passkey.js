'use strict';
// ─── PASSKEY ─────────────────────────────────────────────────────────────────
// Requires globals: db, cryptoOps
const passkey = {
  PRF_SALT: (() => {
    const salt = new Uint8Array(32);
    const src = new TextEncoder().encode('ssd-keyring-v1-salt');
    salt.set(src.slice(0, 32));
    return salt;
  })(),

  _withTimeout(credFn, ms = 20000) {
    return new Promise((resolve, reject) => {
      const ctrl = new AbortController();
      const timer = setTimeout(() => {
        ctrl.abort();
        db.put('settings', { key: 'webauthn_unreliable', value: true }).catch(() => {});
        const err = new Error(
          `The screen lock prompt didn't appear — this can happen on Android tablets in app mode.<br>` +
          `<a href="${location.href}" target="_blank" rel="noopener" style="color:var(--primary)">Open in browser instead</a>` +
          ` — your documents and keys are shared between the app and browser versions.`
        );
        err.isAuthTimeout = true;
        reject(err);
      }, ms);
      credFn(ctrl.signal).then(
        result => { clearTimeout(timer); resolve(result); },
        err    => { clearTimeout(timer); if (err.name === 'AbortError' && ctrl.signal.aborted) return; reject(err); }
      );
    });
  },

  _policyError(message) {
    const error = new Error(message);
    error.code = 'SSD_AUTH_POLICY';
    return error;
  },

  _requireDeviceBound(credential, registration = false) {
    const data = registration
      ? credential?.response?.getAuthenticatorData?.()
      : credential?.response?.authenticatorData;
    if (!(data instanceof ArrayBuffer) || data.byteLength < 37) {
      throw this._policyError('The browser did not return the credential properties needed to check device-bound protection.');
    }
    const flags = new Uint8Array(data)[32];
    if ((flags & 0x05) !== 0x05) {
      throw this._policyError('Local user verification did not complete. SSD has not accepted this credential.');
    }
    // residentKey is only a preference. Enforce the returned BE/BS flags too:
    // a platform authenticator can otherwise supply a cloud-syncable credential.
    if (flags & 0x18) {
      throw this._policyError('SSD requires a device-bound credential. A backup-eligible or synced credential was returned and has not been accepted.');
    }
    if (registration && credential.authenticatorAttachment !== 'platform') {
      throw this._policyError('SSD requires this device\'s local authenticator.');
    }
  },

  async register(label, { requirePrf = false } = {}) {
    const challenge = window.crypto.getRandomValues(new Uint8Array(32));
    const userId = window.crypto.getRandomValues(new Uint8Array(16));
    const rpId = window.location.hostname || 'localhost';

    const cred = await this._withTimeout(signal => navigator.credentials.create({
      publicKey: {
        challenge,
        rp: { name: 'Signed, Sealed, Delivered', id: rpId },
        user: { id: userId, name: label, displayName: label },
        pubKeyCredParams: [
          { alg: -7,   type: 'public-key' },  // ES256  — broadest support
          { alg: -257, type: 'public-key' },  // RS256  — Windows Hello TPM fallback
          { alg: -8,   type: 'public-key' },  // Ed25519 — newer platforms
        ],
        authenticatorSelection: {
          authenticatorAttachment: 'platform',
          userVerification: 'required',
          // SSD retains the credential ID and supplies allowCredentials. It
          // does not need discoverable/account-synced passkey storage.
          residentKey: 'discouraged',
        },
        extensions: { prf: {} },
      },
      signal,
    }));

    this._requireDeviceBound(cred, true);
    const ext = cred.getClientExtensionResults();
    const prfSupported = !!(ext.prf && ext.prf.enabled);
    if (requirePrf && !prfSupported) {
      throw this._policyError('Local device verification works, but this authenticator does not provide the PRF secret SSD needs to protect your keys without an app PIN. Setup has stopped; no SSD identity or PIN fallback was created.');
    }

    const credIdB64 = cryptoOps.b64enc(cred.rawId);
    await db.put('settings', { key: 'credential_id', value: credIdB64 });
    await db.put('settings', { key: 'prf_supported', value: prfSupported });
    return { credIdB64, prfSupported };
  },

  async authenticate() {
    const [credSetting, prfSetting] = await Promise.all([
      db.get('settings', 'credential_id'),
      db.get('settings', 'prf_supported'),
    ]);
    if (!credSetting) throw new Error('No passkey registered. Go to Settings first.');

    const requestPrf = prfSetting?.value === true;
    const credentialId = cryptoOps.b64dec(credSetting.value);
    const challenge = window.crypto.getRandomValues(new Uint8Array(32));
    const rpId = window.location.hostname || 'localhost';

    const assertion = await this._withTimeout(signal => navigator.credentials.get({
      publicKey: {
        challenge,
        rpId,
        allowCredentials: [{ type: 'public-key', id: credentialId }],
        userVerification: 'required',
        ...(requestPrf && { extensions: { prf: { eval: { first: this.PRF_SALT } } } }),
      },
      signal,
    }));

    this._requireDeviceBound(assertion);
    const ext = assertion.getClientExtensionResults();
    const prfOutput = ext.prf?.results?.first;

    if (!prfOutput) return { aesKey: null, prfUnsupported: true };

    const prfKey = await window.crypto.subtle.importKey('raw', prfOutput, { name: 'HKDF' }, false, ['deriveKey']);
    const aesKey = await window.crypto.subtle.deriveKey(
      {
        name: 'HKDF', hash: 'SHA-256',
        salt: new TextEncoder().encode('ssd-keyring-v1'),
        info: new TextEncoder().encode('private-key-encryption'),
      },
      prfKey,
      { name: 'AES-GCM', length: 256 },
      false,
      ['encrypt', 'decrypt']
    );
    return { aesKey };
  },

  async confirmAndSign(privateKey, dataBytes) {
    const credSetting = await db.get('settings', 'credential_id');
    if (!credSetting) {
      // Rung 3 — PIN only, no passkey available for per-sign confirmation
      return cryptoOps.sign(privateKey, dataBytes);
    }

    const credentialId = cryptoOps.b64dec(credSetting.value);
    const challenge = window.crypto.getRandomValues(new Uint8Array(32));
    const rpId = window.location.hostname || 'localhost';

    const assertion = await this._withTimeout(signal => navigator.credentials.get({
      publicKey: {
        challenge,
        rpId,
        allowCredentials: [{ type: 'public-key', id: credentialId }],
        userVerification: 'required',
      },
      signal,
    }));

    this._requireDeviceBound(assertion);
    return cryptoOps.sign(privateKey, dataBytes);
  },
};
