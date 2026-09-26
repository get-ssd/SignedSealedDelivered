"""Wait for the test server instead of assuming it binds within 500 ms."""
import time
import urllib.request


def wait_for_server(server, port, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f'Test server exited with {server.returncode}')
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f'Test server on port {port} did not become ready')
