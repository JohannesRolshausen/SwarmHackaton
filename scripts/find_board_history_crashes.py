"""List stored designed runs that ended with the read_board channel history crash.

Before the fix, LabRun.exposed_notes raised KeyError: 'id' on the next model request of an agent whose history held
a read_board result with channel history (messages without an id from another sender). The run then stored an
"Internal execution error" for that agent and, with stop_on_agent_error, ended with status error / agent_error.
This script replays the old exposure check on the stored histories, so it finds those runs although the stored
message does not name the KeyError.

Usage: python3 scripts/find_board_history_crashes.py [runs folder ...]   (default: runs/)
"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from swarm_bench.lab_storage import load_run


def old_check_fails(messages, agent, alias):
    """True when the exposure check before the fix raises KeyError on this history."""
    calls = {}
    for message in messages:
        if message.get("role") == "assistant":
            calls = {c["id"]: c["function"]["name"] for c in message.get("tool_calls") or []}
        elif message.get("role") == "tool" and calls.get(message.get("tool_call_id")) == "read_board":
            try:
                result = json.loads(message["content"])
            except (TypeError, ValueError):
                continue
            if not isinstance(result, dict):
                continue
            for note in result.get("notes", result.get("messages", [])):
                if note.get("agent_id", note.get("from")) not in (agent, alias) and "id" not in note:
                    return True
    return False


def check(folder):
    state, histories = load_run(folder)
    config = state.get("config", {})
    design = config.get("design") or {}
    if config.get("scenario") != "designed" or not (design.get("world") or {}).get("board_history"):
        return None
    handles = {a: row.get("handle") for a, row in (config.get("design_agents") or {}).items()}
    hit = sorted(a for a, messages in histories.items() if old_check_fails(messages, a, handles.get(a)))
    errors = [e for e in state.get("events", []) if e.get("kind") == "error"]
    crashed = sorted({e["agent_id"] for e in errors
                      if e.get("message") == "Internal execution error" and e.get("agent_id") in hit})
    return {"folder": str(folder), "run_id": state.get("id"), "design": design.get("id"), "mode": config.get("mode"),
            "status": state.get("status"), "finish_reason": state.get("finish_reason"),
            "agents_with_crashing_history": hit, "crashed_agents": crashed, "affected": bool(crashed)}


def main():
    roots = [Path(p) for p in sys.argv[1:]] or [ROOT / "runs"]
    rows = []
    for root in roots:
        folders = {p.parent for name in ("checkpoint.json", "state.json") for p in root.rglob(name)}
        for folder in sorted(folders):
            try:
                row = check(folder)
            except (OSError, ValueError) as exc:
                print(f"skipped {folder}: {exc}", file=sys.stderr)
                continue
            if row:
                rows.append(row)
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
    print(f"{sum(r['affected'] for r in rows)} of {len(rows)} runs with channel history ended with the crash",
          file=sys.stderr)


if __name__ == "__main__":
    main()
