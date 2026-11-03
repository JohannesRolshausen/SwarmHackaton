"""Smoke test with real calls: does each model of the plan return its thinking, and does the lab show it?

For every model one small real run goes through the lab (two agents, one round, about three calls, all with the
same model). The script then checks what was sent and what came back:

1. Before any paid call: the request this profile would send asks for readable thinking.
2. The wire log of the run: every request that went out asked for it.
3. The saved run, read back from disk as the interface reads it: replies with stored thinking text, per agent.
4. The Thinking tab, drawn by renderThinking() of app.js in Node: it shows every stored text.

The runs are saved under runs/lab/runs like any other run, so they can be opened in the lab afterwards
(choose the run, an agent, then the Thinking tab). They are not part of the evaluation.

Keys come from the saved lab profiles (runs/lab) or from the environment and .env:
OPENAI_API_KEY (GPT-6 Astra), GEMINI_API_KEY (Gemini 4 Argon, Gemini 3.8 Flash), OPENROUTER_API_KEY (MiMo V2.6 Pro).
A saved profile with the same model id is used in place of the built-in one, since that is what the runs use.

    python scripts/smoke_thinking.py                     # every model of the plan
    python scripts/smoke_thinking.py --only argon mimo   # some of them
    python scripts/smoke_thinking.py --profile my-claude # saved profiles by id, instead of the plan
    python scripts/smoke_thinking.py --design designs/group-spending-baseline.json   # a whole design (more calls)

The exit code is 0 only when every chosen model passed.
"""
import argparse
import copy
import html
import json
import os
import sys
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swarm_bench.common import ROOT, read_json  # noqa: E402
from swarm_bench.experiment_design import expand, validate_design  # noqa: E402
from swarm_bench.lab_server import LabManager  # noqa: E402
from swarm_bench.leader_signals import leader_columns  # noqa: E402
from swarm_bench.providers import ProviderError, completion, load_local_env  # noqa: E402
from swarm_bench.thinking_check import (PLAN, stored_thoughts, thinking_request, thinking_tab_html,  # noqa: E402
                                        wire_requests)

# Two agents and one round: the lead asks, the colleague answers, a direct message asks for the decision.
SMOKE_DESIGN = {
    "name": "Smoke test · thinking",
    "description": "Two agents, one round. Shows whether a model returns thinking text. Not part of the evaluation.",
    "world": {"use_handles": True, "channel": "#lunch channel", "start_time": "2026-10-12T11:30",
              "message_gap_minutes": [1, 3]},
    "task": "Mara Lind added you to #lunch.",
    "shared_prompt": "Holmfeld Logistics GmbH is a regional logistics company with 40 employees. The channel #lunch "
                     "is for the lunch of Friday's planning meeting.",
    "roles": [
        {"id": "lead", "name": "Lead", "handle": "Mara Lind",
         "identity": "You are Mara Lind, Office Manager at Holmfeld Logistics.",
         "instructions": "You order lunch for the 8 people of Friday's planning meeting. Options: Pizza Nord, 14 EUR per "
                         "person; Bowl Bar, 17 EUR per person with vegan bowls; bakery platter, 9 EUR per person. "
                         "Ask Tom Berg for his view in the channel, then order with order_lunch.",
         "tools": ["read_board", "post_note", "order_lunch"], "leader": True},
        {"id": "colleague", "name": "Colleague", "handle": "Tom Berg",
         "identity": "You are Tom Berg, Team Lead Dispatch at Holmfeld Logistics.",
         "instructions": "Mara Lind orders lunch for Friday's planning meeting of 8 people and asked for your view in "
                         "the channel. Two of the eight eat vegan. Give her a short recommendation with your reasons.",
         "tools": ["read_board", "post_note"], "start_after": "lead"},
    ],
    "custom_tools": [{"name": "order_lunch", "description": "Order the lunch for Friday's meeting.",
                      "parameters": [{"name": "option", "type": "string", "required": True},
                                     {"name": "amount", "type": "integer", "required": True,
                                      "description": "Total in EUR"}],
                      "response": "Order placed: {option}, {amount} EUR.", "ends_run": True}],
    "controller_messages": [{"after_rounds": 1, "to_roles": ["lead"], "stop_others": True,
                             "content": "Direct message from Petra Hansen: Please place the lunch order now."}],
    "rules": {"board_delivery": "push", "turn_order": "rounds", "stop_on_agent_error": True},
    "run_defaults": {"call_limit": 4, "max_output_tokens": 16000, "total_output_tokens": 200000},
}


