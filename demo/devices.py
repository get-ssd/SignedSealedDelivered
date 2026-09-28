"""Device plumbing for multi-tablet demos: ADB, the Android user guard, CDP
attachment to the visible Chrome tab, the user's Downloads via the media
provider, screenshots and a timestamped timeline. Nothing SSD-specific.

Safety: every command targets one Android user (config "android_user"). A
device is refused unless that user is in the foreground and every running
Chrome process belongs to it, so the DevTools socket cannot be another user's
browser. Storage is reached only through `content --user <n>`, never /sdcard
(which is user 0's storage on these tablets).
"""
import json
import os
import re
import subprocess
import time

T0 = time.time()


class StopRun(Exception):
    """Raised by Timeline.check in test mode at the first failure."""


class Timeline:
    def __init__(self, path, stop_on_fail=False):
        self.path = path
        self.failures = []
        self.stop_on_fail = stop_on_fail
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def log(self, message):
        line = f"{time.strftime('%H:%M:%S')} {time.time() - T0:7.1f}s  {message}"
        print(line, flush=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def check(self, ok, message):
        self.log(("ok    " if ok else "FAIL  ") + message)
        if not ok:
            self.failures.append(message)
            if self.stop_on_fail:
                raise StopRun(message)
        return ok


def wait_until(fn, timeout=10.0, interval=0.3):
    end = time.time() + timeout
    while True:
        value = fn()
        if value or time.time() >= end:
            return value
        time.sleep(interval)


class Device:
    def __init__(self, serial, persona, cdp_port, android_user, app_port):
        self.serial, self.persona, self.cdp_port = serial, persona, cdp_port
        self.user, self.app_port = str(android_user), app_port
        self.browser = self.page = None
        self.pin = None  # operator-approved demo PIN the driver may type; None = a human types it

    def __str__(self):
        return self.persona

    def adb(self, *args, check=True, binary=False, input=None):
        r = subprocess.run(["adb", "-s", self.serial, *args], capture_output=True, check=check, input=input)
        return r.stdout if binary else r.stdout.decode(errors="replace").replace("\r", "")

    def shell(self, command, **kw):
        return self.adb("shell", command, **kw)

    # ── Guard ──────────────────────────────────────────────────────────────
    def guard(self):
        current = self.shell("am get-current-user").strip()
        if current != self.user:
            raise SystemExit(f"{self}: foreground Android user is {current}, not {self.user} — switch the tablet to the Demo user")
        procs = self.shell("ps -A -o USER,NAME | grep com.android.chrome", check=False).split("\n")
        owners = {p.split()[0].split("_")[0] for p in procs if p.strip()}
        if owners - {f"u{self.user}"}:
            raise SystemExit(f"{self}: Chrome is running for another Android user ({sorted(owners)}) — refusing to attach")
        sockets = self.shell("grep -a '@chrome_devtools_remote$' /proc/net/unix", check=False).strip().split("\n")
        if len([s for s in sockets if s.strip()]) != 1:
            raise SystemExit(f"{self}: expected exactly one Chrome DevTools socket, found {len(sockets)}")

    # ── Browser ────────────────────────────────────────────────────────────
    def base(self):
        return f"http://localhost:{self.app_port}"

    def attach(self, pw, path="/"):
        self.guard()
        self.shell("input keyevent KEYCODE_WAKEUP")
        self.adb("reverse", f"tcp:{self.app_port}", f"tcp:{self.app_port}")
        self.adb("forward", f"tcp:{self.cdp_port}", "localabstract:chrome_devtools_remote")
        self.browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.cdp_port}", timeout=15000)
        # CDP attachment can redirect downloads to Playwright's host-side temp
        # path. Android must use Chrome's own download handling instead.
        download_session = self.browser.new_browser_cdp_session()
        download_session.send("Browser.setDownloadBehavior", {"behavior": "default"})
        download_session.detach()
        ctx = self.browser.contexts[0]
        page = None
        for pg in ctx.pages:
            try:
                if pg.url.startswith(self.base()) and pg.evaluate("document.visibilityState") == "visible":
                    page = pg
            except Exception:
                pass
        if page is None:
            self.shell(f"am start --user {self.user} -a android.intent.action.VIEW -d {self.base()}{path} com.android.chrome")
            page = wait_until(lambda: next((pg for pg in ctx.pages if pg.url.startswith(self.base())), None), 15)
            if page is None:
                raise SystemExit(f"{self}: no {self.base()} tab appeared in Chrome")
        # Another tab on the app origin holds IndexedDB open and blocks the
        # factory reset's deleteDatabase; close only same-origin extras.
        for pg in list(ctx.pages):
            if pg is not page and pg.url.startswith(self.base()):
                pg.close()
        page.bring_to_front()
        # Persona label on screen so the operator can tell the tablets apart.
        ctx.add_init_script(self.LABEL_JS % self.persona.upper())
        page.evaluate(self.LABEL_JS % self.persona.upper())
        self.page = page
        return page

    LABEL_JS = """(() => { const put = () => {
      const existing = document.getElementById('__persona');
      if (existing) existing.remove();
      if (window.__personaResize) window.__personaResize.disconnect();
      const el = document.createElement('div'); el.id = '__persona';
      el.innerHTML = '<div style="font:bold 40px/1.3 sans-serif;letter-spacing:6px">%s</div>'
        + '<div id="__caption" style="font:22px/1.3 sans-serif;padding:0 12px 8px"></div>';
      el.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;pointer-events:none;'
        + 'background:#c00;color:#fff;text-align:center';
      document.documentElement.appendChild(el);
      const c = sessionStorage.getItem('__caption'); if (c) el.querySelector('#__caption').textContent = c;
      const body = document.body;
      if (body.dataset.demoPaddingTop === undefined)
        body.dataset.demoPaddingTop = getComputedStyle(body).paddingTop;
      const reserve = () => {
        const height = Math.ceil(el.getBoundingClientRect().height);
        body.style.paddingTop = (parseFloat(body.dataset.demoPaddingTop) + height) + 'px';
        document.documentElement.style.scrollPaddingTop = (height + 8) + 'px';
      };
      window.__personaResize = new ResizeObserver(reserve);
      window.__personaResize.observe(el);
      reserve(); };
      document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', put) : put(); })()"""

    def caption(self, text, colour="#c00"):
        """Show what is happening under the persona label on the tablet."""
        try:
            self.page.evaluate("""([t, c]) => { sessionStorage.setItem('__caption', t);
              const e = document.getElementById('__caption'); if (e) e.textContent = t;
              const p = document.getElementById('__persona'); if (p) p.style.background = c; }""", [text, colour])
        except Exception:
            pass

    def detach(self):
        self.adb("forward", "--remove", f"tcp:{self.cdp_port}", check=False)

    def screenshot(self, directory, name):
        os.makedirs(directory, exist_ok=True)
        png = self.adb("exec-out", "screencap", "-p", binary=True)
        path = os.path.join(directory, f"{name}-{self.persona}.png")
        with open(path, "wb") as f:
            f.write(png)
        return path

    # ── Downloads (media provider, this user only) ──────────────────────────
    DOWNLOADS = "content://media/external/downloads"

    def downloads(self):
        """{_id: display_name} for this user's Downloads."""
        out = self.shell(f"content query --user {self.user} --uri {self.DOWNLOADS} --projection _id:_display_name", check=False)
        rows = {}
        for m in re.finditer(r"_id=(\d+), _display_name=(.*)", out):
            rows[m.group(1)] = m.group(2).strip()
        return rows

    def read_download(self, row_id):
        # ZIP/.ssd payloads are binary. `adb shell` may use a PTY and rewrite
        # line endings; exec-out keeps the byte stream intact.
        return self.adb("exec-out", "content", "read", "--user", self.user,
                        "--uri", f"{self.DOWNLOADS}/{row_id}", binary=True)

    def write_download(self, name, data):
        """Create Download/<name> for this user; returns the new row id."""
        before = set(self.downloads())
        self.shell(f"content insert --user {self.user} --uri {self.DOWNLOADS} "
                   f"--bind _display_name:s:{name} --bind relative_path:s:Download/ "
                   f"--bind mime_type:s:application/octet-stream")
        new = set(self.downloads()) - before
        if len(new) != 1:
            raise RuntimeError(f"{self}: could not create download row for {name}")
        row = new.pop()
        # Disable the remote PTY so binary bytes are not transformed in transit.
        self.adb("shell", "-T", "content", "write", "--user", self.user,
                 "--uri", f"{self.DOWNLOADS}/{row}", input=data)
        return row

    def delete_download(self, row_id):
        self.shell(f"content delete --user {self.user} --uri {self.DOWNLOADS}/{row_id}", check=False)


def load_devices(config_path):
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    devices = [Device(d["serial"], d["persona"], cfg.get("cdp_base_port", 9811) + i,
                      cfg.get("android_user", 10), cfg.get("app_port", 8105))
               for i, d in enumerate(cfg["devices"])]
    for d, spec in zip(devices, cfg["devices"]):
        d.pin = spec.get("pin")
    return cfg, devices
