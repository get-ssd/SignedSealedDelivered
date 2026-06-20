'use strict';

// ─── SIGNER ──────────────────────────────────────────────────────────────────
const signer = {
  canonicalise(rawText) {
    let t = rawText;
    const lastTok = t.lastIndexOf('[SSD:');
    if (lastTok !== -1) t = t.slice(0, lastTok);
    t = t.normalize('NFC').replace(/\r\n|\r/g, '\n');
    t = t.split('\n').map(l => l.trimEnd()).join('\n');
    t = t.replace(/^\n+/, '').replace(/\n+$/, '');
    t = this.wrap(t);
    if (!t.endsWith('\n')) t += '\n';
    return t;
  },
  wrap(text, width = 80) {
    return text.split('\n').map(line => {
      if (line.length <= width) return line;
      const words = line.split(' ');
      const out = [];
      let cur = '';
      for (const w of words) {
        if (cur && (cur + ' ' + w).length > width) { out.push(cur); cur = w; }
        else cur = cur ? cur + ' ' + w : w;
      }
      if (cur) out.push(cur);
      return out.join('\n');
    }).join('\n');
  },
  buildSignatureBlock(hash8, name, timestamp, capacity = ['ssd:author'], sigField = '[see signature.json]') {
    return [
      '---',
      `Signed by: ${name} [${hash8}]`,
      `Timestamp: ${timestamp}`,
      `Capacity: ${capacity.join(', ')}`,
      'Render spec: ssd-render-1.0',
      'I rendered this document using the above spec, confirmed the output, and signed it.',
      `Signature: ${sigField}`,
      '---',
    ].join('\n');
  },
  buildRender(wrappedText, signatureBlock) {
    return wrappedText + '\n' + signatureBlock;
  },
};

// ─── RENDER ENGINES ──────────────────────────────────────────────────────────
function _canonicalJSON(v) {
  if (Array.isArray(v)) return '[' + v.map(_canonicalJSON).join(',') + ']';
  if (v !== null && typeof v === 'object') {
    return '{' + Object.keys(v).sort().map(k => JSON.stringify(k) + ':' + _canonicalJSON(v[k])).join(',') + '}';
  }
  return JSON.stringify(v);
}

const renderEngines = {
  'ssd-render-1.0': {
    pack({ source, render }) {
      return {
        'source.txt': fflate.strToU8(source),
        'render.txt': fflate.strToU8(render),
      };
    },
    unpack(files) {
      for (const f of ['source.txt', 'render.txt'])
        if (!files[f]) throw new Error(`Missing ${f} in archive.`);
      return {
        source: fflate.strFromU8(files['source.txt']),
        render: fflate.strFromU8(files['render.txt']),
      };
    },
    hashTargets({ source, render }) {
      const enc = new TextEncoder();
      return { 'source.txt': enc.encode(source), 'render.txt': enc.encode(render) };
    },
    toElement({ render }) {
      const pre = document.createElement('pre');
      pre.className = 'render-preview';
      pre.textContent = render;
      return pre;
    },
  },

  'ssd-json-1.0': {
    pack({ data }) {
      const canonical = _canonicalJSON(data);
      const pretty    = JSON.stringify(data, null, 2);
      return {
        'source.json': fflate.strToU8(canonical),
        'render.json': fflate.strToU8(pretty),
      };
    },
    unpack(files) {
      for (const f of ['source.json', 'render.json'])
        if (!files[f]) throw new Error(`Missing ${f} in archive.`);
      return {
        source: fflate.strFromU8(files['source.json']),
        render: fflate.strFromU8(files['render.json']),
        data:   JSON.parse(fflate.strFromU8(files['source.json'])),
      };
    },
    hashTargets({ source, render }) {
      const enc = new TextEncoder();
      return { 'source.json': enc.encode(source), 'render.json': enc.encode(render) };
    },
    toElement({ render }) {
      const pre = document.createElement('pre');
      pre.className = 'render-preview';
      pre.textContent = render;
      return pre;
    },
  },

  'ssd-key-transfer-1.0': {
    _src(c) { return JSON.stringify({ key_name: c.key_name, signing_pub_b64: c.signing_pub_b64, signing_priv_b64: c.signing_priv_b64 }); },
    pack(content)    { return { 'source.json': fflate.strToU8(this._src(content)) }; },
    unpack(files)    {
      if (!files['source.json']) throw new Error('Missing source.json in key transfer archive.');
      return JSON.parse(fflate.strFromU8(files['source.json']));
    },
    hashTargets(content) { return { 'source.json': new TextEncoder().encode(this._src(content)) }; },
    toElement(content) {
      const pre = document.createElement('pre');
      pre.className = 'render-preview';
      pre.textContent = `Key Transfer\nKey: ${content.key_name}\n\nThis document transfers a signing key to this device.\nVerify the sender below before importing.`;
      return pre;
    },
  },
};