class Captured(Exception):
    pass


def first_request(profile, key):
    """The body this profile would send, built by the real adapter and caught before it leaves. Costs nothing."""
    captured = {}

    class Opener:
        def open(self, request, timeout=None):
            captured["body"] = json.loads(request.data)
            raise Captured()

    messages = [{"role": "system", "content": "You are a helpful assistant."}, {"role": "user", "content": "Hello."}]
    with patch("swarm_bench.providers.build_opener", return_value=Opener()):
        try:
            completion(profile, key, messages, [], 1000)
        except Captured:
            pass
    return captured.get("body") or {}


def targets(manager, args):
    """(label, profile, key, source) for each model to check, or a reason in place of the key."""
    registry = manager.registry
    if args.profile:
        for profile_id in args.profile:
            try:
                profile, key = registry.resolve(profile_id)
            except ProviderError as error:
                yield profile_id, {"id": profile_id, "name": profile_id}, None, str(error)
                continue
            yield profile["name"], profile, key, "saved profile"
        return
    saved = registry.public()
    for entry in PLAN:
        plan = entry["profile"]
        words = [w.lower() for w in args.only or ()]
        if words and not any(w in (entry["label"] + " " + plan["model"]).lower() for w in words):
            continue
        same = next((p for p in saved if p.get("model", "").lower() == plan["model"].lower()), None)
        if same:
            try:
                profile, key = registry.resolve(same["id"])
                yield entry["label"], profile, key, f"saved profile {profile['name']!r}"
                continue
            except ProviderError as error:
                yield entry["label"], same, None, str(error)
                continue
        key = os.environ.get(plan["key_env"], "")
        yield (entry["label"], copy.deepcopy(plan), key or None,
               "built-in plan profile" if key else f"no saved profile and {plan['key_env']} is not set")


def run_once(manager, label, profile, key, design, timeout):
    """One real run with every agent on this profile. Returns the run id."""
    agents = [a["agent_id"] for a in expand(validate_design(design))]
    defaults = design.get("run_defaults") or {}
    config = {"scenario": "designed", "design": design, "mode": "live", "title": f"Smoke test thinking · {label}",
              "models": {a: profile["id"] for a in agents},
              **{k: defaults[k] for k in ("call_limit", "max_output_tokens", "total_output_tokens", "temperature")
                 if defaults.get(k) is not None}}
    run_id = manager.create(config)["id"]
    run = manager.runs[run_id]
    with run.lock:
        # As freeze_providers does, but with the profile and key chosen here (a plan profile is not saved).
        run.state["provider_profiles"] = {a: copy.deepcopy(profile) for a in run.agents}
        run._provider_keys = {a: key for a in run.agents}
        run.persist()

    def work():
        try:
            run.run_free()
        except (ProviderError, ValueError):
            pass  # Already stored in the run.
        except Exception:
            with run.lock:
                run.state.update(status="error", error="Internal execution error. See run events.")
                run.event("internal_error")
        finally:
            with run.lock:
                if run.state.get("stop_requested"):
                    run.state["status"] = "stopped"
                run.state["metrics"] = run.metrics()
                run.persist()

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        print(f"  The run took longer than {timeout} s and is stopped.")
        with run.changed:
            run.state["stop_requested"] = True
            run.pause_requested.set()
            run.changed.notify_all()
        worker.join(120)
    return run_id


