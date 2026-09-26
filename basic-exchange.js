'use strict';

// Basic-shell exchange primitives.  The envelope and contact shapes deliberately
// match advanced.html; this module is a plain global so the PWA keeps no build step.
const basicExchange = {
  async saveContact(card, receivedVia, localName) {
    if (await cryptoOps.hash8(card.signing_public_key) !== card.hash8) throw new Error('Signing key fingerprint mismatch.');
    await cryptoOps.importPublicKeyB64(card.signing_public_key);
    const keys = await db.getAll('contact_keys');
    const existing = keys.find(k => k.hash8 === card.hash8);
    const incomingEnc = card.encryption_public_key
      ? cryptoOps.b64enc(cryptoOps.b64urldec(card.encryption_public_key)) : null;
    if (incomingEnc) await cryptoOps.importX25519PublicKeyB64(incomingEnc);
    if (existing) {
      if (cryptoOps.b64enc(cryptoOps.b64urldec(existing.public_key_b64)) !== cryptoOps.b64enc(cryptoOps.b64urldec(card.signing_public_key))) {
        throw new Error('Fingerprint collision: the signing keys differ. Existing contact kept.');
      }
      const person = existing.person_id ? await db.get('contacts', existing.person_id) : null;
      if (incomingEnc && person?.encryption_key_pub && person.encryption_key_pub !== incomingEnc) {
        return { state: 'conflict', existing: await cryptoOps.hash8(person.encryption_key_pub), incoming: await cryptoOps.hash8(incomingEnc) };
      }
      if (!incomingEnc) return { state: 'known', canSeal: !!person?.encryption_key_pub };
      let personId = existing.person_id;
      if (!personId) {
        personId = crypto.randomUUID();
        await db.put('contacts', { id: personId, local_name: localName, notes: null, created: new Date().toISOString(), external_id: null, ...(incomingEnc ? { encryption_key_pub: incomingEnc } : {}) });
        await db.put('contact_keys', { ...existing, person_id: personId });
      } else if (incomingEnc && !person?.encryption_key_pub) {
        await db.put('contacts', { ...person, encryption_key_pub: incomingEnc });
      } else return { state: 'known', canSeal: !!person?.encryption_key_pub };
      return { state: 'upgraded', canSeal: !!incomingEnc };
    }
    const personId = crypto.randomUUID();
    await db.put('contacts', { id: personId, local_name: localName, notes: null, created: new Date().toISOString(), external_id: null, ...(incomingEnc ? { encryption_key_pub: incomingEnc } : {}) });
    await db.put('contact_keys', {
      id: crypto.randomUUID(), person_id: personId, name: card.name || localName,
      hash8: card.hash8, public_key_b64: card.signing_public_key,
      identicon_algorithm: card.identicon_algorithm || 'ssd-identicon-1.0', self_image_b64: card.self_image || null,
      received_via: receivedVia, received_at: new Date().toISOString(), expires: card.expires || null,
      recheck_interval_days: card.recheck_interval_days || 90, last_checked: new Date().toISOString(),
      revocation_hint: card.revocation_hint || null, trust_type: 'peer', local_label: null,
      local_identicon_algorithm: null, countersignatures: [], is_revoked: false, is_quarantined: false,
      quarantined_at: null, quarantine_reason: null,
    });
    return { state: 'added', canSeal: !!incomingEnc, personId };
  },

  async seal(innerZip, recipients, meta, privateKey) {
    const enc = new TextEncoder();
    const outerMeta = JSON.stringify({ ssd: 'sealed-enc-v1', signed_at: meta.signed_at, signer_hash8: meta.signer_hash8, manifest_hash: meta.manifest_hash });
    const outerSig = await cryptoOps.sign(privateKey, enc.encode(outerMeta));
    const dekBytes = crypto.getRandomValues(new Uint8Array(32));
    const dekKey = await cryptoOps.importAESKey(dekBytes);
    const encrypted = await cryptoOps.encryptBytes(innerZip, dekKey);
    const slots = [];
    for (const r of recipients) {
      const pub = await cryptoOps.importX25519PublicKeyB64(r.pub);
      slots.push({ for_key: r.pub, ...await cryptoOps.wrapDEK(dekBytes, pub) });
    }
    return enc.encode(JSON.stringify({ ssd: 'sealed-enc-v1', signed_at: meta.signed_at, signer_hash8: meta.signer_hash8, manifest_hash: meta.manifest_hash, outer_sig: outerSig, recipients: slots, iv: encrypted.iv_b64, ct: encrypted.ct_b64 }));
  },

  async unwrap(rawBytes, unlock) {
    let envelope = null;
    try { const p = JSON.parse(new TextDecoder().decode(rawBytes)); if (p.ssd === 'sealed-enc-v1') envelope = p; } catch {}
    if (!envelope) return { seal: { state: 'S1', count: 0 }, zipBytes: rawBytes };
    if (!Array.isArray(envelope.recipients) || !envelope.recipients.length || envelope.recipients.some(r => !r || typeof r.for_key !== 'string') || typeof envelope.iv !== 'string' || typeof envelope.ct !== 'string') throw new Error('Malformed sealed envelope.');
    const held = await db.getAll('encryption_keys');
    const slot = envelope.recipients?.find(r => held.some(k => k.pub === r.for_key));
    if (!slot) return { seal: { state: 'S3', count: envelope.recipients?.length || 0 }, envelope };
    try {
      await unlock();
      const priv = await keyring.getEncryptionPrivateKey(slot.for_key);
      const dek = await cryptoOps.unwrapDEK(slot, priv);
      const key = await cryptoOps.importAESKey(dek);
      const identity = (await db.get('settings', 'my_device_name'))?.value || 'this device';
      return { seal: { state: 'S2', count: envelope.recipients.length, key: slot.for_key, identity }, envelope, zipBytes: await cryptoOps.decryptBytes(envelope.iv, envelope.ct, key) };
    } catch (error) { return { seal: { state: 'S4', count: envelope.recipients?.length || 0 }, envelope, error }; }
  },

  async open(rawBytes, unlock) {
    let opened;
    try { opened = await this.unwrap(rawBytes, unlock); }
    catch (e) { return { seal: null, signature: 'V7', detail: e.message }; }
    const result = { seal: opened.seal, signature: 'V6', signer_hash8: opened.envelope?.signer_hash8 || null, signer_name: null, outerOnly: !opened.zipBytes };
    const myKeys = await db.getAll('my_keys');
    const contacts = await db.getAll('contact_keys');
    const held = [...myKeys, ...contacts];
    if (!opened.zipBytes) {
      const e = opened.envelope;
      const known = held.find(k => k.hash8 === e.signer_hash8);
      if (known && e.outer_sig) {
        const meta = JSON.stringify({ ssd: 'sealed-enc-v1', signed_at: e.signed_at, signer_hash8: e.signer_hash8, manifest_hash: e.manifest_hash });
        try {
          const key = await cryptoOps.importPublicKeyB64(known.public_key_b64);
          const valid = await cryptoOps.hash8(known.public_key_b64) === e.signer_hash8 && await cryptoOps.verify(key, e.outer_sig, new TextEncoder().encode(meta));
          result.signature = !valid ? 'V5' : known.is_revoked || known.is_quarantined ? 'V3' : 'V1';
          result.signer_name = known.name;
        } catch { result.signature = 'V5'; }
      }
      return result;
    }
    let unpacked, manifestBytes;
    try {
      unpacked = await artifact.unpack(opened.zipBytes);
      manifestBytes = fflate.unzipSync(opened.zipBytes)['manifest.json'];
      if (!unpacked.manifest || !unpacked.signature || !unpacked.manifest.files) throw new Error('Missing signature or manifest.');
    } catch (e) { return { ...result, signature: 'V7', detail: e.message }; }
    const { manifest, signature, content, engine } = unpacked;
    result.unpacked = unpacked;
    result.signer_hash8 = signature.signer_hash8;
    result.signed_at = manifest.signed_at;
    for (const [name, bytes] of Object.entries(engine.hashTargets(content))) {
      if ('sha256:' + await cryptoOps.sha256(bytes) !== manifest.files[name]) return { ...result, signature: 'V4' };
    }
    const hash = 'sha256:' + await cryptoOps.sha256(manifestBytes);
    if (hash !== signature.manifest_hash || (opened.envelope && hash !== opened.envelope.manifest_hash)) return { ...result, signature: 'V4' };
    if (signature.algorithm !== 'Ed25519' || signature.signer_hash8 !== manifest.signer_hash8) return { ...result, signature: 'V5' };
    const known = held.find(k => k.hash8 === signature.signer_hash8);
    const pub = signature.signing_public_key || known?.public_key_b64;
    // Advanced's current artifacts omit the embedded key. Never call an
    // unverified signature valid when neither source supplies a public key.
    if (!pub) return { ...result, signature: 'V6' };
    try {
      if (await cryptoOps.hash8(pub) !== signature.signer_hash8) return { ...result, signature: 'V5' };
      if (known && cryptoOps.b64enc(cryptoOps.b64urldec(pub)) !== cryptoOps.b64enc(cryptoOps.b64urldec(known.public_key_b64))) return { ...result, signature: 'V5' };
      const key = await cryptoOps.importPublicKeyB64(pub);
      if (!await cryptoOps.verify(key, signature.signature, manifestBytes)) return { ...result, signature: 'V5' };
    } catch { return { ...result, signature: 'V5' }; }
    result.signature = known ? known.is_revoked || known.is_quarantined ? 'V3' : 'V1' : 'V2';
    result.signer_name = known?.name || null;
    if (known && signature.capacity?.includes('ssd:revocation')) {
      for (const [store, records] of [['my_keys', myKeys], ['contact_keys', contacts]]) {
        for (const record of records.filter(k => k.hash8 === signature.signer_hash8)) await db.put(store, { ...record, is_revoked: true });
      }
      result.revocation_applied = true;
      result.signature = 'V3';
    }
    return result;
  },

  async persistReceived(bytes, result) {
    const hash = 'sha256:' + await cryptoOps.sha256(bytes);
    const existing = (await db.getAll('artifacts')).find(a => a.artifact_hash === hash);
    const now = new Date().toISOString();
    const verification = { seal_state: result.seal?.state || null, signature_state: result.signature, verified_at: now, opening_identity: result.seal?.identity || null };
    if (existing) {
      await db.put('artifacts', { ...existing, ...verification });
      return existing.id;
    }
    if (!['S1', 'S2'].includes(result.seal?.state) || !['V1', 'V2', 'V3'].includes(result.signature)) return null;
    const id = crypto.randomUUID();
    const path = `artifacts/${id}.ssd`;
    await opfsStore.write(path, bytes);
    await db.put('artifacts', {
      id, direction: 'received', received_at: now, created: now, signed_at: result.signed_at,
      hash8: result.signer_hash8, signer_hash8: result.signer_hash8, artifact_hash: hash,
      render_spec: result.unpacked.manifest.render_spec, encrypted: result.seal.state === 'S2',
      opfs_path: path, manifest_hash: result.unpacked.signature.manifest_hash,
      summary: String(result.unpacked.content.source || 'Received document').slice(0, 80), ...verification,
    });
    return id;
  },
};
