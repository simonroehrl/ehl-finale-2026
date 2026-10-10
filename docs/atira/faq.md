# FAQ and event details

## FAQ

**Which models can I use?** The hosted models are free, and the starter kit shows how to call them. Or bring your own key for
any provider (Anthropic, OpenAI, OpenRouter, Google, Groq, Mistral, DeepSeek, xAI, Together, Fireworks, or others the
organizers add) and send its calls through the metering proxy. `GET /v1/runs/current` lists the proxy base URLs. Models must have a published
price (OpenRouter reports its own) and satisfy any organizer model allowlist. See
[the run contract](https://ehl-challenge.atira.ai/docs/run-contract.md#llm-calls) for SDK setup.

**Where do I keep my model key?** As a [team secret](https://ehl-challenge.atira.ai/docs/run-contract.md#team-secrets): the team page's Secrets list
injects it into your agent's run step in test and scored runs, redacted from logs. Don't put keys in your
package; we store packages and the review agent reads them.

**Why does my model call get a 401?** "Add your OpenAI API key to the request" means no provider key arrived: set
`OPENAI_API_KEY` (in test and scored runs, as a [team secret](https://ehl-challenge.atira.ai/docs/run-contract.md#team-secrets)). A 401 from the
provider itself means the key is wrong. Check that `OPENAI_BASE_URL` points at the run's proxy
(`$LLM_PROXY_URL/openai/v1`, already set in test and scored runs); the same holds for any OpenAI-compatible provider.

**How do my teammates and I sign in from a terminal?** Each person runs `atira login` on their own computer
and approves it in the browser. Every device gets its own token; there is no shared team key to pass around.
Without a browser on the machine, `atira login --device` prints a link and a code to approve elsewhere.

**I lost a laptop, or a token leaked.** Revoke that device's token on your team page under Signed-in devices
(`atira logout` revokes the one on the computer you are at). Leaving or being removed from a team ends your
tokens' access to it at once.

**My invite link says it is for someone else.** Invites are for one GitHub account. Sign in with the account
the captain invited, or ask the captain to invite your GitHub username.

**Does it cost me anything?** We charge nothing, and the models the platform hosts are free and never charged. If
you bring your own key for another provider, that provider bills your key. The proxy measures every call and
ranks you on its cost at the published prices; hosted calls count at simulated API prices.

**How can I try my agent?** Develop locally against the public websites (`atira scout --env` exports the
scouting logins), then test in the sandbox (`atira test-run ./my-agent`). A test run runs on `public-1`, the public
RFQ scenario, and returns your score with the points for each part of the answer. With the scouting logins nothing
counts: a bid entered on the tender portal is acknowledged without counting.

**Is the public scenario's answer published?** No. The correct answer isn't published; your system has to work it
out from the websites. Score breakdowns show the points for each part of the answer, never the expected values.

**Which submission counts?** Your best submission holds your place on the leaderboard, which ranks scored runs on
the public dataset. Your latest one that passed review (not your best one) goes into the final run: when the
hackathon ends, the organizers run it once by default (or best of three if you opt in before the final run starts) on a private dataset that is similar, not the same. It has the same jobs (a tender portal, a supplier portal, an ERP and a webmail inbox), the same bid rules and the same answer format, but different software, with a different look, navigation and site names, other content and data, and the information split differently across them. Your agent has to handle it as it finds it.
That run weighs most when we pick the winners, ahead of your public score and the elegance of your system, so
build an agent that works the answer out from the websites, not one tuned to the public scenario. See
[Winners](https://ehl-challenge.atira.ai/docs/scoring.md#winners).

**Why did my answer get a 400?** It does not match `answer_schema`. The response lists the issues; the run is
still open, so fix it and send it again.

**Can I run my agent outside the sandbox?** While you develop, yes, anywhere, with the scouting logins. Test runs
and scored runs always run in our sandbox.

**Why is my run still queued?** Runs share the sandboxes in a fair order: scored runs first, then teams take turns,
the team with the fewest runs in flight first. Your team runs up to two test runs at a time and can have up to three
waiting; the next waits while other teams' runs start. The run page says how many runs are ahead of yours, and
`atira test-run --follow` prints it. See [the run queue](https://ehl-challenge.atira.ai/docs/api.md#the-run-queue).

**How do I stop a run?** `atira cancel <run>`, or Cancel run on its run page. The sandbox is removed, the run fails
with "Cancelled by @you", and you can start the next test run at once. A cancelled scored run ends its
submission: the leaderboard ignores it, and you can submit again.

**Why do my requests get a 403 "Error 1010"?** The edge in front of our hosts refuses some default HTTP
clients, such as Python's `urllib`. Send your own `User-Agent` header (the starter kit does).

**Python says `CERTIFICATE_VERIFY_FAILED` on my Mac.** Python from python.org ships without CA certificates.
Run `Install Certificates.command` from its folder in Applications, or set `SSL_CERT_FILE=/etc/ssl/cert.pem`.

**What can my agent reach during a scored run?** Our API (including the model proxy) and the challenge's
websites, nothing else. The `HTTP(S)_PROXY` variables in the run's environment route there; Python's
`urllib`, `requests`, `httpx`, the Anthropic SDK and Node's `fetch` pick them up automatically. Chromium is
already configured. Setup (`uv sync`, `npm ci`, …) has network, but nothing it starts survives into the
run.

## Event details

- The event is on 10 October 2026.
- A team has up to five people.
- A scored run has 20 minutes on the clock; a test run has up to 2 hours.
- Ask the organizers about the submission deadline and prizes.

## Final private evaluation: one attempt or best of three

The final runs your latest passed submission once, or up to three times if you opt in to **Best of 3** on your team
page before the final starts (`atira final --best-of 3`). Your best attempt counts. Details:
[Answers and scoring](https://ehl-challenge.atira.ai/docs/scoring.md#final-private-evaluation-one-attempt-or-best-of-three).
