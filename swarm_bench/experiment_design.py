"""Experiment Lab designs: roles, prompts, files, tools, and rules of a new experiment.

A design is plain data. ``validate_design`` checks it and fills in defaults; ``expand`` turns its roles into one
entry per agent, in role order (agent_01 is the first instance of the first role). The lab engine runs a design as
``scenario: designed`` with the whole design frozen in the run configuration, so a run stays reproducible after the
design is edited or deleted. Designs are shared through the tracked ``designs/`` folder; they never hold model
profiles or keys."""
import copy
import json
import re
from pathlib import Path

from .common import ROOT, read_json
from .lab_storage import atomic_json

FORMAT = 1
MAX_AGENTS = 32
MAX_FILES = 20
DESIGNS_DIR = ROOT / "designs"
TEMPLATES_DIR = Path(__file__).parent / "design_templates"
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,59}")
ROLE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{1,39}")
PARAM_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")
FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}")
BUILTIN_TOOLS = ("list_files", "read_file", "create_file", "edit_file", "read_board", "post_note", "submit_answer",
                 "run_python")
# Tools of other scenarios. A custom tool cannot take these names either.
RESERVED_TOOLS = BUILTIN_TOOLS + ("query_account", "place_order", "arc_observe", "arc_step")
DEFAULT_TOOLS = ["list_files", "read_file", "read_board", "post_note", "submit_answer"]
PARAM_TYPES = ("string", "integer", "number", "boolean")
PLACEHOLDER = re.compile(r"\{(handle|role|agent_id|n|others|group_size|channel|leader)\}")
DEFAULT_CHANNEL = "shared channel"
RULE_DEFAULTS = {"board_delivery": "tool_only", "board_message_limit": None, "wait_for_peer_after_post": False,
                 "answer_policy": "none", "idle_policy": "finish", "idle_wait_seconds": 2, "stop_on_breach": False}
RUN_DEFAULTS = {"call_limit": 24, "max_output_tokens": 16000, "total_output_tokens": 1500000, "temperature": None}


def render(text, values):
    """Fill the known placeholders. Any other text in braces, such as JSON, stays as written."""
    if not isinstance(text, str):
        return text
    return PLACEHOLDER.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")[:50].strip("-")
    return slug or "design"


def _text(value, label, limit, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{label}: text of at most {limit:,} characters")
    if required and not value.strip():
        raise ValueError(f"{label} is required")
    return value


def _flag(value, label, default=False):
    value = default if value is None else value
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be true or false")
    return value


def _integer(value, label, low, high, default=None, optional=False):
    if value is None and optional:
        return None
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{label} must be a whole number from {low:,} to {high:,}")
    return value


def _choice(value, label, choices, default):
    value = default if value is None else value
    if value not in choices:
        raise ValueError(f"{label} must be one of: {', '.join(choices)}")
    return value


def _files(value, label):
    value = {} if value is None else value
    if not isinstance(value, dict) or len(value) > MAX_FILES:
        raise ValueError(f"{label}: an object of at most {MAX_FILES} files")
    out = {}
    for name, content in value.items():
        if not isinstance(name, str) or not FILENAME.fullmatch(name):
            raise ValueError(f"{label}: invalid filename {name!r}; use a simple name such as notes.txt, without folders")
        try:
            size = len(json.dumps(content, ensure_ascii=False, allow_nan=False).encode())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}: {name} must hold text or JSON") from exc
        if size > 64_000:
            raise ValueError(f"{label}: {name} exceeds 64 KiB")
        out[name] = copy.deepcopy(content)
    return out


def _tool_texts(value, label, known):
    value = {} if value is None else value
    if not isinstance(value, dict):
        raise ValueError(f"{label}: tool descriptions must be an object")
    out = {}
    for name, text in value.items():
        if name not in known:
            raise ValueError(f"{label}: description for unknown tool {name!r}")
        text = _text(text, f"{label}: description of {name}", 4000)
        if text.strip():
            out[name] = text.strip()
    return out


