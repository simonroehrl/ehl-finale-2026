# Quorum — 3-minute pitch desk

Fullscreen product UI for the teammate HTTP agent (`pitch-agent`, Atira package `ac0554c735ab`).

Two bindings:

- **Replay (default, pitch-safe).** The public test run `run_add76ceceb77e72e`: quality 100, eligible, 04:53, $0.1898. The same package scored 100 again in 04:02.
- **Live.** Starts the unchanged agent against the scouting sites and streams stdout into the same four-worker desk.

The winning package is not modified. Bid numbers on the paper are from that public test run, not baked into the agent.

## Start the pitch

```powershell
cd pitch
uv run python server.py
```

Open http://127.0.0.1:8787 and press **F** for fullscreen.

Replay needs no Atira login and no model key.

### Optional: live attach

Run the server from a terminal that already has scouting loaded:

```powershell
# atira scout --env   (or the PowerShell equivalent)
cd pitch
uv run python server.py
```

Then click **Live**. Needs `CHALLENGE_SCOUTING` and `OPENROUTER_API_KEY` (or `~\.config\rfq\openrouter_key`). Local live skips portal submit unless `RFQ_SUBMIT=1`. The agent uses `fcntl` file locks, so live attach belongs on Linux, WSL, or the Atira sandbox — on Windows use replay.

## Keys

| Key | Action |
| --- | --- |
| Space | Continue / pause |
| 1–4 | Opening · System · Desk · Bid |
| R | Restart replay |
| T | 3:00 pitch clock |
| H | Speaker notes |
| F | Fullscreen |

## 3-minute script

Space is the story. Do not linger on the desk.

1. **0:00–0:20 Opening** Private dataset decides. Memorized pages are already dead.
2. **0:20–0:40 Split** Model finds. Kernel decides. That is why you generalize.
3. **0:40–1:10 Desk** Proof it actually runs. Skip as soon as the store starts to close.
4. **1:10–1:50 Proof** HZ-2170: one steel, two prices. This is the product.
5. **1:50–2:20 Kernel** 01.05 as a notary ledger. HALF_UP once.
6. **2:20–2:40 Private** Sockets move. Schema and kernel do not.
7. **2:40–3:00 Bid** 100, eligible. Leave the last line standing.

## Package

`pitch-agent` is the extracted tarball `Ctrl_Alt_Del-run_add76ceceb77e72e.tar.gz`. Do not copy public-run answers into that folder.
