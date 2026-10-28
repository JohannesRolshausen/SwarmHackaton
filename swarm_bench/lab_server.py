"""Local operator UI and run controller. No agent can access these operator endpoints."""
import copy
import csv
import io
import json
import mimetypes
import re
import secrets
import shutil
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .common import ROOT, read_json, write_json
from .experiment_design import DESIGNS_DIR, DesignStore
from .lab_engine import DEFAULT_COMMON_PROMPT, DEFAULT_SOLO_COMMON_PROMPT, DEFAULT_COMMUNICATION_PROMPT, DEFAULT_RESTRICTION, ASSET_RESTRICTION, SEGMENT_RESTRICTION, TOOLS, GROUP_TOOLS, PYTHON_TOOL, ARC_TOOLS, ALTRUISM_TOOLS, ACCOUNT_TOOL, PLACE_ORDER_TOOL, PROCUREMENT_PRESSURE, ARC_PROMPT, DESIGNED, LabRun, agent_text_defaults, assemble_prompt, is_segment
from .lab_storage import atomic_json, load_run, save_run
from .pdf_export import render_run_pdf
from .providers import ProviderError, ProviderRegistry, load_local_env

_TITLE_SUFFIX = re.compile(r"^(.*) \(\d+\)$")
_MAX_TITLE_LEN = 150
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,80}$")
_CLIENT_DISCONNECTED = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)
_FINISHED = ("complete", "stopped", "error")
_MAX_REPETITIONS = 100


def unique_experiment_title(title, existing_titles):
    """If title is taken, append Windows-style (1), (2), … to the base name."""
    taken = set(existing_titles)
    if title not in taken:
        return title
    match = _TITLE_SUFFIX.match(title)
    base = match.group(1) if match else title
    n = 1
    while True:
        suffix = f" ({n})"
        room = _MAX_TITLE_LEN - len(suffix)
        candidate = (base[:room] if len(base) > room else base) + suffix
        if candidate not in taken:
            return candidate
        n += 1


def light_config(config):
    """A run's configuration for lists: a designed run keeps its design name and roles, not every text and file."""
    if config.get("scenario") != DESIGNED:
        return config
    light = {k: v for k, v in config.items() if k not in ("design", "design_agents", "workspace_files")}
    design = config.get("design") or {}
    light["design"] = {"id": design.get("id", ""), "name": design.get("name", "")}
    light["design_agents"] = {a: {k: row.get(k) for k in ("role", "role_name", "handle", "forbidden_files", "start_after")}
                              for a, row in (config.get("design_agents") or {}).items()}
    return light


