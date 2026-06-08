'use strict';
// ─── CRYPTO OPS ──────────────────────────────────────────────────────────────
const cryptoOps = {
  // b64enc/b64dec: standard base64, used for internal storage (AES-GCM, private keys).
  b64enc: buf => btoa(String.fromCharCode(...(buf instanceof Uint8Array ? buf : new Uint8Array(buf)))),
  b64dec: b64 => Uint8Array.from(atob(b64), c => c.charCodeAt(0)),
  // b64urlenc/b64urldec: base64url no-pad (RFC 4648 §5), used for all protocol wire fields.
  b64urlenc: buf => {
    const b64 = btoa(String.fromCharCode(...(buf instanceof Uint8Array ? buf : new Uint8Array(buf))));
    return b64.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  },
  b64urldec: s => {
    s = s.replace(/-/g, '+').replace(/_/g, '/');
    while (s.length % 4) s += '=';
    return Uint8Array.from(atob(s), c => c.charCodeAt(0));
  },

  async generateKeypair() {
    return window.crypto.subtle.generateKey({ name: 'Ed25519' }, true, ['sign', 'verify']);
  },
  async exportPublicKeyB64(key) {
    return this.b64urlenc(await window.crypto.subtle.exportKey('raw', key));
  },
  async exportPrivateKeyB64(key) {
    return this.b64enc(await window.crypto.subtle.exportKey('pkcs8', key));
  },
  async importPublicKeyB64(b64) {
    return window.crypto.subtle.importKey('raw', this.b64urldec(b64), { name: 'Ed25519' }, true, ['verify']);
  },
  async importPrivateKeyB64(b64) {
    return window.crypto.subtle.importKey('pkcs8', this.b64dec(b64), { name: 'Ed25519' }, true, ['sign']);
  },
  async sign(privateKey, dataBytes) {
    const sig = await window.crypto.subtle.sign({ name: 'Ed25519' }, privateKey, dataBytes);
    return this.b64urlenc(sig);
  },
  async verify(publicKey, signatureB64, dataBytes) {
    return window.crypto.subtle.verify({ name: 'Ed25519' }, publicKey, this.b64urldec(signatureB64), dataBytes);
  },
  async sha256(dataBytes) {
    const h = await window.crypto.subtle.digest('SHA-256', dataBytes);
    return Array.from(new Uint8Array(h)).map(b => b.toString(16).padStart(2, '0')).join('');
  },
  async sha256B64(dataBytes) {
    return this.b64enc(await window.crypto.subtle.digest('SHA-256', dataBytes));
  },
  async hash8(publicKeyB64) {
    const raw = this.b64urldec(publicKeyB64);
    const hex = await this.sha256(raw);
    return hex.slice(0, 8).toUpperCase();
  },
  async identiconSeed(publicKeyB64) {
    const raw = this.b64urldec(publicKeyB64);
    const h = await window.crypto.subtle.digest('SHA-256', raw);
    return new Uint8Array(h);
  },
  async encryptPrivateKey(privateKeyB64, aesKey) {
    const data = this.b64dec(privateKeyB64);
    const iv = window.crypto.getRandomValues(new Uint8Array(12));
    const ct = await window.crypto.subtle.encrypt({ name: 'AES-GCM', iv }, aesKey, data);
    return { ciphertext_b64: this.b64enc(ct), iv_b64: this.b64enc(iv) };
  },
  async decryptPrivateKey(ciphertextB64, ivB64, aesKey) {
    const pt = await window.crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: this.b64dec(ivB64) },
      aesKey,
      this.b64dec(ciphertextB64)
    );
    return this.b64enc(pt);
  },
  async generateX25519Keypair() {
    return window.crypto.subtle.generateKey({ name: 'X25519' }, true, ['deriveKey', 'deriveBits']);
  },
  async exportX25519PublicKeyB64(key) {
    return this.b64enc(await window.crypto.subtle.exportKey('raw', key));
  },
  async exportX25519PrivateKeyB64(key) {
    return this.b64enc(await window.crypto.subtle.exportKey('pkcs8', key));
  },
  async importX25519PublicKeyB64(b64) {
    return window.crypto.subtle.importKey('raw', this.b64dec(b64), { name: 'X25519' }, true, []);
  },
  async importX25519PrivateKeyB64(b64) {
    return window.crypto.subtle.importKey('pkcs8', this.b64dec(b64), { name: 'X25519' }, true, ['deriveKey', 'deriveBits']);
  },
  async deriveSharedKey(myPrivateKey, theirPublicKey, info = 'ssd-sync-v1') {
    const bits = await window.crypto.subtle.deriveBits({ name: 'X25519', public: theirPublicKey }, myPrivateKey, 256);
    const hkdf = await window.crypto.subtle.importKey('raw', bits, { name: 'HKDF' }, false, ['deriveKey']);
    return window.crypto.subtle.deriveKey(
      { name: 'HKDF', hash: 'SHA-256', salt: new Uint8Array(32), info: new TextEncoder().encode(info) },
      hkdf, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']
    );
  },
  async encryptJSON(payload, aesKey) {
    const iv = window.crypto.getRandomValues(new Uint8Array(12));
    const ct = await window.crypto.subtle.encrypt({ name: 'AES-GCM', iv }, aesKey, new TextEncoder().encode(JSON.stringify(payload)));
    return { iv_b64: this.b64enc(iv), ct_b64: this.b64enc(ct) };
  },
  async decryptJSON(iv_b64, ct_b64, aesKey) {
    const pt = await window.crypto.subtle.decrypt({ name: 'AES-GCM', iv: this.b64dec(iv_b64) }, aesKey, this.b64dec(ct_b64));
    return JSON.parse(new TextDecoder().decode(pt));
  },
  async encryptBytes(data, aesKey) {
    const iv = window.crypto.getRandomValues(new Uint8Array(12));
    const ct = await window.crypto.subtle.encrypt({ name: 'AES-GCM', iv }, aesKey, data);
    return { iv_b64: this.b64enc(iv), ct_b64: this.b64enc(ct) };
  },
  async decryptBytes(iv_b64, ct_b64, aesKey) {
    const pt = await window.crypto.subtle.decrypt({ name: 'AES-GCM', iv: this.b64dec(iv_b64) }, aesKey, this.b64dec(ct_b64));
    return new Uint8Array(pt);
  },
  async importAESKey(rawBytes) {
    return window.crypto.subtle.importKey('raw', rawBytes, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
  },
  async wrapDEK(dekBytes, recipientPub) {
    const ephemeral    = await this.generateX25519Keypair();
    const ephPubB64    = await this.exportX25519PublicKeyB64(ephemeral.publicKey);
    const transportKey = await this.deriveSharedKey(ephemeral.privateKey, recipientPub, 'ssd-sealed-v1');
    const { iv_b64, ct_b64 } = await this.encryptBytes(dekBytes, transportKey);
    return { ep: ephPubB64, iv: iv_b64, ek: ct_b64 };
  },
  async unwrapDEK(slot, myPrivKey) {
    const ephPub       = await this.importX25519PublicKeyB64(slot.ep);
    const transportKey = await this.deriveSharedKey(myPrivKey, ephPub, 'ssd-sealed-v1');
    return this.decryptBytes(slot.iv, slot.ek, transportKey);
  },
  async verificationCode(pubKeyB64) {
    const hex = await this.sha256(this.b64urldec(pubKeyB64));
    return String(parseInt(hex.slice(0, 8), 16) % 1000000).padStart(6, '0');
  },
};
