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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["setup", "run", "status", "setdown"])
    ap.add_argument("--exchange", choices=["qr", "paste"], default="qr")
    ap.add_argument("--delivery", choices=["share", "courier"], default="courier")
    ap.add_argument("--config", default=os.path.join(HERE, "personas.json"))
    ap.add_argument("--shots", action="store_true", help="save a screenshot per device per step under demo/runs/")
    args = ap.parse_args()
    cfg, devices = load_devices(args.config)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tl = Timeline(os.path.join(RUNS, f"{stamp}-{args.command}.log"))
    shots = os.path.join(RUNS, stamp) if args.shots else None
    with sync_playwright() as pw:
        demo = Demo(pw, devices, tl, shots)
        try:
            if args.command == "run":
                demo.run(args.exchange, args.delivery)
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
