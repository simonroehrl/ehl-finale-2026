# Debug runs from your coding agent

> In this document, `$API` is https://ehl-challenge.atira.ai (the CLI and API guide call it `$ATIRA_API`).

`atira mcp` makes the CLI an MCP server, so Claude Code, Codex, Cursor and other coding agents can read your runs
and debug them with you: find the failing step in the logs, see the browser's screen at that moment, check what the
agent clicked and typed, then start the next test run. It uses the CLI's sign-in, so there is nothing new to set up.

## Set it up

Install the CLI and sign in once ([CLI guide](https://ehl-challenge.atira.ai/docs/cli.md)):

```sh
curl -fsSL $API/install.sh | sh
atira login
```

Then add the server to your coding agent:

```sh
claude mcp add atira -- atira mcp     # Claude Code
codex mcp add atira -- atira mcp      # Codex
```

Other clients (Cursor, Windsurf, Claude Desktop, ...) take the usual `mcpServers` entry:

```json
{ "mcpServers": { "atira": { "command": "atira", "args": ["mcp"] } } }
```

For Codex you can also write it in `~/.codex/config.toml`:

```toml
[mcp_servers.atira]
command = "atira"
args = ["mcp"]
```

If your editor can't find `atira`, give its full path: `command -v atira` prints it. `ATIRA_TOKEN` works instead of
the stored sign-in, and `atira mcp --help` prints these lines.

## Tools

| Tool | What it does | About |
| --- | --- | --- |
| `challenge_brief` | The challenge: task, websites, rules and limits, run contract, scoring and answer schema | 6k tokens |
| `list_runs` | Your team's recent test and scored runs: status, quality, cost, time and package | 50 tokens a run |
| `get_run` | One run: status and failure, queue place or warm-up while it waits, phases, package, model calls by model with failures and queue waits, points per field, recordings, answer, run page | 1–2k |
| `diagnose_run` | Why a run failed and whether to rerun it as it is: the failure class, the last 20 stdout and stderr lines, model errors, idle stretches over 60 s, browsers whose screen froze | 2–3k |
| `run_logs` | Log lines in time order, by time (`from`, `to`), stream, text (`grep`) and `browser`; `tail` for the last lines; page by page | 30 a line |
| `run_llm_calls` | Every model call, one line each, under a header with time in calls, p50/p95 latency, the most calls at once, failures by status and, for models the organizers host, the queue wait | 35 a call |
| `run_profile` | Where the time went, per 30 s, 1 min or 5 min: inputs and screen activity per browser, model calls, seconds and cost; then the critical path | 1.5–3k |
| `run_screenshot` | A browser's screen at a time with the clicks just before it marked, every browser at once (`browser: "all"`), or a filmstrip (`frames`) | 1.5k an image |
| `run_inputs` | The clicks, scrolls and typing the platform recorded in each browser, with their times | 25 an input |
| `compare_runs` | Two runs side by side: package, quality, time, cost, cost × time, calls and failures per model, phases, points per field and answer fields that changed | 1–2k |
| `wait_for_run` | Wait until a run is running or done, including recording collection and scoring (at most `timeout_s`, default 50 s), then say how it stands | 300 |
| `list_submissions` | Your scored submissions: review status, any organizer note, result and package | 60 a submission |
| `get_submission` | One submission (`sub_...`): review status, any organizer note, its runs | 300–800 |
| `run_package` | The package a run or submission used, as uploaded: SHA-256, label, agent.json and each file's size, never the contents; `out` saves the `.tar.gz` in a folder | 15 a file |
| `leaderboard` | The board with each entry's cost × time and the Pareto front, your rank and the gap to the next; `compare_run` places one of your runs on it | 500 |
| `models` | The models the organizers host, if any, and how to call them from your computer through `atira models proxy` | 300 |
| `test_run` | Pack a folder and start a test run; returns its id, queue place, package SHA-256 and label (the git commit by default) | 150 |
| `cancel_run` | Cancel one of your runs that hasn't finished; only with `confirm: true` | 60 |
| `submit` | Pack a folder and submit it for a scored run, public and final; only with `confirm: true` after the person said yes | 100 |

Everything only reads, except `test_run`, `cancel_run` and `submit`, and `run_package` with `out`, which saves a file on your computer. A scored submission is public and can't be undone, and a cancelled run
doesn't come back, so `submit` and `cancel_run` refuse unless the call says `confirm: true`: tell your agent to ask
you first. Your agent sees only what your team sees on the run page; another team's run is "404: run not found".

Run tools take a run id (`run_...`), a test run id (`reh_...`) or a submission id (`sub_...`, meaning its latest run).
`run_package` takes the same ids; a submission id there means the package it submitted, which its runs ran.

## What it shows

`get_run` while a run waits, then once it failed:

```
run_8af0981b3710558a · test run on the public dataset · package 3f2a1c9e0b12 (2 files) "a41c2d9-dirty"
Status queued, phase queued
Queued: 2 runs ahead of yours, starts in about 3 min
...
Status failed, phase done · Failure: Run exited with code 0 without submitting an answer (no answer)
```

`run_llm_calls`:

```
run_0d7f1203b576189a · 86 model calls in a run of 16:22 · $0.4130
Time in calls 6:41 · latency p50 2.1s, p95 31s · at most 4 at once (at 02:13)
  anthropic/claude-haiku-4-5: 33 calls, $0.4126, 57,619 in (72,128 cached) / 5,867 out, 2:05 in calls
  openai/gpt-5-mini: 1 call, $0.0004, 1,845 in / 12 out, 1.2s in calls
Failures: openai: 52 failed (429 rate limited)

[02:10.4 +3.2s] anthropic/claude-haiku-4-5 200 in 4,210 (cached 3,900) out 312 $0.00471 id=c_812
[02:13.9 +0.4s] openai 429 rate limited $0.00 id=c_815
```

`run_profile` (the screen columns are seconds the browser's screen was changing; `–` is still):

```
time   b1 in · screen  b2 in · screen  b3 in · screen  b4 in · screen  calls  in calls  busy  cost
02:00  6 · 21s         3 · 8.0s        10 · 44s        0 · –           14     1:02      48s   $0.0612
03:00  4 · 12s         0 · –           0 · –           2 · 3.5s        3      41s       38s   $0.0140
Critical path:
Longest stretches over 10 s with no input, no screen change and no model call in flight:
  03:12.4–04:40.1 (01:27)
    last line before: [03:11.9] stdout: mail TAN: waiting for the TAN message
Browsers that went quiet:
  browser 3: last screen change at 02:45.4, then nothing for 04:53 until the end
```

`diagnose_run` names the failure's class and the next step: `cancelled`, `no answer` (your agent system ended
without entering one: fix it, a rerun alone won't help), `agent exit` (see stderr), `timeout` (make it faster),
`answer rejected` (fix the answer's shape) or `platform` (not your agent system: rerun it as it is). A reason that
starts with "Setup" is agent.json's setup: `run_logs` with stream `setup` shows why. Where the platform records a
run's failure class, that one is used.

## Times

Times are on the run's clock, as the run page shows them: `02:13`, `1:02:13`, `3m20s` or `133` seconds from the
run's start, or an ISO 8601 time; durations are written the same way. The same time in the run page's address,
`?t=02:13`, opens the replay there. Links point at the platform's own address, whichever name the CLI talks to.

A screenshot is cut from the recording with ffmpeg on the platform. If the platform can't do that right now, the
CLI cuts it with your own ffmpeg (`brew install ffmpeg`, or `sudo apt install ffmpeg`); without one it says so and
links the replay. A time outside a recording gets its nearest frame and says so ("browser 1's recording covers
-00:03.7–07:48.9").

## What it can't do

- **Past frames of a live run.** The recording is stored after the run ends and collection finishes; until then `run_screenshot` shows each
  browser's latest live frame, about a second behind the screen (its time says when), and
  filmstrips and screen activity wait for the end. The run page's live view shows it moving.
- **Logs as they arrive.** Tools answer once; `wait_for_run` waits up to 600 s if your client allows long tool calls
  (Codex stops a tool after 60 s unless you raise `tool_timeout_sec`). In a terminal, `atira logs <run> --follow`
  streams a run's logs until it ends.
- **Log streams** are `setup`, `stdout`, `stderr` and `input`: errors are in `stderr` and in `get_run`/`diagnose_run`,
  screen activity is in `run_profile`.
- **Typed secrets.** Inputs show typed text with keys, tokens and your run's secrets masked.

From a terminal, `atira cancel <run>` cancels a run and `atira status` lists your submissions and test runs.

## Try it

Ask your agent things like:

- "Why did my last test run fail? Diagnose it, then show me the screen at the moment it went idle."
- "Where did the time go in run_19932ad4ade523e4? Which model calls were slowest?"
- "Compare my last two test runs: what changed in the score and the cost?"
- "Start a test run of ./my-agent, wait until it's running, and show me every browser's screen at 01:00."
- "Which files did my latest submission's package have? Save it to ./downloaded and diff it against ./my-agent."

This guide is at `$API/docs/mcp.md`.
