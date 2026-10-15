"""Provider adapters. No default credentials, model IDs or network calls at import time."""
import copy
import json
import os
import re
import threading
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .common import read_json, write_json


class ProviderError(Exception):
    def __init__(self, message, usage=None, retryable=False, diagnostic=None):
        super().__init__(message)
        self.usage = usage
        self.retryable = retryable
        self.diagnostic = diagnostic or {}


def response_diagnostic(data):
    """Structural evidence only: never log prompts, reasoning or credentials."""
    if not isinstance(data, dict):
        return {"body_type": type(data).__name__}
    choices = data.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else None
    return {"has_error": bool(data.get("error")), "choices_type": type(choices).__name__,
            "choice_count": len(choices) if isinstance(choices, list) else None,
            "message_type": type(first.get("message")).__name__ if isinstance(first, dict) else None}


def check_provider_error(data, usage=None):
    error = data.get("error") if isinstance(data, dict) else None
    choices = data.get("choices") if isinstance(data, dict) else None
    if not error and isinstance(choices, list) and choices and isinstance(choices[0], dict):
        error = choices[0].get("error")
    if error:
        code = error.get("code") if isinstance(error, dict) else None
        code = code if isinstance(code, int) else None
        raise ProviderError(f"Provider error in JSON (code {code or 'unspecified'}).",
                            usage=usage, retryable=code in (408, 429, 500, 502, 503, 504),
                            diagnostic={**response_diagnostic(data), "provider_error_code": code})


def thinking_always_on(model):
    """Google: reasoning cannot be turned off for Gemini 2.5 Pro or Gemini 3 models."""
    return re.match(r"(models/|google/)?gemini-(3|2\.5-pro)", model or "") is not None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward an API credential to a redirected origin.
        raise HTTPError(req.full_url, code, "Provider redirect refused", headers, fp)


def load_local_env(path):
    """Read KEY=value lines into this process. Existing variables win. Values are never logged."""
    path = Path(path)
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,99}", name) or name in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if value and len(value) <= 2000:
            os.environ[name] = value


