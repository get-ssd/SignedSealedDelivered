"""SSD demonstration driver — SPEC-BASIC-EXCHANGE §10.

    py demo/ssd_demo.py setup
    py demo/ssd_demo.py run [--exchange=qr|paste] [--delivery=share|courier]
    py demo/ssd_demo.py status
    py demo/ssd_demo.py setdown

Personas and serials come from demo/personas.json (see personas.example.json).
The app must be served on the configured port (00-startup.bat serves 8105).
All app actions go through the tablet UI. Human steps (PIN entry, QR scan,
share sheet) are announced with the persona and exact action; the driver waits
for them and asserts the result. It never reads PINs.
"""
import argparse
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

from devices import Timeline, load_devices, wait_until

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "runs")
SESSION = os.path.join(RUNS, "session.json")
AGREEMENT = "Agreement: Alice, Bob and Carol will meet on Friday at 10:00 to review the SSD demonstration."
HUMAN_TIMEOUT = 900
PAUSE = 1.5  # seconds between visible UI actions, so the operator can follow

STATE_JS = """async () => {
  const s = {version: typeof APP_VERSION !== 'undefined' ? APP_VERSION : null};
  if (typeof db === 'undefined' || !db._db) return s;
  s.rung = (await db.get('settings','keyring_rung'))?.value ?? null;
  const owner = (await db.getAll('my_keys')).find(k => k.name.startsWith('O:') && !k.is_revoked);
  s.identity = owner ? {name: owner.name.slice(2), hash8: owner.hash8} : null;
  s.wizard_done = !!(await db.get('settings','home_wizard_done'))?.value;
  const persons = await db.getAll('contacts');
  s.contacts = (await db.getAll('contact_keys')).map(k => ({name: k.name, hash8: k.hash8,
    label: persons.find(p => p.id === k.person_id)?.encryption_key_pub ? 'Can seal to' : 'Verify only'}));
  s.docs = (await db.getAll('artifacts')).length;
  return s;
}"""


