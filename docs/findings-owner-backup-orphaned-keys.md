# Findings — owner-backup failure: orphaned keys under a stale session key

Batch: owner-backup-debug · Diagnosed live on device (Android, ADB + CDP) 2026-07-11 · Fix shipped in **ssd-v77** (commit 630d655).
Audience: **Designer** — the root cause below is a key-lifecycle defect, not a backup-path bug. CC scope (error surfacing + graceful message) is done; the recovery/rewrap is yours.

## What the user saw
`backupIdentity()` threw immediately with a blank red "backup failed" banner and an empty console (Android, rung-1 PRF; and a slow Windows desktop). Deployed version at time of report: ssd-v75.

## Confirmed cause (not speculation — read from live IndexedDB)
- Failing step: `keyring.exportEncryptionPrivateKeyB64` → `cryptoOps.decryptPrivateKey` (keyring.js:158 / cryptoOps.js:64).
- Thrown: WebCrypto `OperationError` with an **empty `.message`** — which is why the old catch (`${e.message}`) rendered a blank banner and logged nothing.
- Decrypting every key with the **current** rung-1 PRF session key splits them into two cohorts:

  | Key | Created | Decrypts with current session key |
  |-----|---------|-----------------------------------|
  | `O:My Identity` | Jun 27 | ✅ |
  | `D:My Device` | recent | ✅ |
  | `fb:Owner FB Key` (default signer) | Jun 17 | ❌ OperationError |
  | encryption key (`is_current`) | **Jun 17** | ❌ OperationError |

- rung-1 PRF is deterministic (same credential + salt ⇒ same AES key every unlock), so anything wrapped under the current credential decrypts. The Jun-17 records do not ⇒ they were wrapped under a session key the current credential no longer reproduces. Interpretation: a **passkey re-registration / rung change ~Jun 27**; keys created after that were wrapped under the new session key, the Jun-17 records were orphaned.
- `keyring.js` has **no rewrap-on-credential-change path**: the only `encryptPrivateKey` sites are creation-time (`ensureEncryptionKey`, `createKey`, `importEncryptionKey`, `importKey`). Nothing re-wraps existing records when the session key changes.

Hypothesis verdict: **H1 refuted** (O: exists and decrypts), **H2 refuted** (enc key + `my_encryption_key_pub` present and matching), **H3/H4 refuted** (the export/seal code is correct; the data is unreadable). This is the H1/H2 family — a key that should be usable is present but cryptographically orphaned.

## Cross-install note
Each browser install has a **separate** IndexedDB keyring. Observed: 2 of 3 installs hit the orphaned-key path ("can't be decrypted"), 1 has a healthy keyring and backs up fine. This is consistent — the fault is per-keyring data state, not code.

## Serious latent impact (beyond backup)
The current encryption key being undecryptable also breaks `getEncryptionPrivateKey` (keyring.js:88). On an affected install the user **cannot open sealed `.ssd` documents sent to their encryption key**, and **restore is affected** too. Backup was simply the first operation to touch the encryption key.

## Designer decisions needed
1. **Rewrap-on-key-change.** When the passkey credential (or rung) changes, existing `encryption_keys` + signing-key records must be re-encrypted under the new session key, or the change must be blocked until they are. Absent this, every credential change orphans pre-existing keys.
2. **Recovery for already-orphaned installs.** Before any re-mint that would discard the orphaned key, determine whether ~Jun 27 was:
   - a **rung change** (old PIN still known) ⇒ the old session key is reproducible; the orphaned `encryption_keys` / `fb:` records may be re-wrappable, or
   - a **credential re-registration** ⇒ old PRF output is gone; those records are unrecoverable and a fresh encryption key must be minted (accepting that inbound docs sealed to the old key are lost).

## Out of scope here (unchanged)
First-registration O:-mint fix; restore-path verification; Keys-tab layout; share/revoke. The misleading `passkey.js` >20s-timeout copy ("screen-lock prompt didn't appear… open in browser") also fires on a slow **desktop** — noted, untouched (rung-1 PRF path).