// ─── ARTIFACT ────────────────────────────────────────────────────────────────
const artifact = {
  async pack(content, engineSpec, manifestObj, signatureObj) {
    const engine = renderEngines[engineSpec];
    if (!engine) throw new Error(`Unknown render spec: ${engineSpec}`);
    return fflate.zipSync({
      'manifest.json':  fflate.strToU8(JSON.stringify(manifestObj, null, 2)),
      'signature.json': fflate.strToU8(JSON.stringify(signatureObj, null, 2)),
      ...engine.pack(content),
    });
  },

  async unpack(sealedBytes) {
    let files;
    try { files = fflate.unzipSync(sealedBytes); }
    catch { throw new Error('File is not a valid .ssd archive.'); }
    for (const f of ['manifest.json', 'signature.json'])
      if (!files[f]) throw new Error(`Missing ${f} in archive.`);
    const manifest  = JSON.parse(fflate.strFromU8(files['manifest.json']));
    const signature = JSON.parse(fflate.strFromU8(files['signature.json']));
    const engine = renderEngines[manifest.render_spec];
    if (!engine) throw new Error(`Unknown render spec: ${manifest.render_spec}`);
    const content = engine.unpack(files);
    return { manifest, content, signature, engine };
  },

  async verify(unpacked) {
    const { manifest, content, signature, engine } = unpacked;
    const enc = new TextEncoder();
    const result = {
      signature_valid: false, content_unmodified: false,
      signed_at: signature.signed_at, render_spec: manifest.render_spec,
      signer_hash8: signature.signer_hash8, signer_name: null,
      signer_known: false, trust_types: [], key_expired: false, key_revoked: false,
      recheck_due: false, recheck_overdue: false, decryption_logged: false,
      errors: [], warnings: [],
    };

    // Verify content hashes via engine
    for (const [filename, bytes] of Object.entries(engine.hashTargets(content))) {
      const hash = 'sha256:' + await cryptoOps.sha256(bytes);
      if (hash !== manifest.files?.[filename])
        result.errors.push(`${filename} hash mismatch — content modified.`);
    }
    result.content_unmodified = result.errors.length === 0;

    // Verify signature
    const myKeys = await db.getAll('my_keys');
    const contactKeys = await db.getAll('contact_keys');
    const signerRec = [...myKeys, ...contactKeys].find(k => k.hash8 === signature.signer_hash8);

    if (!signerRec) {
      result.warnings.push(`Signer key ${signature.signer_hash8} not in keyring — cannot verify signature.`);
      return result;
    }

    result.signer_name = signerRec.name;
    result.signer_known = true;
    if (signerRec.is_quarantined) {
      const via = signerRec.received_via || 'unknown source';
      const when = signerRec.received_at ? new Date(signerRec.received_at).toLocaleDateString() : 'unknown date';
      result.errors.push(`This key has been quarantined (received via ${via} on ${when}). Documents signed with it cannot be trusted.`);
      result.key_revoked = true;
      return result;
    }
    result.trust_types = myKeys.find(k => k.id === signerRec.id) ? ['self'] : [signerRec.trust_type || 'peer'];

    // Verify Ed25519 signature over manifest bytes
    const manifestText = JSON.stringify(manifest, null, 2);
    const manifestBytes = enc.encode(manifestText);
    const manifestHash = 'sha256:' + await cryptoOps.sha256(manifestBytes);

    if (manifestHash !== signature.manifest_hash) {
      result.errors.push('Manifest hash in signature.json does not match manifest.json.');
    }

    try {
      const pubKey = await cryptoOps.importPublicKeyB64(signerRec.public_key_b64);
      result.signature_valid = await cryptoOps.verify(pubKey, signature.signature, manifestBytes);
      if (!result.signature_valid) result.errors.push('Ed25519 signature verification failed.');
    } catch (e) {
      result.errors.push('Signature verification error: ' + e.message);
    }

    if (signerRec.is_revoked) { result.key_revoked = true; result.errors.push('Signing key is revoked.'); }
    if (signerRec.expires) {
      const expiry = new Date(signerRec.expires), signedAt = new Date(signature.signed_at);
      if (signedAt > expiry) { result.key_expired = true; result.warnings.push('Document was signed after key expiry.'); }
    }
    if (signerRec.recheck_interval_days && signerRec.last_checked) {
      const days = (Date.now() - new Date(signerRec.last_checked)) / 86400000;
      if (days > signerRec.recheck_interval_days) {
        result.recheck_overdue = true;
        result.warnings.push(`Key recheck overdue — last verified ${Math.floor(days)} days ago.`);
      }
    }

    return result;
  },
};