class ProviderRegistry:
    def __init__(self, path):
        self.path = Path(path)
        self.secrets_path = self.path.with_name(".provider-secrets.json")
        self.lock = threading.RLock()
        self.secrets = self._read_secrets()
        self.profiles = read_json(self.path) if self.path.exists() else []

    def _read_secrets(self):
        """Local keys for this installation. Absent from profiles, API responses, and exports."""
        leftover = self.secrets_path.with_name(".provider-secrets.json.tmp")
        try:
            leftover.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        if not self.secrets_path.exists():
            return {}
        try:
            data = read_json(self.secrets_path)
        except (OSError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        loaded = {}
        for key, value in data.items():
            if (isinstance(key, str) and isinstance(value, str) and value.strip()
                    and re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", key) and len(value.strip()) <= 2000):
                loaded[key] = value.strip()
        return loaded

    def _write_secrets(self):
        payload = {k: v for k, v in self.secrets.items() if isinstance(v, str) and v}
        if not payload:
            try:
                self.secrets_path.unlink()
            except FileNotFoundError:
                pass
            return
        write_json(self.secrets_path, payload)
        try:
            self.secrets_path.chmod(0o600)
        except OSError:
            pass

    def _key(self, profile):
        return self.secrets.get(profile["id"], "") or os.environ.get(profile.get("key_env", ""), "")

    def public(self):
        with self.lock:
            return [{**copy.deepcopy(p), "key_present": bool(self._key(p))} for p in self.profiles]

    def save(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Invalid profile")
        fields = {"id", "name", "kind", "base_url", "model", "key_env", "token_parameter", "reasoning_effort", "api_key", "clear_key"}
        if set(payload) - fields:
            raise ValueError("Unknown profile field")
        profile = {k: str(payload.get(k, "")).strip() for k in fields - {"api_key", "clear_key"}}
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", profile["id"]):
            raise ValueError("Invalid profile ID")
        if not profile["name"] or len(profile["name"]) > 100 or len(profile["model"]) > 200:
            raise ValueError("Invalid profile or model name")
        if profile["kind"] not in ("openai_compatible", "openai_responses", "anthropic"):
            raise ValueError("Unknown provider format")
        if profile["reasoning_effort"] not in ("", "none", "low", "medium", "high", "xhigh", "max"):
            raise ValueError("Invalid reasoning setting")
        if profile["kind"] == "anthropic" and profile["reasoning_effort"]:
            raise ValueError("This reasoning setting uses the OpenAI-compatible format")
        if profile["reasoning_effort"] == "none" and thinking_always_on(profile["model"]):
            raise ValueError("This Gemini model cannot turn thinking off. Choose Low, Medium or High")
        if profile["key_env"] and not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,99}", profile["key_env"]):
            raise ValueError("Invalid key environment variable name")
        if profile["base_url"]:
            u = urlparse(profile["base_url"])
            if not u.hostname or u.username or u.password or u.query or u.fragment:
                raise ValueError("Invalid base URL")
            if u.scheme != "https" and not (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1")):
                raise ValueError("HTTPS required except for a local server")
        profile["base_url"] = profile["base_url"].rstrip("/")
        profile["token_parameter"] = ("max_output_tokens" if profile["kind"] == "openai_responses"
                                      else profile["token_parameter"] or "max_tokens")
        allowed_token_parameters = ("max_output_tokens",) if profile["kind"] == "openai_responses" else ("max_tokens", "max_completion_tokens")
        if profile["token_parameter"] not in allowed_token_parameters:
            raise ValueError("Unknown budget parameter")
        if any(len(v) > 2000 for v in profile.values()):
            raise ValueError("Profile too long")
        secret = str(payload.get("api_key") or "").strip()
        if len(secret) > 2000:
            raise ValueError("Key too long")
        with self.lock:
            profiles = [p for p in self.profiles if p["id"] != profile["id"]] + [profile]
            secrets = dict(self.secrets)
            if payload.get("clear_key"):
                secrets.pop(profile["id"], None)
            if secret:
                secrets[profile["id"]] = secret
            stored = json.dumps(profiles)
            if any(value and value in stored for value in secrets.values()):
                raise ValueError("Refusing to store a profile that contains a key")
            self.profiles = profiles
            self.secrets = secrets
            write_json(self.path, self.profiles)
            self.path.chmod(0o600)
            self._write_secrets()
            return self.public()

    def resolve(self, profile_id):
        with self.lock:
            p = next((p for p in self.profiles if p["id"] == profile_id), None)
            if p is None:
                raise ProviderError("Model profile not found")
            if not p["base_url"] or not p["model"]:
                raise ProviderError(f"Complete the URL and model for profile {p['name']}")
            key = self._key(p)
            if not key and urlparse(p["base_url"]).hostname not in ("127.0.0.1", "localhost", "::1"):
                raise ProviderError(f"No key for {p['name']}. No call sent.")
            return copy.deepcopy(p), key


def anthropic_messages(messages):
    result = []
    for m in messages:
        if m["role"] == "system":
            continue
        role = "user" if m["role"] == "tool" else m["role"]
        if m["role"] == "tool":
            blocks = [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}]
        elif m.get("_anthropic_content"):
            blocks = copy.deepcopy(m["_anthropic_content"])
        else:
            blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
            for call in m.get("tool_calls", []):
                blocks.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"],
                               "input": json.loads(call["function"]["arguments"])})
        if not blocks:
            continue
        if result and result[-1]["role"] == role:
            result[-1]["content"].extend(blocks)
        else:
            result.append({"role": role, "content": blocks})
    return result


def responses_input(messages):
    """Replay native output items, including opaque reasoning, with their tool results."""
    result = []
    for message in messages:
        if message["role"] == "assistant" and "_response_items" in message:
            result.extend(copy.deepcopy(message["_response_items"]))
        elif message["role"] == "tool":
            result.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                           "output": message["content"]})
        else:
            if message.get("content"):
                result.append({"role": message["role"], "content": message["content"]})
            for call in message.get("tool_calls", []):
                result.append({"type": "function_call", "call_id": call["id"],
                               "name": call["function"]["name"], "arguments": call["function"]["arguments"]})
    return result


