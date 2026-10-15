"""PDF downloads for one run: full results, or agent chats only. Stdlib, with a local TrueType font."""
import json
import os
import struct
import zlib
from pathlib import Path

from .providers import split_thought_tags

PAGE_W = 595.28
PAGE_H = 841.89
MARGIN = 48
INK = (0.145, 0.196, 0.180)
MUTED = (0.42, 0.47, 0.45)
GREEN = (0.204, 0.369, 0.286)
BLUE = (0.306, 0.427, 0.533)
AMBER = (0.714, 0.475, 0.149)

SCENARIOS = {
    "peer_pressure": "Peer pressure · split data",
    "asset_aggregation": "Peer pressure · asset aggregation",
    "segment_report": "Peer pressure · 2024 segments",
    "segment_disclose": "Peer pressure · 2024 segments · do not disclose",
    "gpu_procurement": "Peer pressure · GPU procurement",
    "custom": "Free experiment",
    "group_misalignment": "Group misalignment",
    "communication": "Spontaneous communication",
    "altruism": "Mutual aid",
    "arc": "ARC-AGI-3",
    "designed": "Experiment Lab design",
}
STATUSES = {
    "ready": "Ready", "running": "Running", "working": "Active", "waiting": "Waiting",
    "rate_limited": "Rate limited",
    "paused": "Paused", "complete": "Complete", "stopped": "Stopped", "error": "Error",
    "archived": "Archive", "done": "Done", "limit": "Limit reached",
}
FINISH = {
    "call_limit": "Call limit reached",
    "conversation_idle": "Conversation idle",
    "agents_finished": "All agents finished",
    "all_submitted": "All agents submitted",
    "token_limit": "Token budget reached",
    "first_breach": "Stopped after a forbidden read",
    "order_placed": "Order placed",
    "no_order": "Ended without an order",
    "action_ended": "Ended by an action tool",
}
POLICIES = {
    "plurality": "Most frequent answer",
    "leader": "Leader's answer",
    "none": "Individual answers only",
}
DELIVERY = {
    "auto": "Notes and automatic text replies",
    "tool_only": "Agents read with read_board",
    "push": "Push each note into peers' context",
}


def render_run_pdf(payload, kind):
    if kind not in ("results", "chats"):
        raise ValueError("Unknown PDF export")
    regular, bold = load_font_pair()
    doc = Document(regular, bold)
    if kind == "chats":
        write_chats_document(doc, payload)
    else:
        write_results_document(doc, payload)
    return doc.to_pdf(payload.get("id") or "run", payload.get("config", {}).get("title") or "Experiment")


def write_results_document(doc, payload):
    config = payload.get("config") or {}
    metrics = payload.get("metrics") or {}
    doc.kicker("Run results")
    doc.heading(config.get("title") or "Experiment")
    doc.meta(" · ".join(part for part in (
        payload.get("id"),
        STATUSES.get(payload.get("status"), payload.get("status")),
        "Demo" if config.get("mode") == "demo" else "Live models",
        SCENARIOS.get(config.get("scenario"), config.get("scenario")),
        (config.get("design") or {}).get("name"),
        format_time(payload.get("created_at")),
    ) if part))
    if config.get("mode") == "demo":
        doc.paragraph("Scripted demo. These traces are not model measurements.", size=10, color=AMBER, before=2)

    doc.section("Question")
    task = config.get("task_id") or ""
    if task and config.get("scenario") == "peer_pressure":
        doc.paragraph(task.replace("_", " ").upper(), size=9, bold=True, color=MUTED, after=2)
    doc.paragraph(payload.get("question") or "No question recorded.")

    doc.section("Summary")
    for line in summary_lines(payload, config, metrics):
        doc.paragraph(line, size=10, after=2)
    if payload.get("error"):
        doc.paragraph(str(payload["error"]), size=10, color=AMBER, before=2)
    for warning in payload.get("archive_warnings") or []:
        doc.paragraph(str(warning), size=10, color=AMBER)

    doc.section("Participants")
    write_participants(doc, payload, config, metrics)

    doc.section("Group notes")
    notes = payload.get("notes") or []
    if not notes:
        doc.paragraph("No notes were posted.", color=MUTED)
    visible_notes = []
    for note in notes:
        visible, _thought = split_thought_tags(note.get("content") or "")
        if visible.strip():
            visible_notes.append((note, visible))
    if notes and not visible_notes:
        doc.paragraph("The notes were reasoning summaries. They are included with each agent's chat.", color=MUTED)
    for note, visible in visible_notes:
        who = agent_label(note.get("agent_id"))
        doc.paragraph(f"{who}  ·  {format_time(note.get('at'))}  ·  #{note.get('id')}", size=9, bold=True, color=GREEN, after=1)
        doc.paragraph(visible, after=6)

    doc.section("Observation log")
    events = [event for event in payload.get("events") or [] if event.get("kind") in LOG_KINDS]
    if not events:
        doc.paragraph("No observation events.", color=MUTED)
    for event in events:
        doc.paragraph(event_line(event), size=9, after=2)

    doc.section("Agent chats")
    write_agent_chats(doc, payload)


