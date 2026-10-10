# Building your agent

Your agent system has to be agentic: a model decides where to go on the websites, finds the information and makes
the decisions, at run time. How is up to you: drive the browser through the DOM, through pixels, or both; use one
model or several, in parallel if you like; give the model tools and helpers for the browser, parsing, arithmetic and
checks. The model chooses where to go, what to search for and which documents matter. Rejected in review: a fixed
script that clicks through known pages with the model unused or only as a fallback, and a crawler that collects the
websites and hands everything to the model at the end.

## The browser

In the sandbox a headful Chromium runs on the desktop (`DISPLAY=:1`, 1280×800) with CDP on
`127.0.0.1:9222`.

- **DOM agents:** attach with Playwright (`chromium.connect_over_cdp(CDP_URL)`) or any CDP client.
- **Pixel agents:** take screenshots and send mouse and keyboard input to the desktop (`xdotool` and
  ImageMagick's `import` are installed), for example to drive Claude's computer-use tool.
- **Locally:** without `CDP_URL`, launch your own Chromium. The websites are the same: `eval "$(atira scout --env)"`
  gives your agent the scouting logins in `CHALLENGE_SITE_ACCESS`, the way a run gets its logins.

### Several browsers

To work on several websites at once, ask for up to four browsers in `agent.json`:

```json
{"setup": ["npm", "ci"], "run": ["node", "agent.js"], "browsers": 2}
```

Each is its own Chromium on its own screen (browser n on `DISPLAY=:n`), and `CDP_URLS` lists their CDP URLs in
order (`CDP_URL` stays the first). With Playwright:

```javascript
import { chromium } from "playwright"

const sites = JSON.parse(process.env.CHALLENGE_SITE_ACCESS)
const browsers = await Promise.all(JSON.parse(process.env.CDP_URLS).map((url) => chromium.connectOverCDP(url)))
const pages = browsers.map((browser) => browser.contexts()[0].pages()[0])
await Promise.all([pages[0].goto(sites[0].url), pages[1].goto(sites[1].url)])
```

At most 4 browsers run at a time. Use tabs (`context.newPage()`) and browser contexts (`browser.newContext()`)
inside them as freely as you like, but don't start browsers of your own, headless ones included: the sandbox stops
them and says so in the run's logs. A test run shows its browsers side by side while it runs and keeps a recording
of each. Locally there is no `CDP_URLS`: on your machine your agent starts its own browsers.

## Models

The hosted models are free to use. Every run gets `QWEN_BASE_URL` and `QWEN_API_KEY` for Qwen3.8-Flash-Next, an
OpenAI-compatible chat model with tool calls and images; on your machine `atira scout --env` sets the same variables,
pointed at `atira models proxy`. You can also bring your own key for any provider through the metering proxy;
[how cost is measured](https://ehl-challenge.atira.ai/docs/run-contract.md#how-cost-is-measured) has the details. After quality you are ranked on cost × time, so a cheaper model or a faster run that gets the answer right moves you
up.

## What makes it hard, on purpose

The websites are designed to trip agents up: cookie walls, flaky error pages that need a retry, expiring sessions
that send you back to the login, documents inside iframes and ZIP files, popups that close themselves, paging and
"load more" buttons, look-alike records, superseded versions shown first, a timezone that is not yours. Treat
every page as adversarial and verify what you read.

## Tips

- Read the brief's rules carefully: they define the one correct answer.
- Keep what the model reads small: page text without duplicated labels, only the parts you need.
- Use prompt caching for your system prompt and tool definitions.
- Re-read a page after every action that changes it; pages update asynchronously.
- Drive four browsers in parallel, one per website (`"browsers": 4`, see [Several browsers](#several-browsers)):
  reading the four sites at once speeds up each run and your iteration, at the cost of more complex code.
- Validate your answer against `answer_schema` before sending it; a `400` costs only time.
- Submit before the clock runs out: a scored run has 20 minutes (`expires_at` in the run). Every part of the answer
  scores on its own, so have your agent submit its best answer about a minute before the end rather than keep
  searching; a run that ends without an answer scores 0.
- Develop against the public websites locally (`atira scout --env`), then test in the sandbox (`atira test-run`):
  a test run shows the full score breakdown, so use it to find what your agent gets wrong.
- Work the answer out from the websites: the final run, which weighs most for the winners, uses a private dataset that is similar, not the same. It has the same jobs (a tender portal, a supplier portal, an ERP and a webmail inbox), the same bid rules and the same answer format, but different software, with a different look, navigation and site names, other content and data, and the information split differently across them. Your agent has to handle it as it finds it.

## Starter kit

[`starter-kit/`](https://ehl-challenge.atira.ai/starter-kit.tar.gz) is a minimal Python skeleton, not an agent: it reads the run and
submits an answer, attaches to the run's browsers over CDP with Playwright, and shows one call to each hosted model.
Your agent goes in `solve()` in `agent.py`; out of the box it exits without an answer. Start from it or from scratch.
