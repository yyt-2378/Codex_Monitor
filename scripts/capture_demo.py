from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"
URL = "http://127.0.0.1:8765"


def wait_until_ready(timeout: float = 25) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{URL}/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError("Demo server did not become ready")


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    source_root = str(ROOT / "src")
    environment["PYTHONPATH"] = source_root + os.pathsep + environment.get("PYTHONPATH", "")
    process = subprocess.Popen(
        [sys.executable, "-m", "codex_monitor", "demo", "--no-browser", "--port", "8765"],
        cwd=ROOT,
        env=environment,
    )
    try:
        wait_until_ready()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()

            desktop = browser.new_page(viewport={"width": 1536, "height": 960}, device_scale_factor=1)
            desktop.goto(URL, wait_until="networkidle")
            desktop.locator("#appShell:not([hidden])").wait_for()
            desktop.screenshot(path=ASSETS / "dashboard-desktop.png", full_page=False)
            desktop.close()

            mobile = browser.new_page(
                viewport={"width": 390, "height": 844},
                device_scale_factor=1,
                is_mobile=True,
                has_touch=True,
            )
            mobile.goto(URL, wait_until="networkidle")
            mobile.locator("#appShell:not([hidden])").wait_for()
            mobile.locator('.mobile-nav button[data-view="approvals"]').click()
            mobile.locator("#view-approvals.active").wait_for()
            mobile.wait_for_timeout(700)
            mobile.screenshot(path=ASSETS / "approvals-mobile.png", full_page=False)
            mobile.close()
            browser.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()


if __name__ == "__main__":
    main()
