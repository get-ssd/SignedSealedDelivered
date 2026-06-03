'use strict';
// ─── DB ───────────────────────────────────────────────────────────────────────
// Requires globals: MOCK_MODE, MOCK_DB (defined inline before this script loads)
const db = {
  _db: null,
  async init() {
    if (this._db) return this._db;
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(MOCK_MODE ? MOCK_DB : 'ssd-keyring', 6);
      req.onupgradeneeded = e => {
        const idb = e.target.result;
        const tx  = e.target.transaction;

        if (e.oldVersion < 1) {
          for (const store of ['my_keys', 'contact_keys', 'artifacts', 'settings']) {
            idb.createObjectStore(store, { keyPath: store === 'settings' ? 'key' : 'id' });
          }
        }

        if (e.oldVersion < 2) {
          if (!idb.objectStoreNames.contains('contacts')) {
            idb.createObjectStore('contacts', { keyPath: 'id' });
          }
        }

        if (e.oldVersion < 3) {
          if (!idb.objectStoreNames.contains('paired_devices')) {
            idb.createObjectStore('paired_devices', { keyPath: 'id' });
          }
        }

        if (e.oldVersion < 4) {
          idb.createObjectStore('encryption_keys', { keyPath: 'pub' });
        }

        if (e.oldVersion < 5) {
          idb.createObjectStore('drafts', { keyPath: 'id' });
        }

        if (e.oldVersion < 6) {
          idb.createObjectStore('social_posts', { keyPath: 'id' });
        }

        if (e.oldVersion < 2) {
          // Migrate existing contact_keys: create a person record for each
          if (e.oldVersion >= 1) {
            const contactsStore = tx.objectStore('contacts');
            tx.objectStore('contact_keys').openCursor().onsuccess = function(ev) {
              const cursor = ev.target.result;
              if (!cursor) return;
              const rec = cursor.value;
              if (!rec.person_id) {
                const personId = crypto.randomUUID();
                contactsStore.add({ id: personId, local_name: rec.name, notes: null,
                  created: rec.received_at || new Date().toISOString(), external_id: null });
                cursor.update({ ...rec, person_id: personId });
              }
              cursor.continue();
            };
          }
        }
      };
      req.onsuccess = e => { this._db = e.target.result; resolve(this._db); };
      req.onerror = e => reject(e.target.error);
      req.onblocked = () => {
        document.body.innerHTML = '<div style="padding:40px;max-width:400px;margin:60px auto;font-family:sans-serif;color:#ccc;background:#1a1a1a;border-radius:8px"><h2 style="margin-bottom:12px;color:#fff">Refresh needed</h2><p>Close all other tabs of this app, then refresh this page.</p></div>';
        reject(new Error('Database upgrade blocked — close other tabs and refresh.'));
      };
    });
  },
  async _tx(store, mode, fn) {
    const idb = await this.init();
    return new Promise((resolve, reject) => {
      const tx = idb.transaction(store, mode);
      const req = fn(tx.objectStore(store));
      req.onsuccess = () => resolve(req.result);
      req.onerror = e => reject(e.target.error);
    });
  },
  async get(store, key)    { return this._tx(store, 'readonly',  s => s.get(key)); },
  async getAll(store)      { return this._tx(store, 'readonly',  s => s.getAll()); },
  async put(store, value)  { return this._tx(store, 'readwrite', s => s.put(value)); },
  async del(store, key)    { return this._tx(store, 'readwrite', s => s.delete(key)); },
};