class LabManager:
    def __init__(self, data_dir, work_dir, designs_dir=DESIGNS_DIR):
        self.data_dir, self.work_dir = Path(data_dir), Path(work_dir)
        self.designs = DesignStore(designs_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.chmod(0o700)
        self.registry = ProviderRegistry(self.work_dir / "models.json")
        self.runs = {}
        self.workers = {}
        self.lock = threading.RLock()
        # Preserve previous runs as inspectable archives. Never silently resume model calls.
        self.archives = {}
        self.archive_histories = {}
        for folder in (self.work_dir / "runs").glob("*"):
            if not folder.is_dir():
                continue
            try:
                s, histories = load_run(folder)
                if s["status"] in ("running", "ready", "paused", "error"):
                    s["status"] = "archived"
                self.archives[s["id"]] = s
                self.archive_histories[s["id"]] = histories
            except (ValueError, KeyError, OSError):
                continue
        self.favorites = self._read_favorites()
        stale = self.favorites - set(self.archives)
        if stale:
            self.favorites -= stale
            try:
                self._write_favorites()
            except OSError:
                pass
        # Experiment packages: one configuration run several times, one after another.
        self.series = self._read_series()
        self.series_workers = {}
        self.series_cancel = {}
        if self.series:
            try:
                self._write_series()
            except OSError:
                pass
        self.tasks = []
        for item in read_json(self.data_dir / "manifest.json")["items"]:
            problem = read_json(self.data_dir / item["variants"]["split10"] / "public/problem.json")
            self.tasks.append({"id": item["task_id"], "question": problem["question"], "cue": item["cue"]})

    def summary(self, state):
        metrics = state.get("metrics", {})
        usage = state.get("usage", {}).values()
        return {"id": state["id"], "title": state["config"]["title"], "config": light_config(state["config"]),
                "status": state["status"], "created_at": state["created_at"], "metrics": metrics,
                "finish_reason": state.get("finish_reason"),
                "usage": {key: sum(row.get(key) or 0 for row in usage)
                          for key in ("input_tokens", "output_tokens", "calls", "successful_calls", "failed_calls")},
                "series_id": self._series_of(state["id"]),
                "favorite": state["id"] in self.favorites}

    def bootstrap(self):
        with self.lock:
            states = list(self.archives.values()) + [r.snapshot() for r in self.runs.values()]
            return {"tasks": self.tasks, "providers": self.registry.public(), "restriction_prompt": DEFAULT_RESTRICTION,
                    "asset_restriction_prompt": ASSET_RESTRICTION, "segment_restriction_prompt": SEGMENT_RESTRICTION,
                    "procurement_pressure_prompt": PROCUREMENT_PRESSURE,
                    "common_prompt": DEFAULT_COMMON_PROMPT, "solo_common_prompt": DEFAULT_SOLO_COMMON_PROMPT,
                    "communication_prompt": DEFAULT_COMMUNICATION_PROMPT, "arc_prompt": ARC_PROMPT, "tools": copy.deepcopy(TOOLS + [t for t in ARC_TOOLS if t["function"]["name"] not in {x["function"]["name"] for x in TOOLS}] + [ACCOUNT_TOOL, PLACE_ORDER_TOOL]),
                    "runs": [self.summary(s) for s in sorted(states, key=lambda s: s["created_at"], reverse=True)],
                    "series": self.series_list(),
                    "designs": self.designs.list(), "design_templates": self.designs.templates(),
                    "builtin_tools": copy.deepcopy(ALTRUISM_TOOLS)}

    def design_list(self):
        return {"designs": self.designs.list(), "templates": self.designs.templates()}

    def get_design(self, design_id):
        return self.designs.get(design_id)

    def save_design(self, body):
        """Write a design to designs/. ``create`` picks a free id from the name instead of overwriting."""
        create = body.get("create", False)
        if not isinstance(create, bool):
            raise ValueError("create must be true or false")
        with self.lock:
            design = self.designs.save(body.get("design"), create=create)
            return {"design": design, "designs": self.designs.list()}

    def delete_design(self, design_id):
        with self.lock:
            self.designs.delete(design_id)
            return {"designs": self.designs.list()}

    def create(self, config):
        config = copy.deepcopy(config)
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        with self.lock:
            titles = [s["config"]["title"] for s in self.archives.values()]
            titles.extend(r.state["config"]["title"] for r in self.runs.values())
            title = config.get("title", "Peer pressure experiment")
            if isinstance(title, str):
                config["title"] = unique_experiment_title(title, titles)
            run = LabRun(config, self.data_dir, self.work_dir / "runs" / run_id, self.registry)
            self.runs[run_id] = run
        return run.snapshot()

    def snapshot(self, run_id):
        with self.lock:
            if run_id in self.runs:
                snapshot = self.runs[run_id].snapshot()
                snapshot["worker_active"] = bool(self.workers.get(run_id) and self.workers[run_id].is_alive())
                if snapshot["worker_active"] and snapshot["status"] == "paused":
                    snapshot["status"] = "running"
                return snapshot
            if run_id in self.archives:
                return copy.deepcopy(self.archives[run_id])
            raise ValueError("Unknown experiment")

    def inspect(self, run_id, agent):
        if run_id in self.runs:
            return self.runs[run_id].inspect(agent)
        state = self.snapshot(run_id)
        if agent not in state["config"]["agents"]:
            raise ValueError("Unknown agent")
        folder = self.work_dir / "runs" / run_id
        history = copy.deepcopy(self.archive_histories[run_id].get(agent, []))
        account = None
        intact = None
        if state["config"].get("scenario") in ("custom", "communication", "group_misalignment", "altruism", "arc", "gpu_procurement", DESIGNED):
            files = state.get("workspace_state", state["config"].get("workspace_files", {})).get(agent, {})
            file = files
        elif is_segment(state["config"].get("scenario")):
            files = state.get("workspace_state", {}).get(agent, {})
            intact = state.get("segment_report", {}).get("intact_notes", {}).get(agent)
            file = json.loads(intact) if intact else files
            if agent not in state["config"].get("restricted", []):
                intact = None
        elif state["config"].get("scenario") == "asset_aggregation":
            account = state.get("accounts", {}).get(agent)
            file, files = account, state.get("workspace_state", {}).get(agent, {})
        else:
            file = read_json(folder / "agents" / agent / "notes.json")
            files = {"notes.json": file}
        names = state["config"].get("agent_tools", {}).get(agent, state["config"].get("enabled_tools", [t["function"]["name"] for t in TOOLS]))
        sections, exact = assemble_prompt(state["config"], agent) if history else (None, None)
        if exact != (history[0]["content"] if history else None):
            sections = None
        designed = state["config"].get("scenario") == DESIGNED
        return {"agent_id": agent, "system_prompt": history[0]["content"] if history else "History unavailable for this agent.",
                # A designed role can have its own task: the first user message of that agent.
                "question": history[1]["content"] if designed and len(history) > 1 else state["question"],
                **({"role": state["config"].get("design_agents", {}).get(agent)} if designed else {}),
                "file": file, "files": files,
                **({"sections": sections} if sections is not None else {}),
                **({"account": account} if account is not None else {}),
                **({"intact_file": intact} if intact is not None else {}),
                "tools": state.get("tool_schemas", {}).get(agent, [t for t in TOOLS if t["function"]["name"] in names]),
                "answers": state["answers"][agent], "history": history,
                **({"archive_warnings": state["archive_warnings"]} if state.get("archive_warnings") else {})}

    def export(self, run_id):
        # Snapshot and agent histories belong to the same instant, including during a live run.
        with self.lock:
            run = self.runs.get(run_id)
            with run.lock if run else threading.RLock():
                state = self.snapshot(run_id)
                return {**state, "schema_version": 1,
                        "participants": {a: self.inspect(run_id, a) for a in state["config"]["agents"]}}

    def export_pdf(self, run_id, kind):
        return render_run_pdf(self.export(run_id), kind)

    def wire_log(self, run_id):
        self.snapshot(run_id)  # Unknown experiments are rejected before any path is built.
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("Unknown experiment")
        run = self.runs.get(run_id)
        path = run.wire_log.path if run and run.wire_log else self.work_dir / "runs" / run_id / "wire_log.md"
        with run.wire_log.lock if run and run.wire_log else threading.Lock():
            if not path.is_file():
                raise ValueError("No provider requests were logged for this experiment. "
                                 "Demo runs and runs from before the wire log have none.")
            return path.read_bytes()

    def control(self, run_id, action):
        with self.lock:
            if run_id not in self.runs:
                raise ValueError("Archived run is view-only; duplicate its configuration to run again")
            run = self.runs[run_id]
            if run.state["status"] in ("complete", "stopped"):
                raise ValueError("Experiment finished")
            if action in ("pause", "stop"):
                run.pause_requested.set()
                with run.changed:
                    run.event("operator_" + action)
                    if not self.workers.get(run_id, None) or not self.workers[run_id].is_alive():
                        run.state["status"] = "stopped" if action == "stop" else "paused"
                    run.state["stop_requested"] = action == "stop"
                    run.changed.notify_all()
                    run.persist()
                return self.snapshot(run_id)
            if action != "play":
                raise ValueError("Unknown command")
            if self.workers.get(run_id) and self.workers[run_id].is_alive():
                raise ValueError("Execution is already in progress")
            if not run.remaining_agents():
                raise ValueError("Call limit reached; create a new run to use a fresh budget")
            if run.config["mode"] == "demo" and not run.demo_supported():
                raise ValueError("Scripted demo uses a peer pressure type with all of its default tools; choose Live models for a free configuration")
            if run.config["mode"] == "live":
                run.freeze_providers()
            run.pause_requested.clear()
            run.state["stop_requested"] = False

            def work():
                try:
                    run.run_free()
                except (ProviderError, ValueError):
                    pass  # Error already persisted in the run.
                except Exception:
                    with run.lock:
                        run.state.update(status="error", error="Internal execution error. See run events.")
                        run.event("internal_error")
                finally:
                    with run.lock:
                        if run.state.get("stop_requested"):
                            run.state["status"] = "stopped"
                        elif run.pause_requested.is_set() and run.state["status"] not in ("error", "complete"):
                            run.state["status"] = "paused"
                        run.state["metrics"] = run.metrics()
                        run.persist()
            thread = threading.Thread(target=work, daemon=True)
            self.workers[run_id] = thread
            thread.start()
            return self.snapshot(run_id)

    def export_csv(self, series_id=None):
        data = self.bootstrap()
        rows = data["runs"]
        titles = {s["id"]: s["title"] for s in data["series"]}
        if series_id is not None:
            record = next((s for s in data["series"] if s["id"] == series_id), None)
            if record is None:
                raise ValueError("Unknown experiment package")
            order = {rid: i for i, rid in enumerate(record["run_ids"])}
            rows = sorted((r for r in rows if r["id"] in order), key=lambda r: order[r["id"]])
        buffer = io.StringIO()
        fields = ["id", "series_id", "series_title", "series_run", "title", "scenario", "mode", "task_id", "agents", "restricted",
                  "leader", "importance", "status", "finish_reason", "breaches", "breach_rate", "before_peer_exposure",
                  "after_peer_exposure", "opened", "team_answer", "team_correct", "notes", "tool_errors", "calls",
                  "output_tokens", "input_tokens", "seed", "breached_agents", "disclosures", "disclosed_agents",
                  "disclosed_via", "order_total", "order_budget_status", "design_id", "design_name", "custom_actions",
                  "tested_models", "peer_models"]
        # One column per custom tool parameter, such as set_spending.amount, with the value of the tool's last call.
        def last_arguments(m):
            last = {}
            for e in m.get("custom_actions") or ():
                last[e["tool"]] = e.get("arguments") or {}
            return {f"{tool}.{key}": value for tool, arguments in last.items() for key, value in arguments.items()}
        argument_fields = list(dict.fromkeys(f for r in rows for f in last_arguments(r["metrics"])))
        fields += argument_fields
        # Three columns per design measure, such as budget.class, budget.over_by and budget.disclosure_term.
        def measure_columns(m):
            return {f"{name}.{key}": "" if result[key] is None else result[key]
                    for name, result in (m.get("measures") or {}).items()
                    for key in ("class", "over_by", "disclosure_term")}
        fields += list(dict.fromkeys(f for r in rows for f in measure_columns(r["metrics"])))
        profile_names = {p["id"]: p.get("name") or p["id"] for p in data["providers"]}
        def model_names(c, tested):
            return " | ".join(sorted({profile_names.get(p, p) for a, p in (c.get("models") or {}).items()
                                      if p and (a in c["restricted"]) == tested}))
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        positions = {rid: i + 1 for s in data["series"] for i, rid in enumerate(s["run_ids"])}
        for r in rows:
            c, m, u = r["config"], r["metrics"], r.get("usage", {})
            agents = m.get("agents") or {}
            writer.writerow({"id": r["id"], "series_id": r.get("series_id") or "", "series_title": titles.get(r.get("series_id"), ""),
                             "series_run": positions.get(r["id"], "") if r.get("series_id") else "", "title": r["title"],
                             "scenario": c.get("scenario", "peer_pressure"), "mode": c["mode"], "task_id": c["task_id"], "agents": c["agent_count"],
                             "restricted": len(c["restricted"]), "leader": c["leader"] or "", "importance": c["importance"],
                             "status": r["status"], "finish_reason": r.get("finish_reason") or "",
                             "breaches": m.get("breach_count"), "breach_rate": m.get("breach_rate"),
                             "before_peer_exposure": m.get("before_peer_exposure"), "after_peer_exposure": m.get("after_peer_exposure"),
                             "opened": m.get("opened_count"), "team_answer": m.get("team_answer") if m.get("team_answer") is not None else "",
                             "team_correct": m.get("team_correct"), "notes": m.get("note_count"), "tool_errors": m.get("tool_error_count"),
                             "calls": u.get("calls"), "output_tokens": u.get("output_tokens"), "input_tokens": u.get("input_tokens"),
                             "seed": c["seed"], "breached_agents": " ".join(a for a, s in agents.items() if s.get("breached")),
                             "disclosures": m.get("disclosure_count"),
                             "disclosed_agents": " ".join(a for a, s in agents.items() if s.get("disclosed")),
                             "disclosed_via": " ".join(sorted({v for s in agents.values() for v in s.get("disclosed_via") or ()})),
                             "order_total": (m.get("order") or {}).get("total", ""),
                             "order_budget_status": (m.get("order") or {}).get("budget_status", ""),
                             "design_id": (c.get("design") or {}).get("id", ""),
                             "design_name": (c.get("design") or {}).get("name", ""),
                             "custom_actions": " ".join(f"{e['agent_id']}:{e['tool']}" for e in m.get("custom_actions") or ()),
                             "tested_models": model_names(c, True), "peer_models": model_names(c, False),
                             **last_arguments(m), **measure_columns(m)})
        return buffer.getvalue()

    def _favorites_path(self):
        return self.work_dir / "favorites.json"

    def _read_favorites(self):
        path = self._favorites_path()
        if not path.exists():
            return set()
        try:
            data = read_json(path)
        except (OSError, ValueError):
            return set()
        ids = data.get("ids") if isinstance(data, dict) else None
        if not isinstance(ids, list):
            return set()
        return {item for item in ids if isinstance(item, str) and _RUN_ID.fullmatch(item)}

    def _write_favorites(self):
        atomic_json(self._favorites_path(), {"ids": sorted(self.favorites)})

    def _known(self, run_id):
        return run_id in self.runs or run_id in self.archives

    def _run_ids(self, ids):
        if not isinstance(ids, list) or not ids:
            raise ValueError("Choose at least one experiment")
        if len(ids) > 200:
            raise ValueError("Too many experiments at once")
        unique = []
        seen = set()
        for item in ids:
            if not isinstance(item, str) or not _RUN_ID.fullmatch(item):
                raise ValueError("Unknown experiment")
            if item not in seen:
                seen.add(item)
                unique.append(item)
        return unique

    def _run_folder(self, run_id):
        root = (self.work_dir / "runs").resolve()
        folder = (root / run_id).resolve()
        if folder.parent != root:
            raise ValueError("Unknown experiment")
        return folder

    def _titles(self, except_id=None):
        titles = []
        for run_id, state in self.archives.items():
            if run_id != except_id:
                titles.append(state["config"]["title"])
        for run_id, run in self.runs.items():
            if run_id != except_id:
                with run.lock:
                    titles.append(run.config["title"])
        return titles

    @staticmethod
    def _clean_title(title):
        if not isinstance(title, str):
            raise ValueError("Enter a name")
        title = " ".join(title.split())
        if not title or len(title) > _MAX_TITLE_LEN:
            raise ValueError("Name must be 1-150 characters")
        return title

    def rename(self, run_id, title):
        title = self._clean_title(title)
        with self.lock:
            if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) or not self._known(run_id):
                raise ValueError("Unknown experiment")
            if title in self._titles(except_id=run_id):
                raise ValueError("An experiment already uses this name")
            if run_id in self.runs:
                run = self.runs[run_id]
                with run.lock:
                    previous = run.config["title"]
                    if previous == title:
                        return self.summary(run.snapshot())
                    run.config["title"] = title
                    try:
                        run.persist()
                    except Exception:
                        run.config["title"] = previous
                        raise
            else:
                state = self.archives[run_id]
                previous = state["config"]["title"]
                if previous != title:
                    state["config"]["title"] = title
                    try:
                        save_run(self._run_folder(run_id), state, self.archive_histories[run_id])
                    except Exception:
                        state["config"]["title"] = previous
                        raise
            return self.summary(self.snapshot(run_id))

    def set_favorites(self, ids, favorite):
        if not isinstance(favorite, bool):
            raise ValueError("Invalid favorite value")
        unique = self._run_ids(ids)
        with self.lock:
            if any(not self._known(run_id) for run_id in unique):
                raise ValueError("Unknown experiment")
            if favorite:
                self.favorites.update(unique)
            else:
                self.favorites.difference_update(unique)
            self._write_favorites()
            return {"ids": unique, "favorite": favorite}

    def delete_runs(self, ids):
        unique = self._run_ids(ids)
        with self.lock:
            if any(not self._known(run_id) for run_id in unique):
                raise ValueError("Unknown experiment")
            busy = []
            for run_id in unique:
                worker = self.workers.get(run_id)
                if worker and worker.is_alive() and run_id in self.runs:
                    with self.runs[run_id].lock:
                        busy.append(self.runs[run_id].config["title"])
            if busy:
                names = ", ".join(busy[:3])
                extra = "" if len(busy) <= 3 else f" +{len(busy) - 3}"
                raise ValueError("Stop running experiments before deleting them: " + names + extra)
            if any(self._series_active(self._series_of(run_id)) for run_id in unique):
                raise ValueError("Stop the experiment package before deleting its runs")
            for run_id in unique:
                folder = self._run_folder(run_id)
                if folder.exists():
                    if not folder.is_dir():
                        raise ValueError("Unknown experiment")
                    shutil.rmtree(folder)
                self.runs.pop(run_id, None)
                self.archives.pop(run_id, None)
                self.archive_histories.pop(run_id, None)
                self.workers.pop(run_id, None)
                self.favorites.discard(run_id)
            self._write_favorites()
            gone = set(unique)
            if any(gone & set(record["run_ids"]) for record in self.series.values()):
                for record in self.series.values():
                    record["run_ids"] = [rid for rid in record["run_ids"] if rid not in gone]
                self._write_series()
            return {"deleted": unique}

    # Experiment packages ------------------------------------------------------------------

    def _series_path(self):
        return self.work_dir / "series.json"

    def _read_series(self):
        path = self._series_path()
        if not path.exists():
            return {}
        try:
            data = read_json(path)
        except (OSError, ValueError):
            return {}
        records = {}
        for record in data.get("series", []) if isinstance(data, dict) else []:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not _RUN_ID.fullmatch(record["id"]):
                continue
            if not isinstance(record.get("config"), dict) or not isinstance(record.get("run_ids"), list):
                continue
            record["run_ids"] = [rid for rid in record["run_ids"] if rid in self.archives]
            if record.get("status") == "running":
                # The worker died with the previous server; resuming is the operator's decision.
                record["status"] = "interrupted"
            record["active_run"] = None
            records[record["id"]] = record
        return records

    def _write_series(self):
        atomic_json(self._series_path(), {"series": sorted(self.series.values(), key=lambda r: r["created_at"])})

    def _series_of(self, run_id):
        return next((sid for sid, record in self.series.items() if run_id in record["run_ids"]), None)

    def _series_active(self, series_id):
        worker = self.series_workers.get(series_id)
        return bool(worker and worker.is_alive())

    def _run_status(self, run_id):
        if run_id in self.runs:
            run = self.runs[run_id]
            with run.lock:
                return run.state["status"]
        if run_id in self.archives:
            return self.archives[run_id]["status"]
        return None

    def series_summary(self, record):
        statuses = [self._run_status(rid) for rid in record["run_ids"]]
        return {**{k: copy.deepcopy(v) for k, v in record.items() if k != "config"},
                "config": copy.deepcopy(record["config"]), "worker_active": self._series_active(record["id"]),
                "finished_count": sum(s in _FINISHED for s in statuses)}

    def series_list(self):
        with self.lock:
            return [self.series_summary(r) for r in sorted(self.series.values(), key=lambda r: r["created_at"], reverse=True)]

    def start_series(self, run_id, repetitions):
        if isinstance(repetitions, bool) or not isinstance(repetitions, int) or not 2 <= repetitions <= _MAX_REPETITIONS:
            raise ValueError(f"Repetitions must be an integer between 2 and {_MAX_REPETITIONS}")
        with self.lock:
            if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) or not self._known(run_id):
                raise ValueError("Unknown experiment")
            source = self.snapshot(run_id)
            config = copy.deepcopy(source["config"])
            # Reject an unrunnable configuration now, not after the first member fails.
            if config["mode"] == "demo" and not LabRun(config, self.data_dir, self.work_dir / "preview", self.registry, preview=True).demo_supported():
                raise ValueError("Scripted demo uses a peer pressure type with all of its default tools; choose Live models for a free configuration")
            series_id = datetime.now().strftime("S%Y%m%d-%H%M%S-") + secrets.token_hex(3)
            title = _TITLE_SUFFIX.match(config["title"]).group(1) if _TITLE_SUFFIX.match(config["title"]) else config["title"]
            members = []
            # An experiment that was created but never started becomes run #1 of its package.
            unstarted = run_id in self.runs and source["status"] == "ready" and not source["events"]
            if unstarted and self._series_of(run_id) is None:
                members.append(run_id)
            self.series[series_id] = {"id": series_id, "title": title, "repetitions": repetitions, "run_ids": members,
                                      "source_run": run_id, "status": "running", "error": None, "active_run": None,
                                      "created_at": LabRun.now(), "config": config}
            self._write_series()
            self._launch_series(series_id)
            return self.series_summary(self.series[series_id])

    def control_series(self, series_id, action, count=1):
        with self.lock:
            record = self.series.get(series_id) if isinstance(series_id, str) else None
            if record is None:
                raise ValueError("Unknown experiment package")
            if action == "stop":
                cancel = self.series_cancel.get(series_id)
                if cancel:
                    cancel.set()
                active = record.get("active_run")
                if active in self.runs and self._run_status(active) not in _FINISHED:
                    self.control(active, "stop")
                if not self._series_active(series_id) and record["status"] == "running":
                    record["status"] = "stopped"
                    self._write_series()
            elif action == "continue":
                if self._series_active(series_id):
                    raise ValueError("This package is already running")
                record.update(status="running", error=None)
                self._write_series()
                self._launch_series(series_id)
            elif action == "extend":
                if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= _MAX_REPETITIONS:
                    raise ValueError(f"Add between 1 and {_MAX_REPETITIONS} runs")
                if record["repetitions"] + count > _MAX_REPETITIONS:
                    raise ValueError(f"A package holds at most {_MAX_REPETITIONS} runs")
                record["repetitions"] += count
                if not self._series_active(series_id):
                    record.update(status="running", error=None)
                    self._write_series()
                    self._launch_series(series_id)
                else:
                    self._write_series()
            else:
                raise ValueError("Unknown command")
            return self.series_summary(record)

    def delete_series(self, series_id):
        with self.lock:
            record = self.series.get(series_id) if isinstance(series_id, str) else None
            if record is None:
                raise ValueError("Unknown experiment package")
            if self._series_active(series_id):
                raise ValueError("Stop the experiment package before deleting it")
            if record["run_ids"]:
                self.delete_runs(list(record["run_ids"]))
            self.series.pop(series_id, None)
            self.series_cancel.pop(series_id, None)
            self.series_workers.pop(series_id, None)
            self._write_series()
            return {"deleted": series_id}

    def _launch_series(self, series_id):
        cancel = threading.Event()
        self.series_cancel[series_id] = cancel
        thread = threading.Thread(target=self._series_loop, args=(series_id, cancel), daemon=True)
        self.series_workers[series_id] = thread
        thread.start()

    def _series_loop(self, series_id, cancel):
        try:
            while not cancel.is_set():
                with self.lock:
                    record = self.series.get(series_id)
                    if record is None:
                        return
                    statuses = {rid: self._run_status(rid) for rid in record["run_ids"]}
                    if sum(s in _FINISHED for s in statuses.values()) >= record["repetitions"]:
                        record.update(status="complete", active_run=None)
                        self._write_series()
                        return
                    pending = next((rid for rid in record["run_ids"] if rid in self.runs and statuses[rid] not in _FINISHED), None)
                    if cancel.is_set():
                        break
                    if pending is None:
                        position = len(record["run_ids"]) + 1
                        created = self.create({**record["config"], "title": f"{record['title']} · #{position}"})
                        pending = created["id"]
                        record["run_ids"].append(pending)
                    record["active_run"] = pending
                    self._write_series()
                    worker = self.workers.get(pending)
                    if self._run_status(pending) == "ready" and not (worker and worker.is_alive()):
                        self.control(pending, "play")
                self._wait_for_run(pending, cancel)
                with self.lock:
                    run = self.runs.get(pending)
                    if run is None:
                        continue  # Deleted by the operator; the loop creates a replacement.
                    with run.lock:
                        status = run.state["status"]
                        succeeded = sum(u.get("successful_calls", 0) for u in run.state["usage"].values())
                        error = run.state.get("error")
                    if status == "error" and run.config["mode"] == "live" and not succeeded:
                        # Nothing reached a model; the next run would fail the same way.
                        record.update(status="error", active_run=None,
                                      error="Run without any successful model call: " + (error or "provider error"))
                        self._write_series()
                        return
            with self.lock:
                record = self.series.get(series_id)
                if record is not None:
                    record.update(status="stopped", active_run=None)
                    self._write_series()
        except Exception as exc:
            with self.lock:
                record = self.series.get(series_id)
                if record is not None:
                    record.update(status="error", active_run=None,
                                  error=str(exc) if isinstance(exc, (ValueError, ProviderError)) else "Internal package error")
                    try:
                        self._write_series()
                    except OSError:
                        pass

    def _wait_for_run(self, run_id, cancel):
        # A paused member keeps the package waiting until the operator resumes or stops it.
        while not cancel.is_set():
            with self.lock:
                worker = self.workers.get(run_id)
                status = self._run_status(run_id)
            if worker and worker.is_alive():
                worker.join(0.5)
                continue
            if status is None or status in _FINISHED:
                return
            cancel.wait(0.5)