def load_session():
    if os.path.exists(SESSION):
        with open(SESSION, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_session(session):
    os.makedirs(RUNS, exist_ok=True)
    with open(SESSION, "w", encoding="utf-8") as f:
        json.dump(session, f, indent=2)


class Demo:
    def __init__(self, pw, devices, tl, shots):
        self.pw, self.devices, self.tl, self.shots = pw, devices, tl, shots
        self.by_name = {d.persona: d for d in devices}
        self.auto_go = False
        self.reseal_check = False

    # ── helpers ────────────────────────────────────────────────────────────
    def attach_all(self):
        for d in self.devices:
            d.attach(self.pw)
            d.page.on("dialog", lambda dlg, d=d: self._dialog(d, dlg))
            self.tl.log(f"{d}: attached to Chrome (Android user {d.user}), {d.page.url}")

    def _dialog(self, d, dlg):
        self.tl.log(f"{d}: {dlg.type} dialog answered OK: {dlg.message.splitlines()[0][:70]!r}"
                    + (f" -> {dlg.default_value!r}" if dlg.type == "prompt" else ""))
        dlg.accept(dlg.default_value if dlg.type == "prompt" else None)

    def state(self, d):
        return d.page.evaluate(STATE_JS)

    def home(self, d):
        d.page.goto(d.base() + "/", wait_until="domcontentloaded")
        d.page.wait_for_function("typeof APP_VERSION!=='undefined' && document.getElementById('home-loading').style.display==='none'", timeout=30000)

    def tab(self, d, name):
        d.page.locator(f"#tab-{name}-btn").click()

    def shot(self, step):
        if self.shots:
            for d in self.devices:
                d.screenshot(self.shots, step)

    def pin_visible(self, d):
        return d.page.locator("#_pin-overlay-input").is_visible()

    def step(self, title, captions):
        """Stop before a step: show what comes next on each tablet, wait for
        the operator's go (demo/runs/go), then run it. captions: {device: text}."""
        self.step_no = getattr(self, "step_no", 0) + 1
        for d in self.devices:
            d.caption(f"NEXT (automatic — just watch): {captions[d]}" if d in captions else "nothing on this tablet", "#b36b00" if d in captions else "#555")
        self.tl.log(f"STEP {self.step_no}: {title} — waiting for go")
        if self.auto_go:
            self.tl.log(f"STEP {self.step_no}: auto-released by operator instruction")
        else:
            go = os.path.join(RUNS, "go")
            while not os.path.exists(go):
                time.sleep(0.3)
            os.remove(go)
        for d in self.devices:
            d.caption(captions.get(d, "watching"), "#c00" if d in captions else "#555")
        self.tl.log(f"STEP {self.step_no}: {title} — running")

    def pause(self, seconds=PAUSE):
        time.sleep(seconds)

    def human(self, pairs, timeout=HUMAN_TIMEOUT):
        """pairs: [(device, action, done_fn)]. Announce all, wait for all."""
        print()
        for d, action, _ in pairs:
            print(f"  >>> {d.persona.upper()} tablet is ready: {action}", flush=True)
            d.caption(f"YOUR TURN: {action}", "#0a7d2c")
        print(flush=True)
        for d, action, _ in pairs:
            self.tl.log(f"{d}: waiting for human — {action}")
        pending = list(pairs)
        end = time.time() + timeout
        while pending and time.time() < end:
            for p in list(pending):
                try:
                    if p[2]():
                        pending.remove(p)
                        self.tl.log(f"{p[0]}: human step done")
                except Exception:
                    pass
            time.sleep(0.5)
        for d, action, _ in pending:
            self.tl.check(False, f"{d}: human step not completed in {timeout}s — {action}")
        return not pending

    def auto_pin(self, d):
        """Type the configured demo PIN into a visible PIN box (operator direction
        2026-09-27: the operator types Alice's, the driver types the others')."""
        if not d.pin or not self.pin_visible(d):
            return False
        d.page.locator("#_pin-overlay-input").press_sequentially(d.pin, delay=150)
        self.pause(0.6)
        d.page.locator("#_pin-overlay-ok").click()
        self.tl.log(f"{d}: demo PIN typed by driver")
        wait_until(lambda: not self.pin_visible(d), 10)
        return True

    def unlock_if_asked(self, d, why):
        if self.auto_pin(d):
            return
        if self.pin_visible(d):
            self.human([(d, f"enter your SSD PIN ({why}) and press Confirm", lambda: not self.pin_visible(d))])

    # ── setdown ────────────────────────────────────────────────────────────
    def setdown(self):
        self.attach_all()
        session = load_session()
        self.step("wipe SSD on all three tablets (Demo user's SSD site only)", {d: "wiping SSD data on this tablet" for d in self.devices})
        for d in self.devices:
            self.home(d)
            self.tab(d, "keys")
            with d.page.expect_navigation(timeout=30000):
                d.page.get_by_role("button", name="Wipe all data…").click()
            d.page.wait_for_function("typeof db!=='undefined' && !!db._db", timeout=30000)
            s = self.state(d)
            self.tl.check(s.get("rung") is None and not s.get("identity") and not s.get("contacts") and not s.get("docs"),
                          f"{d}: SSD origin state wiped (keyring, contacts, docs)")
        removed = []
        if session:
            for item in session.get("created", []):
                d = self.by_name.get(item["persona"])
                if d and d.downloads().get(item["row"]) == item["name"]:
                    d.delete_download(item["row"])
                    removed.append(f"{item['persona']}: Download/{item['name']}")
            os.remove(SESSION)
        for r in removed:
            self.tl.log(f"removed recorded file {r}")
        self.tl.log("remaining (not removed by setdown):")
        for line in ["SSD service worker and cache on each tablet (app installation)",
                     "Chrome site permissions (e.g. camera), history, other sites' data",
                     "files received via the share sheet whose location the driver did not observe (manual check)",
                     "the Android Demo user and everything outside the SSD site — never touched"]:
            self.tl.log("  - " + line)
        for d in self.devices:
            unknown = [n for i, n in d.downloads().items() if n.endswith(".ssd")]
            if unknown:
                self.tl.log(f"  - {d}: .ssd files in Download not created by this session (left in place): {unknown}")

    # ── setup ──────────────────────────────────────────────────────────────
    def setup(self):
        if load_session():
            raise SystemExit("A demo session is already set up — run setdown first.")
        self.attach_all()
        self.step("set up Alice, Bob and Carol (PIN-only)", {d: "setting up a fresh identity" for d in self.devices})
        ready = []
        for d in self.devices:
            self.home(d)
            s = self.state(d)
            if s.get("rung") is not None or s.get("identity"):
                self.tl.check(False, f"{d}: SSD identity already present ({s.get('identity')}) — run setdown first; setup skipped here")
                continue
            self.tl.log(f"{d}: {s['version']}, no SSD identity — starting PIN-only setup")
            d.page.locator("#wz-btn-pin").click()
            d.page.locator("#_pin-overlay-input").wait_for(state="visible", timeout=10000)
            ready.append(d)
        if not ready:
            return
        for d in ready:
            if self.auto_pin(d):
                d.caption("set up with demo PIN", "#c00")
        humans = [d for d in ready if not d.pin]
        self.human([(d, "type a 4-digit app PIN (your choice; you will need it to sign and open) and press Confirm",
                     lambda d=d: self.state(d).get("rung") == 3 and not self.pin_visible(d)) for d in humans])
        for d in ready:
            d.page.locator("#wz-identity-name").fill(d.persona)
            d.page.locator("#wz-btn-2").click()
            self.unlock_if_asked(d, "create identity")
            wait_until(lambda: (self.state(d).get("identity") or {}).get("name") == d.persona, 30)
            d.page.locator("#wz-btn-3").click()
            wait_until(lambda: self.state(d).get("wizard_done"), 15)
            s = self.state(d)
            self.tl.check(s.get("identity", {}).get("name") == d.persona and s.get("rung") == 3 and s.get("wizard_done"),
                          f"{d}: identity {s.get('identity')} created, rung {s.get('rung')} "
                          f"({d.page.locator('#home-passkey-status').inner_text().strip()!r})")
        self.shot("setup")
        save_session({"started": time.strftime("%Y-%m-%d %H:%M:%S"), "runs": 0, "created": [],
                      "download_baseline": {d.persona: sorted(d.downloads()) for d in self.devices}})

    # ── run ────────────────────────────────────────────────────────────────
    def key_card(self, d):
        self.tab(d, "keys")
        d.page.locator("#keys-beacon-list").get_by_role("button", name="Show QR").first.click()
        d.page.locator("#qr-modal").wait_for(state="visible")
        wait_until(lambda: d.page.locator("#qr-modal-payload").inner_text().strip(), 10)
        return d.page.locator("#qr-modal-payload").inner_text().strip()

    def close_qr(self, d):
        d.page.locator("#qr-modal").get_by_role("button", name="Close").click()

    def has_contact(self, d, name):
        # The key card's name carries the owner-key prefix ("O:Alice").
        return any(c["name"] in (name, "O:" + name) and c["label"] == "Can seal to" for c in self.state(d).get("contacts", []))

    def exchange(self, mode):
        for src in self.devices:
            others = [d for d in self.devices if d is not src]
            names = " and ".join(o.persona for o in others)
            if mode == "paste":
                self.step(f"{src.persona}'s key card goes to {names}",
                          {src: "showing my key card (QR + text)",
                           **{o: f"pasting {src.persona}'s key card into Import" for o in others}})
            else:
                self.step(f"{names} scan {src.persona}'s key card QR",
                          {src: "showing my key card QR",
                           **{o: f"scanning {src.persona}'s QR" for o in others}})
            payload = self.key_card(src)
            self.pause()
            for dst in others:
                if self.has_contact(dst, src.persona):
                    self.tl.log(f"{dst}: already holds {src}'s key card")
                    continue
                self.tab(dst, "keys")
                if mode == "paste":
                    box = dst.page.locator("#keys-import-text")
                    box.scroll_into_view_if_needed()
                    box.fill(payload[:-40])
                    box.press_sequentially(payload[-40:], delay=25)
                    self.pause()
                    dst.page.get_by_role("button", name="Import key").click()
                    wait_until(lambda: self.has_contact(dst, src.persona), 15)
                else:
                    dst.page.get_by_role("button", name="Scan QR code").click()
                    dst.page.locator("#scan-modal").wait_for(state="visible")
                    self.human([(dst, f"point this camera at the QR on {src.persona.upper()}",
                                 lambda: self.has_contact(dst, src.persona))])
                msg = dst.page.locator("#msg").inner_text().strip()
                dst.caption(f"✓ {msg}")
                self.tl.check(self.has_contact(dst, src.persona), f"{dst}: {src} added as 'Can seal to' (exchange={mode}) — {msg!r}")
                dst.page.locator("#keys-contact-list").scroll_into_view_if_needed()
                self.pause()
            self.close_qr(src)
        self.shot("exchange")

    def sign_and_seal(self, alice, recipients):
        p = alice.page
        names = " and ".join(r.persona for r in recipients)
        self.step("Alice writes the agreement", {alice: "writing the agreement"})
        self.tab(alice, "docs")
        p.get_by_role("button", name="+ New").click()
        p.locator("#docs-text").press_sequentially(AGREEMENT, delay=35)
        self.pause()
        self.step(f"Alice reviews and chooses who can open it: Me, {names}",
                  {alice: f"review — sealing for Me, {names}"})
        p.get_by_role("button", name="Preview & Sign").click()
        p.locator("#docs-preview").wait_for(state="visible")
        self.pause()
        for r in recipients:
            p.locator("#docs-recipient-list label").filter(has_text=r.persona).locator("input").check()
            self.pause(0.8)
        chosen = p.evaluate("[...document.querySelectorAll('.docs-recipient:checked')].map(c=>c.closest('label').textContent.trim())")
        self.tl.log(f"{alice}: review — sealed for {chosen}")
        p.locator("#docs-sign-btn").scroll_into_view_if_needed()
        self.step("Alice signs (her PIN)", {alice: "sign — enter Alice's PIN"})
        p.locator("#docs-sign-btn").click()
        self.human([(alice, "enter Alice's PIN and press Confirm", lambda: p.locator("#docs-deliver").is_visible())])
        summary = p.locator("#docs-deliver-summary").inner_text().strip()
        alice.caption(f"✓ {summary}")
        self.tl.check(p.locator("#docs-deliver").is_visible(), f"{alice}: signed once — {summary!r}")
        can_share = p.evaluate("navigator.canShare ? navigator.canShare({files:[new File([new Uint8Array(1)],'x.ssd',{type:'application/octet-stream'})]}) : false")
        self.tl.log(f"{alice}: canShare(.ssd, application/octet-stream) = {can_share}")

    def record(self, session, d, row, name):
        session["created"].append({"persona": d.persona, "row": row, "name": name})
        save_session(session)

    def deliver(self, mode, alice, recipients, session):
        """Returns {recipient: (row, name)} of the file in each recipient's Downloads."""
        landed = {}
        names = " and ".join(r.persona for r in recipients)
        if mode == "courier":
            self.step(f"Alice downloads the sealed file; it is copied to {names}",
                      {alice: "Download the sealed .ssd", **{r: "receiving Alice's file (USB copy)" for r in recipients}})
            before = set(alice.downloads())
            alice.page.get_by_role("button", name="Download").click()
            new = wait_until(lambda: [r for r in alice.downloads() if r not in before and alice.downloads()[r].endswith(".ssd")], 30)
            if not self.tl.check(bool(new), f"{alice}: Download saved an .ssd in Download/"):
                return landed
            row = new[0]
            name = alice.downloads()[row]
            self.record(session, alice, row, name)
            data = alice.read_download(row)
            alice.caption(f"✓ saved Download/{name}")
            self.tl.log(f"{alice}: Download/{name} ({len(data)} bytes)")
            for r in recipients:
                rrow = r.write_download(name, data)
                self.record(session, r, rrow, name)
                landed[r] = (rrow, name)
                r.caption("✓ Alice's file is in my Download folder")
                self.tl.log(f"{r}: courier copy written to Download/{name} (adb, user {r.user})")
        else:
            self.step(f"Alice shares the sealed file to {names}", {alice: "Share", **{r: "receiving Alice's file" for r in recipients}})
            before = {r: set(r.downloads()) for r in recipients}
            alice.page.locator("#docs-share-btn").click()
            def arrived(r):
                return [x for x in r.downloads() if x not in before[r] and r.downloads()[x].endswith(".ssd")]
            self.human([(alice, f"share sheet: send to {names} (e.g. Quick Share); accept on each",
                         lambda: all(arrived(r) for r in recipients))])
            for r in recipients:
                new = arrived(r)
                if self.tl.check(bool(new), f"{r}: shared file arrived in Download/"):
                    name = r.downloads()[new[0]]
                    self.record(session, r, new[0], name)
                    landed[r] = (new[0], name)
                    self.tl.log(f"{r}: received Download/{name} via share sheet")
        self.pause()
        return landed

    def open_and_verify(self, r, row, name):
        p = r.page
        self.step(f"{r.persona} opens Alice's sealed file", {r: "open Alice's sealed file — enter my PIN"})
        self.tab(r, "docs")
        self.pause()
        data = r.read_download(row)
        with p.expect_file_chooser(timeout=15000) as fc:
            p.get_by_role("button", name="Open received .ssd").click()
        fc.value.set_files(files=[{"name": name, "mimeType": "application/octet-stream", "buffer": data}])
        self.tl.log(f"{r}: Docs 'Open received .ssd' — chose Download/{name} ({len(data)} bytes, read from this tablet)")
        states = lambda: p.evaluate("[...document.querySelectorAll('#docs-verification p')].map(e=>e.dataset.state)")
        wait_until(lambda: self.pin_visible(r) or "S2" in states() or "S3" in states() or "V7" in states(), 30)
        self.unlock_if_asked(r, f"enter {r.persona}'s PIN to open")
        wait_until(lambda: {"S2", "V1"} <= set(states()) or any(s in states() for s in ("S3", "S4", "V2", "V3", "V4", "V5", "V6", "V7")), 30)
        text = p.locator("#docs-verification").inner_text().strip().replace("\n", " | ")
        content = p.locator("#docs-view-content").inner_text()
        ok = [self.tl.check("S2" in states(), f"{r}: S2 opened as own identity — {text!r}"),
              self.tl.check("V1" in states(), f"{r}: V1 signed by Alice"),
              # ssd-render-1.0 wraps long lines, so compare with whitespace collapsed.
              self.tl.check(" ".join(AGREEMENT.split()) in " ".join(content.split()), f"{r}: rendered text equals the agreement")]
        r.caption("✓ opened, sealed for me, signed by Alice" if all(ok) else "✗ see driver log", "#0a7d2c" if all(ok) else "#c00")

    def run(self, exchange, delivery):
        session = load_session()
        if not session:
            raise SystemExit("No demo session — run setup first.")
        session["runs"] += 1
        save_session(session)
        self.tl.log(f"run {session['runs']}: exchange={exchange}{' (automated)' if exchange == 'paste' else ''} "
                    f"delivery={delivery}{' (adb)' if delivery == 'courier' else ''}")
        self.attach_all()
        for d in self.devices:
            self.home(d)
            if not self.tl.check((self.state(d).get("identity") or {}).get("name") == d.persona, f"{d}: identity present"):
                return
        alice, *recipients = self.devices
        self.exchange(exchange)
        self.sign_and_seal(alice, recipients)
        self.shot("signed")
        landed = self.deliver(delivery, alice, recipients, session)
        for r in recipients:
            if r in landed:
                self.open_and_verify(r, *landed[r])
        self.shot("verified")
        self.tl.log("run complete")
        if self.reseal_check:
            self.reseal_test()

    # ── status ─────────────────────────────────────────────────────────────
    def status(self):
        session = load_session()
        self.tl.log(f"session: {'set up ' + session['started'] + ', runs ' + str(session['runs']) if session else 'none'}")
        for d in self.devices:
            try:
                d.attach(self.pw)
                s = self.state(d)
                labels = ", ".join(f"{c['name']} ({c['label']})" for c in s.get("contacts", [])) or "none"
                self.tl.log(f"{d}: reachable, tab attached, {s.get('version')}, identity "
                            f"{s.get('identity') or 'none'}, rung {s.get('rung')}, contacts: {labels}, docs: {s.get('docs', 0)}")
            except SystemExit as e:
                self.tl.check(False, str(e))
            except Exception as e:
                self.tl.check(False, f"{d}: unreachable — {e}")

    # ── Keys screen ────────────────────────────────────────────────────────
    def keys(self):
        """Exercise Home/Keys exports on Bob, then import his public share on Carol."""
        if not load_session():
            raise SystemExit("No demo session — run setup first.")
        self.attach_all()
        owner, importer = self.by_name["Bob"], self.by_name["Carol"]
        for d in self.devices:
            self.home(d)

        self.step("copy signing code from Home", {owner: "copying Bob's signing code"})
        owner.page.locator("#home-status").get_by_role("button", name="Copy signing code").click()
        message = wait_until(lambda: owner.page.locator("#msg").inner_text().strip()
                             if "Signing code copied" in owner.page.locator("#msg").inner_text()
                             or "Copy this" in owner.page.locator("#msg").inner_text() else None, 10)
        self.tl.check(bool(message),
                      f"Bob Home signing code action: {message!r}")

        self.tab(owner, "keys")
        self.step("show Bob's signed key-card QR", {owner: "showing the owner key-card QR"})
        owner.page.locator("#keys-beacon-list").get_by_role("button", name="Show QR").click()
        owner.page.locator("#qr-modal").wait_for(state="visible", timeout=10000)
        payload = owner.page.locator("#qr-modal-payload").inner_text().strip()
        try:
            card = json.loads(payload)
        except Exception:
            card = None
        identity = self.state(owner).get("identity") or {}
        self.tl.check(bool(card and card.get("hash8") == identity.get("hash8")),
                      f"Bob QR contains his self-signed key card ({identity.get('hash8')})")
        owner.page.locator("#qr-modal").get_by_role("button", name="Close").click()

        self.step("export Bob's encrypted identity backup", {owner: "exporting Bob's encrypted identity backup"})
        owner.page.reload(wait_until="domcontentloaded")
        owner.page.wait_for_function("typeof APP_VERSION!=='undefined' && document.getElementById('home-loading').style.display==='none'", timeout=30000)
        self.tab(owner, "keys")
        before_backup = set(owner.downloads())
        owner.page.locator('button[onclick="app.backupIdentity()"]', has_text="Back up identity").click()
        wait_until(lambda: self.pin_visible(owner) or any(word in owner.page.locator("#msg").inner_text().lower()
                                                          for word in ("backup downloaded", "backup failed", "no owner key")), 30)
        self.unlock_if_asked(owner, "export Bob's backup")
        wait_until(lambda: any(word in owner.page.locator("#msg").inner_text().lower()
                               for word in ("backup downloaded", "backup failed", "no owner key")), 60)
        backup_message = owner.page.locator("#msg").inner_text().strip()
        new_backup_rows = set(owner.downloads()) - before_backup
        backup_names = [owner.downloads()[row] for row in new_backup_rows]
        backup_file = next((name for name in backup_names if name.startswith("ssd-backup-") and name.endswith(".ssd")), None)
        backup_row = next((row for row in new_backup_rows if owner.downloads()[row] == backup_file), None)
        self.tl.check("backup downloaded" in backup_message.lower() and backup_file is not None,
                      f"Bob backup export: message={backup_message!r}, new files={backup_names!r}")
        if backup_file:
            self.tl.log(f"Bob encrypted backup retained in Demo Downloads: {backup_file}")

        if backup_row is not None:
            self.step("restore Bob's backup on its source device", {owner: "restoring Bob's encrypted backup"})
            backup_bytes = owner.read_download(backup_row)
            owner.page.locator("#keys-restore-file").set_input_files({
                "name": backup_file, "mimeType": "application/octet-stream", "buffer": backup_bytes,
            })
            wait_until(lambda: "restore failed" in owner.page.locator("#msg").inner_text().lower()
                        or "identity restored" in owner.page.locator("#msg").inner_text().lower(), 45)
            restore_message = owner.page.locator("#msg").inner_text().strip()
            after_restore = self.state(owner).get("identity") or {}
            self.tl.check("identity restored" in restore_message.lower()
                          and after_restore.get("hash8") == identity.get("hash8"),
                          f"same-device backup restore preserves Bob's identity: {restore_message!r}; "
                          f"identity={after_restore!r}")

        self.step("export Bob's public-key share", {owner: "exporting a public-only key share"})
        before_share = set(owner.downloads())
        owner.page.locator('button[onclick="app.sharePublicKeys()"]', has_text="Share public keys").click()
        wait_until(lambda: any(word in owner.page.locator("#msg").inner_text().lower()
                               for word in ("public key share downloaded", "share export failed", "no owner key")), 60)
        share_message = owner.page.locator("#msg").inner_text().strip()
        new_share_rows = set(owner.downloads()) - before_share
        share_row = next((row for row in new_share_rows
                          if owner.downloads()[row].startswith("ssd-share-")
                          and owner.downloads()[row].endswith(".ssd")), None)
        self.tl.check("public key share downloaded" in share_message.lower() and share_row is not None,
                      f"Bob public-share export: message={share_message!r}, new files="
                      f"{[owner.downloads()[row] for row in new_share_rows]!r}")

        if share_row is not None:
            share_name = owner.downloads()[share_row]
            self.step("import Bob's public-key share on Carol",
                      {importer: "importing Bob's public-only key share"})
            public_bytes = owner.read_download(share_row)
            importer_row = importer.write_download(share_name, public_bytes)
            received_bytes = importer.read_download(importer_row)
            self.tab(importer, "keys")
            importer.page.locator("#keys-share-import-file").set_input_files({
                "name": share_name, "mimeType": "application/octet-stream", "buffer": received_bytes,
            })
            wait_until(lambda: "key share imported" in importer.page.locator("#msg").inner_text().lower()
                        or "import failed" in importer.page.locator("#msg").inner_text().lower(), 30)
            imported_state = self.state(importer)
            owner_hash = identity.get("hash8")
            received = any(c.get("hash8") == owner_hash and c.get("label") == "Verify only"
                           for c in imported_state.get("contacts", []))
            self.tl.check("key share imported" in importer.page.locator("#msg").inner_text().lower() and received,
                          f"Carol imported Bob's public key as verify-only: "
                          f"{importer.page.locator('#msg').inner_text().strip()!r}")

    def identity_transfer(self):
        """Send Bob's identity to clean Carol, then adopt it without losing Carol's D key/PIN."""
        bob, carol = self.by_name["Bob"], self.by_name["Carol"]
        self.attach_all()
        for d in (bob, carol):
            self.home(d)
            self.tl.check(self.state(d).get("version") == "ssd-v86", f"{d}: current app is v86")
        bob_id = self.state(bob).get("identity") or {}
        carol_id = self.state(carol).get("identity") or {}
        if not self.tl.check(bool(bob_id.get("hash8") and carol_id.get("hash8")),
                             "fresh source and destination identities exist"):
            return

        self.exchange("paste")
        bob_enc_pub = bob.page.evaluate("(async()=> (await db.get('settings','my_encryption_key_pub'))?.value)()")
        carol_enc_pub = carol.page.evaluate("(async()=> (await db.get('settings','my_encryption_key_pub'))?.value)()")
        carol_device = carol.page.evaluate("async()=> (await db.getAll('my_keys')).find(k=>k.name.startsWith('D:'))?.public_key_b64")
        before_carol = carol.page.evaluate("async()=>({docs:(await db.getAll('artifacts')).length, drafts:(await db.getAll('drafts')).length, posts:(await db.getAll('social_posts')).length, keys:(await db.getAll('my_keys')).length})")
        self.tl.check(before_carol == {"docs": 0, "drafts": 0, "posts": 0, "keys": 2},
                      f"Carol is a clean adoption target: {before_carol!r}")

        self.tab(bob, "keys")
        before = set(bob.downloads())
        self.step("Bob exports his identity sealed to himself and Carol", {bob: "exporting Bob's identity for Carol"})
        bob.page.locator('button[onclick="app.backupIdentity()"]', has_text="Send identity to a new device").click()
        wait_until(lambda: self.pin_visible(bob) or "identity file for" in bob.page.locator("#msg").inner_text().lower()
                    or "failed" in bob.page.locator("#msg").inner_text().lower(), 30)
        self.unlock_if_asked(bob, "export Bob's identity")
        wait_until(lambda: "identity file for" in bob.page.locator("#msg").inner_text().lower()
                    or "failed" in bob.page.locator("#msg").inner_text().lower(), 45)
        export_msg = bob.page.locator("#msg").inner_text().strip()
        rows = wait_until(lambda: [r for r in bob.downloads() if r not in before and bob.downloads()[r].endswith(".ssd")], 15)
        if not self.tl.check(bool(rows) and "identity file for" in export_msg.lower(),
                             f"Bob identity export: {export_msg!r}"):
            return
        row = rows[0]
        filename = bob.downloads()[row]
        payload = bob.read_download(row)
        try:
            envelope = json.loads(payload)
            recipient_keys = {r["for_key"] for r in envelope.get("recipients", [])}
        except Exception:
            envelope, recipient_keys = {}, set()
        self.tl.check(envelope.get("ssd") == "sealed-enc-v1" and len(recipient_keys) == 2
                      and recipient_keys == {bob_enc_pub, carol_enc_pub},
                      f"identity .ssd is sealed to Bob and Carol only ({len(recipient_keys)} recipient slots)")

        self.step("copy Bob's identity file to Carol", {bob: "identity file downloaded", carol: "receiving the same .ssd file"})
        carol_row = carol.write_download(filename, payload)
        received = carol.read_download(carol_row)
        import hashlib
        self.tl.check(hashlib.sha256(received).digest() == hashlib.sha256(payload).digest(),
                      "Bob-to-Carol file transfer preserves every byte")

        self.tab(carol, "keys")
        self.step("Carol adopts Bob's identity", {carol: "importing Bob's identity; keeping this device key and PIN"})
        carol.page.locator("#keys-restore-file").set_input_files({
            "name": filename, "mimeType": "application/octet-stream", "buffer": received,
        })
        wait_until(lambda: self.pin_visible(carol) or "now holds" in carol.page.locator("#msg").inner_text().lower()
                    or "can’t adopt" in carol.page.locator("#msg").inner_text().lower()
                    or "can't adopt" in carol.page.locator("#msg").inner_text().lower()
                    or "failed" in carol.page.locator("#msg").inner_text().lower(), 30)
        self.unlock_if_asked(carol, "open Bob's identity file")
        wait_until(lambda: "now holds" in carol.page.locator("#msg").inner_text().lower()
                    or "can’t adopt" in carol.page.locator("#msg").inner_text().lower()
                    or "can't adopt" in carol.page.locator("#msg").inner_text().lower()
                    or "failed" in carol.page.locator("#msg").inner_text().lower(), 45)
        restore_msg = carol.page.locator("#msg").inner_text().strip()
        after_id = self.state(carol).get("identity") or {}
        after_device = carol.page.evaluate("async()=> (await db.getAll('my_keys')).find(k=>k.name.startsWith('D:'))?.public_key_b64")
        self.tl.check("now holds" in restore_msg.lower() and after_id.get("hash8") == bob_id.get("hash8"),
                      f"Carol adopted Bob's O identity: {restore_msg!r}; {after_id!r}")
        self.tl.check(after_device == carol_device,
                      "Carol's original D: device key is retained")

    def public_share(self):
        """Export Bob's signed public keyring share and import it on clean Alice."""
        bob, alice = self.by_name["Bob"], self.by_name["Alice"]
        self.attach_all()
        for d in (bob, alice):
            self.home(d)
            self.tl.check(self.state(d).get("version") == "ssd-v86", f"{d}: current app is v86")
        identity = self.state(bob).get("identity") or {}
        self.tl.check(bool(identity.get("hash8")), "Bob has an owner identity")
        self.tab(bob, "keys")
        before = set(bob.downloads())
        self.step("Bob exports his signed public keyring share", {bob: "exporting a public-only key share"})
        bob.page.locator('button[onclick="app.sharePublicKeys()"]', has_text="Share public keys").click()
        wait_until(lambda: self.pin_visible(bob)
                    or "public key share downloaded" in bob.page.locator("#msg").inner_text().lower()
                    or "share export failed" in bob.page.locator("#msg").inner_text().lower(), 30)
        self.unlock_if_asked(bob, "export Bob's public share")
        wait_until(lambda: "public key share downloaded" in bob.page.locator("#msg").inner_text().lower()
                    or "share export failed" in bob.page.locator("#msg").inner_text().lower(), 45)
        message = bob.page.locator("#msg").inner_text().strip()
        rows = wait_until(lambda: [r for r in bob.downloads() if r not in before and bob.downloads()[r].endswith(".ssd")], 15)
        if not self.tl.check("public key share downloaded" in message.lower() and bool(rows),
                             f"Bob public-share export: {message!r}"):
            return
        row = rows[0]
        filename = bob.downloads()[row]
        data = bob.read_download(row)
        archive = bob.page.evaluate("bytes => Object.keys(fflate.unzipSync(new Uint8Array(bytes)))", list(data))
        self.tl.check({"manifest.json", "signature.json", "source.json"}.issubset(set(archive)),
                      f"share archive contains manifest, signature, and keyring payload: {archive!r}")

        self.step("Alice imports Bob's public share", {bob: "public share downloaded", alice: "importing Bob's public share"})
        self.tab(alice, "keys")
        alice.page.locator("#keys-share-import-file").set_input_files({
            "name": filename, "mimeType": "application/octet-stream", "buffer": data,
        })
        wait_until(lambda: "key share imported" in alice.page.locator("#msg").inner_text().lower()
                    or "import failed" in alice.page.locator("#msg").inner_text().lower(), 30)
        result = alice.page.locator("#msg").inner_text().strip()
        imported = any(c.get("hash8") == identity.get("hash8") and c.get("label") == "Verify only"
                       for c in self.state(alice).get("contacts", []))
        self.tl.check("key share imported" in result.lower() and imported,
                      f"Alice imports Bob's owner key as verify-only: {result!r}")

    def open_expected(self, device, row, name, expect_open):
        page = device.page
        state_list = lambda: page.evaluate("[...document.querySelectorAll('#docs-verification p')].map(e=>e.dataset.state)")
        caption = "open Bob's file" if expect_open else "try Bob's file; it should remain sealed"
        self.step(f"{device.persona} checks Bob's resealed file", {device: caption})
        self.tab(device, "docs")
        self.pause()
        data = device.read_download(row)
        with page.expect_file_chooser(timeout=15000) as chooser:
            page.get_by_role("button", name="Open received .ssd").click()
        chooser.value.set_files(files=[{"name": name, "mimeType": "application/octet-stream", "buffer": data}])
        self.tl.log(f"{device}: selected local Download/{name} ({len(data)} bytes)")
        wait_until(lambda: self.pin_visible(device) or any(s in state_list() for s in ("S2", "S3", "S4", "V7")), 30)
        if expect_open:
            self.unlock_if_asked(device, f"enter {device.persona}'s PIN to open")
        wait_until(lambda: any(s in state_list() for s in ("S2", "S3", "S4", "V7")), 30)
        states = state_list()
        verification = page.locator("#docs-verification").inner_text().strip().replace("\n", " | ")
        content = page.locator("#docs-view-content").inner_text().strip()
        if expect_open:
            ok = [self.tl.check("S2" in states, f"{device}: Bob's seal opened (S2) — {verification!r}"),
                  self.tl.check(" ".join(AGREEMENT.split()) in " ".join(content.split()),
                                f"{device}: resealed message content matches")]
            device.caption("Opened Bob's message" if all(ok) else "Open failed", "#0a7d2c" if all(ok) else "#c00")
        else:
            ok = [self.tl.check("S3" in states, f"{device}: Alice gets S3 (not addressed) — {verification!r}"),
                  self.tl.check(not content, f"{device}: Alice cannot read the sealed message")]
            device.caption("Not addressed; content stayed sealed" if all(ok) else "Unexpected result", "#0a7d2c" if all(ok) else "#c00")

    def reseal_test(self):
        """Bob reseals the agreement for Bob and Carol; Alice gets a copy but is excluded."""
        bob, carol, alice = self.by_name["Bob"], self.by_name["Carol"], self.by_name["Alice"]
        if not self.tl.check(self.has_contact(bob, "Carol"), "Bob has Carol's seal-capable key"):
            return
        self.home(bob)
        self.step("Bob reseals the message for himself and Carol", {bob: "creating a message sealed to Bob and Carol"})
        self.tab(bob, "docs")
        page = bob.page
        page.get_by_role("button", name="+ New").click()
        page.locator("#docs-text").fill(AGREEMENT)
        self.step("Bob reviews the two chosen recipients", {bob: "checking Me and Carol are selected"})
        page.get_by_role("button", name="Preview & Sign").click()
        page.locator("#docs-preview").wait_for(state="visible")
        page.locator("#docs-recipient-list label").filter(has_text="Carol").locator("input").check()
        chosen = page.evaluate("[...document.querySelectorAll('.docs-recipient:checked')].map(c=>c.closest('label').textContent.trim())")
        chosen_text = " | ".join(chosen)
        self.tl.log(f"Bob's selected recipients: {chosen_text}")
        self.tl.check(any("Carol" in item for item in chosen) and any("Me" in item for item in chosen),
                      "Bob seals to himself and Carol only")
        self.step("Bob signs the resealed message", {bob: "signing with Bob's demo PIN"})
        page.locator("#docs-sign-btn").click()
        wait_until(lambda: self.pin_visible(bob) or page.locator("#docs-deliver").is_visible(), 20)
        if self.pin_visible(bob) and not self.auto_pin(bob):
            self.human([(bob, "enter Bob's SSD PIN and press Confirm", lambda: page.locator("#docs-deliver").is_visible())])
        wait_until(lambda: page.locator("#docs-deliver").is_visible(), 30)
        summary = page.locator("#docs-deliver-summary").inner_text().strip()
        self.tl.check("Carol" in summary and "Me" in summary, f"Bob's document says sealed for Bob and Carol: {summary!r}")

        session = load_session()
        self.step("send the same sealed file to Bob, Carol, and excluded Alice",
                  {bob: "downloading Bob's resealed file", carol: "receiving the file", alice: "receiving an excluded-recipient copy"})
        before = set(bob.downloads())
        page.get_by_role("button", name="Download").click()
        new_rows = wait_until(lambda: [row for row in bob.downloads()
                                      if row not in before and bob.downloads()[row].endswith(".ssd")], 30)
        if not self.tl.check(bool(new_rows), "Bob's resealed message downloaded"):
            return
        bob_row = new_rows[0]
        filename = bob.downloads()[bob_row]
        self.record(session, bob, bob_row, filename)
        data = bob.read_download(bob_row)
        landed = {bob: (bob_row, filename)}
        for recipient in (carol, alice):
            row = recipient.write_download(filename, data)
            self.record(session, recipient, row, filename)
            landed[recipient] = (row, filename)
            self.tl.log(f"{recipient}: received the identical Bob-sealed file in Demo Downloads")

        self.open_expected(bob, *landed[bob], expect_open=True)
        self.open_expected(carol, *landed[carol], expect_open=True)
        self.open_expected(alice, *landed[alice], expect_open=False)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["setup", "run", "keys", "identity-transfer", "public-share", "status", "setdown"])
    ap.add_argument("--exchange", choices=["qr", "paste"], default="qr")
    ap.add_argument("--delivery", choices=["share", "courier"], default="courier")
    ap.add_argument("--config", default=os.path.join(HERE, "personas.json"))
    ap.add_argument("--shots", action="store_true", help="save a screenshot per device per step under demo/runs/")
    ap.add_argument("--auto", action="store_true", help="release every tablet step without waiting for a go file")
    ap.add_argument("--reseal-check", action="store_true", help="after the standard run, Bob reseals to himself and Carol; Alice receives a copy but cannot open it")
    args = ap.parse_args()
    cfg, devices = load_devices(args.config)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tl = Timeline(os.path.join(RUNS, f"{stamp}-{args.command}.log"))
    shots = os.path.join(RUNS, stamp) if args.shots else None
    with sync_playwright() as pw:
        demo = Demo(pw, devices, tl, shots)
        demo.auto_go = args.auto
        demo.reseal_check = args.reseal_check
        try:
            if args.command == "run":
                demo.run(args.exchange, args.delivery)
            elif args.command == "identity-transfer":
                demo.identity_transfer()
            elif args.command == "public-share":
                demo.public_share()
            else:
                getattr(demo, args.command)()
        finally:
            for d in devices:
                d.detach()
    print()
    print("PASS" if not tl.failures else f"FAIL ({len(tl.failures)})")
    for f in tl.failures:
        print("  -", f)
    print(f"timeline: {tl.path}")
    sys.exit(1 if tl.failures else 0)


if __name__ == "__main__":
    main()