def write_chats_document(doc, payload):
    config = payload.get("config") or {}
    doc.kicker("Agent chats")
    doc.heading(config.get("title") or "Experiment")
    doc.meta(" · ".join(part for part in (
        payload.get("id"),
        "Conversations only",
        format_time(payload.get("created_at")),
    ) if part))
    doc.paragraph("System prompts, tasks, replies, tool calls, and tool results. Metrics, notes, and the observation log are in the results PDF.", size=10, color=MUTED, before=4)
    write_agent_chats(doc, payload)


def write_participants(doc, payload, config, metrics):
    restricted = set(config.get("restricted") or [])
    leader = config.get("leader")
    per_agent = metrics.get("agents") or {}
    usage = payload.get("usage") or {}
    statuses = payload.get("agent_status") or {}
    answers = payload.get("answers") or {}
    asset = config.get("scenario") == "asset_aggregation"
    segment = config.get("scenario") in ("segment_report", "segment_disclose")
    disclose = config.get("scenario") == "segment_disclose"
    procurement = config.get("scenario") == "gpu_procurement"
    designed = config.get("scenario") == "designed"
    roles = config.get("design_agents") or {}
    order = metrics.get("order")
    actions = metrics.get("custom_actions") or []
    for agent in config.get("agents") or []:
        info = per_agent.get(agent) or {}
        spent = usage.get(agent) or {}
        flags = []
        if designed:
            role = roles.get(agent) or {}
            flags.append(" ".join(part for part in (role.get("role_name"), role.get("handle") and f"({role['handle']})") if part))
            if role.get("start_after"):
                flags.append(f"starts after {role['start_after']}")
            if role.get("forbidden_files"):
                flags.append("forbidden: " + ", ".join(role["forbidden_files"]))
            if role.get("private_instruction"):
                flags.append("private instruction")
            flags.extend(f"called {a['tool']}" + (" (breach)" if a.get("restricted") else "")
                         for a in actions if a.get("agent_id") == agent)
        elif segment and agent in restricted:
            flags.extend(["corrupted extract", "report marked do not disclose" if disclose else "business report forbidden"])
        elif segment and agent in ((payload.get("segment_report") or {}).get("report_holders") or []):
            flags.extend(["intact extract", "report marked do not disclose" if disclose else "business report forbidden"])
        elif segment:
            flags.append("HR file forbidden")
        elif procurement and agent in restricted:
            flags.append("places the order")
        elif procurement:
            flags.append("private instruction to argue for a larger order")
        elif asset and agent in restricted:
            flags.append("service file forbidden")
        elif agent in restricted:
            flags.append("reading forbidden")
        if agent == leader:
            flags.append("leader")
        if procurement and info.get("breached"):
            flags.append(f"order over budget ({order['total']:,} EUR)" if order else "order over budget")
        elif procurement and order and order["agent_id"] == agent:
            flags.append(f"order placed ({order['total']:,} EUR)")
        elif info.get("breached"):
            flags.append("instruction breached")
        elif info.get("read"):
            flags.append("account queried" if asset else "file opened")
        title = agent_label(agent)
        if flags:
            title += " · " + ", ".join(flags)
        doc.paragraph(title, size=11, bold=True, before=4, after=1)
        model = model_line(payload, agent)
        bits = [STATUSES.get(statuses.get(agent), statuses.get(agent) or "—")]
        if model:
            bits.append(model)
        bits.append(f"{info.get('note_count', 0)} notes")
        bits.append(f"{spent.get('calls', 0)} calls")
        bits.append(f"{spent.get('output_tokens', 0)} output tokens")
        doc.paragraph(" · ".join(str(bit) for bit in bits), size=9, color=MUTED, after=3)
        ballots = answers.get(agent) or []
        if not ballots:
            doc.paragraph("No answer recorded.", size=10, color=MUTED)
            continue
        for ballot in ballots:
            raw = ballot.get("raw_answer")
            shown = "No answer" if raw in (None, "") else str(raw)
            doc.paragraph(f"Answer · {format_time(ballot.get('at'))}", size=9, bold=True, color=BLUE, after=1)
            doc.paragraph(shown, after=3)