def make_lab_server(manager, port=8766):
    static = Path(__file__).parent / "web"
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, data, mime="application/json; charset=utf-8", attachment=None):
            if not isinstance(data, bytes):
                data = (json.dumps(data, ensure_ascii=False) if mime.startswith("application/json") else data).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            if attachment:
                self.send_header("Content-Disposition", 'attachment; filename="' + attachment + '"')
            try:
                self.end_headers()
                self.wfile.write(data)
            except _CLIENT_DISCONNECTED:
                return

        def origin_ok(self):
            host = self.headers.get("Host", "")
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            origin = self.headers.get("Origin")
            return host in allowed and (not origin or origin in {"http://" + h for h in allowed})

        def do_GET(self):
            self.dispatch("GET")

        def do_POST(self):
            self.dispatch("POST")

        def dispatch(self, method):
            if not self.origin_ok():
                self.send(403, {"error": "Local access only"})
                return
            try:
                path = urlparse(self.path).path
                parts = path.strip("/").split("/")
                if method == "GET":
                    if path == "/api/bootstrap":
                        self.send(200, manager.bootstrap())
                    elif path == "/api/export.csv":
                        self.send(200, manager.export_csv(), "text/csv; charset=utf-8", "swarm-lab-comparisons.csv")
                    elif path == "/api/series":
                        self.send(200, {"series": manager.series_list()})
                    elif path == "/api/designs":
                        self.send(200, manager.design_list())
                    elif len(parts) == 3 and parts[:2] == ["api", "designs"]:
                        self.send(200, manager.get_design(parts[2]))
                    elif len(parts) == 4 and parts[:2] == ["api", "series"] and parts[3] == "export.csv" and _RUN_ID.fullmatch(parts[2]):
                        self.send(200, manager.export_csv(parts[2]), "text/csv; charset=utf-8", "swarm-lab-package-" + parts[2] + ".csv")
                    elif len(parts) >= 3 and parts[:2] == ["api", "runs"]:
                        if len(parts) == 5 and parts[3] == "agents":
                            self.send(200, manager.inspect(parts[2], parts[4]))
                        elif len(parts) == 4 and parts[3] in ("export.pdf", "chats.pdf"):
                            kind = "results" if parts[3] == "export.pdf" else "chats"
                            suffix = "" if kind == "results" else "-chats"
                            self.send(200, manager.export_pdf(parts[2], kind), "application/pdf", "swarm-lab-" + parts[2] + suffix + ".pdf")
                        elif len(parts) == 4 and parts[3] == "wire_log.md":
                            self.send(200, manager.wire_log(parts[2]), "text/markdown; charset=utf-8",
                                      "swarm-lab-" + parts[2] + "-wire-log.md")
                        elif len(parts) == 4 and parts[3] == "export":
                            self.send(200, manager.export(parts[2]), attachment="swarm-lab-" + parts[2] + ".json")
                        elif len(parts) == 3:
                            self.send(200, manager.snapshot(parts[2]))
                        else:
                            self.send(404, {"error": "Unknown route"})
                    elif path in ("/", "/app.js", "/lab.js", "/style.css"):
                        file = static / ("index.html" if path == "/" else path[1:])
                        self.send(200, file.read_bytes(), (mimetypes.guess_type(file.name)[0] or "text/plain") + "; charset=utf-8")
                    elif path == "/favicon.ico":
                        self.send(204, b"", "image/x-icon")
                    else:
                        self.send(404, {"error": "Unknown route"})
                    return
                if not self.headers.get("Content-Type", "").startswith("application/json"):
                    raise ValueError("Content-Type application/json required")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2_000_000:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("JSON object required")
                if path == "/api/providers":
                    self.send(200, {"providers": manager.registry.save(body)})
                elif path == "/api/preview":
                    run = LabRun(body, manager.data_dir, manager.work_dir / "preview", manager.registry, preview=True)
                    self.send(200, {"question": run.question, "config": light_config(run.config),
                              "agents": {a: {"prompt": run.prompt(a), "sections": run.prompt_sections(a),
                              "task": run.questions.get(a, run.question),
                              **({"role": run.config["design_agents"][a]} if run.config["scenario"] == DESIGNED else {}),
                              "file": run.files[a], "files": run.workspaces[a], "tools": run.tools_for(a),
                              "defaults": agent_text_defaults(run.config, a),
                              **({"account": run.accounts[a]} if run.accounts else {}),
                              **({"intact_file": run.segment_data["intact_notes"][a]}
                                 if is_segment(run.config["scenario"]) and a in run.config["restricted"] else {})}
                              for a in run.agents}})
                elif path == "/api/runs":
                    self.send(201, manager.create(body))
                elif path == "/api/designs":
                    self.send(200, manager.save_design(body))
                elif len(parts) == 4 and parts[:2] == ["api", "designs"] and parts[3] == "delete":
                    self.send(200, manager.delete_design(parts[2]))
                elif path == "/api/runs/delete":
                    self.send(200, manager.delete_runs(body.get("ids")))
                elif path == "/api/series":
                    self.send(201, manager.start_series(body.get("run_id"), body.get("repetitions")))
                elif len(parts) == 4 and parts[:2] == ["api", "series"] and parts[3] == "control":
                    self.send(200, manager.control_series(parts[2], body.get("action"), body.get("count", 1)))
                elif len(parts) == 4 and parts[:2] == ["api", "series"] and parts[3] == "delete":
                    self.send(200, manager.delete_series(parts[2]))
                elif path == "/api/runs/favorite":
                    self.send(200, manager.set_favorites(body.get("ids"), body.get("favorite")))
                elif len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "rename":
                    self.send(200, manager.rename(parts[2], body.get("title")))
                elif len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "control":
                    self.send(200, manager.control(parts[2], body.get("action")))
                else:
                    self.send(404, {"error": "Unknown route"})
            except (ValueError, TypeError, KeyError, ProviderError) as exc:
                self.send(400, {"error": str(exc)})
            except OSError as exc:
                if isinstance(exc, _CLIENT_DISCONNECTED) or getattr(exc, "winerror", None) == 10053:
                    return
                self.send(500, {"error": "Local read or write error"})
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve_lab(data_dir=ROOT / "data", work_dir=ROOT / "runs/lab", port=8766):
    load_local_env(ROOT / ".env")
    manager = LabManager(data_dir, work_dir)
    server = make_lab_server(manager, port)
    print(f"Swarm Lab — http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        for cancel in manager.series_cancel.values():
            cancel.set()
        for run in manager.runs.values():
            run.pause_requested.set()
    finally:
        server.server_close()
