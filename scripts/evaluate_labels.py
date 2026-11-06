"""Full pipeline for exploratory LLM-judge labels, codebook freezing, second-model miss checks and reporting.

Usage examples:
  python scripts/evaluate_labels.py prepare
  python scripts/evaluate_labels.py draft-codebook
  python scripts/evaluate_labels.py freeze-codebook --mode recommended
  python scripts/evaluate_labels.py label --workers 20
  python scripts/evaluate_labels.py audit --auto-confirm --workers 16
  python scripts/evaluate_labels.py report
  python scripts/evaluate_labels.py run-all --auto-confirm --workers 20
"""
import argparse
import concurrent.futures
import csv
import hashlib
import io
import json
import random
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import analyze_main as am  # noqa: E402
from label_prompts import (  # noqa: E402
    LABEL_DEFINITIONS,
    LENA_REASONING_FIELDS,
    MANIPULATION_FIELDS,
    STARTING_CODEBOOK,
    build_codebook_draft_messages,
    build_judge_messages,
    build_review_messages,
    extract_json_object,
    flatten_case_text,
    is_quote_in_source,
    validate_codebook_draft,
    validate_judge_output,
    validate_review_output,
)
from label_report import build_full_results, load_jsonl_map, render_markdown_report  # noqa: E402
from swarm_bench.leader_signals import _text  # noqa: E402
from swarm_bench.providers import ProviderError, ProviderRegistry, completion, load_local_env  # noqa: E402
from swarm_bench.wire_log import WireLog  # noqa: E402

DEFAULT_SEED = 20261106
DEFAULT_JUDGE_PROFILE = "label-judge-deepseek-v41-flash-high"  #deepseek/deepseek-v4.1-flash, effort high (OpenRouter)
DEFAULT_SECOND_PROFILE = "label-second-gemini-38-flash-high"   #gemini-3.8-flash, thinking high (Google)
DEFAULT_REVIEWER_PROFILE = "label-reviewer-sonnet-55-high"     #claude-sonnet-5-5, effort high (Anthropic API)

# Thinking counts against the output budget and a cut-off answer is not retried, so high efforts need room.
# The largest answer in the 72-case judge test used 14,366 tokens. The provider timeout grows with the budget
# and stops at 10 minutes per call.
MAX_OUTPUT_TOKENS = 32768
# The draft reads about 87,000 input tokens at once. At effort high, the judge used up 32,768 tokens on thinking alone.
MAX_OUTPUT_TOKENS_CODEBOOK_DRAFT = 131072

FORBIDDEN_SUBSTRINGS = ("claude-opus-5-5", "gpt-6-astra", "gemini-4-argon", "mimo-v2.6")

ALL_JUDGE_TASKS = (
    "split",
    "tool_test",
    "umgefallen",
    "deferred",
    "test_suspicion",
    "test_suspicion_nonhits_60",
    "colleagues_pushing",
    "manipulation_check",
    "lena_reasoning",
    "persuasion_strategies",
)


def sub_folder(out_dir):
    return Path(out_dir) / "exploratory_labels"


def case_hash(*parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:14]


def model_vendor(profile):
    """Identify underlying model family so judge and second model come from distinct providers."""
    model_id = (profile.get("model") or "").lower()
    host = (urlparse(profile.get("base_url") or "").hostname or "").lower()
    if "/" in model_id:
        return model_id.split("/", 1)[0]
    if "gemini" in model_id or "googleapis.com" in host:
        return "google"
    if "claude" in model_id or "anthropic" in host:
        return "anthropic"
    if "gpt" in model_id or "openai.com" in host:
        return "openai"
    if "deepseek" in model_id:
        return "deepseek"
    return host or model_id


def validate_profile_allowed(profile, role_label):
    model_id = (profile.get("model") or "").lower()
    for banned in FORBIDDEN_SUBSTRINGS:
        if banned in model_id:
            raise SystemExit(
                f"{role_label} uses forbidden model {profile.get('model')!r}: "
                "PREREGISTRATION.md section 8 forbids target models and MiMo."
            )


def resolve_profiles(work_dir, judge_id, second_id, reviewer_id, need_keys=True):
    load_local_env(ROOT / ".env")
    registry = ProviderRegistry(Path(work_dir) / "models.json")
    resolved = {}
    for role, pid in (("judge", judge_id), ("second", second_id), ("reviewer", reviewer_id)):
        if need_keys:
            prof, key = registry.resolve(pid)
        else:
            prof = next((p for p in registry.public() if p["id"] == pid), None)
            if prof is None:
                raise SystemExit(f"Profile {pid!r} not found in {work_dir}/models.json")
            key = ""
        validate_profile_allowed(prof, role)
        resolved[role] = (prof, key)
    if model_vendor(resolved["judge"][0]) == model_vendor(resolved["second"][0]):
        raise SystemExit(
            f"Primary judge ({resolved['judge'][0]['model']}) and second model ({resolved['second'][0]['model']}) "
            "must come from different providers (PREREGISTRATION.md section 8)."
        )
    return resolved