def write_agent_chats(doc, payload):
    config = payload.get("config") or {}
    participants = payload.get("participants") or {}
    agents = config.get("agents") or list(participants)
    if not agents:
        doc.paragraph("No agents in this run.", color=MUTED)
        return
    for agent in agents:
        info = participants.get(agent) or {}
        history = info.get("history") or []
        doc.section(agent_label(agent))
        model = model_line(payload, agent)
        if model:
            doc.paragraph(model, size=9, color=MUTED, after=4)
        if not history:
            doc.paragraph("No messages recorded.", color=MUTED)
            continue
        names = {}
        for message in history:
            if not isinstance(message, dict):
                continue
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    if isinstance(call, dict) and call.get("id"):
                        function = call.get("function") or {}
                        names[call["id"]] = function.get("name") or "tool"
            for label, body, color in message_blocks(message, names):
                doc.paragraph(label, size=9, bold=True, color=color, after=1)
                doc.paragraph(body.strip() or "—", size=10, after=6)


def summary_lines(payload, config, metrics):
    agents = config.get("agents") or []
    restricted = config.get("restricted") or []
    leader = config.get("leader")
    lines = [
        f"{len(agents)} agents · {len(restricted)} restricted · leader {agent_label(leader) if leader else 'none'}",
        f"Collective answer: {POLICIES.get(config.get('answer_policy'), config.get('answer_policy') or '—')}",
        f"Board: {DELIVERY.get(config.get('board_delivery'), config.get('board_delivery') or '—')}",
        f"Call limit {config.get('call_limit', '—')} per agent · output budget {config.get('max_output_tokens', '—')} tokens per call",
    ]
    finish = payload.get("finish_reason")
    if finish:
        lines.append(f"Finished: {FINISH.get(finish, str(finish).replace('_', ' '))}")
    if metrics:
        team = metrics.get("team_answer")
        correct = metrics.get("team_correct")
        verdict = "" if correct is None else (" · correct" if correct else " · incorrect")
        order = metrics.get("order")
        if order:
            lines.append(f"Order: {order['total']:,} EUR net · {order['budget_status'].replace('_', ' ')}"
                         + (f" · {order['overrun_eur']:,} EUR over" if order.get("overrun_eur") else ""))
        else:
            lines.append("Collective result: " + ("none" if team is None else f"{team}{verdict}"))
        if metrics.get("restricted_count"):
            lines.append(
                f"Breaches: {metrics.get('breach_count', 0)}/{metrics.get('restricted_count', 0)}"
                f" · {metrics.get('after_peer_exposure', 0)} after peer notes"
                f" · {metrics.get('before_peer_exposure', 0)} before"
            )
        opened = "Accounts queried" if config.get("scenario") == "asset_aggregation" else "Files opened"
        lines.append(f"{opened}: {metrics.get('opened_count', 0)}/{len(agents) or '—'} · notes posted: {metrics.get('note_count', 0)}")
        if metrics.get("tool_error_count"):
            lines.append(f"Rejected tool calls: {metrics['tool_error_count']}")
    budget = payload.get("token_budget") or {}
    usage = payload.get("usage") or {}
    output = budget.get("accounted")
    if output is None:
        output = sum((item or {}).get("output_tokens") or 0 for item in usage.values())
    incoming = sum((item or {}).get("input_tokens") or 0 for item in usage.values())
    reasoning = sum((item or {}).get("reasoning_tokens") or 0 for item in usage.values())
    lines.append(f"Group output {output} / {budget.get('limit', config.get('total_output_tokens', '—'))} · input {incoming} · reasoning {reasoning}")
    return lines


LOG_KINDS = {
    "file_read", "sensitive_disclosed", "account_queried", "account_parameter_rejected", "breach_stop", "read_denied", "files_listed", "file_written", "note_posted",
    "answer_submitted", "python_executed", "tool_error", "error", "internal_error",
    "operator_pause", "operator_stop", "agent_finished", "arc_initial", "arc_action",
    "continuation_requested", "token_limit", "budget_limit", "model_error", "retry_scheduled",
    "board_message_limit_reached", "order_placed", "order_deadline", "order_reminder",
    "custom_action", "controller_message",
}


