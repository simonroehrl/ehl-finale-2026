# Benchmark: the three-stage pipeline with Claude Code, on public-1, locally

retrieval (`claude -p` + Playwright MCP) → `evidence.json` (schema: `bid_engine/evidence.py`) →
`bid_engine.resolve` (deterministic rules + `my-agent/pricing.py`) → bid → submission (`claude -p`).

```sh
eval "$(atira scout --challenge rfq --env)"
my-agent/.venv/bin/python benchmark/pipeline.py              # all three stages
my-agent/.venv/bin/python benchmark/pipeline.py --no-submit  # stop after the bid
```

Each run writes `benchmark/out/<time>/`: `evidence.json` and `resolution.json` (bid, trace, gaps, warnings, timings).
That output holds real public-1 values, so `out/` is git-ignored. **Never copy anything from it into a package**:
copied scenario values get a submission rejected. Use it only to compare other versions against, field by field.
