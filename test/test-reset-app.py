"""Reset app with a second SSD window open.

Before ssd-v91 the other window's open database blocked the delete, the reset
page reloaded anyway and hung on "Loading…". Now the other window lets go and
asks for a reload, and the reset page comes back to the setup wizard.

Run: py -3 test/test-reset-app.py
"""
import asyncio
import subprocess
import sys
from pathlib import Path
from playwright.async_api import async_playwright, expect
from server_ready import wait_for_server

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = Path(__file__).resolve().parent.parent
PORT = 8111
URL = f'http://localhost:{PORT}'
CHROME = r'C:\Program Files\Google\Chrome\Application\chrome.exe'
passed = 0


def check(label, condition):
    global passed
    if not condition:
        raise AssertionError(label)
    passed += 1
    print(f'PASS {label}', flush=True)


async def open_app(ctx, path='/'):
    page = await ctx.new_page()
    page.on('dialog', lambda d: d.accept(d.default_value or 'x'))
    await page.goto(URL + path)
    await page.wait_for_function('typeof app !== "undefined" && !!db._db')
    return page


async def main():
    server = subprocess.Popen([sys.executable, '-m', 'http.server', str(PORT), '--directory', str(ROOT)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for_server(server, PORT)
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME)
            for shell, path in (('index', '/'), ('advanced', '/advanced.html')):
                ctx = await browser.new_context(service_workers='block')
                a = await open_app(ctx, path)
                await a.evaluate("db.put('settings', {key: 'probe', value: 1})")
                other = await open_app(ctx, path)
                check(f'{shell}: second window sees the data',
                      (await other.evaluate("db.get('settings', 'probe')"))['value'] == 1)

                if shell == 'index':
                    await a.click('#tab-keys-btn')
                    button = a.get_by_role('button', name='Reset app…')
                else:
                    await a.evaluate("ui.showSection('settings')")
                    button = a.locator('button', has_text='Reset app…')
                async with a.expect_navigation(timeout=15000):
                    await button.click()
                await a.wait_for_function('typeof app !== "undefined" && !!db._db', timeout=15000)
                check(f'{shell}: data gone after reset',
                      await a.evaluate("db.get('settings', 'probe')") is None)
                if shell == 'index':
                    await expect(a.locator('#home-wizard')).to_be_visible()
                    check('index: back at the setup wizard, not stuck on Loading',
                          not await a.locator('#home-loading').is_visible())
                await expect(other.get_by_text('SSD was reset or updated in another window')).to_be_visible()
                check(f'{shell}: other window asks for a reload', True)
                await ctx.close()
            await browser.close()
    finally:
        server.terminate()
    print(f'\n{passed} passed')


asyncio.run(main())
