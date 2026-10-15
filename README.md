# GPU procurement experiment

Multi-agent experiment on a simulated GPU purchase. One agent can place an order; peers discuss hardware choices and can receive an instruction to argue for spending above the approved budget. The neutral solo baseline and group condition share the same supplier quote and server-host constraints.

## Run locally

```sh
git clone https://github.com/maaxgrin/SwarmHackaton.git
cd SwarmHackaton
python3 -m swarm_bench lab --port 8766
```

Open http://127.0.0.1:8766/. Python 3.10+ is sufficient for the lab. Under **Experiment type**, choose GPU procurement, one of the peer pressure experiments, another built-in type, or a design from the **Lab**. Configure a model profile, assign it to the agents, preview their prompts and tools, then create and start a run. The key stays in a gitignored local secret file or environment variable.

The experiment remains model-agnostic: **Gemini, DeepSeek and other supported providers** can be selected in Models. Gemini's thinking configuration and thought-summary handling remain available. Saving a profile makes no paid call.

## GPU conditions

- `examples/gpu-procurement-baseline.json`: neutral solo buyer.
- `examples/gpu-procurement.json`: buyer plus peers with an upsell instruction.
- Approved net budget: €50,000–100,000.
- Only the buyer has `place_order`; purchases are entirely local simulations.
- The quote, host sheet and per-team private files are generated reproducibly from the seed.
- The order deadline is configurable; `no_order`, a budget overrun and a provider failure are separate outcomes.

The existing GPU prompts, private files, deadline behavior and supplier simulation are preserved. Configure model IDs locally before importing examples.

## Experiment Lab

To design a new experiment without code, open **Lab**: start from a template or a new design, write the roles, prompts, files, tools, and rules, check every agent on **Preview**, and save. The design lands in `designs/` (commit it to share it) and appears under **Experiment type**. Each run keeps a frozen copy of its design. See [Experiment Lab](docs/EXPERIMENT_LAB.md).

## Traces and credentials

Independent agent contexts, author-labelled board messages, tool calls and rejected arguments, model usage, simulated orders and provider errors are saved locally. Replays and JSON/CSV/PDF exports are available in the interface. Each run also writes `wire_log.md`, every provider HTTP request and response as sent and read, with API keys replaced by `[REDACTED]`. Profiles, API keys and raw traces under `runs/` are not committed. Closing the browser does not stop a run; keep the host awake and online.

ImpossibleBench, synthetic peers and school supplies are not part of this publication. The peer pressure presets (split data, asset aggregation, 2024 segments, do not disclose) and the other built-in types remain in the menu, with their configurations in `examples/`.

## Verification

```sh
python3 -m unittest discover -s tests -v
```

Tests use fake providers and simulated purchases, without paid calls.
