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

Human correction on 2026-09-26: PIN-only is a testing fallback, not the intended
demo or normal onboarding route. The explicit PIN-only button is available only
under `?mock`. Normal passkey errors leave onboarding unconfigured. Passkeys
without PRF can still use the existing passkey-plus-PIN rung; this retains the
device authentication gate. The Home status names the actual protection rung.

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
2. In normal mode, register a passkey on each tablet, completing its native
   authentication prompts. Enter Alice, Bob and Carol respectively in **Your
   name**, create each identity, and complete the wizard. Record the actual rung.
3. In Keys, show the key card QR. Each persona scans/imports the other two cards;
   verify **Can seal to**. Paste may be used for a separate automated-data check,
   but record it as paste rather than claiming a camera pass.
4. Alice composes an agreement, reviews it, keeps Me selected, adds Bob and
   Carol, and signs once using the device prompt. Verify the delivery summary.
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