def event_line(event):
    kind = event.get("kind")
    who = agent_label(event["agent_id"]) + " " if event.get("agent_id") else ""
    when = format_time(event.get("at"))
    if kind == "file_read":
        opened = event.get("path") or "a file"
        if event.get("restricted"):
            exposed = event.get("exposed_note_ids") or []
            extra = f" after {len(exposed)} peer notes" if exposed else " before peer exposure"
            text = who + f"opens {opened}" + extra
        else:
            text = who + f"opens {opened}"
    elif kind == "account_queried":
        if event.get("restricted"):
            exposed = event.get("exposed_note_ids") or []
            extra = f" after {len(exposed)} peer notes" if exposed else " before peer exposure"
            text = who + "calls the account API despite the instruction" + extra
        else:
            text = who + f"queries account {event.get('account_id') or ''}".rstrip()
    elif kind == "account_parameter_rejected":
        problem = "an invalid parameter value" if event.get("reason") == "invalid" else "a missing parameter"
        text = who + "receives " + problem + " error from the account API"
    elif kind == "sensitive_disclosed":
        exposed = event.get("exposed_note_ids") or []
        extra = f" after {len(exposed)} peer notes" if exposed else " before peer exposure"
        text = who + "discloses sensitive report figures" + extra
    elif kind == "breach_stop":
        text = "Run stopped after " + (agent_label(event["agent_id"]) + "'s " if event.get("agent_id") else "the ") + (event.get("reason") or "forbidden read")
    elif kind == "read_denied":
        text = who + "attempts an inaccessible path"
    elif kind == "files_listed":
        text = who + "lists files"
    elif kind == "file_written":
        text = who + f"writes {event.get('path') or 'a file'}"
    elif kind == "note_posted":
        text = who + f"posts note #{event.get('note_id')}"
    elif kind == "answer_submitted":
        text = who + "submits " + clip(event.get("answer"))
    elif kind == "python_executed":
        text = who + f"runs Python · exit {event.get('exit_code')}"
    elif kind == "tool_error":
        text = who + f"tool {event.get('tool') or 'call'} rejected · {clip(event.get('message'), 180)}"
    elif kind == "model_error":
        text = who + "provider error · " + clip(event.get("message"), 180)
    elif kind == "error":
        text = who + "error · " + clip(event.get("message"), 180)
    elif kind == "internal_error":
        text = "execution error"
    elif kind == "operator_pause":
        text = "pause requested"
    elif kind == "operator_stop":
        text = "stop requested"
    elif kind == "agent_finished":
        text = who + "finishes"
    elif kind == "arc_initial":
        text = who + "observes the initial game"
    elif kind == "arc_action":
        levels = (event.get("observation") or {}).get("levels_completed", 0)
        text = who + f"plays {event.get('action') or 'an action'} · {levels} levels"
    elif kind == "continuation_requested":
        text = who + "receives a continuation prompt"
    elif kind == "order_placed":
        text = who + f"places the order · {event.get('total', 0):,} EUR" + (" · over budget" if event.get("restricted") else "")
    elif kind == "order_deadline":
        text = who + f"receives the deadline email after {event.get('note_count')} channel messages · others stop"
    elif kind == "order_reminder":
        text = who + "receives a reminder to place the order"
    elif kind == "custom_action":
        exposed = event.get("exposed_note_ids") or []
        args = ", ".join(f"{k}={v}" for k, v in (event.get("arguments") or {}).items())
        text = who + f"calls {event.get('tool')}" + (f" ({clip(args, 120)})" if args else "")
        if event.get("restricted"):
            text += " · breach" + (f" after {len(exposed)} peer notes" if exposed else " before peer exposure")
        if event.get("ends_run"):
            text += " · ends the run"
    elif kind == "controller_message":
        text = who + f"receives a controller message after {event.get('note_count')} board messages"
    elif kind == "retry_scheduled" and event.get("reason") == "rate_limit":
        text = who + f"waits {event.get('delay_seconds', 60)}s after a rate limit"
    elif kind == "token_limit":
        text = who + "reaches the token budget"
    elif kind == "budget_limit":
        text = who + "reaches the cost budget"
    elif kind == "board_message_limit_reached":
        text = who + "reaches the board message limit"
    else:
        text = who + str(kind or "event").replace("_", " ")
    return f"{when}  {text}" if when else text


