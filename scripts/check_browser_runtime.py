#!/usr/bin/env python3
"""Fail CI early when the rendered Google fallback cannot start Chrome."""

from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.set_content("<title>Flight Radar browser check</title><main>ok</main>")
        if page.title() != "Flight Radar browser check":
            raise RuntimeError("Chrome did not render the smoke-test page")
        browser.close()


if __name__ == "__main__":
    main()
