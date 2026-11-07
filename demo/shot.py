"""Screenshot the live graph. PLAYWRIGHT_BROWSERS_PATH=~/.moltbot/pw-browsers python3 demo/shot.py [url] [out]"""
import asyncio
import sys

from playwright.async_api import async_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18090/"
OUT = sys.argv[2] if len(sys.argv) > 2 else "demo/graph.png"


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1440, "height": 900})
        await pg.goto(URL)
        await pg.wait_for_timeout(6000)
        await pg.screenshot(path=OUT)
        await b.close()


asyncio.run(main())
