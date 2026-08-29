"""Capture the README screenshots from the demo install.

Drives the browser against the cleanroom on the Pi, which holds invented
systems, corporations and characters. Nothing here touches the real alliance's
data, so the images can be published without leaking which hub runs dry when.

Uses the Edge already on this machine rather than downloading a browser.
"""

import os
import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("HOLDFAST_DEMO_URL", "http://127.0.0.1:8899")
SESSION = sys.argv[1] if len(sys.argv) > 1 else None
OUT = pathlib.Path(__file__).resolve().parent / "images"

SHOTS = [
    ("sov-fuel", "/holdfast/sov/fuel/"),
    ("sov-timers", "/holdfast/sov/timers/"),
    ("skyhook-dashboard", "/holdfast/skyhook/dashboard/"),
    ("den-admin", "/holdfast/den/admin/"),
]


def main():
    if not SESSION:
        raise SystemExit("usage: shoot.py <sessionid>")
    OUT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        for channel in ("msedge", "chrome", None):
            try:
                browser = p.chromium.launch(channel=channel) if channel else p.chromium.launch()
                print(f"using {channel or 'bundled chromium'}")
                break
            except Exception as error:
                print(f"  {channel}: {str(error).splitlines()[0][:80]}")
        else:
            raise SystemExit("no browser available")

        context = browser.new_context(
            viewport={"width": 1680, "height": 1050},
            device_scale_factor=2,        # retina-ish, so text stays sharp in a README
            locale="en-GB",
        )
        context.add_cookies([{
            "name": "sessionid", "value": SESSION,
            "url": BASE, "path": "/",
        }])
        page = context.new_page()

        for name, path in SHOTS:
            page.set_viewport_size({"width": 1680, "height": 1050})
            page.goto(BASE + path, wait_until="networkidle")
            page.wait_for_timeout(700)

            # Shrink the window to what the page actually fills. A shot sized
            # to a fixed viewport is mostly empty grey below a six-row table,
            # which in a README reads as "this tool has nothing in it".
            # Measure our own content block, not the page. Auth's sidebar runs
            # the full window height whatever is in it, so measuring the page
            # just reproduces the window and leaves a field of grey under a
            # four-row table.
            height = page.evaluate("""() => {
                const block = document.querySelector('.holdfast');
                if (!block) return 700;
                const bottom = block.getBoundingClientRect().bottom + window.scrollY;
                return Math.round(Math.min(Math.max(bottom + 28, 340), 1600));
            }""")
            page.set_viewport_size({"width": 1680, "height": height})
            page.wait_for_timeout(300)

            target = OUT / f"{name}.png"
            page.screenshot(path=str(target))
            size = target.stat().st_size
            print(f"  {name:<20} 1680x{height:<5} {size/1024:6.0f} KB")

        browser.close()


if __name__ == "__main__":
    main()
