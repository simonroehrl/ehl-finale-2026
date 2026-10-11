"""The run's browsers: attach over CDP and get a page. Driving them is up to you.

In a run the sandbox starts the browsers (agent.json "browsers": 1 to 4, default 1) before the clock starts and sets
CDP_URL (browser 1) and CDP_URLS (all of them, as a JSON array). Use those: the sandbox stops browsers your agent
starts itself. On your own machine there is no CDP_URLS, so `connect` starts a local Chromium (once:
`uv run playwright install chromium`; HEADLESS=1 hides it). Docs: $API/docs/building-an-agent.md
"""
import json
import os
from urllib.request import urlopen


def cdp_urls(env=os.environ) -> list[str]:
    """Every browser of the run, in order; [] on your own machine."""
    try:
        urls = [url for url in json.loads(env.get("CDP_URLS") or "[]") if isinstance(url, str)]
    except ValueError:
        urls = []
    return urls or ([env["CDP_URL"]] if env.get("CDP_URL") else [])


LOCAL_CDP_BASE_PORT = 9301
SANDBOX_SCREEN = "1280,800"  # the sandbox desktop; locally the same, so pages lay out alike


def check_cdp(url: str) -> str:
    """The browser behind a CDP URL (e.g. "Chrome/141.0..."). Fails fast, before any agent starts."""
    try:
        with urlopen(url.rstrip("/") + "/json/version", timeout=10) as response:
            return json.load(response).get("Browser", "unknown")
    except (OSError, ValueError) as error:
        raise SystemExit(f"No browser answers at {url}: {error}")


async def cdp_endpoints(playwright, count: int = 1) -> tuple[list[str], list]:
    """CDP URLs for `count` browsers, for tools that attach on their own (Playwright MCP). In a run, the sandbox's;
    locally, Chromiums started here with a debugging port. Returns the URLs and the local browsers to close."""
    urls, launched = cdp_urls(), []
    if urls:
        if len(urls) < count:
            raise SystemExit(f"{count} browsers needed, the sandbox has {len(urls)}: set \"browsers\" in agent.json")
        urls, where = urls[:count], "sandbox"
    else:
        for n in range(count):
            launched.append(await playwright.chromium.launch(
                headless=os.environ.get("HEADLESS") == "1",
                args=[f"--remote-debugging-port={LOCAL_CDP_BASE_PORT + n}", f"--window-size={SANDBOX_SCREEN}"]))
        urls, where = [f"http://127.0.0.1:{LOCAL_CDP_BASE_PORT + n}" for n in range(count)], "local"
    for n, url in enumerate(urls, 1):
        print(f"Browser {n} ({where}): {check_cdp(url)} at {url}", flush=True)
    return urls, launched


async def connect(playwright, number: int = 0):
    """Browser `number` of the run (0 is browser 1) and a page in it. Locally, a Chromium of its own."""
    urls = cdp_urls()
    if urls:
        browser = await playwright.chromium.connect_over_cdp(urls[number])
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
    else:
        browser = await playwright.chromium.launch(headless=os.environ.get("HEADLESS") == "1")
        context = await browser.new_context()
    page = context.pages[0] if context.pages else await context.new_page()
    return browser, page
