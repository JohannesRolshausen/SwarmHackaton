import argparse
import json
from pathlib import Path

from .common import ORCHESTRATIONS, PRESSURES, PROMPT_STYLES, ROOT, VARIANTS, read_json, write_json
from .evaluate import dry_run, score, validate
from .generate import generate
from .runtime import Experiment, make_server


def main():
    parser = argparse.ArgumentParser(description="100 file-dependent GSM8K problems for ten agents; no model API calls.")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate", help="Generate a new dataset into an empty directory")
    gen.add_argument("--count", type=int, default=100)
    gen.add_argument("--seed", type=int, default=20260912)
    gen.add_argument("--output", type=Path, default=ROOT / "data")
    gen.add_argument("--source", type=Path, default=ROOT / "sources/gsm8k-test.jsonl")
    lab = sub.add_parser("lab", help="Open the local experiment dashboard; no API key required")
    lab.add_argument("--data", type=Path, default=ROOT / "data")
    lab.add_argument("--work-dir", type=Path, default=ROOT / "runs/lab")
    lab.add_argument("--port", type=int, default=8766)
    check = sub.add_parser("check-design", help="Check an Experiment Lab design file and list its agents")
    check.add_argument("file", type=Path)
    for name in ("validate", "dry-run", "serve", "score"):
        p = sub.add_parser(name)
        p.add_argument("--data", type=Path, default=ROOT / "data")
        if name in ("dry-run", "serve"):
            p.add_argument("--task", default="gsm8k_0001")
            p.add_argument("--variant", choices=VARIANTS, default="split10")
            p.add_argument("--orchestration", choices=ORCHESTRATIONS, default="swarm")
            p.add_argument("--pressure", choices=PRESSURES, default="none")
            p.add_argument("--prompt-style", choices=PROMPT_STYLES, default="implicit")
            p.add_argument("--run-dir", type=Path, required=name == "serve")
        if name == "serve":
            p.add_argument("--port", type=int, default=8765)
        if name == "score":
            p.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "lab":
            from .lab_server import serve_lab
            serve_lab(args.data, args.work_dir, args.port)
            return
        elif args.command == "generate":
            result = generate(args.output, args.count, args.seed, args.source)
        elif args.command == "validate":
            result = validate(args.data)
        elif args.command == "check-design":
            from .lab_engine import validate_config
            config = validate_config({"scenario": "designed", "design": read_json(args.file)})
            result = {"valid": True, "name": config["design"]["name"], "restricted": config["restricted"],
                      "agents": [{"agent_id": a, "role": row["role_name"], "handle": row["handle"],
                                  "leader": row["leader"], "forbidden_files": row["forbidden_files"],
                                  "tools": config["agent_tools"][a]} for a, row in config["design_agents"].items()]}
        elif args.command == "dry-run":
            result = dry_run(args.data, args.task, args.variant, args.orchestration, args.pressure, args.run_dir, args.prompt_style)
            if args.run_dir:
                write_json(args.run_dir / "score.json", result)
        elif args.command == "score":
            state = read_json(args.run_dir / "state.json")
            gold = next(g for g in read_json(args.data / "evaluator/gold.json") if g["task_id"] == state["task_id"])
            result = score(state, gold)
            write_json(args.run_dir / "score.json", result)
        else:
            experiment = Experiment(args.data, args.task, args.variant, args.orchestration, args.pressure, args.run_dir, args.prompt_style)
            server = make_server(experiment, port=args.port)
            url = f"http://127.0.0.1:{server.server_address[1]}"
            experiment.export_environments(url)
            print(json.dumps({"url": url, "run_dir": str(args.run_dir.resolve()),
                              "agents": "10 scoped environments exported; attach your own model adapters.",
                              "stop": "Ctrl-C; state is saved after each action. Restart/resume is not implemented."}), flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, StopIteration) as exc:
        parser.exit(1, f"Error: {exc}\n")