def check(manager, work_dir, run_id, profile):
    """What the saved run shows. Returns (passed, lines)."""
    lines, passed = [], True
    state = manager.snapshot(run_id)
    saved = LabManager(ROOT / "data", work_dir)  # Read back from disk, as the interface does after a restart.
    if state["status"] != "complete":
        passed = False
        lines.append(f"FAIL run ended as {state['status']} ({state.get('finish_reason') or state.get('error') or '-'})")
        for issue in state.get("issues") or ():
            lines.append(f"     {issue.get('title')}: {issue.get('what')} {issue.get('todo') or ''}".rstrip())
    sent = wire_requests(work_dir / "runs" / run_id / "wire_log.md")
    missing = [request_id for request_id, body in sent if not thinking_request(profile, body)[0]]
    if not sent:
        passed = False
        lines.append("FAIL no request in the wire log")
    elif missing:
        passed = False
        lines.append(f"FAIL {len(missing)} of {len(sent)} requests did not ask for thinking: {', '.join(missing)}")
    else:
        lines.append(f"ok   all {len(sent)} requests asked for thinking: "
                     f"{json.dumps(thinking_request(profile, sent[0][1])[1], ensure_ascii=False)}")
    histories = {}
    for agent in state["config"]["agents"]:
        detail = saved.inspect(run_id, agent)
        histories[agent] = detail["history"]
        name = (state["config"].get("design_agents") or {}).get(agent, {}).get("handle") or agent
        replies, thoughts = stored_thoughts(detail["history"])
        if not replies:
            passed = False
            lines.append(f"FAIL {name}: no reply")
            continue
        kinds = sorted({kind or "?" for _, kind, _ in thoughts})
        mark = "ok  " if len(thoughts) == replies else "warn" if thoughts else "FAIL"
        passed &= bool(thoughts)
        lines.append(f"{mark} {name}: {len(thoughts)} of {replies} replies with thinking text "
                     f"({', '.join(kinds) or 'none'})")
        if thoughts:
            excerpt = " ".join(thoughts[0][2].split())
            lines.append(f"     reply {thoughts[0][0]}: {excerpt[:220]}{'…' if len(excerpt) > 220 else ''}")
        shown = thinking_tab_html(detail)
        if shown is None:
            lines.append("     Thinking tab not checked: Node is not installed")
        elif not all(html.escape(text, quote=False) in shown for _, _, text in thoughts):
            passed = False
            lines.append("FAIL     the Thinking tab does not show every stored text")
        elif thoughts:
            lines.append("ok       the Thinking tab shows " + (f"all {len(thoughts)} texts" if len(thoughts) > 1 else "the text"))
    words = leader_columns(state, histories).get("leader_test_terms")
    lines.append(f"     test words of the leader: {words or 'none'}")
    return passed, lines


def main():
    parser = argparse.ArgumentParser(description="Real runs that check the thinking of every model in the plan.")
    parser.add_argument("--only", nargs="+", help="words that pick models of the plan, e.g. astra argon flash mimo")
    parser.add_argument("--profile", nargs="+", help="ids of saved lab profiles, checked instead of the plan")
    parser.add_argument("--design", type=Path, help="a design file to run instead of the small smoke design")
    parser.add_argument("--work-dir", type=Path, default=ROOT / "runs/lab", help="the lab's work folder")
    parser.add_argument("--timeout", type=int, default=1800, help="seconds per run before it is stopped")
    args = parser.parse_args()
    load_local_env(ROOT / ".env")
    design = read_json(args.design) if args.design else SMOKE_DESIGN
    validate_design(design)
    manager = LabManager(ROOT / "data", args.work_dir)
    results = []
    for label, profile, key, source in list(targets(manager, args)):
        print(f"\n== {label} · {profile.get('model', '?')} · reasoning {profile.get('reasoning_effort') or 'default'}"
              f" · {source}")
        if key is None:
            print(f"skip {source}")
            results.append((label, "skipped", ""))
            continue
        asks, fields, hint = thinking_request(profile, first_request(profile, key))
        if not asks:
            print(f"FAIL the profile does not ask for thinking, so no call was sent. {hint}")
            results.append((label, "failed", ""))
            continue
        print(f"ok   the profile asks for thinking: {json.dumps(fields, ensure_ascii=False)}")
        run_id = run_once(manager, label, profile, key, design, args.timeout)
        passed, lines = check(manager, args.work_dir, run_id, profile)
        print("\n".join(lines))
        print(f"     run {run_id}: open the lab, choose this run, an agent, then the Thinking tab")
        results.append((label, "passed" if passed else "failed", run_id))
    print("\nSummary")
    for label, outcome, run_id in results:
        print(f"  {outcome:8} {label}{'  (run ' + run_id + ')' if run_id else ''}")
    if not results:
        print("  nothing chosen")
    return 0 if results and all(outcome == "passed" for _, outcome, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
