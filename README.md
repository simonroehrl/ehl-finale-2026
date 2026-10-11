# EHL RFQ agent: DeepSeek over plain HTTP

The package submitted to the Atira EHL RFQ challenge (submission `sub_587f300706333b01`).

An agent researches the four websites of a tender (tender portal, customer portal, ERP, webmail), records what they say
in a shared evidence store, and code applies the brief's rules to those records and prices the bid. The agent then
enters and signs the bid on the tender portal.

- `main.py`: the model loop. `deepseek/deepseek-v4.1-flash` on OpenRouter (providers Relace, Baidu, DeepSeek; a stalled
  call gets a twin request on the next provider). Parallel research workers, gap rounds, then the submit session.
- `web_tools.py`: the four tools over plain HTTP: `view` (page dumps with a link index), `submit` (HTML form posts),
  `record` (schema-checked evidence with grounding, unit and number-format guards) and `gaps`.
- `evidence.py`: the evidence schema. `resolver.py`: applies the brief's rules to the records. `pricing.py`: the bid
  arithmetic. `run.py`: the run contract.

Run: `agent.json` (`uv sync`, then `uv run --no-sync python main.py`). Needs `OPENROUTER_API_KEY`.
Sandbox test runs: 100 quality in 3:46–5:08 at about $0.08–0.10 per run.
