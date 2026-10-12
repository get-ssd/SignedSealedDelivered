'use strict';
// One explicit local sync. Only public fields are sent to Tick; no export or
// unlock is needed. The extension asks for approval before exposing its keys.
const tickSync = {
  _busy: false,
  _request(action, fields = {}, timeout = 120000) {
    return new Promise((resolve, reject) => {
      const id = crypto.randomUUID();
      const timer = setTimeout(() => { cleanup(); reject(new Error(action === 'hello'
        ? 'Tick is not available here. Install or update Tick, then reload SSD in the same browser.'
        : 'Sync did not finish. Some additions may already be saved; click Sync again to finish safely.')); }, timeout);
      const cleanup = () => { clearTimeout(timer); window.removeEventListener('message', listener); };
      const listener = event => {
        const data = event.data;
        if (event.source !== window || event.origin !== location.origin || data?.channel !== 'SSD_TICK_KEYS_V1' ||
            data.direction !== 'response' || data.id !== id) return;
        cleanup();
        if (data.ok) resolve(data); else reject(new Error(data.error || 'Tick sync failed.'));
      };
      window.addEventListener('message', listener);
      window.postMessage({ channel: 'SSD_TICK_KEYS_V1', direction: 'request', id, action, ...fields }, location.origin);
    });
  },

  // This allowlist is the wire format, not a copy of either database schema.
  async _public(row, own = false) {
    const input = row.public_key_b64;
    if (typeof input !== 'string' || !/^[A-Za-z0-9+/_-]+={0,2}$/.test(input)) throw new Error('Invalid public key.');
    const bytes = cryptoOps.b64urldec(input);
    if (bytes.length !== 32 || await cryptoOps.hash8(input) !== row.hash8) throw new Error('Public key fingerprint mismatch.');
    const source = ['direct', 'bundle', 'profile', 'url'].includes(row.source) ? row.source
      : own || row.trust_type === 'peer' ? 'direct' : 'unknown';
    const out = { hash8: row.hash8, public_key_b64: cryptoOps.b64enc(bytes),
      name: typeof row.name === 'string' ? row.name : row.hash8, source,
      trust_type: typeof row.trust_type === 'string' ? row.trust_type : source === 'direct' ? 'peer' : 'unverified',
      is_revoked: !!(row.is_revoked || row.revoked || row.revoked_at), is_quarantined: row.is_quarantined === true };
    for (const field of ['identity', 'issued', 'expires', 'revoked_at', 'quarantine_reason', 'vouched_by', 'bundle_name']) {
      if (typeof row[field] === 'string') out[field] = row[field];
    }
    return out;
  },

  async run() {
    if (this._busy) throw new Error('A key sync is already in progress.');
    this._busy = true;
    let wroteSSD = false;
    try {
      const myKeys = await db.getAll('my_keys');
      const ownerRow = myKeys.find(k => k.name.startsWith('O:') && !k.is_revoked);
      if (!ownerRow) throw new Error('Set up your SSD identity first.');
      const owner = await this._public(ownerRow, true);
      await this._request('hello', {}, 2000);
      const response = await this._request('read', { owner });
      if (!Array.isArray(response.records) || response.records.length > 10000) throw new Error('Tick returned an invalid public-key collection.');
      const incoming = await Promise.all(response.records.map(row => this._public(row)));
      const own = new Map(await Promise.all(myKeys.map(async row => [row.hash8, await this._public(row, true)])));
      const idb = await db.init();
      // Merge within one transaction: an invalid/conflicting collection never
      // leaves a partially imported SSD set. Repeated syncs are additive.
      const result = await new Promise((resolve, reject) => {
        const tx = idb.transaction('contact_keys', 'readwrite');
        const store = tx.objectStore('contact_keys');
        const get = store.getAll();
        const counts = { added: 0, updated: 0, kept: 0 };
        let failure;
        get.onsuccess = () => {
          try {
            const existing = new Map(get.result.map(row => [row.hash8, row]));
            for (const record of incoming) {
              const held = own.get(record.hash8) || existing.get(record.hash8);
              if (held) {
                const heldPub = cryptoOps.b64enc(cryptoOps.b64urldec(held.public_key_b64));
                if (heldPub !== record.public_key_b64) throw new Error(`Conflicting public key ${record.hash8}; nothing was written to SSD.`);
                if (own.has(record.hash8)) continue; // Tick cannot edit SSD's own keys.
                if (held.name !== record.name || (held.source || (held.trust_type === 'peer' ? 'direct' : 'unknown')) !== record.source ||
                    held.trust_type !== record.trust_type) counts.kept++;
                if ((record.is_revoked && !held.is_revoked) || (record.is_quarantined && !held.is_quarantined)) {
                  const merged = { ...held, is_revoked: held.is_revoked || record.is_revoked,
                    is_quarantined: held.is_quarantined || record.is_quarantined };
                  store.put(merged); existing.set(record.hash8, merged); counts.updated++;
                }
              } else {
                const now = new Date().toISOString();
                const added = { ...record, id: crypto.randomUUID(), person_id: null,
                  trust_type: record.trust_type || (record.source === 'direct' ? 'peer' : 'unverified'),
                  identicon_algorithm: 'ssd-identicon-1.0', received_via: 'tick-sync', received_at: now,
                  recheck_interval_days: 90, last_checked: now };
                store.put(added); existing.set(record.hash8, added); counts.added++;
              }
            }
          } catch (error) { failure = error; tx.abort(); }
        };
        tx.oncomplete = () => resolve(counts);
        tx.onabort = tx.onerror = () => reject(failure || tx.error || new Error('SSD key sync was not saved.'));
      });
      wroteSSD = true;
      const currentKeys = await db.getAll('my_keys');
      if (!currentKeys.some(k => k.hash8 === owner.hash8 && !k.is_revoked)) throw new Error('SSD identity changed during sync.');
      const contacts = await db.getAll('contact_keys');
      const records = [...await Promise.all(currentKeys.map(row => this._public(row, true))),
        ...await Promise.all(contacts.map(row => this._public(row)))];
      const tick = await this._request('merge', { owner, records, token: response.token });
      return { ssd: result, tick };
    } catch (error) {
      if (wroteSSD) error.message += ' SSD additions were kept. Click Sync again to finish; existing keys will not be duplicated.';
      throw error;
    } finally { this._busy = false; }
  },
};
