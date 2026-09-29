"""A wrong unlock PIN must not leave the keyring half-unlocked.

Before ssd-v90 a wrong PIN at the unlock prompt was kept as the unlock key; every
later sign failed with a blank error and never asked for the PIN again.

Run: py -3 test/test-wrong-pin.py
"""
import asyncio
import subprocess
import sys
from pathlib import Path
from playwright.async_api import async_playwright, expect
from server_ready import wait_for_server

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = Path(__file__).resolve().parent.parent
PORT = 8110
URL = f'http://localhost:{PORT}'
CHROME = r'C:\Program Files\Google\Chrome\Application\chrome.exe'
passed = 0


def check(label, condition):
    global passed
    if not condition:
        raise AssertionError(label)
    passed += 1
    print(f'PASS {label}', flush=True)


async def enter_pin(page, pin, title):
    overlay = page.locator('#_pin-overlay-input')
    await overlay.wait_for(state='visible', timeout=5000)
    await expect(page.locator('h3').filter(has_text=title)).to_be_visible()
    await overlay.fill(pin)
    await page.click('#_pin-overlay-ok')


async def main():
    server = subprocess.Popen([sys.executable, '-m', 'http.server', str(PORT), '--directory', str(ROOT)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for_server(server, PORT)
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME)
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

            # As if the app were reopened: keyring locked.
            await page.evaluate('() => { keyring._sessionKey = null; keyring._pinCanary = null; }')
            await page.click('#tab-social-btn')
            await page.evaluate("document.getElementById('social-identity').value = 'fb:alice.mock'")
            await page.locator('#social-text').fill('Wrong PIN test post')
            sign = page.get_by_role('button', name='Sign post')

            await sign.click()
            await enter_pin(page, '9999', 'Enter your PIN')
            await expect(page.locator('#msg')).to_contain_text('Incorrect PIN', timeout=10000)
            check('wrong unlock PIN reported as incorrect', True)
            check('keyring stays locked after a wrong PIN',
                  await page.evaluate('keyring._sessionKey === null && keyring._pinCanary === null'))

            await sign.click()
            await enter_pin(page, '1234', 'Enter your PIN')
            check('second attempt prompts for the PIN again', True)
            await enter_pin(page, '1234', 'Enter PIN to sign')
            await expect(page.locator('#social-token-display')).to_contain_text(':fb:alice.mock:', timeout=10000)
            check('right PIN then signs the post', True)

            # Per-signature check still refuses a wrong PIN, and the canary is the real PIN's.
            check('canary accepts the right PIN only',
                  await page.evaluate("async () => (await keyring.verifyPin('1234')) && !(await keyring.verifyPin('9999'))"))
            await browser.close()
    finally:
        server.terminate()
    print(f'\n{passed} passed')


asyncio.run(main())
