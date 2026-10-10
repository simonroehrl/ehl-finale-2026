# Submitting

> In this document, `$API` is https://ehl-challenge.atira.ai (the CLI and API guide call it `$ATIRA_API`).

You submit your agent system as a gzipped tarball. We review it, run it once in the sandbox on the public dataset
and score it. That run determines its rank on the leaderboard. When the hackathon ends, your latest submission that
passed review runs once more, on a private dataset, in the [final run](#what-happens-next), which weighs most when we pick the winners.

## Package layout

```
agent.json          {"setup": ["uv", "sync"], "run": ["uv", "run", "python", "agent.py"], "models": ["qwen"]}
pyproject.toml      (or package.json, requirements.txt, ...)
agent.py            your code
```

- `agent.json` at the root: `setup` (optional) and `run`, each an argv array. Both run in the package directory.
  `browsers` (optional, 1 to 4, default 1) is how many browsers the sandbox starts for the run; see
  [the run contract](https://ehl-challenge.atira.ai/docs/run-contract.md#browsers). `models` (optional) lists the hosted models your agent system calls,
  by provider id, all of them by default: the run's clock waits for those to warm up, and `[]` starts it right after
  setup; see [the run contract](https://ehl-challenge.atira.ai/docs/run-contract.md#scored-run).
- At most 20 MB compressed; regular files only.
- No secrets: the run gets everything it needs from the environment.

## Upload

```
tar czf agent.tar.gz -C my-agent .
curl --data-binary @agent.tar.gz \
  -H "x-api-key: $ATIRA_TOKEN" -H "content-type: application/gzip" \
  $API/v1/challenges/rfq/submissions
```

The response (`202`) has your submission's id. Review is still pending and no runs exist yet: follow it with
`GET $API/v1/challenges/rfq/submissions` for the review result and run ids, or watch the leaderboard.

`$ATIRA_TOKEN` is your personal token: `atira login` signs the [CLI](https://ehl-challenge.atira.ai/docs/cli.md) in through the browser, and
`atira token` prints the token (or sign in with plain curl, as the [API guide](https://ehl-challenge.atira.ai/docs/api.md#signing-in) shows).

Or let the CLI pack, check and upload the folder: `atira submit my-agent` (and `atira test-run my-agent` for a
test run). It leaves out `.git`, `node_modules` and what `.gitignore` excludes. In a terminal, `submit` then opens
your team page, which shows the review and links each scored run as it starts, and `test-run` opens the run page.

## Test it in the sandbox first

Upload the same tarball as a test run ("Test runs" on your team page, or
`POST $API/v1/challenges/rfq/rehearsals` with the same headers). It runs once in the scored-run sandbox against
the public scenario `public-1`, scored with the points for each part of the answer. Its live view shows the
browser (a frame about every 1.5 s, or the live screen where available), the phases, every model call,
and your logs as the agent prints them. Afterwards the recorded video stays available to
your team. Up to two test runs at a time per team. `atira test-run my-agent` uploads it, starts the run
and opens it live; `atira cancel` (or Cancel run on the run page) stops a run that has not finished, and frees its
test-run slot at once.

## Linking your GitHub repository

The organizers ask each team to link the GitHub repository it builds its agent in, so they can review the
code, for example to compare a winning submission with its source. On your team page, the captain chooses
Connect a GitHub repository, installs the organizers' GitHub App on the account that owns the repository
(granting it only that repository is enough), and picks the repository. Every member sees the linked repository,
who linked it and when; the captain can switch or unlink it at any time.

The app only reads the contents of the repositories you grant it; it cannot push, open issues or change
settings. Linking does not change how you submit: scored submissions and test runs are still the tarballs
described above. To withdraw access completely, unlink the repository and uninstall the app in your GitHub
settings.

## Watching your submission

Each scored run has a live view with its phase, timer, model calls, tokens and cost. On the public dataset it also
shows your team the browser (frames, and the live screen where available), the logs and errors
unredacted and the scenario id, and the recording afterwards. The final run on the private dataset shows its
screen, frames and recordings to the organizers only; your team reads its logs and errors with private values
masked as `[private]`.

## Resubmitting

Submit as often as you like, one at a time: while a submission is still in review or has unfinished runs, the
next upload is refused with `429`. Your **best complete** submission holds your place on the leaderboard; your
**latest** one that passed review goes into the final run. Your other submissions stay in your history at
`$API/team`. Cancelling a submission's scored run ends that submission: it never completes, the leaderboard ignores it, and you
can submit again.

## What happens next

1. **Review:** passed, flagged or rejected (no runs). A flagged submission still runs and scores, but its result
   stays off the public leaderboard and the activity feed until an organizer passes it; your team sees it marked
   "flagged: waiting for an organizer". Your team sees the outcome, not the reviewer's reasons. The organizers can
   still pass or disqualify a submission later; a disqualified one reads "disqualified", leaves the leaderboard and
   the final run, and your team page gives the organizers' reason.
2. **Scored run:** one run in a fresh sandbox on the public dataset, setup first (up to 5 minutes, not counted),
   then your run command (timed).
3. **Ranking:** once the run has finished and review is passed, your submission appears on the leaderboard with its quality, cost and
   time; ineligible answers rank after every eligible one.
4. **Final run:** when the hackathon ends, the organizers run every team's latest submission that passed review
   (organizers resolve outstanding reviewer flags before the final run starts; rejected and disqualified ones don't count) once by default (or best of three if you opt in before the final run starts) on a private dataset that is similar, not the same. It has the same jobs (a tender portal, a supplier portal, an ERP and a webmail inbox), the same bid rules and the same answer format, but different software, with a different look, navigation and site names, other content and data, and the information split differently across them. Your agent has to handle it as it finds it. It is the latest one, not the best one.
   That run weighs most when we pick the winners ([Winners](https://ehl-challenge.atira.ai/docs/scoring.md#winners)), so build a system that works the answer out from the websites, not one tuned to
   the public scenario. Its results stay with the organizers until they publish them; then the leaderboard shows
   the final results, tagged "Private dataset · final", and the public-dataset board stays at
   `$API/challenges/rfq/leaderboard?dataset=public`.

## Final private evaluation: one attempt or best of three

The final runs your latest passed submission once, or up to three times if you opt in to **Best of 3** on your team
page before the final starts (`atira final --best-of 3`). Your best attempt counts. Details:
[Answers and scoring](https://ehl-challenge.atira.ai/docs/scoring.md#final-private-evaluation-one-attempt-or-best-of-three).
