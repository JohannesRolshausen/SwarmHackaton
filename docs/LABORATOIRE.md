# GPU lab

Start `python3 -m swarm_bench lab --port 8766`. Configure models, choose agents and the buyer, preview the scenario, create and start. Use the baseline example for a neutral solo buyer and the group example for upsell pressure.

Each agent has a private history and tools. Shared notes preserve author identity and can be read voluntarily or injected into peer contexts. JSON, CSV and PDF exports retain the experiment's configuration and observable events. Keys and local raw histories are excluded from Git. Purchases are simulations.

Existing provider adapters, including Gemini thinking support and DeepSeek, and the GPU order-deadline behavior are preserved. No experiment starts just by loading this page or saving a model profile.

The **Lab** page designs new experiments without code; see [Experiment Lab](EXPERIMENT_LAB.md).

`runs/lab/runs/<id>/wire_log.md` is written at the HTTP layer, not from the engine's events: a urllib handler records each request (URL, headers, body) right before the connection is made, and `completion()` records the status, headers and body bytes read back, or the timeout or connection error. API keys are replaced with `[REDACTED]`. JSON bodies are pretty-printed; the listed size and SHA-256 belong to the exact bytes. The file is append-only, so a resumed run continues it. The **Wire log** button next to **Chats PDF** downloads it (`GET /api/runs/<id>/wire_log.md`).