def _custom_tool(raw, index):
    if not isinstance(raw, dict):
        raise ValueError(f"Custom tool {index}: invalid definition")
    name = raw.get("name")
    if not isinstance(name, str) or not TOOL_NAME.fullmatch(name):
        raise ValueError(f"Custom tool {index}: name must start with a letter and use a-z, 0-9 and _ (2 to 40 characters)")
    if name in RESERVED_TOOLS:
        raise ValueError(f"Custom tool {name}: this name belongs to a built-in tool")
    label = f"Custom tool {name}"
    params = raw.get("parameters") or []
    if not isinstance(params, list) or len(params) > 12:
        raise ValueError(f"{label}: at most 12 parameters")
    parameters, seen = [], set()
    for item in params:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not PARAM_NAME.fullmatch(item["name"]):
            raise ValueError(f"{label}: parameter names start with a letter and use a-z, 0-9 and _")
        if item["name"] in seen:
            raise ValueError(f"{label}: parameter {item['name']} appears twice")
        seen.add(item["name"])
        parameters.append({"name": item["name"],
                           "type": _choice(item.get("type"), f"{label}: type of {item['name']}", PARAM_TYPES, "string"),
                           "required": _flag(item.get("required"), f"{label}: required", True),
                           "description": _text(item.get("description"), f"{label}: description of {item['name']}", 1000).strip()})
    return {"name": name, "description": _text(raw.get("description"), f"{label}: description", 4000, True).strip(),
            "parameters": parameters,
            "response": _text(raw.get("response") or "Done.", f"{label}: response", 4000).strip() or "Done.",
            "breach": _flag(raw.get("breach"), f"{label}: counts as breach"),
            "ends_run": _flag(raw.get("ends_run"), f"{label}: ends the run")}


def _role(raw, index, known_tools):
    if not isinstance(raw, dict):
        raise ValueError(f"Role {index}: invalid definition")
    role_id = raw.get("id")
    if not isinstance(role_id, str) or not ROLE_ID.fullmatch(role_id):
        raise ValueError(f"Role {index}: id must use a-z, 0-9, _ and - (up to 40 characters)")
    label = f'Role "{role_id}"'
    count = _integer(raw.get("count"), f"{label}: count", 1, MAX_AGENTS, 1)
    tools = raw.get("tools", DEFAULT_TOOLS)
    if not isinstance(tools, list) or len(tools) != len(set(tools)):
        raise ValueError(f"{label}: tools must be a list without repeats")
    unknown = [t for t in tools if t not in known_tools]
    if unknown:
        raise ValueError(f"{label}: unknown tool {unknown[0]!r}")
    forbidden = raw.get("forbidden_files") or []
    if not isinstance(forbidden, list) or any(not isinstance(f, str) for f in forbidden) or len(forbidden) != len(set(forbidden)):
        raise ValueError(f"{label}: forbidden files must be a list of filenames")
    instances = raw.get("instances") or []
    if not isinstance(instances, list) or len(instances) > count:
        raise ValueError(f"{label}: at most one instance entry per agent ({count})")
    cleaned_instances = []
    for i, item in enumerate(instances, 1):
        if item is None:
            item = {}
        if not isinstance(item, dict):
            raise ValueError(f"{label}: instance {i} is invalid")
        cleaned_instances.append({
            "handle": _text(item.get("handle"), f"{label}: handle of instance {i}", 60).strip(),
            "extra_instruction": _text(item.get("extra_instruction"), f"{label}: extra instruction of instance {i}", 4000).strip(),
            "files": _files(item.get("files"), f"{label}: files of instance {i}")})
    while cleaned_instances and cleaned_instances[-1] == {"handle": "", "extra_instruction": "", "files": {}}:
        cleaned_instances.pop()  # Trailing empty entries carry nothing.
    task = raw.get("task")
    start_after = raw.get("start_after") or None
    if start_after is not None and not isinstance(start_after, str):
        raise ValueError(f"{label}: start_after must be a role id or null")
    return {"id": role_id,"name": _text(raw.get("name") or role_id, f"{label}: name", 100).strip() or role_id,
            "count": count,
            "handle": _text(raw.get("handle"), f"{label}: handle", 60).strip(),
            "identity": _text(raw.get("identity"), f"{label}: identity", 4000).strip(),
            "instructions": _text(raw.get("instructions"), f"{label}: instructions", 16000).strip(),
            "private_instruction": _text(raw.get("private_instruction"), f"{label}: private instruction", 6000).strip(),
            "private_position": _choice(raw.get("private_position"), f"{label}: private instruction position",
                                        ("inline", "start"), "inline"),
            "task": None if task is None or (isinstance(task, str) and not task.strip())
            else _text(task, f"{label}: task", 20000).strip(),
            "files": _files(raw.get("files"), f"{label}: files"),
            "forbidden_files": list(forbidden),
            "tools": list(tools),
            "tool_descriptions": _tool_texts(raw.get("tool_descriptions"), label, known_tools),
            "leader": _flag(raw.get("leader"), f"{label}: leader"),
            "start_after": start_after,
            "instances": cleaned_instances}


