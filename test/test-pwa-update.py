"""Exercise the real worker/update helper across two releases, without screenshots."""
import asyncio
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import threading
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
CURRENT = re.search(r"const CACHE_NAME = '([^']+)'", (ROOT / 'service-worker.js').read_text(encoding='utf-8')).group(1)
release = 'ssd-v9001'
passed = 0


def check(label, condition):
    global passed
    if not condition:
        raise AssertionError(label)
    passed += 1
    print('PASS ' + label, flush=True)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass

    def do_GET(self):
        pathname = self.path.split('?')[0]
        if pathname in ['/', '/index.html', '/advanced.html']:
            body = f'''<!doctype html><title>Update test</title><input id="draft">
              <span id="version">{release}</span><script src="pwa-updates.js"></script>
              <script>const APP_VERSION='{release}';
              sessionStorage.loads=Number(sessionStorage.loads||0)+1;
              pwaUpdates.start(APP_VERSION);</script>'''
        elif pathname == '/service-worker.js':
            body = (ROOT / 'service-worker.js').read_text(encoding='utf-8').replace(CURRENT, release)
        else:
            return super().do_GET()
        data = body.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/javascript' if pathname.endswith('.js') else 'text/html')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


async def main():
    global release
    for shell in ['index.html', 'advanced.html']:
        version = re.search(r"const APP_VERSION = '([^']+)'", (ROOT / shell).read_text(encoding='utf-8')).group(1)
        check(shell + ' version matches worker', version == CURRENT)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://localhost:{server.server_port}/'
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe')
            context = await browser.new_context()
            idle = await context.new_page()
            await idle.goto(url)
            await idle.wait_for_function('navigator.serviceWorker.controller !== null')
            await idle.evaluate("caches.open('unrelated-app-cache')")
            dirty = await context.new_page()
            await dirty.goto(url)
            await dirty.locator('#draft').fill('Keep this unsent document')
            release = 'ssd-v9002'
            await idle.evaluate('navigator.serviceWorker.getRegistration().then(r=>r.update())')
            await expect(idle.locator('#version')).to_have_text(release, timeout=20000)
            check('idle tab adopts new release at the same URL', idle.url == url)
            check('one automatic reload, no reload loop', await idle.evaluate('Number(sessionStorage.loads)') == 2)
            await expect(dirty.locator('#ssd-update-notice')).to_contain_text(release)
            check('draft is retained in the old page until explicit refresh', await dirty.locator('#draft').input_value() == 'Keep this unsent document' and await dirty.locator('#version').inner_text() == 'ssd-v9001')
            keys = await idle.evaluate('caches.keys()')
            check('old SSD cache removed; unrelated cache preserved', 'ssd-v9001' not in keys and 'ssd-v9002' in keys and 'unrelated-app-cache' in keys)
            await context.set_offline(True)
            await idle.goto(url + '?ssd=not-a-real-document')
            await expect(idle.locator('#version')).to_have_text(release)
            check('new release remains available offline', True)
            check('document URL is not retained in cache keys', await idle.evaluate("async()=>!(await (await caches.open('ssd-v9002')).keys()).some(r=>r.url.includes('ssd='))"))
            await browser.close()
    finally:
        server.shutdown()
        server.server_close()
    print(f'{passed} passed', flush=True)


asyncio.run(main())