def _block_text(block, depth=0):
    """Readable strings from one reasoning block. Encrypted blobs are not text."""
    if depth > 6:
        return []
    if isinstance(block, str):
        return [block.strip()] if block.strip() else []
    if isinstance(block, list):
        found = []
        for item in block:
            found.extend(_block_text(item, depth + 1))
        return found
    if not isinstance(block, dict):
        return []
    kind = str(block.get("type") or "")
    if "encrypted" in kind or kind == "redacted_thinking":
        return []
    found = []
    for key in ("text", "thinking", "summary", "reasoning", "content"):
        if key in block:
            found.extend(_block_text(block[key], depth + 1))
    return found


def _classify_reasoning(value):
    """Split provider reasoning into raw text and summaries."""
    raw, summary = [], []
    blocks = value if isinstance(value, list) else [value]
    for block in blocks:
        if isinstance(block, str):
            if block.strip():
                raw.append(block.strip())
            continue
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if "encrypted" in kind or kind == "redacted_thinking":
            continue
        texts = _block_text(block)
        if not texts:
            continue
        if block.get("thought") is True or _google_thought_part(block) or "summary" in kind or kind in ("thinking", "summary_text"):
            summary.extend(texts)
        else:
            raw.extend(texts)
    return raw, summary


def _finish_reasoning(raw, summary):
    def unique(parts):
        seen = []
        for part in parts:
            if part and part not in seen:
                seen.append(part)
        return seen

    raw, summary = unique(raw), unique(summary)
    if raw:
        return "\n\n".join(raw), "transcript"
    if summary:
        return "\n\n".join(summary), "summary"
    return "", ""


def _google_thought_part(block):
    """Gemini's OpenAI layer marks a summary part with extra_content.google.thought."""
    if not isinstance(block, dict):
        return False
    extra = block.get("extra_content")
    google = extra.get("google") if isinstance(extra, dict) else None
    return isinstance(google, dict) and google.get("thought") is True


def _thought_block(block):
    if not isinstance(block, dict):
        return False
    if block.get("thought") is True or _google_thought_part(block):
        return True
    kind = str(block.get("type") or "")
    return kind in ("thinking", "reasoning", "reasoning_text") or kind.startswith("reasoning.")


def split_thought_tags(text):
    """Separate a Gemini <thought> wrapper from the text that should be posted."""
    if not isinstance(text, str) or "<thought" not in text.lower():
        return text if isinstance(text, str) else "", ""
    opens = list(re.finditer(r"<thought>", text, re.IGNORECASE))
    closes = list(re.finditer(r"</thought>", text, re.IGNORECASE))
    if not opens:
        return text, ""
    if not closes:
        inner = text[opens[0].end():].strip()
        return text[:opens[0].start()].strip(), inner
    inner = text[opens[0].end():closes[-1].start()]
    inner = re.sub(r"</?thought>", "", inner, flags=re.IGNORECASE).strip()
    visible = (text[:opens[0].start()] + text[closes[-1].end():]).strip()
    return visible, inner


