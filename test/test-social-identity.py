"""Social identity hint: platform dropdown, handle clean-up, link preview, signing.

Run: py -3 test/test-social-identity.py
"""
import asyncio
import subprocess
import sys
from pathlib import Path
from playwright.async_api import async_playwright, expect
from server_ready import wait_for_server

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT.parent / 'ssd.desktop'
PORT, DESKTOP_PORT = 8108, 8109
URL = f'http://localhost:{PORT}'
CHROME = r'C:\Program Files\Google\Chrome\Application\chrome.exe'
passed = 0

CASES = [  # (platform chosen, typed, expected identity)
    ('fb', 'https://www.facebook.com/alice.smith', 'fb:alice.smith'),
    ('fb', 'm.facebook.com/alice.smith?ref=bookmarks', 'fb:alice.smith'),
    ('fb', 'https://www.facebook.com/profile.php?id=100012345', 'url:https://www.facebook.com/profile.php?id=100012345'),
    ('fb', '@alice.smith', 'fb:alice.smith'),
    ('x', '@alice', 'x:alice'),
    ('fb', 'https://x.com/alice', 'x:alice'),
    ('fb', 'twitter.com/alice', 'x:alice'),
    ('rd', 'u/alice_s', 'rd:alice_s'),
    ('fb', 'https://www.reddit.com/user/alice_s/', 'rd:alice_s'),
    ('tg', '@alice_s', 'tg:alice_s'),
    ('wa', '+44 7700 900123', 'wa:447700900123'),
    ('fb', 'https://wa.me/447700900123', 'wa:447700900123'),
    ('fb', 'tw:bob', 'x:bob'),
    ('url', 'https://example.com/me', 'url:https://example.com/me'),
    ('url', 'example', ''),
    ('fb', 'alice smith', ''),
    ('wa', 'abc', ''),
]


def check(label, condition):
    global passed
    if not condition:
        raise AssertionError(label)
    passed += 1
    print(f'PASS {label}', flush=True)


async def setup(browser):
    context = await browser.new_context(service_workers='block')
    page = await context.new_page()
    page.on('dialog', lambda d: d.accept(d.default_value or 'Test contact'))
    await page.goto(URL + '/?mock')
    await page.wait_for_function('typeof basicExchange !== "undefined"')
    await page.click('#wz-btn-pin')
    await page.locator('#_pin-overlay-input').fill('1234')
    await page.click('#_pin-overlay-ok')
    await page.locator('#wz-identity-name').fill('Alice')
    await page.click('#wz-btn-2')
    await page.wait_for_function("!document.getElementById('wz-btn-3').disabled")
    await page.click('#wz-btn-3')
    await page.locator('#home-status').wait_for(state='visible')
    return page


async def main():
    servers = [subprocess.Popen([sys.executable, '-m', 'http.server', str(port), '--directory', str(d)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
               for port, d in ((PORT, ROOT), (DESKTOP_PORT, DESKTOP))]
    try:
        wait_for_server(servers[0], PORT)
        wait_for_server(servers[1], DESKTOP_PORT)
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME)
            page = await setup(browser)
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))

            for platform, typed, want in CASES:
                got = await page.evaluate('''([p, t]) => {
                  const r = socialIdentity.fromInput(p, t);
                  return socialIdentity.toIdentity(r.platform, r.handle);
                }''', [platform, typed])
                check(f'{platform} + {typed!r} -> {want!r} (got {got!r})', got == want)

            # The form: dropdown + handle box replace the old free-text field.
            await page.click('#tab-social-btn')
            hidden = page.locator('#social-identity')
            sel = page.locator('#social-compose .si-platform')
            box = page.locator('#social-compose .si-handle')
            check('old field hidden, widget shown', await hidden.is_hidden() and await sel.is_visible())

            await sel.select_option('x')
            await expect(page.locator('#social-compose .si-prompt')).to_contain_text('without the @')
            await box.fill('@alice')
            check('typing updates the identity', await hidden.input_value() == 'x:alice')
            await expect(page.locator('#social-compose .si-preview a')).to_have_attribute('href', 'https://x.com/alice')
            check('preview links to the profile Tick will use', True)

            await box.fill('https://www.facebook.com/alice.smith')
            await box.dispatch_event('change')
            check('pasted Facebook address switches platform and keeps the handle',
                  await sel.input_value() == 'fb' and await box.input_value() == 'alice.smith'
                  and await hidden.input_value() == 'fb:alice.smith')

            await box.fill('alice smith')
            await expect(page.locator('#social-compose .si-preview')).to_contain_text('No spaces')
            check('bad handle is flagged and not used', await hidden.input_value() == '')
            await box.fill('alice.smith')

            # Code that sets the value (prefill, edit) re-renders the widget.
            await page.evaluate("document.getElementById('social-identity').value = 'rd:carol'")
            check('set value shows in the widget',
                  await sel.input_value() == 'rd' and await box.input_value() == 'carol')
            await page.evaluate("document.getElementById('social-identity').value = 'fb:alice.smith'")

            # Sign: the identity goes into the token and is remembered on the key.
            await page.locator('#social-text').fill('Hello from the dropdown test')
            await page.get_by_role('button', name='Sign post').click()
            pin = page.locator('#_pin-overlay-input')
            try:
                await pin.wait_for(state='visible', timeout=1500)
                await pin.fill('1234')
                await page.click('#_pin-overlay-ok')
            except Exception:
                pass
            await expect(page.locator('#social-token-display')).to_contain_text(':fb:alice.smith:', timeout=10000)
            check('token carries fb:alice.smith', True)
            saved = await page.evaluate("async () => (await db.getAll('my_keys')).some(k => k.identity === 'fb:alice.smith')")
            check('identity remembered on the key', saved)

            # Advanced and desktop shells mount it; an unknown prefix is kept as entered.
            for name, url in (('advanced', URL + '/advanced.html?mock'), ('desktop', f'http://localhost:{DESKTOP_PORT}/')):
                ctx = await browser.new_context(service_workers='block')
                pg = await ctx.new_page()
                pg.on('pageerror', lambda e, n=name: errors.append(f'{n}: {e}'))
                await pg.goto(url)
                await pg.wait_for_function('typeof app !== "undefined" && typeof socialIdentity !== "undefined"')
                await pg.evaluate("document.getElementById('social-identity').value = 'fp:ABCD1234'")
                opt = await pg.evaluate("document.querySelector('.si-platform').value + '|' + document.querySelector('.si-handle').value")
                check(f'{name}: widget mounted, fp: kept as entered ({opt})', opt == 'fp|ABCD1234')
                await ctx.close()

            check(f'no page errors ({errors})', not errors)
            await browser.close()
    finally:
        for s in servers:
            s.terminate()
    print(f'\n{passed} passed')


asyncio.run(main())
