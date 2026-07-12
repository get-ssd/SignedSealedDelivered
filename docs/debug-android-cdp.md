# Debug recipe — live-inspect the SSD PWA on an Android phone (ADB + Chrome DevTools Protocol)

How to attach to the running SSD PWA on a physical Android phone and read/run JS against
its live page — including its IndexedDB keyring — from the dev machine. This is the path
used to diagnose the owner-backup orphaned-key failure (see
`findings-owner-backup-orphaned-keys.md`): the on-device state was read live because the
failure is **data-dependent** and does not reproduce headlessly.

> **Provenance note.** This doc was reconstructed from `cdp_diag.py` (the project's actual
> CDP websocket usage — port 9224, `Runtime.evaluate`), the DB schema in `db.js`, and the
> orphaned-key findings. The earlier version lived only in a per-project agent-memory file,
> which is a methodology dark gap; it now lives here as documentation. Verify the exact
> `adb forward` / page-id values against your session — they are environment-specific.

---

## When to use this

- A bug only manifests **on the device** (rung-1 PRF path, real passkey/PIN, per-install
  IndexedDB state) and is "not headlessly testable".
- You need to read the live keyring (`my_keys`, `encryption_keys`, `settings`) or run an
  arbitrary snippet in the page context (e.g. "does this record decrypt with the current
  `keyring._sessionKey`?").

## Prerequisites

- Phone: **USB debugging** enabled (Developer options), plugged in and authorised
  (`adb devices` shows it as `device`, not `unauthorized`).
- Phone: the SSD PWA open in **Chrome** (CDP exposes Chrome/WebView tabs; Firefox uses a
  different remote-debug protocol — see the Firefox note at the end).
- Dev machine: `adb` on PATH; Python via the **Windows launcher `py`** (not `python3` —
  on this machine `python3` opens the Microsoft Store). Install the websocket client once:
  `py -m pip install websocket-client`.

## Step 1 — forward the DevTools socket over ADB

Android Chrome exposes its DevTools endpoint on an abstract unix socket
(`localabstract:chrome_devtools_remote`). Forward it to a local TCP port. `cdp_diag.py`
uses **9224**, so keep that unless it clashes:

```
adb forward tcp:9224 localabstract:chrome_devtools_remote
```

`adb forward --list` shows active forwards; `adb forward --remove tcp:9224` clears it.

> **Windows caveat:** run `adb` device-side commands (anything touching `/sdcard/`, `adb
> shell`, `adb push`) from the **PowerShell tool, not Git Bash** — Git Bash rewrites
> `/sdcard/...` into a Windows path and the command fails. `adb forward` above is path-free
> and safe from either shell.

## Step 2 — find the SSD page and its websocket URL

With the forward up, list the debuggable targets:

```
curl -s http://localhost:9224/json/list
```

Each entry has a `url`, a `title`, an `id`, and a `webSocketDebuggerUrl`. Find the SSD tab
(its `url` is the deployed PWA origin). Note either the numeric page `id` (e.g. `17`, as
hardcoded in `cdp_diag.py` → `ws://localhost:9224/devtools/page/17`) or copy
`webSocketDebuggerUrl` verbatim — it is the more robust choice because the id changes per
tab/session.

## Step 3 — run JS in the page over CDP

Open the websocket and call `Runtime.evaluate`. Generalised from `cdp_diag.py`:

```python
import websocket, json, sys

WS = sys.argv[1] if len(sys.argv) > 1 else 'ws://localhost:9224/devtools/page/17'

def evaluate(expression, await_promise=False):
    ws = websocket.create_connection(WS, timeout=15, origin='http://localhost')
    ws.send(json.dumps({
        'id': 1,
        'method': 'Runtime.evaluate',
        'params': {
            'expression': expression,
            'returnByValue': True,
            'awaitPromise': await_promise,   # True for async/IndexedDB snippets
        },
    }))
    print(ws.recv())
    ws.close()

evaluate('navigator.userAgent')            # sanity check
```

Notes:
- `origin='http://localhost'` is required — Chrome rejects the CDP websocket without an
  Origin header.
- IndexedDB reads are async, so wrap them in a promise and pass `awaitPromise=True`.
- `returnByValue=True` serialises the result so you get JSON back, not a remote object ref.

## Step 4 — read the SSD keyring (IndexedDB `ssd-keyring`, v7)

The keyring DB is named **`ssd-keyring`**. Relevant stores (`db.js`):

| Store | keyPath | Holds |
|-------|---------|-------|
| `my_keys` | `id` | owner/device/signer keys (`O:`, `D:`, `fb:` …), private key wrapped under the session key |
| `encryption_keys` | `pub` | X25519 encryption keypairs; private half wrapped under the session key |
| `settings` | `key` | scalar settings incl. `my_encryption_key_pub` (which enc key is current) |
| `contact_keys`, `contacts`, `paired_devices`, `artifacts`, `drafts`, `social_posts` | `id`/… | the rest |

Dump every record of a store (async → `awaitPromise=True`):

```js
new Promise((resolve, reject) => {
  const r = indexedDB.open('ssd-keyring');
  r.onerror = () => reject(r.error);
  r.onsuccess = () => {
    const dbh = r.result;
    const tx  = dbh.transaction(['my_keys','encryption_keys','settings'], 'readonly');
    const out = {};
    const grab = (name) => new Promise((res) => {
      const q = tx.objectStore(name).getAll();
      q.onsuccess = () => { out[name] = q.result; res(); };
    });
    Promise.all([grab('my_keys'), grab('encryption_keys'), grab('settings')])
      .then(() => resolve(JSON.stringify(out, (k, v) =>
        v instanceof ArrayBuffer ? `[ArrayBuffer ${v.byteLength}]` : v)));
  };
})
```

This tells you **which keys exist** and **which enc key `my_encryption_key_pub` points at**
— enough to settle "is the key missing?" (H1/H2) vs "is the key present but unreadable?".

## Step 5 — the orphaned-key discriminator (what actually cracked the backup bug)

The decisive test was not *whether* keys exist but *whether each one decrypts with the
**current** unlock session key* (`keyring._sessionKey`). rung-1 PRF is deterministic, so a
record wrapped under a superseded credential throws a WebCrypto `OperationError` (with an
**empty `.message`** — which is why the failure was invisible). After the user has unlocked
(so `keyring._sessionKey` is populated), evaluate against the loaded page globals:

```js
// requires the app loaded and unlocked in that tab, so keyring._sessionKey is set
(async () => {
  const rows = [];
  for (const rec of await db.getAll('encryption_keys')) {
    try { await keyring.exportEncryptionPrivateKeyB64(rec.pub); rows.push([rec.pub, 'ok']); }
    catch (e) { rows.push([rec.pub, `FAIL ${e.name}:${e.message||'(blank)'}`]); }
  }
  return JSON.stringify(rows);
})()
```

Split into an "ok" cohort and a "FAIL OperationError" cohort ⇒ the FAIL cohort was wrapped
under a session key the current credential no longer reproduces (a passkey re-registration
or rung change with no rewrap-on-credential-change). That is the confirmed root cause in
`findings-owner-backup-orphaned-keys.md`.

## Teardown

```
adb forward --remove tcp:9224
```

## Firefox note

FF Android does **not** speak CDP; it uses the Remote Debugging Protocol over
`localabstract:org.mozilla.firefox/firefox-debugger` and `about:debugging` on the desktop
side. This recipe is Chrome/Chromium-WebView only. (FF Android is otherwise a supported SSD
target — passkey works — but live-debug it through `about:debugging`, not the steps above.)