def chat_reasoning(original):
    """Readable reasoning plus, when content is a part list, the visible answer."""
    raw, summary = [], []
    for field in ("reasoning", "reasoning_content"):
        value = original.get(field)
        if isinstance(value, str):
            if value.strip():
                raw.append(value.strip())
        elif isinstance(value, (list, dict)):
            found_raw, found_summary = _classify_reasoning(value)
            raw.extend(found_raw)
            summary.extend(found_summary)
    if original.get("reasoning_details") is not None:
        found_raw, found_summary = _classify_reasoning(original["reasoning_details"])
        raw.extend(found_raw)
        summary.extend(found_summary)
    content = original.get("content")
    visible = content if isinstance(content, str) else ""
    provider_content = None
    if isinstance(content, list):
        thought = False
        parts = []
        for block in content:
            if isinstance(block, str):
                if block.strip():
                    parts.append(block.strip())
                continue
            if _thought_block(block):
                thought = True
                found_raw, found_summary = _classify_reasoning(block)
                raw.extend(found_raw)
                summary.extend(found_summary)
                continue
            if isinstance(block, dict):
                if block.get("extra_content"):
                    thought = True
                text = block.get("text") if isinstance(block.get("text"), str) else block.get("content")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        if thought:
            visible = "\n".join(parts)
            provider_content = content
        else:
            visible = content
    elif content is None:
        visible = ""
    elif not isinstance(content, str):
        raise TypeError("content must be text or a list of parts")
    if isinstance(visible, str):
        visible, tagged = split_thought_tags(visible)
        if tagged:
            summary.append(tagged)
    text, kind = _finish_reasoning(raw, summary)
    return visible, provider_content, text, kind


def responses_reasoning(items):
    """OpenAI returns a summary on reasoning items, not the raw chain of thought."""
    raw, summary = [], []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        summary_value = item.get("summary")
        if isinstance(summary_value, str) and summary_value.strip():
            summary.append(summary_value.strip())
        elif isinstance(summary_value, list):
            for block in summary_value:
                summary.extend(_block_text(block))
        elif isinstance(summary_value, dict):
            summary.extend(_block_text(summary_value))
        content = item.get("content")
        if content:
            found_raw, found_summary = _classify_reasoning(content)
            raw.extend(found_raw)
            summary.extend(found_summary)
    return _finish_reasoning(raw, summary)


def relay_messages(messages, kind):
    """Drop display-only fields. Keep the provider's own reasoning for the next call."""
    private = set()
    if kind == "openai_responses":
        private.add("_response_items")
    if kind == "anthropic":
        private.add("_anthropic_content")
    relayed = []
    for message in messages:
        item = {}
        for key, value in message.items():
            if key in ("reasoning_text", "reasoning_kind", "_board_note_ids", "_board_source_agent"):
                continue
            if key == "_provider_content":
                if kind == "openai_compatible":
                    item["content"] = value
                continue
            if key.startswith("_") and key not in private:
                continue
            item[key] = value
        relayed.append(item)
    return relayed


def _store_reasoning(msg, text, kind):
    if text:
        msg["reasoning_text"] = text
        msg["reasoning_kind"] = kind


