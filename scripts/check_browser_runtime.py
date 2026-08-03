#!/usr/bin/env python3
"""Fail CI early when the rendered Google fallback cannot start Chrome."""

from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--no-sandbox"],
            )
        except Exception:
            # Match scanner/google_browser.py: CI normally has Chrome, while
            # local machines may only have Playwright's bundled Chromium.
            browser = playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox"],
            )
        page = browser.new_page()
        page.set_content("<title>Flight Radar browser check</title><main>ok</main>")
        if page.title() != "Flight Radar browser check":
            raise RuntimeError("Chrome did not render the smoke-test page")
        browser.close()


if __name__ == "__main__":
    main()