def validate_design(raw):
    """A complete, normalized design, or ValueError naming the field to fix."""
    if not isinstance(raw, dict):
        raise ValueError("A design must be a JSON object")
    if raw.get("format", FORMAT) != FORMAT:
        raise ValueError(f"Unsupported design format; this lab reads format {FORMAT}")
    design_id = raw.get("id") or ""
    if design_id and (not isinstance(design_id, str) or not SLUG.fullmatch(design_id)):
        raise ValueError("Design id must use a-z, 0-9 and - (up to 60 characters)")
    world = raw.get("world") or {}
    if not isinstance(world, dict):
        raise ValueError("world must be an object")
    use_handles = _flag(world.get("use_handles"), "world.use_handles")
    channel = _text(world.get("channel"), "Channel name", 100).strip()
    custom = raw.get("custom_tools") or []
    if not isinstance(custom, list) or len(custom) > 12:
        raise ValueError("At most 12 custom tools")
    custom_tools = [_custom_tool(item, i) for i, item in enumerate(custom, 1)]
    names = [t["name"] for t in custom_tools]
    if len(names) != len(set(names)):
        raise ValueError("Two custom tools have the same name")
    known_tools = list(BUILTIN_TOOLS) + names
    roles_raw = raw.get("roles")
    if not isinstance(roles_raw, list) or not roles_raw:
        raise ValueError("A design needs at least one role")
    roles = [_role(item, i, known_tools) for i, item in enumerate(roles_raw, 1)]
    role_ids = [r["id"] for r in roles]
    if len(role_ids) != len(set(role_ids)):
        raise ValueError("Two roles have the same id")
    starts = {r["id"]: r["start_after"] for r in roles}
    for role in roles:
        if role["start_after"] is not None and role["start_after"] not in starts:
            raise ValueError(f'Role "{role["id"]}": starts after unknown role {role["start_after"]!r}')
        seen, current = {role["id"]}, role["start_after"]
        while current is not None:
            if current in seen:
                raise ValueError(f'Role "{role["id"]}": the start order runs in a circle; one role must start with the run')
            seen.add(current)
            current = starts[current]
    total = sum(r["count"] for r in roles)
    if total > MAX_AGENTS:
        raise ValueError(f"The roles add up to {total} agents; the lab runs at most {MAX_AGENTS}")
    if sum(r["leader"] for r in roles) > 1:
        raise ValueError("Only one role can lead; its first agent is the leader")
    rules_raw = raw.get("rules") or {}
    if not isinstance(rules_raw, dict):
        raise ValueError("rules must be an object")
    rules = {"board_delivery": _choice(rules_raw.get("board_delivery"), "Board sharing", ("tool_only", "push", "auto"), "tool_only"),
             "board_message_limit": _integer(rules_raw.get("board_message_limit"), "Board message limit", 1, 100000, optional=True),
             "wait_for_peer_after_post": _flag(rules_raw.get("wait_for_peer_after_post"), "Wait for a peer after posting"),
             "answer_policy": _choice(rules_raw.get("answer_policy"), "Collective answer", ("none", "plurality", "leader"), "none"),
             "idle_policy": _choice(rules_raw.get("idle_policy"), "When agents stop acting", ("finish", "continue"), "finish"),
             "idle_wait_seconds": _integer(rules_raw.get("idle_wait_seconds"), "Wait before continuation", 0, 60, 2),
             "stop_on_breach": _flag(rules_raw.get("stop_on_breach"), "Stop at the first breach")}
    if rules["answer_policy"] == "leader" and not any(r["leader"] for r in roles):
        raise ValueError("The collective answer is the leader's, but no role leads")
    run_raw = raw.get("run_defaults") or {}
    if not isinstance(run_raw, dict):
        raise ValueError("run_defaults must be an object")
    temperature = run_raw.get("temperature")
    if temperature is not None and (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
                                    or not 0 <= temperature <= 2):
        raise ValueError("Temperature must be between 0 and 2")
    run_defaults = {"call_limit": _integer(run_raw.get("call_limit"), "Max calls per agent", 1, 500, 24),
                    "max_output_tokens": _integer(run_raw.get("max_output_tokens"), "Tokens per call", 128, 400000, 16000),
                    "total_output_tokens": _integer(run_raw.get("total_output_tokens"), "Shared output budget", 128,
                                                    1_000_000_000, 1500000),
                    "temperature": temperature}
    messages_raw = raw.get("controller_messages") or []
    if not isinstance(messages_raw, list) or len(messages_raw) > 20:
        raise ValueError("At most 20 controller messages")
    messages = []
    for i, item in enumerate(messages_raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Controller message {i}: invalid definition")
        targets = item.get("to_roles") or []
        if not isinstance(targets, list) or not targets or len(targets) != len(set(targets)):
            raise ValueError(f"Controller message {i}: choose at least one role to receive it")
        missing = [t for t in targets if t not in role_ids]
        if missing:
            raise ValueError(f"Controller message {i}: unknown role {missing[0]!r}")
        messages.append({"after_notes": _integer(item.get("after_notes"), f"Controller message {i}: after board messages", 1, 100000, 10),
                         "to_roles": list(targets),
                         "content": _text(item.get("content"), f"Controller message {i}", 6000, True).strip(),
                         "stop_others": _flag(item.get("stop_others"), f"Controller message {i}: stop the other roles")})
    design = {"format": FORMAT, "id": design_id,
              "name": _text(raw.get("name"), "Design name", 150, True).strip(),
              "description": _text(raw.get("description"), "Description", 8000).strip(),
              "world": {"use_handles": use_handles, "channel": channel},
              "task": _text(raw.get("task"), "Shared task", 20000, True).strip(),
              "shared_prompt": _text(raw.get("shared_prompt"), "Shared prompt", 16000).strip(),
              "shared_files": _files(raw.get("shared_files"), "Shared files"),
              "roles": roles, "custom_tools": custom_tools, "controller_messages": messages,
              "rules": rules, "run_defaults": run_defaults}
    expand(design)  # Per-agent checks: handles, forbidden files, and file counts.
    return design


def _handle(role, n, agent_id, instance):
    template = instance.get("handle") or role["handle"] or (role["id"] if role["count"] == 1 else role["id"] + "-{n}")
    return render(template, {"n": n, "agent_id": agent_id, "role": role["id"]}).strip()


def expand(design):
    """One entry per agent, in role order, with every placeholder filled."""
    rows = []
    for role in design["roles"]:
        for n in range(1, role["count"] + 1):
            agent_id = f"agent_{len(rows) + 1:02d}"
            instance = role["instances"][n - 1] if n <= len(role["instances"]) else {}
            rows.append({"agent_id": agent_id, "role": role, "n": n, "instance": instance,
                         "handle": _handle(role, n, agent_id, instance)})
    use_handles = design["world"]["use_handles"]
    channel = design["world"]["channel"] or DEFAULT_CHANNEL
    if use_handles:
        handles = [row["handle"] for row in rows]
        if any(not h or "\n" in h for h in handles):
            raise ValueError("Every agent needs a one-line handle")
        repeated = next((h for h in handles if handles.count(h) > 1), None)
        if repeated:
            raise ValueError(f"Two agents would share the handle {repeated!r}; use {{n}} in the role handle")
    def name(r):
        return r["handle"] if use_handles else r["agent_id"]

    leader = next((name(r) for r in rows if r["role"]["leader"] and r["n"] == 1), "none")
    out = []
    for row in rows:
        role, agent_id = row["role"], row["agent_id"]
        others = ", ".join(f"{name(r)} ({r['role']['name']})" for r in rows if r["agent_id"] != agent_id)
        values = {"handle": name(row), "role": role["name"], "agent_id": agent_id, "n": row["n"],
                  "others": others or "none", "group_size": len(rows), "channel": channel, "leader": leader}
        files = {**design["shared_files"], **role["files"], **row["instance"].get("files", {})}
        if len(files) > MAX_FILES:
            raise ValueError(f'Role "{role["id"]}": at most {MAX_FILES} files per agent, shared files included')
        missing = [f for f in role["forbidden_files"] if f not in files]
        if missing:
            raise ValueError(f'Role "{role["id"]}": forbidden file {missing[0]} is not in its files')
        if not any((role["identity"], design["shared_prompt"], role["instructions"], role["private_instruction"])):
            raise ValueError(f'Role "{role["id"]}": write an identity, instructions, or a shared prompt; '
                             "the system prompt cannot be empty")
        controller = {str(i): render(m["content"], values)
                      for i, m in enumerate(design["controller_messages"]) if role["id"] in m["to_roles"]}
        out.append({"agent_id": agent_id, "role": role["id"], "role_name": role["name"], "n": row["n"],
                    "handle": row["handle"] if use_handles else None,
                    "identity": render(role["identity"], values),
                    "shared_prompt": render(design["shared_prompt"], values),
                    "instructions": render(role["instructions"], values),
                    "private_instruction": render(role["private_instruction"], values),
                    "private_position": role["private_position"],
                    "task": render(role["task"] if role["task"] is not None else design["task"], values),
                    "files": {k: render(v, values) for k, v in files.items()},
                    "forbidden_files": list(role["forbidden_files"]),
                    "tools": list(role["tools"]),
                    "tool_descriptions": dict(role["tool_descriptions"]),
                    "extra_instruction": render(row["instance"].get("extra_instruction", ""), values),
                    "controller_messages": controller,
                    "leader": role["leader"] and row["n"] == 1,
                    "start_after": role["start_after"]})
    return out


def summary(design):
    roles = [{"id": r["id"], "name": r["name"], "count": r["count"]} for r in design["roles"]]
    return {"id": design["id"], "name": design["name"], "description": design["description"],
            "agent_count": sum(r["count"] for r in design["roles"]), "roles": roles,
            "custom_tools": [t["name"] for t in design["custom_tools"]]}


class DesignStore:
    """Designs in the tracked designs/ folder, one JSON file per design, and read-only shipped templates."""

    def __init__(self, folder=DESIGNS_DIR, templates=TEMPLATES_DIR):
        self.folder, self.templates_dir = Path(folder), Path(templates)

    def _path(self, design_id):
        if not isinstance(design_id, str) or not SLUG.fullmatch(design_id):
            raise ValueError("Unknown design")
        return self.folder / f"{design_id}.json"

    def _read(self, path):
        design = validate_design(read_json(path))
        design["id"] = path.stem
        return design

    def list(self):
        if not self.folder.is_dir():
            return []
        rows = []
        for path in sorted(self.folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            if not SLUG.fullmatch(path.stem):
                continue
            try:
                rows.append(summary(self._read(path)))
            except (ValueError, OSError) as exc:
                rows.append({"id": path.stem, "name": path.stem, "error": str(exc)})
        return rows

    def get(self, design_id):
        path = self._path(design_id)
        if not path.is_file():
            raise ValueError("Unknown design")
        return self._read(path)

    def save(self, raw, create=False):
        """Validate and write a design. ``create`` picks a free id from the name instead of overwriting."""
        design = validate_design(raw)
        if create or not design["id"]:
            base = slugify(design["id"] or design["name"])
            design["id"], n = base, 2
            while self._path(design["id"]).exists():
                design["id"] = f"{base[:55]}-{n}"
                n += 1
        self.folder.mkdir(parents=True, exist_ok=True)
        atomic_json(self._path(design["id"]), design)
        return design

    def delete(self, design_id):
        path = self._path(design_id)
        if not path.is_file():
            raise ValueError("Unknown design")
        path.unlink()

    def templates(self):
        if not self.templates_dir.is_dir():
            return []
        out = []
        for path in sorted(self.templates_dir.glob("*.json")):
            design = validate_design(read_json(path))
            design["id"] = path.stem
            out.append(design)
        return out