def completion(profile, key, messages, tools, output_budget, temperature=None, wire=None):
    """One HTTP completion; caller owns tool execution and bounded iteration.

    ``wire`` is an optional ``WireExchange`` that records the request and response as sent and read.
    """
    # Internal Swarm Lab delivery metadata is for audit/exposure accounting only.
    # It must never be sent as an unknown chat-message field to a provider.
    provider_messages = relay_messages(messages, profile["kind"])
    if profile["kind"] == "anthropic":
        endpoint = profile["base_url"] + "/messages"
        payload = {"model": profile["model"], "max_tokens": output_budget,
                   "system": "\n\n".join(m["content"] for m in provider_messages if m["role"] == "system"),
                   "messages": anthropic_messages(provider_messages),
                   "tools": [{"name": t["function"]["name"], "description": t["function"]["description"],
                              "input_schema": t["function"]["parameters"]} for t in tools]}
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    elif profile["kind"] == "openai_responses":
        endpoint = profile["base_url"] + "/responses"
        payload = {"model": profile["model"], "input": responses_input(provider_messages), "store": False,
                   "max_output_tokens": output_budget,
                   "tools": [{"type": "function", **copy.deepcopy(t["function"])} for t in tools]}
        if profile.get("reasoning_effort"):
            payload["reasoning"] = {"effort": profile["reasoning_effort"]}
        # Summary is the readable text OpenAI will return. Raw reasoning stays encrypted.
        if profile.get("reasoning_effort") != "none":
            payload.setdefault("reasoning", {})["summary"] = "auto"
        headers = {"Authorization": "Bearer " + key} if key else {}
    else:
        endpoint = profile["base_url"] + "/chat/completions"
        payload = {"model": profile["model"], "messages": provider_messages, "tools": tools,
                   profile.get("token_parameter", "max_tokens"): output_budget}
        if urlparse(profile["base_url"]).hostname == "openrouter.ai" and profile["model"] == "nvidia/nemotron-3-ultra-550b-a55b":
            payload["provider"] = {"max_price": {"prompt": 0.625, "completion": 3.125}}
        host = urlparse(profile["base_url"]).hostname
        effort = profile.get("reasoning_effort") or ""
        if host == "generativelanguage.googleapis.com":
            # reasoning_effort and thinking_config overlap. Together, Gemini keeps the
            # effort and drops the thought summary. The level replaces the effort.
            if effort == "none":
                payload["reasoning_effort"] = "none"
            else:
                thinking = {"include_thoughts": True}
                if effort:
                    thinking["thinking_level"] = {"xhigh": "high", "max": "high"}.get(effort, effort)
                payload["extra_body"] = {"google": {"thinking_config": thinking}}
        elif effort:
            if host == "openrouter.ai":
                payload["reasoning"] = {"effort": effort}
                if effort != "none":
                    payload["reasoning"]["exclude"] = False
            else:
                payload["reasoning_effort"] = effort
        headers = {"Authorization": "Bearer " + key} if key else {}
    if temperature is not None:
        payload["temperature"] = temperature
    if not tools:
        payload.pop("tools", None)
    headers["Content-Type"] = "application/json"
    request = Request(endpoint, data=json.dumps(payload).encode(), headers=headers, method="POST")
    # Independent agents may queue behind one local GPU inference worker.
    # A remote thinking model sends nothing until its whole answer is ready, so the
    # wait grows with the output budget: 60 s of queueing plus 50 tokens per second.
    timeout = (600 if urlparse(endpoint).hostname in ("127.0.0.1", "localhost", "::1")
               else min(600, 60 + output_budget // 50))
    handlers = [NoRedirect()] + ([wire.tap(timeout, key)] if wire is not None else [])
    try:
        with build_opener(*handlers).open(request, timeout=timeout) as response:
            raw = response.read(4_000_001)
            if wire is not None:
                wire.received(response.status, response.reason, response.headers, raw)
            if len(raw) > 4_000_000:
                raise ProviderError("Provider response too large")
            data = json.loads(raw)
    except HTTPError as exc:
        if wire is not None:
            wire.http_error(exc)
        # Never echo the response body, URL or headers: a gateway may reflect a key.
        raise ProviderError(f"Provider returned HTTP {exc.code}. Check the profile and limits.",
                            retryable=exc.code in (408, 429, 500, 502, 503, 504), diagnostic={"http_status": exc.code}) from None
    except TimeoutError as exc:
        if wire is not None:
            wire.failed("timeout", exc)
        raise ProviderError(f"Provider did not respond within {timeout} seconds.", retryable=True,
                            diagnostic={"timeout_seconds": timeout}) from None
    except (URLError, OSError, ValueError) as exc:
        if wire is not None and not isinstance(exc, ValueError):
            wire.failed("connection failed", exc)
        raise ProviderError("Connection failed or invalid JSON from provider") from None
    if not isinstance(data, dict):
        raise ProviderError("Response format incompatible with the chosen profile")
    usage = data.get("usage")
    reported_usage = None
    omitted_thinking = 0
    if isinstance(usage, dict):
        reported_usage = {"input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
                          "output_tokens": usage.get("output_tokens", usage.get("completion_tokens"))}
        reported_usage = {k: v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None
                          for k, v in reported_usage.items()}
        details = usage.get("output_tokens_details", usage.get("completion_tokens_details"))
        reasoning_tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None
        if isinstance(reasoning_tokens, int) and not isinstance(reasoning_tokens, bool) and reasoning_tokens >= 0:
            reported_usage["reasoning_tokens"] = reasoning_tokens
        else:
            # Gemini's OpenAI layer bills thinking inside the output cap but often
            # leaves it out of completion_tokens. total_tokens still includes it.
            total = usage.get("total_tokens")
            prompt, completion = reported_usage.get("input_tokens"), reported_usage.get("output_tokens")
            if (isinstance(total, int) and not isinstance(total, bool) and isinstance(prompt, int)
                    and isinstance(completion, int) and total > prompt + completion):
                omitted_thinking = total - prompt - completion
                reported_usage["reasoning_tokens"] = omitted_thinking
                reported_usage["output_tokens"] = completion + omitted_thinking
        cost = usage.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            reported_usage["cost_usd"] = cost
    check_provider_error(data, reported_usage)
    try:
        if (data.get("stop_reason") == "max_tokens" or
                data.get("status") == "incomplete" and (data.get("incomplete_details") or {}).get("reason") == "max_output_tokens" or
                any(c.get("finish_reason") == "length" for c in data.get("choices", []))):
            message = "Response hit the token limit before finishing. Increase the output budget."
            if omitted_thinking:
                message += " Thinking tokens used the rest of the cap and were omitted from the visible output count."
            raise ProviderError(message, usage=reported_usage)
        if profile["kind"] == "openai_responses":
            if data.get("status") != "completed" or not isinstance(data.get("output"), list):
                raise ProviderError("Responses API call did not complete.", usage=reported_usage)
            items = data["output"]
            msg = {"role": "assistant", "content": "\n".join(
                block.get("text", block.get("refusal", "")) for item in items if item["type"] == "message"
                for block in item.get("content", []) if block["type"] in ("output_text", "refusal")),
                "_response_items": copy.deepcopy(items)}
            text, kind = responses_reasoning(items)
            _store_reasoning(msg, text, kind)
            calls = [{"id": item["call_id"], "type": "function", "function": {
                "name": item["name"], "arguments": item["arguments"]}}
                for item in items if item["type"] == "function_call"]
        elif profile["kind"] == "anthropic":
            blocks = data["content"]
            if not isinstance(blocks, list):
                raise TypeError("content must be a list")
            msg = {"role": "assistant", "content": "\n".join(
                b["text"] for b in blocks if isinstance(b, dict) and b.get("type") == "text")}
            thinking = [b for b in blocks if isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")]
            if thinking:
                msg["_anthropic_content"] = copy.deepcopy(blocks)
                pieces = [b["thinking"].strip() for b in thinking
                          if isinstance(b.get("thinking"), str) and b["thinking"].strip()]
                _store_reasoning(msg, "\n\n".join(pieces), "summary")
            calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
                     for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        else:
            original = data["choices"][0]["message"]
            if not isinstance(original, dict):
                raise TypeError("message must be an object")
            visible, provider_content, text, kind = chat_reasoning(original)
            msg = {"role": "assistant", "content": visible or ""}
            if provider_content is not None:
                msg["_provider_content"] = copy.deepcopy(provider_content)
            if text and urlparse(profile["base_url"]).hostname == "generativelanguage.googleapis.com":
                kind = "summary"
            _store_reasoning(msg, text, kind)
            if "extra_content" in original:
                msg["extra_content"] = copy.deepcopy(original["extra_content"])
            for field in ("reasoning", "reasoning_content", "reasoning_details"):
                if field in original:
                    msg[field] = copy.deepcopy(original[field])
            calls = original.get("tool_calls") or []
            if not isinstance(calls, list):
                raise TypeError("tool_calls must be a list")
            for call in calls:
                if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not isinstance(call.get("function"), dict):
                    raise TypeError("invalid tool call")
                if isinstance(call["function"].get("arguments"), dict):
                    call["function"]["arguments"] = json.dumps(call["function"]["arguments"])
                if not isinstance(call["function"].get("arguments"), str) or not isinstance(call["function"].get("name"), str):
                    raise TypeError("invalid tool function")
        if calls:
            msg["tool_calls"] = calls
        return msg, reported_usage
    except (AttributeError, KeyError, TypeError, IndexError):
        raise ProviderError("Response format incompatible with the chosen profile", usage=reported_usage,
                            diagnostic=response_diagnostic(data)) from None
