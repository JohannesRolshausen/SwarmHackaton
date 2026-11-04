"""Thinking of the models in the evaluation plan: does a request ask for it, did the run store it, does the
Thinking tab show it. Used by the fake-answer tests (tests/test_thinking_providers.py) and by the smoke test
with real calls (scripts/smoke_thinking.py). Without thinking text the test words of the leader cannot be read."""
import json
import re
import shutil
import subprocess
from urllib.parse import urlparse

from .common import ROOT
from .providers import GEMINI_HOST

APP_JS = ROOT / "swarm_bench" / "web" / "app.js"

# The models of the plan with a profile that asks for their thinking. A saved profile with the same model id
# wins in the smoke test, since that is the profile the runs use.
PLAN = (
    {"label": "GPT-6 Astra", "provider": "OpenAI Responses API",
     "profile": {"id": "plan-gpt-6-astra", "name": "OpenAI · GPT-6 Astra", "kind": "openai_responses",
                 "base_url": "https://api.openai.com/v1", "model": "gpt-6-astra", "token_parameter": "max_output_tokens",
                 "reasoning_effort": "", "service_tier": "", "key_env": "OPENAI_API_KEY"}},
    {"label": "Gemini 4 Argon", "provider": "Gemini (OpenAI layer)",
     "profile": {"id": "plan-gemini-4-argon", "name": "Gemini 4 Argon", "kind": "openai_compatible",
                 "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-4-argon",
                 "token_parameter": "max_tokens", "reasoning_effort": "", "service_tier": "", "key_env": "GEMINI_API_KEY"}},
    {"label": "Gemini 3.8 Flash", "provider": "Gemini (OpenAI layer)",
     "profile": {"id": "plan-gemini-3-8-flash", "name": "Gemini 3.8 Flash", "kind": "openai_compatible",
                 "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-3.8-flash",
                 "token_parameter": "max_tokens", "reasoning_effort": "", "service_tier": "", "key_env": "GEMINI_API_KEY"}},
    {"label": "MiMo V2.6 Pro", "provider": "OpenRouter",
     "profile": {"id": "plan-mimo-v2-6-pro", "name": "MiMo V2.6 Pro · OpenRouter", "kind": "openai_compatible",
                 "base_url": "https://openrouter.ai/api/v1", "model": "xiaomi/mimo-v2.6-pro", "token_parameter": "max_tokens",
                 # OpenRouter turns reasoning on through the reasoning field. Without an effort nothing is sent.
                 "reasoning_effort": "medium", "service_tier": "", "key_env": "OPENROUTER_API_KEY"}},
)


def thinking_request(profile, body):
    """(asks, fields, hint): does this request body ask the provider for readable thinking, the fields that
    say so, and what to change in the profile if it does not."""
    body = body if isinstance(body, dict) else {}
    host = urlparse(profile.get("base_url") or "").hostname
    if profile.get("kind") == "openai_responses":
        fields = {k: body[k] for k in ("reasoning", "include") if k in body}
        asks = (body.get("reasoning") or {}).get("summary") in ("auto", "concise", "detailed")
        return asks, fields, "" if asks else "Reasoning is Off, so no summary is requested. Choose an effort."
    if profile.get("kind") == "anthropic":
        fields = {k: body[k] for k in ("thinking", "output_config") if k in body}
        asks = (body.get("thinking") or {}).get("display") == "summarized"
        return asks, fields, "" if asks else "Without an effort Claude returns empty thinking. Choose an effort."
    if host == GEMINI_HOST:
        fields = {k: body[k] for k in ("extra_body", "reasoning_effort") if k in body}
        config = ((body.get("extra_body") or {}).get("google") or {}).get("thinking_config") or {}
        asks = config.get("include_thoughts") is True
        return asks, fields, "" if asks else "Reasoning is Off, so Gemini sends no thought summary."
    if host == "openrouter.ai":
        fields = {k: body[k] for k in ("reasoning",) if k in body}
        reasoning = body.get("reasoning") or {}
        asks = bool(reasoning) and reasoning.get("exclude") is not True and reasoning.get("effort") != "none"
        return asks, fields, "" if asks else ("No reasoning field is sent without an effort, and a model such as "
                                              "MiMo may then not think. Choose an effort in the profile.")
    fields = {k: body[k] for k in ("reasoning_effort", "reasoning") if k in body}
    if body.get("reasoning_effort") == "none":
        return False, fields, "Reasoning is Off. Choose an effort."
    return bool(fields), fields, "" if fields else "No thinking field is sent to this server. Choose an effort."


def wire_requests(path):
    """Request bodies as sent, from a run's wire_log.md: [(request id, body)]. Keys there are redacted."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    found = []
    for block in re.finditer(r"^## → (\S+) · request\n(.*?)(?=^## [→←] |\Z)", text, re.M | re.S):
        fence = re.search(r"^(`{3,})json\n(.*?)\n\1$", block.group(2), re.M | re.S)
        if fence:
            try:
                found.append((block.group(1), json.loads(fence.group(2))))
            except ValueError:
                continue
    return found


def stored_thoughts(history):
    """(replies, [(reply number, kind, text)]) of an agent: the thinking text each assistant reply kept."""
    replies = [m for m in history or () if isinstance(m, dict) and m.get("role") == "assistant"]
    thoughts = [(n, m.get("reasoning_kind") or "", m["reasoning_text"]) for n, m in enumerate(replies, 1)
                if isinstance(m.get("reasoning_text"), str) and m["reasoning_text"].strip()]
    return len(replies), thoughts


def _function_source(lines, name):
    start = next(i for i, line in enumerate(lines) if line.startswith(f"function {name}("))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


def thinking_tab_html(detail):
    """The Thinking tab of one agent as the interface draws it: renderThinking() of app.js, run in Node on the
    agent detail that /api/runs/<run>/agents/<agent> returns. None when Node is not installed."""
    node = shutil.which("node")
    if not node:
        return None
    lines = APP_JS.read_text(encoding="utf-8").splitlines()
    parts = [next(line for line in lines if line.startswith("const esc ="))]
    parts += [_function_source(lines, name) for name in ("splitThoughtTags", "replyThought", "renderThinking")]
    script = "\n".join(parts) + ("\nlet currentRun={};\n"
                                 "const detail=JSON.parse(require('fs').readFileSync(0,'utf8'));\n"
                                 "process.stdout.write(renderThinking(detail));\n")
    result = subprocess.run([node, "-e", script], input=json.dumps(detail), capture_output=True, text=True,
                            encoding="utf-8", timeout=60, check=True)
    return result.stdout