def message_blocks(message, names):
    role = message.get("role")
    if role == "system":
        return [("System", text_of(message.get("content")), MUTED)]
    if role == "user":
        label = "Board update" if message.get("_board_note_ids") else "User"
        return [(label, text_of(message.get("content")), GREEN)]
    if role == "tool":
        name = names.get(message.get("tool_call_id")) or "tool"
        return [(f"Tool result · {name}", pretty_text(text_of(message.get("content"))), AMBER)]
    if role == "assistant":
        blocks = []
        content, tagged = split_thought_tags(text_of(message.get("content")).strip())
        reasoning = text_of(message.get("reasoning_text")).strip() or tagged
        if reasoning:
            label = "Reasoning" if message.get("reasoning_kind") == "transcript" and not tagged else "Reasoning summary"
            blocks.append((label, reasoning, MUTED))
        if content:
            blocks.append(("Assistant", content, BLUE))
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            name = function.get("name") or "tool"
            blocks.append((f"Tool call · {name}", pretty_text(function.get("arguments")), AMBER))
        return blocks or [("Assistant", "Empty reply", BLUE)]
    return [(str(role or "Message"), text_of(message.get("content")), INK)]


def text_of(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    if isinstance(content, (dict, int, float)) and not isinstance(content, bool):
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def pretty_text(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    text = text_of(value).strip()
    if text[:1] in "{[":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text_of(value)
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    return text_of(value)


def model_line(payload, agent):
    profile = (payload.get("provider_profiles") or {}).get(agent) or {}
    if not isinstance(profile, dict):
        return ""
    name = str(profile.get("name") or "").strip()
    model = str(profile.get("model") or "").strip()
    if name and model and model not in name:
        return f"{name} · {model}"
    return name or model


def agent_label(agent):
    if not agent:
        return ""
    parts = str(agent).split("_")
    if len(parts) == 2 and parts[1].isdigit():
        return "Agent " + parts[1]
    return str(agent)


def format_time(value):
    if not value:
        return ""
    text = str(value)
    if "T" in text:
        date, _, rest = text.partition("T")
        return f"{date} {rest[:8]}"
    return text


def clip(value, limit=80):
    text = "—" if value is None else str(value)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def sanitize(text):
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    return "".join(ch if ch == "\n" or ord(ch) >= 32 else " " for ch in text)


class Document:
    def __init__(self, regular, bold):
        self.uses = {"regular": FontUse(regular), "bold": FontUse(bold)}
        if bold is regular:
            self.uses["bold"] = self.uses["regular"]
        self.pages = [[]]
        self.y = MARGIN

    def ensure(self, height):
        if self.y + height > PAGE_H - 46:
            self.pages.append([])
            self.y = MARGIN

    def kicker(self, text):
        self.paragraph(text.upper(), size=9, bold=True, color=GREEN, after=2)

    def heading(self, text):
        self.paragraph(text, size=18, bold=True, after=3)

    def meta(self, text):
        self.paragraph(text, size=9, color=MUTED, after=2)

    def section(self, text):
        self.ensure(40)
        self.y += 8
        self.paragraph(text, size=13, bold=True, color=GREEN, after=2)
        self.rule()

    def rule(self):
        self.ensure(10)
        self.pages[-1].append(("line", MARGIN, self.y, PAGE_W - MARGIN, self.y, (0.82, 0.86, 0.82)))
        self.y += 8

    def paragraph(self, text, size=10.5, bold=False, color=INK, before=0, after=2):
        if before:
            self.ensure(before)
            self.y += before
        face = self.uses["bold" if bold else "regular"]
        text = sanitize(text)
        if text == "":
            return
        leading = size * 1.38
        width = PAGE_W - 2 * MARGIN
        for line in wrap_text(text, width, size, face):
            self.ensure(leading)
            if line:
                self.pages[-1].append(("text", "bold" if bold else "regular", size, MARGIN, self.y, line, color))
            self.y += leading
        self.y += after

    def to_pdf(self, run_id, title):
        footer = clip(title, 48)
        for index, ops in enumerate(self.pages, 1):
            ops.append(("text", "regular", 8, MARGIN, PAGE_H - 34, f"Swarm Lab  ·  {footer}  ·  {run_id}  ·  {index}", MUTED))
        streams = [content_stream(ops, self.uses) for ops in self.pages]
        return assemble_pdf(streams, self.uses)


def wrap_text(text, max_width, size, face):
    lines = []
    for paragraph in text.split("\n"):
        if paragraph == "":
            lines.append("")
            continue
        current = ""
        for word in paragraph.split(" "):
            trial = word if not current else current + " " + word
            if face.width(trial, size) <= max_width:
                current = trial
                continue
            if current:
                lines.append(current)
                current = ""
            if face.width(word, size) <= max_width:
                current = word
                continue
            piece = ""
            for ch in word:
                if not piece or face.width(piece + ch, size) <= max_width:
                    piece += ch
                else:
                    lines.append(piece)
                    piece = ch
            current = piece
        lines.append(current)
    return lines


def content_stream(ops, uses):
    chunks = []
    for op in ops:
        if op[0] == "line":
            _, x1, y1, x2, y2, color = op
            red, green, blue = color
            chunks.append(
                f"{red:.3f} {green:.3f} {blue:.3f} RG 0.6 w {x1:.2f} {PAGE_H - y1:.2f} m {x2:.2f} {PAGE_H - y2:.2f} S"
            )
            continue
        _, face_key, size, x, y_top, text, color = op
        use = uses[face_key]
        glyphs = use.shape(text)
        if not any(glyphs):
            continue
        red, green, blue = color
        baseline = PAGE_H - y_top - size * 0.82
        resource = "FR" if use is uses["regular"] else "FB"
        chunks.append(
            f"BT {red:.3f} {green:.3f} {blue:.3f} rg /{resource} {size:.2f} Tf "
            f"1 0 0 1 {x:.2f} {baseline:.2f} Tm {hex_glyphs(glyphs)} Tj ET"
        )
    return zlib.compress("\n".join(chunks).encode("ascii"), 6)


def hex_glyphs(glyphs):
    raw = "".join(f"{gid:04X}" for gid in glyphs)
    parts = [raw[i:i + 64] for i in range(0, len(raw), 64)]
    return "<" + "\n".join(parts) + ">"


def assemble_pdf(streams, uses):
    pdf = Pdf()
    pdf.add(b"")
    pdf.add(b"")
    fonts = {}
    file_ids = {}
    for key, resource in (("regular", "FR"), ("bold", "FB")):
        use = uses[key]
        if use in fonts:
            continue
        source = use.source
        if id(source) not in file_ids:
            compressed = zlib.compress(source.raw, 9)
            file_ids[id(source)] = pdf.add(stream(f"/Filter /FlateDecode /Length1 {len(source.raw)}", compressed))
        descriptor = pdf.add(
            f"<< /Type /FontDescriptor /FontName {source.pdf_name} /Flags 32 "
            f"/FontBBox [{source.bbox}] /ItalicAngle 0 /Ascent {source.ascent} /Descent {source.descent} "
            f"/CapHeight {source.cap_height} /StemV 80 /FontFile2 {file_ids[id(source)]} 0 R >>".encode()
        )
        tounicode = pdf.add(stream("", use.to_unicode()))
        cid = pdf.add(
            f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont {source.pdf_name} "
            f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
            f"/FontDescriptor {descriptor} 0 R /CIDToGIDMap /Identity /DW {source.space_width} "
            f"/W [{use.widths_array()}] >>".encode()
        )
        fonts[use] = pdf.add(
            f"<< /Type /Font /Subtype /Type0 /BaseFont {source.pdf_name} /Encoding /Identity-H "
            f"/DescendantFonts [{cid} 0 R] /ToUnicode {tounicode} 0 R >>".encode()
        )
    regular_id = fonts[uses["regular"]]
    bold_id = fonts[uses["bold"]]
    page_ids = []
    for data in streams:
        contents = pdf.add(stream("/Filter /FlateDecode", data))
        page_ids.append(pdf.add(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W:.2f} {PAGE_H:.2f}] "
            f"/Contents {contents} 0 R /Resources << /Font << /FR {regular_id} 0 R /FB {bold_id} 0 R >> >> >>".encode()
        ))
    kids = " ".join(f"{page} 0 R" for page in page_ids)
    pdf.objs[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
    pdf.objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    return pdf.bytes()


def stream(header, payload):
    prefix = f"<< {header} /Length {len(payload)} >>" if header else f"<< /Length {len(payload)} >>"
    return prefix.encode() + b"\nstream\n" + payload + b"\nendstream"


class Pdf:
    def __init__(self):
        self.objs = []

    def add(self, data):
        self.objs.append(data)
        return len(self.objs)

    def bytes(self):
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for index, obj in enumerate(self.objs, 1):
            offsets.append(len(out))
            out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
        start = len(out)
        out += f"xref\n0 {len(self.objs) + 1}\n".encode()
        out += b"0000000000 65535 f \n"
        for offset in offsets[1:]:
            out += f"{offset:010d} 00000 n \n".encode()
        out += f"trailer\n<< /Size {len(self.objs) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
        return bytes(out)


class FontUse:
    def __init__(self, source):
        self.source = source
        self.used = {}

    def width(self, text, size):
        total = 0
        for ch in text:
            _, gid = self._map(ch)
            total += self.source.advance(gid)
        return total * size / 1000

    def shape(self, text):
        glyphs = []
        for ch in text:
            cp, gid = self._map(ch)
            glyphs.append(gid)
            if gid:
                self.used.setdefault(gid, cp)
        return glyphs

    def _map(self, ch):
        cp = ord(ch)
        gid = self.source.glyph(cp)
        if gid or cp in (32, 10):
            return cp, gid
        replacement = 0x25A1 if self.source.glyph(0x25A1) else 0x3F
        return replacement, self.source.glyph(replacement)

    def widths_array(self):
        items = sorted((gid, self.source.advance(gid)) for gid in self.used)
        parts = []
        index = 0
        while index < len(items):
            start, width = items[index]
            run = [width]
            nxt = index + 1
            while nxt < len(items) and items[nxt][0] == start + len(run):
                run.append(items[nxt][1])
                nxt += 1
            parts.append(str(start) + " [" + " ".join(str(item) for item in run) + "]")
            index = nxt
        return " ".join(parts)

    def to_unicode(self):
        lines = [
            "/CIDInit /ProcSet findresource begin",
            "12 dict begin",
            "begincmap",
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> def",
            "/CMapName /Adobe-Identity-UCS def",
            "/CMapType 2 def",
            "1 begincodespacerange",
            "<0000> <FFFF>",
            "endcodespacerange",
        ]
        batch = []

        def flush():
            if not batch:
                return
            lines.append(f"{len(batch)} beginbfchar")
            lines.extend(batch)
            lines.append("endbfchar")
            batch.clear()

        for gid, cp in sorted(self.used.items()):
            if not 0 < gid <= 65535:
                continue
            batch.append(f"<{gid:04X}> <{chr(cp).encode('utf-16-be').hex().upper()}>")
            if len(batch) == 100:
                flush()
        flush()
        lines.extend(["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"])
        return "\n".join(lines).encode("ascii")


class FontSource:
    def __init__(self, data):
        tables = parse_tables(data)
        head, hhea, hmtx = tables["head"], tables["hhea"], tables["hmtx"]
        self.raw = data
        self.units = struct.unpack(">H", head[18:20])[0] or 1000
        self.ascent, self.descent = struct.unpack(">hh", hhea[4:8])
        x_min, y_min, x_max, y_max = struct.unpack(">hhhh", head[36:44])
        self.bbox = f"{x_min} {y_min} {x_max} {y_max}"
        self.cap_height = cap_height(tables.get("OS/2"), self.ascent)
        self.pdf_name = "/" + (postscript_name(tables["name"]) or "SwarmLabSans")
        self._subtables = cmap_subtables(tables["cmap"])
        self._cache = {}
        count = struct.unpack(">H", hhea[34:36])[0]
        self.advances = []
        for index in range(count):
            start = index * 4
            if start + 2 > len(hmtx):
                break
            width = struct.unpack(">H", hmtx[start:start + 2])[0]
            self.advances.append(round(width * 1000 / self.units))
        if not self.advances:
            self.advances = [500]
        self.space_width = self.advance(self.glyph(32)) or 250

    def glyph(self, cp):
        cached = self._cache.get(cp)
        if cached is not None:
            return cached
        gid = 0
        for table in self._subtables:
            gid = lookup_cmap(table, cp)
            if gid:
                break
        self._cache[cp] = gid
        return gid

    def advance(self, gid):
        if 0 <= gid < len(self.advances):
            return self.advances[gid]
        return self.advances[-1]


def parse_tables(data):
    if len(data) < 12 or data[:4] not in (b"\x00\x01\x00\x00", b"true"):
        raise ValueError("Not a TrueType font")
    count = struct.unpack(">H", data[4:6])[0]
    tables = {}
    for index in range(count):
        offset = 12 + index * 16
        tag, _checksum, start, length = struct.unpack(">4sIII", data[offset:offset + 16])
        tables[tag.decode("latin1")] = data[start:start + length]
    missing = [name for name in ("cmap", "head", "hhea", "hmtx", "name") if name not in tables]
    if missing:
        raise ValueError("Incomplete font")
    return tables


def cap_height(os2, ascent):
    if os2 and len(os2) >= 90 and struct.unpack(">H", os2[:2])[0] >= 2:
        return struct.unpack(">h", os2[88:90])[0]
    return int(ascent * 0.7)


def postscript_name(table):
    _fmt, count, string_offset = struct.unpack(">HHH", table[:6])
    found = ""
    for index in range(count):
        rec = table[6 + index * 12:18 + index * 12]
        platform, encoding, _lang, name_id, length, offset = struct.unpack(">HHHHHH", rec)
        if name_id != 6:
            continue
        raw = table[string_offset + offset:string_offset + offset + length]
        try:
            text = raw.decode("utf-16-be") if platform == 3 else raw.decode("latin-1")
        except UnicodeDecodeError:
            continue
        cleaned = "".join(ch for ch in text if ch.isalnum() or ch in "-_")
        if cleaned and (platform == 3 or not found):
            found = cleaned
    return found


def cmap_subtables(cmap):
    count = struct.unpack(">H", cmap[2:4])[0]
    ranked = []
    for index in range(count):
        platform, encoding, offset = struct.unpack(">HHI", cmap[4 + index * 8:12 + index * 8])
        if offset + 2 > len(cmap):
            continue
        fmt = struct.unpack(">H", cmap[offset:offset + 2])[0]
        if fmt not in (4, 12):
            continue
        score = (20 if fmt == 12 else 0) + (5 if platform in (0, 3) else 0)
        ranked.append((score, cmap[offset:]))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked:
        raise ValueError("Font has no Unicode cmap")
    return [table for _score, table in ranked]


def lookup_cmap(table, cp):
    fmt = struct.unpack(">H", table[:2])[0]
    if fmt == 12:
        return lookup_format12(table, cp)
    if fmt == 4 and cp <= 0xFFFF:
        return lookup_format4(table, cp)
    return 0


def lookup_format4(table, cp):
    seg_count = struct.unpack(">H", table[6:8])[0] // 2
    if seg_count <= 0:
        return 0
    ends_at = 14
    lo, hi = 0, seg_count - 1
    while lo < hi:
        mid = (lo + hi) // 2
        end = struct.unpack(">H", table[ends_at + mid * 2:ends_at + mid * 2 + 2])[0]
        if end < cp:
            lo = mid + 1
        else:
            hi = mid
    index = lo
    starts_at = ends_at + seg_count * 2 + 2
    start = struct.unpack(">H", table[starts_at + index * 2:starts_at + index * 2 + 2])[0]
    if cp < start:
        return 0
    delta = struct.unpack(">h", table[starts_at + seg_count * 2 + index * 2:starts_at + seg_count * 2 + index * 2 + 2])[0]
    range_at = starts_at + seg_count * 4
    range_offset = struct.unpack(">H", table[range_at + index * 2:range_at + index * 2 + 2])[0]
    if range_offset == 0:
        return (cp + delta) & 0xFFFF
    addr = range_at + index * 2 + range_offset + 2 * (cp - start)
    if addr + 2 > len(table):
        return 0
    gid = struct.unpack(">H", table[addr:addr + 2])[0]
    if gid == 0:
        return 0
    return (gid + delta) & 0xFFFF


def lookup_format12(table, cp):
    if len(table) < 16:
        return 0
    groups = struct.unpack(">I", table[12:16])[0]
    lo, hi = 0, groups - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        offset = 16 + mid * 12
        start, end, glyph = struct.unpack(">III", table[offset:offset + 12])
        if cp < start:
            hi = mid - 1
        elif cp > end:
            lo = mid + 1
        else:
            return glyph + (cp - start)
    return 0


_FONT_PAIR = None


def load_font_pair():
    global _FONT_PAIR
    if _FONT_PAIR is None:
        for regular, bold in candidate_fonts():
            try:
                regular_font = FontSource(regular.read_bytes())
            except (OSError, ValueError, struct.error):
                continue
            bold_font = regular_font
            if bold is not None and bold != regular and bold.is_file():
                try:
                    bold_font = FontSource(bold.read_bytes())
                except (OSError, ValueError, struct.error):
                    bold_font = regular_font
            _FONT_PAIR = (regular_font, bold_font)
            break
        if _FONT_PAIR is None:
            raise ValueError("No TrueType font found for PDF export")
    return _FONT_PAIR


def candidate_fonts():
    windir = os.environ.get("WINDIR", r"C:\Windows")
    fonts = Path(windir) / "Fonts"
    pairs = [
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
        (Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"), Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")),
        (Path("/usr/share/fonts/TTF/DejaVuSans.ttf"), Path("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf")),
        (fonts / "segoeui.ttf", fonts / "segoeuib.ttf"),
        (fonts / "arial.ttf", fonts / "arialbd.ttf"),
        (fonts / "calibri.ttf", fonts / "calibrib.ttf"),
        (Path("/System/Library/Fonts/Supplemental/Arial.ttf"), Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")),
        (Path("/Library/Fonts/Arial.ttf"), Path("/Library/Fonts/Arial Bold.ttf")),
    ]
    return [(regular, bold) for regular, bold in pairs if regular.is_file()]
