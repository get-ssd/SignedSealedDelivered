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
    // An identity file is adopted, not kept as a document (and keeping it would
    // itself stop this device counting as fresh).
    if (result.unpacked.content?.type === 'keyring-backup') return null;
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

  // ── Identity backup to a fresh device (SPEC-IDENTITY-TRANSFER) ─────────────
  // An ordinary signed, sealed .ssd: a keyring-backup document signed by the
  // owner's O: key and sealed to the destination device and to the owner's own
  // encryption key, so either can open it. Shared by both shells.

  _sameKey(a, b) {
    return !!a && !!b && cryptoOps.b64enc(cryptoOps.b64urldec(a)) === cryptoOps.b64enc(cryptoOps.b64urldec(b));
  },

  // True when the private key's own public half equals pub (full bytes).
  async _pairMatches(alg, privB64, pub) {
    const priv = await crypto.subtle.importKey('pkcs8', cryptoOps.b64dec(privB64), { name: alg }, true, alg === 'Ed25519' ? ['sign'] : ['deriveBits']);
    const { x } = await crypto.subtle.exportKey('jwk', priv);
    return this._sameKey(x, pub);
  },

  // Contacts with a sealing key — the devices an identity can be sent to.
  async identityDestinations() {
    const keys = await db.getAll('contact_keys');
    return (await db.getAll('contacts')).filter(p => p.encryption_key_pub).map(p => ({
      name: p.local_name, hash8: keys.find(k => k.person_id === p.id)?.hash8 ?? '?', pub: p.encryption_key_pub,
    }));
  },

  // Keyring must be unlocked.
  async buildIdentityBackup(oKey, destPub) {
    const encPub = (await db.get('settings', 'my_encryption_key_pub'))?.value;
    if (!encPub) throw new Error('No encryption key on this device.');
    if (this._sameKey(destPub, encPub)) throw new Error('That is this device’s own sealing key.');
    const persons = await db.getAll('contacts');
    const contacts = (await db.getAll('contact_keys')).map(({ id, person_id, ...pub }) => {
      const p = persons.find(x => x.id === person_id);
      return { ...pub, person_name: p?.local_name ?? null, person_encryption_key_pub: p?.encryption_key_pub ?? null, person_ref: person_id ?? null };
    });
    const content = {
      type: 'keyring-backup', schema: 2,
      key_name: oKey.name, hash8: oKey.hash8,
      signing_pub_b64: oKey.public_key_b64, signing_priv_b64: await keyring.exportKeyB64(oKey.id),
      encryption_pub_b64: encPub, encryption_priv_b64: await keyring.exportEncryptionPrivateKeyB64(encPub),
      identicon_algorithm: oKey.identicon_algorithm, self_image_b64: oKey.self_image_b64 ?? null,
      created: oKey.created, expires: oKey.expires ?? null,
      recheck_interval_days: oKey.recheck_interval_days ?? 90, revocation_hint: oKey.revocation_hint ?? null,
      signed_card: oKey.signed_card ?? null,
      destination_pub_b64: destPub,
      contacts,
    };
    const enc = new TextEncoder();
    const signed_at = new Date().toISOString();
    const engine = renderEngines['ssd-key-transfer-1.0'];
    const files = {};
    for (const [name, bytes] of Object.entries(engine.hashTargets(content))) files[name] = 'sha256:' + await cryptoOps.sha256(bytes);
    const manifestObj = { version: '1.0', render_spec: 'ssd-key-transfer-1.0', signed_at, signer_hash8: oKey.hash8, signer_name: oKey.name, files };
    const manifestBytes = enc.encode(JSON.stringify(manifestObj, null, 2));
    const manifest_hash = 'sha256:' + await cryptoOps.sha256(manifestBytes);
    const privateKey = await keyring.getPrivateKey(oKey.id);
    const signatureObj = { algorithm: 'Ed25519', signer_hash8: oKey.hash8, signing_public_key: oKey.public_key_b64, capacity: ['ssd:author'], signed_at, manifest_hash, signature: await cryptoOps.sign(privateKey, manifestBytes) };
    const innerZip = await artifact.pack(content, 'ssd-key-transfer-1.0', manifestObj, signatureObj);
    return this.seal(innerZip, [{ pub: destPub }, { pub: encPub }], { signed_at, signer_hash8: oKey.hash8, manifest_hash }, privateKey);
  },

  // Opens and fully validates an identity file. Returns its content or throws.
  async openIdentityBackup(bytes, unlock) {
    const r = await this.open(bytes, unlock);
    const seal = r.seal?.state;
    if (seal !== 'S2') throw new Error(seal === 'S3' ? 'This file was not sealed for this device.'
      : seal === 'S1' ? 'This file is not sealed — an identity file always is.'
      : r.signature === 'V7' ? 'This isn’t a readable .ssd file.' : 'This file could not be opened on this device.');
    if (!['V1', 'V2'].includes(r.signature)) throw new Error(`Signature check failed (${r.signature}) — the file may have been altered.`);
    const c = r.unpacked.content;
    if (r.unpacked.manifest.render_spec !== 'ssd-key-transfer-1.0' || c?.type !== 'keyring-backup') throw new Error('This file is not an identity file.');
    if (c.schema !== 2) throw new Error('This identity backup uses an older format that is no longer supported. Send a new one from the source device.');
    if (!String(c.key_name).startsWith('O:') || !c.signing_priv_b64 || !c.encryption_priv_b64 || !c.encryption_pub_b64 || !Array.isArray(c.contacts))
      throw new Error('This identity file is incomplete.');
    if (!this._sameKey(r.unpacked.signature.signing_public_key, c.signing_pub_b64) || await cryptoOps.hash8(c.signing_pub_b64) !== c.hash8)
      throw new Error('This identity file is not signed by the identity it contains.');
    if (!await this._pairMatches('Ed25519', c.signing_priv_b64, c.signing_pub_b64)) throw new Error('The signing key pair in this file does not match.');
    if (!await this._pairMatches('X25519', c.encryption_priv_b64, c.encryption_pub_b64)) throw new Error('The encryption key pair in this file does not match.');
    const slots = JSON.parse(new TextDecoder().decode(bytes)).recipients.map(s => s.for_key);
    if (!slots.some(k => this._sameKey(k, c.encryption_pub_b64)) || !slots.some(k => this._sameKey(k, c.destination_pub_b64)))
      throw new Error('The file’s recipients do not match its contents.');
    return c;
  },

  // Why this device can't take the identity, or null when it has nothing to lose.
  async identityAdoptionBlocker(c) {
    const [docs, drafts, posts, paired, myKeys, encKeys, contactKeys, persons] = await Promise.all(
      ['artifacts', 'drafts', 'social_posts', 'paired_devices', 'my_keys', 'encryption_keys', 'contact_keys', 'contacts'].map(s => db.getAll(s)));
    if (docs.length) return `this device holds ${docs.length} document(s)`;
    if (drafts.length) return `this device holds ${drafts.length} draft(s)`;
    if (posts.length) return `this device holds ${posts.length} social post(s)`;
    if (paired.length) return 'this device has paired devices';
    if (myKeys.some(k => this._sameKey(k.public_key_b64, c.signing_pub_b64))) return 'this device already has this identity';
    const d = myKeys.filter(k => k.name.startsWith('D:')), o = myKeys.filter(k => k.name.startsWith('O:'));
    if (d.length !== 1 || o.length > 1 || myKeys.length !== d.length + o.length) return 'this device has keys beyond its own device key and setup identity';
    if (encKeys.length > 1) return 'this device has more than one encryption key';
    // The only permitted contact is the source owner's own key card.
    const ownerCards = contactKeys.filter(k => this._sameKey(k.public_key_b64, c.signing_pub_b64));
    if (contactKeys.length > ownerCards.length) return `this device has ${contactKeys.length - ownerCards.length} imported contact key(s)`;
    if (persons.some(p => !ownerCards.some(k => k.person_id === p.id))) return 'this device has imported contacts';
    return null;
  },

  // Replaces the setup O: and encryption key with the file's. Keyring must be
  // unlocked. The new keys are added first and the setup ones removed after,
  // so a failure part-way is rolled back to the setup identity.
  async adoptIdentityBackup(c) {
    const blocker = await this.identityAdoptionBlocker(c);
    if (blocker) throw new Error(`Not adopted: ${blocker}. Nothing was changed.`);
    const myKeys = await db.getAll('my_keys');
    const dKey = myKeys.find(k => k.name.startsWith('D:'));
    const tempO = myKeys.find(k => k.name.startsWith('O:'));
    const tempEnc = await db.getAll('encryption_keys');
    const encSetting = await db.get('settings', 'my_encryption_key_pub');
    const ownerCards = (await db.getAll('contact_keys')).filter(k => this._sameKey(k.public_key_b64, c.signing_pub_b64));
    const added = { keyId: null, encPub: null, contactKeys: [], persons: [] };
    try {
      const rec = await keyring.importKey(c.key_name, c.signing_pub_b64, c.signing_priv_b64);
      added.keyId = rec.id;
      await db.put('my_keys', { ...rec, identicon_algorithm: c.identicon_algorithm || rec.identicon_algorithm,
        self_image_b64: c.self_image_b64 ?? null, created: c.created || rec.created, expires: c.expires ?? null,
        recheck_interval_days: c.recheck_interval_days ?? 90, revocation_hint: c.revocation_hint ?? null,
        signed_card: c.signed_card ?? null, received_via: 'identity-transfer' });
      if (!tempEnc.some(k => k.pub === c.encryption_pub_b64)) added.encPub = c.encryption_pub_b64;
      await keyring.importEncryptionKey(c.encryption_pub_b64, c.encryption_priv_b64);

      // Public contacts: fresh local ids, source person grouping kept.
      const personIds = {};
      const own = [c.signing_pub_b64, dKey.public_key_b64, tempO?.public_key_b64];
      for (const ct of c.contacts) {
        if (!ct.hash8 || !ct.public_key_b64 || own.some(k => this._sameKey(k, ct.public_key_b64))) continue;
        const { person_name, person_encryption_key_pub, person_ref, ...pub } = ct;
        const ref = person_ref || crypto.randomUUID();
        if (!personIds[ref]) {
          personIds[ref] = crypto.randomUUID();
          await db.put('contacts', { id: personIds[ref], local_name: person_name || ct.name || ct.hash8, notes: null,
            created: new Date().toISOString(), external_id: null, ...(person_encryption_key_pub ? { encryption_key_pub: person_encryption_key_pub } : {}) });
          added.persons.push(personIds[ref]);
        }
        const id = crypto.randomUUID();
        await db.put('contact_keys', { ...pub, id, person_id: personIds[ref], imported_via: 'identity-transfer' });
        added.contactKeys.push(id);
      }
    } catch (e) {
      if (added.keyId) await db.del('my_keys', added.keyId).catch(() => {});
      if (added.encPub) await db.del('encryption_keys', added.encPub).catch(() => {});
      if (encSetting) await db.put('settings', encSetting).catch(() => {});
      for (const id of added.contactKeys) await db.del('contact_keys', id).catch(() => {});
      for (const id of added.persons) await db.del('contacts', id).catch(() => {});
      throw new Error(`Adoption failed and was rolled back: ${e.message}`);
    }

    // Retire the setup identity and anything that now points at it.
    if (tempO) await db.del('my_keys', tempO.id);
    for (const k of tempEnc) if (k.pub !== c.encryption_pub_b64) await db.del('encryption_keys', k.pub);
    for (const k of ownerCards) {
      await db.del('contact_keys', k.id);
      if (k.person_id) await db.del('contacts', k.person_id);
    }
    // The device key card advertised the retired sealing key.
    await db.put('my_keys', { ...dKey, signed_card: await keyring.exportKeyCard(dKey.id) });
    return { name: c.key_name, hash8: c.hash8 };
  },
};
