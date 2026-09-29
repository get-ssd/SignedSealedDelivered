# Basic document exchange verification

Run from the PWA repository on this Windows machine:

```
py -3 test/test-basic-exchange.py
py -3 test/test-wizard-rungs.py
py -3 test/test-home-wizard-gate.py
py -3 test/test-master-key.py
py -3 test/test-revocation-smoke.py
py -3 test/test-qr-key-card.py
py -3 test/test-prf-rung1.py
py -3 test/test-rung3-smoke.py
node test/test-key-card-v06.js
node test/test-opfs-identicon-extract.js
node test/test-ssd-param.js
node test/test-passkey-policy.js
```

The new test uses Chrome, Playwright, real WebCrypto and separate fresh browser
contexts. It tests normal passkey setup/signing with a CDP virtual authenticator,
then uses `?mock` PIN setup for repeatable Alice/Bob/Carol fixtures. It exercises
UI imports, review rules, signing, sharing cancellation, download bytes, receiving,
offline dispatcher URLs, signature/seal states, persistence, contact upgrades,
revocations and both directions of sealed exchange with Advanced.

The old QR smoke test currently passes; its previous `card.public_key` error has
already been corrected to `card.signing_public_key`. Camera recognition is still
a physical-device check. The exchange test drives actual paste import through
the UI, including the contact-name dialog.

## Authentication direction

**Superseded in part, 2026-09-29 (ssd-v92):** synced passkeys are accepted. Most
users sign with Google Password Manager, which only issues backup-eligible
credentials, so rejecting BE/BS made signing impossible for them. User
verification, platform attachment on registration, and rejection of the invalid
BS-without-BE state are still enforced. The rejection described below is history.

Human clarification on 2026-09-27: PIN protection is a normal supported fallback,
not a demo-only exception. It is accepted for these tablets. No Google account
or credential sync is acceptable. The two governing rules are: you sign what you
see, and nothing leaves the device unless you choose to send it.

In ssd-v83, registration requests a non-discoverable platform credential with
required user verification. Returned user-presence/verification and backup flags
are checked before persistence and on subsequent assertions. Backup-eligible
credentials are rejected, not merely credentials already backed up. Normal Basic
setup offers device verification plus app-PIN protection when PRF is absent.
Cancelling the PIN choice leaves setup unconfigured. A separate explicit PIN-only
choice is available in normal mode too. Neither path needs a demo flag. An
authentication-policy rejection does not automatically select PIN-only.

Android's screen lock is the first barrier on a stolen locked tablet. The offline
PIN risk requires obtaining SSD's stored keyring first. SSD's PIN-derived wrapping
key then protects the master key, which protects the signing/encryption keys;
this is browser storage, not a hardware-keystore retry-limited PIN operation.
Keep this prerequisite explicit when describing the risk.

This does not make Android's WebAuthn implementation independent of Google Play
Services. Nor can WebAuthn prevent every provider from displaying an account
prompt: `residentKey: 'discouraged'` is a preference, backed by returned-property
validation, not a provider-selection or network-isolation API.

### Actual local-authenticator results (2026-09-26/27)

The original Alice registration invoked Google sign-in and was rejected by the
operator. A separate diagnostic on SERIAL-1 then used `residentKey: 'discouraged'`.
Registration and a subsequent assertion completed through local device
verification. Both returned UP=true, UV=true, BE=false, BS=false. Registration
returned `authenticatorAttachment: 'platform'`, `credProps.rk=false`, and
`prf.enabled=false`; the assertion returned no PRF output. No Google login or
app PIN was used. This proves local authentication, **not** PIN-free keyring
encryption. Bob (SERIAL-2) and Carol (SERIAL-3) subsequently completed both
registration and assertion with identical results: local verification succeeds,
non-syncing credentials, no PRF output. The human entered device PINs. No further
screenshots were taken after the human instructed text-only diagnostics.
These diagnostics are not a passing full SSD exchange demo.
Each localhost diagnostic credential was not attached to an SSD
identity; its ID lived only in the diagnostic tab. Do not claim it was deleted
from the platform.

`?mock` creates a fresh test database on each load. Do not use that mode for a
demo identity expected to survive a reload. No data migration was added.

## Physical acceptance still required

The following device capabilities were observed with the local `ssd-v81` build:

| Serial | Device | Secure localhost | Platform authenticator | `canShare` .ssd / octet-stream | Test-only PIN hidden |
|---|---|---|---|---|---|
| SERIAL-1 | Lenovo TB328FU | Yes | Yes | Yes | Yes |
| SERIAL-2 | Lenovo TB328FU | Yes | Yes | Yes | Yes |
| SERIAL-3 | Samsung SM-T500 | Yes | Yes | Yes | Yes |

The `canShare` observations are file/MIME capability probes, not proof of delivery
through a particular Android share target. No device identity or credential was
created during these probes.

1. Serve the repository on localhost and use ADB reverse for the same port on
   each tablet. The current development check uses `http://localhost:8105/`.
2. In normal mode, use local device registration plus an app PIN, or explicitly
   choose PIN-only. Record the actual protection rung. No account sign-in.
   Enter Alice, Bob and Carol respectively in **Your
   name**, create each identity, and complete the wizard. Record the actual rung.
3. In Keys, show the key card QR. Each persona scans/imports the other two cards;
   verify **Can seal to**. Paste may be used for a separate automated-data check,
   but record it as paste rather than claiming a camera pass.
4. Alice composes an agreement, reviews it, keeps Me selected, adds Bob and
   Carol, checks the complete preview, and signs once using the configured
   confirmation method. Verify the delivery summary. Signing must not send:
   Share or Download remains a separate explicit action.
5. Try Share and record the target and resulting destination path. Also test
   Download. Record any cancelled share separately from a successful transfer.
6. Bob and Carol use **Docs → Open received .ssd** and each see S2, their own
   opening identity, V1 signed by Alice, and the correct document content.
7. If courier delivery is used, record that explicitly. Only attribute files
   whose paths were observed; do not clear arbitrary download directories.
8. When deliberately ending the demo, factory reset clears SSD origin data and
   its stored documents. Downloaded files and platform passkeys need separate,
   explicit cleanup. Keep unrelated browser/profile state intact.

Passkey verification establishes control under the device's verification policy.
On a shared Android profile it does not independently attest that the person
holding the device is the named Alice/Bob/Carol persona.

Phase B (`setup/run/status/setdown` driver) is not started until this physical
acceptance narrative passes. Its original PIN-only premise must be revised to
honour the human's passkey-demo direction and account for credential cleanup.

## Format boundary

New Basic document signatures include `signature.json.signing_public_key` for
cryptographic verification before the signer's contact is known. This field is
checked against both the fingerprint and the signature, and never automatically
imports a contact. The sealed envelope is unchanged.

Advanced remains independent and does not embed that public key. Basic can open
and verify Advanced documents when it holds the signer key; otherwise it reports
that verification cannot be completed. Outer signatures verify envelope metadata
only, so a non-recipient never gets a claim that the hidden content was checked.