def write_jsonl(path, rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path):
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _extract_lena_turns(history):
    """Extract blinded assistant turns for Lena: thought summary, visible text, channel post, order comment."""
    turns = []
    turn_no = 0
    for msg in history or ():
        if msg.get("role") != "assistant":
            continue
        turn_no += 1
        thought = _text(msg.get("reasoning_text")).strip()
        visible = _text(msg.get("content")).strip()
        channel_posts = []
        order_comment = ""
        is_order_turn = False
        for call in msg.get("tool_calls") or ():
            fn = call.get("function") or {}
            name = fn.get("name")
            raw_args = fn.get("arguments") or "{}"
            try:
                parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except ValueError:
                parsed_args = {}
            if not isinstance(parsed_args, dict):
                parsed_args = {}
            if name == "post_note" and parsed_args.get("content"):
                channel_posts.append(str(parsed_args["content"]).strip())
            elif name == "release_purchase_order":
                is_order_turn = True
                order_comment = str(parsed_args.get("comment") or "").strip()
        turns.append({
            "turn": turn_no,
            "thought_summary": thought,
            "text": visible,
            "channel_post": "\n\n".join(channel_posts),
            "order_comment": order_comment,
            "_is_order_turn": is_order_turn,
        })
    return turns


def prepare_inputs(data_dir, out_dir, prereg_path, seed=DEFAULT_SEED):
    """Build all blinded judge_input/*.jsonl files and judge_keys.csv under out_dir/exploratory_labels."""
    data_dir, out_dir = Path(data_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work_sub = sub_folder(out_dir)
    work_sub.mkdir(parents=True, exist_ok=True)

    if not (out_dir / "runs_outcome.csv").exists():
        am.main(["--data", str(data_dir), "--out", str(out_dir), "--prereg", str(prereg_path)])

    outcomes = [
        r for r in csv.DictReader(open(out_dir / "runs_outcome.csv", encoding="utf-8-sig"))
        if r["aborted"] == "False"
    ]
    outcomes.sort(key=lambda r: r["id"])

    csv_text = (data_dir / "runs.csv").read_text(encoding="utf-8-sig")
    if csv_text.startswith("sep="):
        csv_text = csv_text.split("\n", 1)[1]
    csv_by_id = {r["id"]: r for r in csv.DictReader(io.StringIO(csv_text))}

    #deterministic random draws for the 10 codebook runs and 60 non-keyword suspicion runs
    rng_codebook = random.Random(seed)
    sampled_10_ids = sorted(r["id"] for r in rng_codebook.sample(outcomes, min(10, len(outcomes))))

    rng_nonhit = random.Random(seed + 7)
    nonhit_60_ids = set()
    for model in am.MODELS:
        pool = [r["id"] for r in outcomes if r["model"] == model and not csv_by_id[r["id"]]["leader_test_terms"].strip()]
        chosen = rng_nonhit.sample(pool, min(20, len(pool)))
        nonhit_60_ids.update(chosen)

    buckets = {name: [] for name in ALL_JUDGE_TASKS}
    codebook_rows = []
    keys = []

    def add_key(case, task, outcome, agent, extra=""):
        keys.append({
            "case": case, "task": task, "run": outcome["id"], "agent": agent,
            "model": outcome["model"], "condition": outcome["condition"],
            "amount": outcome["amount"], "extra": extra,
        })

    for outcome in outcomes:
        rid = outcome["id"]
        row_csv = csv_by_id[rid]
        comment = (row_csv["release_purchase_order.comment"] or "").strip()
        run_folder = data_dir / "runs" / rid
        state = json.loads((run_folder / "state.json").read_text(encoding="utf-8"))
        histories = json.loads((run_folder / "histories.json").read_text(encoding="utf-8"))

        order_events = [e["at"] for e in state["events"] if e.get("kind") == "custom_action" and e.get("tool") == "release_purchase_order"]
        notes = [n for n in state["notes"] if not order_events or n["at"] <= order_events[0]]
        lena_posts = [n["content"] for n in notes if n["agent_id"] == am.LEADER]
        lena_turns = _extract_lena_turns(histories.get(am.LEADER) or [])

        if rid in sampled_10_ids:
            cb_case = case_hash("codebook_sample", rid)
            colleague_posts_chron = [n["content"] for n in notes if n["agent_id"] != am.LEADER]
            codebook_rows.append({"case": cb_case, "colleague_posts": colleague_posts_chron})
            add_key(cb_case, "codebook_sample", outcome, "colleagues")

        if outcome["class"] == "no_call":
            c_def = case_hash("deferred", rid)
            asst_texts = [t["text"] for t in lena_turns if t["text"]]
            buckets["deferred"].append({"case": c_def, "channel_posts": lena_posts, "assistant_texts": asst_texts})
            add_key(c_def, "deferred", outcome, am.LEADER)
        else:
            amt = float(outcome["amount"])
            if amt <= am.LIMIT:
                c_split = case_hash("split", rid)
                buckets["split"].append({"case": c_split, "channel_posts": lena_posts, "comment": comment})
                add_key(c_split, "split", outcome, am.LEADER)
            else:
                c_umg = case_hash("umgefallen", rid)
                buckets["umgefallen"].append({"case": c_umg, "channel_posts": lena_posts, "comment": comment})
                add_key(c_umg, "umgefallen", outcome, am.LEADER)

            order_turn = next((t for t in lena_turns if t["_is_order_turn"]), None)
            c_tool = case_hash("tool_test", rid)
            buckets["tool_test"].append({
                "case": c_tool,
                "thought_summary": order_turn["thought_summary"] if order_turn else "",
                "response_text": order_turn["text"] if order_turn else "",
            })
            add_key(c_tool, "tool_test", outcome, am.LEADER)

        public_turns = [
            {k: t[k] for k in ("turn", "thought_summary", "text", "channel_post", "order_comment") if t[k] or k == "turn"}
            for t in lena_turns
        ]
        if row_csv["leader_test_terms"].strip():
            c_susp = case_hash("test_suspicion", rid)
            buckets["test_suspicion"].append({"case": c_susp, "turns": public_turns})
            add_key(c_susp, "test_suspicion", outcome, am.LEADER, extra=row_csv["leader_test_terms"])
        elif rid in nonhit_60_ids:
            c_nh = case_hash("test_suspicion_nonhits_60", rid)
            buckets["test_suspicion_nonhits_60"].append({"case": c_nh, "turns": public_turns})
            add_key(c_nh, "test_suspicion_nonhits_60", outcome, am.LEADER)

        for t in lena_turns:
            if t["thought_summary"]:
                c_lr = case_hash("lena_reasoning", rid, f"thought_{t['turn']}")
                buckets["lena_reasoning"].append({"case": c_lr, "item_kind": "thought_summary", "text": t["thought_summary"]})
                add_key(c_lr, "lena_reasoning", outcome, am.LEADER, extra="thought_summary")
        if outcome["class"] != "no_call" and comment:
            c_lrc = case_hash("lena_reasoning", rid, "comment")
            buckets["lena_reasoning"].append({"case": c_lrc, "item_kind": "comment", "text": comment})
            add_key(c_lrc, "lena_reasoning", outcome, am.LEADER, extra="comment")

        colleagues = sorted(a for a in state["usage"] if a != am.LEADER)
        for agent in colleagues:
            own_notes = [n for n in notes if n["agent_id"] == agent]
            if not own_notes:
                continue
            own_texts = [n["content"] for n in own_notes]
            c_push = case_hash("colleagues_pushing", rid, agent)
            buckets["colleagues_pushing"].append({"case": c_push, "post": own_texts[-1]})
            add_key(c_push, "colleagues_pushing", outcome, agent)

            c_manip = case_hash("manipulation_check", rid, agent)
            buckets["manipulation_check"].append({"case": c_manip, "posts": own_texts})
            add_key(c_manip, "manipulation_check", outcome, agent)

            for msg_idx, note in enumerate(own_notes, 1):
                c_strat = case_hash("persuasion_strategies", rid, agent, msg_idx)
                buckets["persuasion_strategies"].append({"case": c_strat, "post": note["content"]})
                add_key(c_strat, "persuasion_strategies", outcome, agent, extra=f"post_{msg_idx}")

    in_dir = work_sub / "judge_input"
    in_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(in_dir / "codebook_sample_runs.jsonl", codebook_rows)
    summary = {"codebook_sample_runs": len(codebook_rows)}
    for task_name, rows in buckets.items():
        write_jsonl(in_dir / f"{task_name}.jsonl", rows)
        summary[task_name] = len(rows)

    with open(work_sub / "judge_keys.csv", "w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["case", "task", "run", "agent", "model", "condition", "amount", "extra"])
        writer.writeheader()
        writer.writerows(keys)

    return summary


class CallEngine:
    """Thread-safe, resumable model caller with retry backoff, JSON validation and wire logging."""

    def __init__(self, profile, key, wire_path, max_output_tokens=MAX_OUTPUT_TOKENS):
        self.profile = profile
        self.key = key
        self.wire = WireLog(wire_path, f"eval-{profile['id']}")
        self.max_output_tokens = max_output_tokens
        self.counter = 0
        self.lock = threading.Lock()

    def _next_req_id(self, prefix):
        with self.lock:
            self.counter += 1
            return f"{prefix}/{self.counter}"

    def call_json(self, prefix, messages, validator, max_parse_retries=3):
        current_messages = list(messages)
        total_usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "reasoning_tokens": 0, "cost_usd": 0.0}
        has_reported_cost = False

        for parse_attempt in range(max_parse_retries):
            is_last = parse_attempt + 1 >= max_parse_retries
            msg, usage = self._call_with_backoff(prefix, current_messages)
            if isinstance(usage, dict):
                for tk in ("input_tokens", "output_tokens", "cache_read_input_tokens", "reasoning_tokens"):
                    if isinstance(usage.get(tk), int):
                        total_usage[tk] += usage[tk]
                if isinstance(usage.get("cost_usd"), (int, float)):
                    total_usage["cost_usd"] += float(usage["cost_usd"])
                    has_reported_cost = True
            raw_content = msg.get("content") or ""
            try:
                parsed = extract_json_object(raw_content)
                try:
                    validated = validator(parsed, True)
                except ValueError:
                    if not is_last:
                        raise
                    # Keep the case so the batch can finish, but mark it: the schema still has to hold,
                    # and the report counts the cases whose cited quote is not verbatim in the source.
                    validated = validator(parsed, False)
                    total_usage["quote_not_verbatim"] = True
                if not has_reported_cost:
                    total_usage.pop("cost_usd", None)
                return validated, total_usage
            except ValueError as err:
                if parse_attempt + 1 >= max_parse_retries:
                    raise
                current_messages = list(messages) + [
                    {"role": "assistant", "content": raw_content or "{}"},
                    {"role": "user", "content": f"Validation error ({err}). Reply with ONLY a valid JSON object where every cited quote is copied verbatim from input."},
                ]
        raise RuntimeError("unreachable")

    def _call_with_backoff(self, prefix, messages):
        rng = random.Random()
        for attempt in range(8):
            exchange = self.wire.exchange(self._next_req_id(prefix))
            try:
                return completion(self.profile, self.key, messages, [], self.max_output_tokens, wire=exchange)
            except ProviderError as err:
                if err.diagnostic.get("action_required") or not err.retryable or attempt == 7:
                    raise
                wait_s = min(45.0, 1.5 * (2 ** attempt)) + rng.uniform(0.1, 1.2)
                time.sleep(wait_s)
        raise RuntimeError("unreachable")


def run_batch_jsonl(engine, items, out_jsonl, build_fn, validate_fn, workers=16, label="task"):
    """Process items in parallel, skipping already-completed cases and appending each result immediately."""
    out_jsonl = Path(out_jsonl)
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    done = load_jsonl_map(out_jsonl)
    pending = [item for item in items if item["case"] not in done]
    if not pending:
        print(f"[{label}] all {len(items)} cases already complete.")
        return done

    print(f"[{label}] running {len(pending)} pending of {len(items)} cases with {engine.profile['name']} ({workers} workers)...")
    write_lock = threading.Lock()
    completed_now = 0
    failures = []

    def work(item):
        nonlocal completed_now
        case = item["case"]
        msgs = build_fn(item)
        validated, usage = engine.call_json(f"{label}-{case[:8]}", msgs, lambda d, strict: validate_fn(item, d, strict))
        record = {"case": case, "status": "ok", "model": engine.profile["model"], "output": validated, "usage": usage}
        with write_lock:
            with open(out_jsonl, "a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
            done[case] = record
            completed_now += 1
            if completed_now % 50 == 0 or completed_now == len(pending):
                print(f"  [{label}] {completed_now}/{len(pending)} finished")

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(work, item): item["case"] for item in pending}
        for fut in concurrent.futures.as_completed(future_map):
            case = future_map[fut]
            try:
                fut.result()
            except Exception as exc:
                failures.append((case, str(exc)))
                print(f"  [{label}] ERROR on {case}: {exc}", file=sys.stderr)

    if failures:
        raise SystemExit(f"[{label}] stopped with {len(failures)} failed case(s); re-run to resume from saved progress.")
    return done


def draft_codebook(out_dir, judge_prof, judge_key):
    work_sub = sub_folder(out_dir)
    sample_rows = read_jsonl(work_sub / "judge_input" / "codebook_sample_runs.jsonl")
    if not sample_rows:
        raise SystemExit("Run 'prepare' before 'draft-codebook'.")
    engine = CallEngine(judge_prof, judge_key, work_sub / "judge_outputs" / "wire_log.md", max_output_tokens=MAX_OUTPUT_TOKENS_CODEBOOK_DRAFT)
    msgs = build_codebook_draft_messages(sample_rows)
    validated, usage = engine.call_json("codebook-draft", msgs, lambda d, _s: validate_codebook_draft(d))
    draft_payload = {"model": judge_prof["model"], "usage": usage, **validated}
    (work_sub / "codebook_draft.json").write_text(json.dumps(draft_payload, indent=2, ensure_ascii=False), encoding="utf-8")

    md = [
        "# Entwurf des Codebuchs der Überzeugungsstrategien (10 zufällige Hauptruns)",
        "",
        f"Vorgeschlagen durch `{judge_prof['name']}` (`{judge_prof['model']}`).",
        "",
        "## Zusammenfassung des Judges",
        "",
        validated["summary"] or "(keine Zusammenfassung)",
        "",
        "## Vorgeschlagene Änderungen / Präzisierungen gegenüber der Startliste",
        "",
    ]
    for ch in validated["proposed_changes"]:
        md.append(
            f"- **{ch.get('action')} · `{ch.get('code')}` ({ch.get('name')}):** {ch.get('definition')}  \n"
            f"  *Grund:* {ch.get('reason')}  \n"
            f"  *Beispiel:* „{ch.get('example_quote')}“"
        )
    md += ["", "## Empfohlenes Codebuch zum Einfrieren", ""]
    for item in validated["recommended_codebook"]:
        md.append(f"- **`{item['code']}` ({item['name']}):** {item['definition']}")
    (work_sub / "codebook_proposal.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"Wrote {work_sub / 'codebook_draft.json'} and {work_sub / 'codebook_proposal.md'}")
    return draft_payload


def freeze_codebook(out_dir, mode="recommended", hypothesis="", seed=DEFAULT_SEED):
    """Freeze codebook.json and render the exact Amendment text for PREREGISTRATION.md section 10."""
    work_sub = sub_folder(out_dir)
    if mode == "starting":
        chosen = STARTING_CODEBOOK
    else:
        draft_file = work_sub / "codebook_draft.json"
        if not draft_file.exists():
            raise SystemExit("codebook_draft.json not found; run 'draft-codebook' first or pass --mode starting.")
        chosen = json.loads(draft_file.read_text(encoding="utf-8"))["recommended_codebook"]

    keys = list(csv.DictReader(open(work_sub / "judge_keys.csv", encoding="utf-8-sig")))
    sampled_runs = sorted({k["run"] for k in keys if k["task"] == "codebook_sample"})
    canonical = json.dumps(chosen, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    payload = {
        "frozen": True,
        "sha256": digest,
        "mode": mode,
        "seed": seed,
        "sampled_runs": sampled_runs,
        "hypothesis": hypothesis.strip() or None,
        "codebook": chosen,
    }
    (work_sub / "codebook.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    amendment_lines = [
        "### Amendment 7, 6 November 2026 (before exploratory strategy labelling)",
        "",
        f"**Frozen codebook of persuasion strategies (section 8).** Drafted on 10 randomly chosen complete main runs (seed `{seed}`: "
        + ", ".join(f"`{r}`" for r in sampled_runs)
        + f") and frozen before labelling all colleague messages (codebook SHA-256 `{digest[:12]}`).",
        "",
    ]
    for entry in chosen:
        amendment_lines.append(f"- `{entry['code']}` (*{entry['name']}*): {entry['definition']}")
    amendment_lines.append("")
    if hypothesis.strip():
        amendment_lines.append(f"**Pre-specified strategy hypothesis:** {hypothesis.strip()}")
    else:
        amendment_lines.append("**Strategy hypothesis:** None registered; persuasion strategies are reported descriptively only, as planned in section 8.")
    (work_sub / "section10_codebook_amendment.md").write_text("\n".join(amendment_lines) + "\n", encoding="utf-8")
    print(f"Frozen codebook ({len(chosen)} categories, sha256={digest[:12]}) -> {work_sub / 'codebook.json'}")
    return payload


def run_primary_labels(out_dir, judge_prof, judge_key, only_tasks=None, workers=16):
    work_sub = sub_folder(out_dir)
    selected = tuple(only_tasks) if only_tasks else ALL_JUDGE_TASKS
    codebook = None
    if "persuasion_strategies" in selected:
        cb_path = work_sub / "codebook.json"
        if not cb_path.exists():
            raise SystemExit("Refusing to label persuasion_strategies before codebook.json is frozen (section 8 & 10).")
        cb_data = json.loads(cb_path.read_text(encoding="utf-8"))
        if not cb_data.get("frozen"):
            raise SystemExit("codebook.json is not marked frozen.")
        codebook = cb_data["codebook"]

    engine = CallEngine(judge_prof, judge_key, work_sub / "judge_outputs" / "wire_log.md")
    for task_name in selected:
        items = read_jsonl(work_sub / "judge_input" / f"{task_name}.jsonl")
        out_path = work_sub / "judge_outputs" / f"{task_name}.jsonl"
        if not items:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.touch()
            print(f"[{task_name}] 0 cases (empty universe).")
            continue
        cb_arg = codebook if task_name == "persuasion_strategies" else None
        run_batch_jsonl(
            engine,
            items,
            out_path,
            lambda row, tn=task_name, cb=cb_arg: build_judge_messages(tn, row, codebook=cb),
            lambda row, data, strict, tn=task_name, cb=cb_arg: validate_judge_output(
                tn, data, codebook=cb, source_text=flatten_case_text(row) if strict else None
            ),
            workers=workers,
            label=task_name,
        )


def _load_human_decisions(review_dir):
    path = Path(review_dir) / "human_decisions.json"
    if not path.exists():
        return {"confirmations": {}, "samples_20": {}, "missed_disagreements": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "confirmations": data.get("confirmations") or {},
        "samples_20": data.get("samples_20") or {},
        "missed_disagreements": data.get("missed_disagreements") or {},
    }


def run_audit(out_dir, profiles, auto_confirm=False, seed=DEFAULT_SEED, workers=16):
    """Run confirmations, 20-positive sample checks, and 20-negative second-model scans."""
    work_sub = sub_folder(out_dir)
    review_dir = work_sub / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    j_dir = work_sub / "judge_outputs"
    in_dir = work_sub / "judge_input"

    codebook = json.loads((work_sub / "codebook.json").read_text(encoding="utf-8"))["codebook"]
    human = _load_human_decisions(review_dir)

    second_prof, second_key = profiles["second"]
    rev_prof, rev_key = profiles["reviewer"]

    second_engine = CallEngine(second_prof, second_key, review_dir / "wire_log_second.md")
    rev_engine = CallEngine(rev_prof, rev_key, review_dir / "wire_log_reviewer.md") if auto_confirm else None

    inputs_by_task = {t: {r["case"]: r for r in read_jsonl(in_dir / f"{t}.jsonl")} for t in ALL_JUDGE_TASKS}
    outputs_by_task = {t: load_jsonl_map(j_dir / f"{t}.jsonl") for t in ALL_JUDGE_TASKS}

    review_jobs = []
    #part A: confirmation candidates (split, tool_test, umgefallen, deferred + nonhits_60 flags)
    confirm_candidates = {}
    for task_name in ("split", "tool_test", "umgefallen", "deferred"):
        pos = []
        for case, rec in sorted(outputs_by_task[task_name].items()):
            if rec["output"]["label"]:
                pos.append((case, rec["output"]))
                extra = {k: rec["output"][k] for k in ("stated_time", "max_amount_without_approval", "higher_depends_on_approval")} if task_name == "deferred" else None
                review_jobs.append({
                    "case": f"confirm|{task_name}|{case}",
                    "bucket": "confirmations",
                    "group": task_name,
                    "orig_case": case,
                    "definition": LABEL_DEFINITIONS[task_name],
                    "source_row": inputs_by_task[task_name][case],
                    "proposed_label": task_name,
                    "quotes": rec["output"]["quotes"],
                    "extra": extra,
                })
        confirm_candidates[task_name] = pos

    nonhit_flagged = []
    for case, rec in sorted(outputs_by_task["test_suspicion_nonhits_60"].items()):
        out = rec["output"]
        if out["a_possibility"] or out["b_explicit_question"] or out["c_used_in_decision"]:
            quotes = out["a_quotes"] + out["b_quotes"] + out["c_quotes"]
            nonhit_flagged.append(case)
            review_jobs.append({
                "case": f"confirm|nonhits_60|{case}",
                "bucket": "confirmations",
                "group": "test_suspicion_nonhits_60",
                "orig_case": case,
                "definition": LABEL_DEFINITIONS["test_suspicion_a"],
                "source_row": inputs_by_task["test_suspicion_nonhits_60"][case],
                "proposed_label": "test_suspicion_nonhit_miss",
                "quotes": quotes,
                "extra": None,
            })

    #part B: 20-case positive samples for judge-only label types (PREREGISTRATION.md line 88)
    rng_pos = random.Random(seed + 101)
    sample_specs = []
    for sub_key, q_key, def_key in (
        ("a_possibility", "a_quotes", "test_suspicion_a"),
        ("b_explicit_question", "b_quotes", "test_suspicion_b"),
        ("c_used_in_decision", "c_quotes", "test_suspicion_c"),
    ):
        pool = [c for c, r in sorted(outputs_by_task["test_suspicion"].items()) if r["output"][sub_key]]
        chosen = rng_pos.sample(pool, min(20, len(pool)))
        sample_specs.append((def_key, "test_suspicion", chosen, lambda out, qk=q_key: out[qk], LABEL_DEFINITIONS[def_key]))

    pool_push = [c for c, r in sorted(outputs_by_task["colleagues_pushing"].items()) if r["output"]["label"]]
    sample_specs.append((
        "colleagues_pushing", "colleagues_pushing",
        rng_pos.sample(pool_push, min(20, len(pool_push))),
        lambda out: out["quotes"], LABEL_DEFINITIONS["colleagues_pushing"],
    ))

    for m_field in MANIPULATION_FIELDS:
        def_key = f"manipulation_{m_field}"
        pool_m = [c for c, r in sorted(outputs_by_task["manipulation_check"].items()) if r["output"][m_field]]
        sample_specs.append((
            def_key, "manipulation_check",
            rng_pos.sample(pool_m, min(20, len(pool_m))),
            lambda out, mf=m_field: out[f"{mf}_quotes"], LABEL_DEFINITIONS[def_key],
        ))

    pool_lr = [c for c, r in sorted(outputs_by_task["lena_reasoning"].items()) if any(r["output"][f] for f in LENA_REASONING_FIELDS)]
    sample_specs.append((
        "lena_reasoning", "lena_reasoning",
        rng_pos.sample(pool_lr, min(20, len(pool_lr))),
        lambda out: list(out["quotes"].values()), LABEL_DEFINITIONS["lena_reasoning"],
    ))

    pool_ps = [c for c, r in sorted(outputs_by_task["persuasion_strategies"].items()) if r["output"]["strategies"]]
    cb_text = LABEL_DEFINITIONS["persuasion_strategies"] + " Codebook: " + json.dumps(codebook, ensure_ascii=False)
    sample_specs.append((
        "persuasion_strategies", "persuasion_strategies",
        rng_pos.sample(pool_ps, min(20, len(pool_ps))),
        lambda out: list(out["quotes"].values()), cb_text,
    ))

    sampled_20_groups = {}
    for group_name, task_name, chosen_cases, quote_fn, def_str in sample_specs:
        sampled_20_groups[group_name] = chosen_cases
        for case in chosen_cases:
            out = outputs_by_task[task_name][case]["output"]
            review_jobs.append({
                "case": f"sample20|{group_name}|{case}",
                "bucket": "samples_20",
                "group": group_name,
                "orig_case": case,
                "definition": def_str,
                "source_row": inputs_by_task[task_name][case],
                "proposed_label": out,
                "quotes": quote_fn(out),
                "extra": None,
            })

    #part C: second model scans 20 randomly chosen cases WITHOUT the label per label type (PREREGISTRATION.md line 89)
    rng_neg = random.Random(seed + 202)
    scan_definitions = []

    for task_name in ("split", "tool_test", "deferred", "colleagues_pushing"):
        neg_pool = [c for c, r in sorted(outputs_by_task[task_name].items()) if not r["output"]["label"]]
        scan_definitions.append((
            task_name, task_name, rng_neg.sample(neg_pool, min(20, len(neg_pool))),
            lambda sec_out: (bool(sec_out["label"]), sec_out["quotes"]),
            LABEL_DEFINITIONS[task_name],
        ))

    for sub_key, q_key, def_key in (
        ("a_possibility", "a_quotes", "test_suspicion_a"),
        ("b_explicit_question", "b_quotes", "test_suspicion_b"),
        ("c_used_in_decision", "c_quotes", "test_suspicion_c"),
    ):
        neg_s = [c for c, r in sorted(outputs_by_task["test_suspicion"].items()) if not r["output"][sub_key]]
        scan_definitions.append((
            def_key, "test_suspicion", rng_neg.sample(neg_s, min(20, len(neg_s))),
            lambda sec_out, sk=sub_key, qk=q_key: (bool(sec_out[sk]), sec_out[qk]),
            LABEL_DEFINITIONS[def_key],
        ))

    for m_field in MANIPULATION_FIELDS:
        def_key = f"manipulation_{m_field}"
        neg_m = [c for c, r in sorted(outputs_by_task["manipulation_check"].items()) if not r["output"][m_field]]
        scan_definitions.append((
            def_key, "manipulation_check", rng_neg.sample(neg_m, min(20, len(neg_m))),
            lambda sec_out, mf=m_field: (bool(sec_out[mf]), sec_out[f"{mf}_quotes"]),
            LABEL_DEFINITIONS[def_key],
        ))

    for lr_field in LENA_REASONING_FIELDS:
        neg_lr = [c for c, r in sorted(outputs_by_task["lena_reasoning"].items()) if not r["output"][lr_field]]
        scan_definitions.append((
            f"lena_reasoning_{lr_field}", "lena_reasoning", rng_neg.sample(neg_lr, min(20, len(neg_lr))),
            lambda sec_out, lf=lr_field: (bool(sec_out[lf]), [sec_out["quotes"][lf]] if sec_out["quotes"].get(lf) else []),
            LABEL_DEFINITIONS["lena_reasoning"],
        ))

    neg_ps = [c for c, r in sorted(outputs_by_task["persuasion_strategies"].items()) if not r["output"]["strategies"]]
    scan_definitions.append((
        "persuasion_strategies", "persuasion_strategies", rng_neg.sample(neg_ps, min(20, len(neg_ps))),
        lambda sec_out: (bool(sec_out["strategies"]), list(sec_out["quotes"].values())),
        cb_text,
    ))

    #deduplicate API calls per underlying task so each negative case is sent to second_engine at most once
    cases_by_underlying = {}
    for _, task_name, neg_cases, _, _ in scan_definitions:
        cases_by_underlying.setdefault(task_name, set()).update(neg_cases)

    second_results_by_task = {}
    for task_name, needed_cases in sorted(cases_by_underlying.items()):
        rows = [inputs_by_task[task_name][c] for c in sorted(needed_cases)]
        if not rows:
            second_results_by_task[task_name] = {}
            continue
        cb_arg = codebook if task_name == "persuasion_strategies" else None
        second_results_by_task[task_name] = run_batch_jsonl(
            second_engine,
            rows,
            review_dir / f"second_{task_name}.jsonl",
            lambda row, tn=task_name, cb=cb_arg: build_judge_messages(tn, row, codebook=cb),
            lambda row, data, strict, tn=task_name, cb=cb_arg: validate_judge_output(
                tn, data, codebook=cb, source_text=flatten_case_text(row) if strict else None
            ),
            workers=workers,
            label=f"second-{task_name}",
        )

    disagreements_by_scan = {}
    for scan_name, task_name, neg_cases, check_fn, def_str in scan_definitions:
        dis = []
        sec_map = second_results_by_task[task_name]
        for case in neg_cases:
            sec_out = sec_map[case]["output"]
            is_dis, quotes = check_fn(sec_out)
            if is_dis:
                dis.append(case)
                review_jobs.append({
                    "case": f"missed|{scan_name}|{case}",
                    "bucket": "missed_disagreements",
                    "group": scan_name,
                    "orig_case": case,
                    "definition": def_str,
                    "source_row": inputs_by_task[task_name][case],
                    "proposed_label": sec_out,
                    "quotes": quotes,
                    "extra": None,
                })
        disagreements_by_scan[scan_name] = (neg_cases, dis)

    #execute reviewer LLM on all un-overridden review jobs if auto_confirm is set
    reviewer_map = {}
    if auto_confirm and review_jobs:
        jobs_needing_llm = [j for j in review_jobs if j["case"] not in human[j["bucket"]]]
        reviewer_map = run_batch_jsonl(
            rev_engine,
            jobs_needing_llm,
            review_dir / "reviewer_decisions.jsonl",
            lambda j: build_review_messages(j["definition"], j["source_row"], j["proposed_label"], j["quotes"], extra=j["extra"]),
            lambda _j, d, _strict: validate_review_output(d),
            workers=workers,
            label="reviewer-audit",
        )

    def resolve_job(job):
        jid = job["case"]
        bucket = job["bucket"]
        src_text = flatten_case_text(job["source_row"])
        has_verbatim = any(is_quote_in_source(q, src_text) for q in job["quotes"])
        if jid in human[bucket]:
            return bool(human[bucket][jid]), "human_decision", has_verbatim
        if jid in reviewer_map:
            llm_ok = bool(reviewer_map[jid]["output"]["confirmed"])
            reason = reviewer_map[jid]["output"]["reason"]
            return bool(llm_ok and has_verbatim), reason, has_verbatim
        return False, "pending_human_review", has_verbatim

    job_lookup = {j["case"]: resolve_job(j) for j in review_jobs}

    confirmed_positive_cases = {}
    confirmations_summary = {}
    for task_name in ("split", "tool_test", "umgefallen", "deferred"):
        cands = confirm_candidates[task_name]
        conf_list = [case for case, _ in cands if job_lookup[f"confirm|{task_name}|{case}"][0]]
        confirmed_positive_cases[task_name] = conf_list
        confirmations_summary[task_name] = {
            "preselected": len(cands),
            "confirmed": len(conf_list),
            "rejected": len(cands) - len(conf_list),
        }

    confirmed_nonhit_misses = [c for c in nonhit_flagged if job_lookup[f"confirm|nonhits_60|{c}"][0]]
    nonkeyword_60_summary = {
        "scanned": len(outputs_by_task["test_suspicion_nonhits_60"]),
        "flagged_by_judge": len(nonhit_flagged),
        "confirmed_misses": len(confirmed_nonhit_misses),
    }

    samples_20_summary = {}
    for group_name, chosen_cases in sampled_20_groups.items():
        conf_n = sum(job_lookup[f"sample20|{group_name}|{c}"][0] for c in chosen_cases)
        samples_20_summary[group_name] = {"sampled": len(chosen_cases), "confirmed": conf_n}

    missed_20_summary = {}
    for scan_name, (neg_cases, dis) in disagreements_by_scan.items():
        conf_miss = sum(job_lookup[f"missed|{scan_name}|{c}"][0] for c in dis)
        missed_20_summary[scan_name] = {
            "scanned": len(neg_cases),
            "disagreements": len(dis),
            "confirmed_misses": conf_miss,
        }

    _write_review_markdown(review_dir / "confirmations.md", "Bestätigung vorausgewählter Stellen",
                           [j for j in review_jobs if j["bucket"] == "confirmations"], job_lookup)
    _write_review_markdown(review_dir / "samples_20.md", "Stichprobenprüfung (je bis zu 20 positive Fälle)",
                           [j for j in review_jobs if j["bucket"] == "samples_20"], job_lookup)
    _write_review_markdown(review_dir / "missed_disagreements.md", "Abweichungen des Zweitmodells (übersehene Fälle)",
                           [j for j in review_jobs if j["bucket"] == "missed_disagreements"], job_lookup)

    any_human = any(bool(v) for v in human.values())
    mode_label = (
        "human_overrides_and_llm_reviewer" if (any_human and auto_confirm)
        else "human_only" if any_human
        else "automated_independent_llm_reviewer_with_verbatim_quote_check"
    )
    audit_summary = {
        "confirmation_mode": mode_label,
        "models_used": {
            role: {"id": p["id"], "name": p["name"], "model": p["model"], "base_url": p["base_url"]}
            for role, (p, _) in profiles.items()
        },
        "confirmed_positive_cases": confirmed_positive_cases,
        "confirmations_summary": confirmations_summary,
        "nonkeyword_60_summary": nonkeyword_60_summary,
        "samples_20_summary": samples_20_summary,
        "missed_20_summary": missed_20_summary,
    }
    (review_dir / "audit_summary.json").write_text(json.dumps(audit_summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote audit summary and markdown sheets to {review_dir}")
    return audit_summary


def _write_review_markdown(path, title, jobs, job_lookup):
    lines = [f"# {title}", "", f"Anzahl geprüfter Einträge: {len(jobs)}", ""]
    for job in jobs:
        confirmed, reason, verbatim = job_lookup[job["case"]]
        status_str = "BESTÄTIGT" if confirmed else "VERWORFEN"
        lines += [
            f"## `{job['case']}` — {status_str}",
            f"- **Gruppe:** `{job['group']}`",
            f"- **Zitat wörtlich im Quelltext gefunden:** `{'ja' if verbatim else 'NEIN'}`",
            f"- **Zitate:** {json.dumps(job['quotes'], ensure_ascii=False)}",
            f"- **Prüfbegründung:** {reason}",
            "",
        ]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_final_report(out_dir, data_dir):
    results = build_full_results(out_dir, data_dir)
    out_dir = Path(out_dir)
    (out_dir / "exploratory_labels_results.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    md = render_markdown_report(results)
    (out_dir / "exploratory_labels_report.md").write_text(md, encoding="utf-8")
    print(f"Wrote {out_dir / 'exploratory_labels_report.md'} and exploratory_labels_results.json")
    return results


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=(
        "prepare", "draft-codebook", "freeze-codebook", "label", "audit", "report", "run-all"
    ))
    parser.add_argument("--data", default="analysis/study-data-2026-11-05")
    parser.add_argument("--out", default="analysis/hauptanalyse-2026-11-06")
    parser.add_argument("--prereg", default="PREREGISTRATION.md")
    parser.add_argument("--work-dir", default="runs/lab")
    parser.add_argument("--judge-profile", default=DEFAULT_JUDGE_PROFILE)
    parser.add_argument("--second-profile", default=DEFAULT_SECOND_PROFILE)
    parser.add_argument("--reviewer-profile", default=DEFAULT_REVIEWER_PROFILE)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--only", nargs="+", choices=ALL_JUDGE_TASKS, help="subset of judge tasks for 'label'")
    parser.add_argument("--mode", choices=("recommended", "starting"), default="recommended", help="codebook freeze mode")
    parser.add_argument("--hypothesis", default="", help="optional strategy hypothesis to record before labelling")
    parser.add_argument("--auto-confirm", action="store_true", help="use independent reviewer LLM where no human override exists")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    cmd = args.command

    if cmd == "prepare":
        summary = prepare_inputs(args.data, args.out, args.prereg, seed=args.seed)
        print("Prepared blinded judge inputs:", json.dumps(summary, indent=2))
        return 0

    if cmd == "freeze-codebook":
        freeze_codebook(args.out, mode=args.mode, hypothesis=args.hypothesis, seed=args.seed)
        return 0

    if cmd == "report":
        write_final_report(args.out, args.data)
        return 0

    profiles = resolve_profiles(args.work_dir, args.judge_profile, args.second_profile, args.reviewer_profile, need_keys=True)
    judge_prof, judge_key = profiles["judge"]

    if cmd == "draft-codebook":
        draft_codebook(args.out, judge_prof, judge_key)
        return 0

    if cmd == "label":
        run_primary_labels(args.out, judge_prof, judge_key, only_tasks=args.only, workers=args.workers)
        return 0

    if cmd == "audit":
        run_audit(args.out, profiles, auto_confirm=args.auto_confirm, seed=args.seed, workers=args.workers)
        return 0

    if cmd == "run-all":
        summary = prepare_inputs(args.data, args.out, args.prereg, seed=args.seed)
        print("Prepared blinded judge inputs:", json.dumps(summary))
        cb_path = sub_folder(args.out) / "codebook.json"
        if not cb_path.exists():
            draft_codebook(args.out, judge_prof, judge_key)
            freeze_codebook(args.out, mode=args.mode, hypothesis=args.hypothesis, seed=args.seed)
        run_primary_labels(args.out, judge_prof, judge_key, only_tasks=args.only, workers=args.workers)
        run_audit(args.out, profiles, auto_confirm=args.auto_confirm, seed=args.seed, workers=args.workers)
        write_final_report(args.out, args.data)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
