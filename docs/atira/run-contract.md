# How a run works

> In this document, `$API` is https://ehl-challenge.atira.ai (the CLI and API guide call it `$ATIRA_API`).

A run is one attempt at one scenario of one challenge. It starts with a run token, gives your agent a task and
logins for the challenge's websites, and ends when your agent submits its answer or time runs out. Test runs and
scored runs both run your package in the [sandbox](#scored-run); you develop on your own machine with the scouting
logins.

## Develop locally, then test in the sandbox

RFQ has one public scenario, `public-1`: the websites as you see them.

1. **Develop locally** against the public websites with the scouting logins the organizers publish:
   `eval "$(atira scout --env)"` (in PowerShell: `atira scout --env | Out-String | Invoke-Expression`) sets
   `CHALLENGE_SITE_ACCESS` (the websites and their logins, the JSON a run gets), each website's URL as
   `CHALLENGE_SITE_<ID>_URL`, and `CHALLENGE_SCOUTING`, the `brief`, `access`, `submission` and `answer_schema` of
   `GET $API/v1/challenges/rfq/scouting` as JSON (`CHALLENGE_SCOUTING_URL` is that address). There is no run token.
   Where the platform hosts models, they need no key: `--env` also exports each one's base URL and key, pointed at
   `atira models proxy` on your computer with `local` as the key, so run `atira login` once, then keep
   `atira models proxy` running in another terminal. Those calls are free, recorded and never ranked. Any other
   provider takes your own key.
   Nothing done with these logins counts.
   A bid entered on the tender portal is acknowledged without counting.
2. **Test in the sandbox:** `atira test-run ./my-agent` uploads your package, starts a test run on `public-1` and
   opens its run page live. It runs in the same sandbox as a scored run, but for up to 2 hours instead of 20 minutes (keep it under
   20 to be safe), and the run page shows its
   score with the full breakdown. `atira cancel` stops it early.

## Inside a run

1. **Read the task:** `GET $CHALLENGE_API_URL/v1/runs/current` with the header `x-api-key: $CHALLENGE_RUN_TOKEN`
   returns:
   - `brief`: the task statement in markdown, with the rules;
   - `access`: one entry per website: `siteId`, `url`, and `credentials` (`username`, `password`);
   - `submission`: where the answer goes: `{"kind": "api", "url": ...}` (POST it to that URL) or
     `{"kind": "site", "siteId": ..., "url": ...}` (submit it on that website, e.g. RFQ's configurator);
   - `answer_url` (API submissions only) and `answer_schema` (JSON Schema of the answer);
   - `started_at`, `expires_at`, and what the run has spent on LLM calls so far.
   - `llm_proxy_url`: the model proxy base with this run's token, `llm_providers` and `llm_base_urls` (each
     provider's proxy base), and `llm_env` (the SDK base-URL variables the run step gets).
   Keep the token secret: it is your agent's identity for this run.
2. **Work:** your agent logs into the websites with those credentials and collects what it needs. Its LLM calls
   go through our metering proxy: a hosted model's with the run token its key variable already holds, any other
   provider's with your own key (below).
3. **Answer** through the run's `submission` target. For an `api` target, `POST` the answer as JSON to
   `answer_url` with the same `x-api-key` header. For a `site` target, submit it on that website: the site
   forwards it to the platform, shows the same validation issues in German, and confirms with a receipt.
   - `400` with `issues`: the answer is malformed. Nothing is recorded, the clock keeps running, fix it and send
     it again.
   - `202`: accepted and final. The run ends, the token and the website logins stop working, and a test run
     returns its score and breakdown. Your agent then has 10 seconds to exit before the platform stops it.

Site logins are issued per run and only work while that run is live. Send tokens in the `x-api-key` header
(`Authorization: Bearer` works too). Send a User-Agent of your own: the edge in
front of the API currently refuses Python's default `Python-urllib/…` agent with 403. Test runs last up to 2 hours.

## LLM calls

Use the [hosted models](#how-cost-is-measured), which are free, or bring your own key for any provider and model.
Every call goes through our proxy, which forwards it unchanged and only measures it: a provider you bring bills your
own key, and the ranking uses what the call costs at the published prices. A scored run's sandbox reaches only our
API and the challenge's websites, so its model calls work only through the proxy; test runs are metered the same way.

The run token sits in the proxy path, so your key keeps its usual header and an SDK needs only a base URL:

```
$API/llm/run/<run token>/<provider>/<the provider's own API path>
```

`llm_proxy_url` is the run's proxy, `<run API>/llm/run/<run token>`; in a test or scored run it is `$LLM_PROXY_URL`. Built-in providers:

| Provider | Forwards to | Base URL for its SDK or an OpenAI-compatible client |
| --- | --- | --- |
| `anthropic` | `https://api.anthropic.com` | `$LLM_PROXY_URL/anthropic` |
| `openai` | `https://api.openai.com` | `$LLM_PROXY_URL/openai/v1` |
| `openrouter` | `https://openrouter.ai/api` | `$LLM_PROXY_URL/openrouter/v1` |
| `google` | `https://generativelanguage.googleapis.com` | `$LLM_PROXY_URL/google` (Gemini SDK), `…/google/v1beta/openai` |
| `groq` | `https://api.groq.com/openai` | `$LLM_PROXY_URL/groq/v1` (use an OpenAI client) |
| `mistral` | `https://api.mistral.ai` | `$LLM_PROXY_URL/mistral/v1` |
| `deepseek` | `https://api.deepseek.com` | `$LLM_PROXY_URL/deepseek/v1` |
| `xai` | `https://api.x.ai` | `$LLM_PROXY_URL/xai/v1` (HTTP API; the gRPC `xai-sdk` cannot use the proxy) |
| `together` | `https://api.together.xyz` | `$LLM_PROXY_URL/together/v1` |
| `fireworks` | `https://api.fireworks.ai/inference` | `$LLM_PROXY_URL/fireworks/v1` |

The organizers may add providers; `llm_providers` lists them all, the models the platform hosts itself included,
each under its own id with its base URL in `<MODEL>_BASE_URL`. Any path under a provider's base works
(messages, chat completions, responses, embeddings, model listings) with the request's method and body intact.

### Your key

Send your provider key the way the provider expects it: `x-api-key` (Anthropic), `Authorization: Bearer`
(OpenAI and compatible providers), `x-goog-api-key` or `?key=` (Gemini), or `api-key`. The proxy forwards it
unchanged and never stores, logs or returns it. For test and scored runs, keep it as a [team secret](#team-secrets),
not in your package: we store packages and the review agent reads them.

With the base-URL variables set, the providers' SDKs need no changes: OpenAI's reads `OPENAI_BASE_URL` and sends
`OPENAI_API_KEY` as `Authorization: Bearer`, Anthropic's reads `ANTHROPIC_BASE_URL` and sends `ANTHROPIC_API_KEY`
as `x-api-key`:

```python
from openai import OpenAI

client = OpenAI()  # reads OPENAI_BASE_URL and OPENAI_API_KEY
```

A client that cannot set the provider's own header may send the key as `x-provider-key`; the proxy forwards it in
the provider's header. For Google's Gemini SDK, set the HTTP base URL explicitly:

```python
import os
from google import genai

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"],
    http_options={"base_url": os.environ["LLM_PROXY_URL"] + "/google"})
```

```javascript
import { GoogleGenAI } from "@google/genai"

const client = new GoogleGenAI({ apiKey: process.env.GEMINI_API_KEY,
  httpOptions: { baseUrl: process.env.LLM_PROXY_URL + "/google" } })
```

A request without a key of its own gets `401` "Add your <provider> API key to the request.", unless the
organizers pay for that provider: then the proxy adds their key to a run's calls. In that case a scored run's
key variable for that provider (for example `OPENAI_API_KEY`) holds the run token as a placeholder, so SDKs
start, unless you have a team secret by that name; the proxy never forwards the token.

**Development outside runs:** with your personal token you may use the proxy without a run, at
`$API/llm/<provider>/…` with the header `x-atira-team-key: <token>`, or at `$API/llm/team/<token>/<provider>/…`
(prefer the header: a token lasts 90 days, and URLs end up in logs). Bring your own key; this usage is recorded
for your team and never ranked. Older clients may still call `$API/llm/<provider>/…` with the run token in
`x-api-key` and the provider key in `x-provider-key`. Without a run token or personal token, the proxy answers
`401`.

A request body may be up to 32 MiB (`413` above that), enough for several full-screen screenshots and a long
context.

### How cost is measured

Every call is recorded. Reported usage, including cached input and thinking tokens, is priced at each
provider's published per-token prices for that model, with any deployment-specific prices published by the organizers.
An upstream-reported USD cost takes precedence. OpenRouter reports one for every call (send
`"usage": {"include": true}` with older clients), so models without a published price are accepted there; a call
that reports no cost is charged at the highest published price. Elsewhere, unpriced models and models outside
a configured allowlist are refused before forwarding. Streamed calls count too: the proxy asks OpenAI-compatible
providers for a stream's usage (`stream_options.include_usage`), so a streamed chat completion ends with one more
chunk with empty `choices` and the usage. A stream cut off before its usage, or one whose provider reports none, counts
an estimate from the text that arrived and from the request, about four characters per token; a client that hangs up
early is still billed for everything the provider sends. There is no spending limit. Model listings and other
endpoints without inference usage cost nothing; Anthropic `count_tokens` also costs nothing. All network and
fair-play rules still apply, including when using provider-side tools.

The models the platform hosts itself (`GET /v1/models/status` lists them with their prices) are free to use, never
charged; each call counts toward your run's cost at simulated API prices, so the cost ranking stays honest.

### Team secrets

Team secrets are environment variables for your agent's run step in test runs and scored runs, such as
`OPENAI_API_KEY`. Manage them on the team page under Secrets, or with your personal token:

```sh
curl -X PUT -H "x-api-key: $ATIRA_TOKEN" -H "content-type: application/json" \
  --data '{"value": "sk-…"}' "$API/v1/teams/me/secrets/OPENAI_API_KEY"
curl -H "x-api-key: $ATIRA_TOKEN" "$API/v1/teams/me/secrets"   # names and dates only
curl -X DELETE -H "x-api-key: $ATIRA_TOKEN" "$API/v1/teams/me/secrets/OPENAI_API_KEY"
```

Values are encrypted, never shown again and redacted from run logs; saving a name again replaces its value.
Setup never receives them. Names are uppercase letters, digits and underscores, starting with a letter, and
cannot be platform variables (`CHALLENGE_*`, `*_BASE_URL`, `*_SERVER_URL`, `*_PROXY`, `LLM_PROXY_URL`,
`CDP_URL`, `CDP_URLS`, `BROWSER_COUNT`, `PATH`, `HOME`, `DISPLAY`, …). A team keeps at most 20; each value is 16 characters to 4 KB long.

## Scored run

Each scored submission runs once in the sandbox on the public dataset (RFQ's `public-1`) and is scored; that run
ranks on the leaderboard. The final run at the end of the hackathon uses the same sandbox on a private dataset (see
[Submitting](https://ehl-challenge.atira.ai/docs/submitting.md#what-happens-next)). Runs are created from your uploaded package. The sandbox is
a Linux VM with a desktop at 1280×800, a **headful Chromium** already open with the Chrome DevTools Protocol on
`127.0.0.1:9222` (or up to four, see [Browsers](#browsers)) and these toolchains, ready for your user:

- **TypeScript and JavaScript:** Node 24 (runs `.ts` files directly), npm, pnpm and bun.
- **Python:** Python 3.12 and uv (`uv venv`, `uv pip install`, `uv run`); there is no system pip.
- **Rust:** stable Rust (1.99) with cargo. Fetch crates during setup; the run is offline except for the platform
  and the websites.
- gcc, make, git, curl and jq.

1. **Setup (not counted toward your time, up to 5 minutes):** your `setup` command runs with network access but without any credentials, e.g. to install dependencies.
2. **Model warm-up (not counted toward your time):** the models the platform hosts itself, when it offers any, scale
   to zero when nobody uses them, and waking one takes one to eight minutes. The platform wakes them as soon as your
   run leaves the queue and starts your run only once they're warm (at most 10 minutes after setup), so a cold start
   never counts toward your time. The run's log says when they're warm. While runs are active, the platform keeps
   them warm. `"models"` in `agent.json` lists the ones your agent system calls, by their `provider` id from
   `GET /v1/models/status` (the start page shows it too); the run waits for those alone, and `"models": []` starts it
   right after setup:

   ```json
   {"setup": ["uv", "sync"], "run": ["uv", "run", "python", "agent.py"], "models": []}
   ```

   Without it the run waits for every hosted model. Anything else, such as an unknown id, rejects the package with
   `Invalid agent.json: models: …` and the ids it takes. It decides only the wait: your run may call any hosted model
   whatever it lists, but one it didn't list may still be waking.

   **If a hosted model fails mid-run:** retry failed calls, as with any provider; a short outage is yours to ride out.
   If our models were down for most of the run (from the first failed call on, a 5xx from our model or a lost
   connection to it, at least 8 in 10 of the run's hosted-model calls failed, over at least half its time) and the run
   then ended without an answer, the platform failed, not your agent system: the run doesn't count toward ranking or
   any limit, and a scored run starts again once by itself. A
   run that answered keeps its result.
3. **Run (timed):** your `run` command starts with this environment. The clock starts here.

| Variable | Value |
| --- | --- |
| `CHALLENGE_API_URL` | the API; read your task at `$CHALLENGE_API_URL/v1/runs/current` |
| `CHALLENGE_RUN_TOKEN` | the run token |
| `CHALLENGE_SITE_ACCESS` | the websites with this run's logins, as JSON: the same as `access` |
| `LLM_PROXY_URL` | `$CHALLENGE_API_URL/llm/run/$CHALLENGE_RUN_TOKEN` |
| `<MODEL>_BASE_URL`, `<MODEL>_API_KEY` | for each model the platform hosts (`GET /v1/models/status`; the start page names them): its base URL through the proxy, under `$LLM_PROXY_URL/<provider>`, and the run token as its key |
| `ANTHROPIC_BASE_URL` | `$LLM_PROXY_URL/anthropic` |
| `OPENAI_BASE_URL` | `$LLM_PROXY_URL/openai/v1` |
| `GOOGLE_GEMINI_BASE_URL` | `$LLM_PROXY_URL/google`; set the Gemini SDK's HTTP base URL explicitly as above |
| `MISTRAL_SERVER_URL`, `TOGETHER_BASE_URL` | `$LLM_PROXY_URL/mistral`, `$LLM_PROXY_URL/together/v1` |
| your [team secrets](#team-secrets) | for example `OPENAI_API_KEY` with your own key |
| `CDP_URL` | `http://127.0.0.1:9222`, browser 1 |
| `CDP_URLS` | every browser's CDP URL in order, as a JSON array: `["http://127.0.0.1:9222"]` for one browser |
| `BROWSER_COUNT` | how many browsers the sandbox started: `1` unless `agent.json` asks for more |
| `DISPLAY` | `:1`, browser 1's screen; browser n is on `:n` |
| `HTTP_PROXY`, `HTTPS_PROXY` (and lowercase) | `http://127.0.0.1:3128`, the only way out of the sandbox |
| `NO_PROXY` | `localhost,127.0.0.1` (CDP stays local) |

The platform sets no API key variables of its own, except the run-token placeholder for a provider the
organizers pay for (above). `setup` runs first, with network (install your dependencies there) but without the
run's variables or your team secrets, and nothing it starts survives into the run. The run itself reaches only our API and the challenge's websites,
through that proxy; Chromium is already configured to use it. A scored run of RFQ has 20 minutes; a run that
ends without an answer scores 0.

## Browsers

The sandbox starts one browser unless `agent.json` asks for more, up to four:

```json
{"setup": ["uv", "sync"], "run": ["uv", "run", "python", "agent.py"], "browsers": 3}
```

`browsers` is a whole number from 1 to 4; anything else rejects the package with
`Invalid agent.json: browsers: must be a whole number from 1 to 4`. Browser n is its own headful Chromium with its
own profile, on display `:n` (1280×800) with the Chrome DevTools Protocol on port 9221 + n (9222 to 9225), behind
the same proxy as browser 1. All of them are ready before the clock starts. `CDP_URL` stays browser 1, `CDP_URLS` lists all of
them in order, and `BROWSER_COUNT` says how many there are. With three:

| Variable | Value |
| --- | --- |
| `CDP_URL` | `http://127.0.0.1:9222` |
| `CDP_URLS` | `["http://127.0.0.1:9222","http://127.0.0.1:9223","http://127.0.0.1:9224"]` |
| `BROWSER_COUNT` | `3` |

**At most 4 browsers run at a time:** ask for up to 4 in `agent.json` (`"browsers"`), use tabs and browser contexts
inside them freely, and don't start browsers of your own, headless ones included. The scored sandbox enforces it:
any other browser the agent starts is stopped within seconds, and the run's logs say so with
`[platform] stopped a browser the agent started: at most 4 run at a time; use the browsers from CDP_URLS`. Every
browser then is one the platform can show and record: test runs show them side by side while they run, with a
recording of each afterwards.

On your own machine, developing with the scouting logins, `CDP_URLS` is not set and your agent starts its own
browsers.
