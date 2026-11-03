"""Explanations for the errors of a run, for the error banner of the interface.

Each problem becomes an issue: what happened, whether the run goes on by itself, and what the operator has to do
if not. The original message stays next to it. The issues are derived from the stored run state and its events,
so runs saved before this module existed are explained too."""

from datetime import datetime, timedelta
from urllib.parse import urlparse

from .lab_engine import ACTION_REQUIRED_WAIT_SECONDS

PROVIDER_HOSTS = (
    ("generativelanguage.googleapis.com", "Google"), ("aiplatform.googleapis.com", "Google"),
    ("api.openai.com", "OpenAI"), ("openrouter.ai", "OpenRouter"), ("api.anthropic.com", "Anthropic"),
    ("api.x.ai", "xAI"), ("api.deepseek.com", "DeepSeek"), ("api.mistral.ai", "Mistral"),
    ("api.moonshot.ai", "Moonshot"), ("api.together.xyz", "Together"), ("api.groq.com", "Groq"),
    ("api.fireworks.ai", "Fireworks"), ("api.cerebras.ai", "Cerebras"), ("api.z.ai", "Z.ai"),
    ("dashscope", "Alibaba Cloud"), ("xiaomimimo.com", "Xiaomi MiMo"),
)

HTTP_NAMES = {408: "request timeout", 500: "internal server error", 502: "bad gateway",
              503: "service unavailable or overloaded", 504: "gateway timeout", 529: "overloaded"}


def provider_label(profile):
    """A short name for the provider behind a profile, e.g. Google for the Gemini endpoint."""
    profile = profile if isinstance(profile, dict) else {}
    host = (urlparse(str(profile.get("base_url") or "")).hostname or "").lower()
    for part, label in PROVIDER_HOSTS:
        if part in host:
            return label
    if profile.get("kind") == "anthropic":
        return "Anthropic"
    if host in ("127.0.0.1", "localhost", "::1"):
        return "the local model server"
    return host or profile.get("name") or "the provider"


def _status(diagnostic):
    code = diagnostic.get("http_status")
    return code if isinstance(code, int) else diagnostic.get("provider_error_code")


def _flex(diagnostic, profile):
    return bool(diagnostic.get("flex_unavailable") or diagnostic.get("service_tier") == "flex"
                or (profile or {}).get("service_tier") == "flex")


def explain_wait(reason, diagnostic, profile):
    """A provider error the run waits out by itself. Returns (title, what, todo)."""
    p = provider_label(profile)
    status = _status(diagnostic)
    if reason == "rate_limit":
        return (f"Rate limit at {p}",
                f"{p} answered with HTTP 429: too many requests or tokens per minute for this key.",
                f"Nothing. If it keeps happening, raise the rate limit with {p} or give fewer agents this key.")
    if reason == "flex_unavailable":
        return ("OpenAI Flex has no capacity",
                "OpenAI has no Flex capacity free right now (HTTP 429, not charged). After the second miss in a row "
                "the next call uses standard processing.",
                "Nothing.")
    if reason == "timeout":
        seconds = diagnostic.get("timeout_seconds")
        within = f" within {seconds} seconds" if isinstance(seconds, (int, float)) else " in time"
        flex = " The next call uses standard processing, then Flex again." if _flex(diagnostic, profile) else ""
        return (f"No answer from {p}", f"{p} did not answer{within}.{flex}",
                "Nothing. If every call times out, check the provider's status page or lower max_output_tokens.")
    if reason == "network":
        if diagnostic.get("invalid_json"):
            return (f"Unreadable answer from {p}",
                    f"{p} sent a response that is not valid JSON, often a sign of an overloaded server or a proxy "
                    "in between.",
                    "Nothing. If it lasts, check the base URL of the profile and any proxy on this machine.")
        return (f"No connection to {p}",
                f"The connection to {p} failed (network error, DNS or connection reset).",
                "Check that this machine is online. The run goes on as soon as the connection is back.")
    if reason == "server_error":
        name = HTTP_NAMES.get(status)
        return (f"Server error at {p}",
                f"{p} answered with HTTP {status}{' (' + name + ')' if name else ''}. The problem is on the "
                "provider's side.",
                f"Nothing. If it lasts a long time, check the status page of {p}.")
    if reason == "action_required":
        code = diagnostic.get("provider_error_code_name") or diagnostic.get("provider_error_code") or status
        return (f"No credit or spend limit at {p}",
                f"{p} refused the call: no credit left or a spend limit reached{f' ({code})' if code else ''}.",
                f"Add credit or raise the spend limit at {p}. The run notices it at the next try.")
    return (f"Provider error at {p}", f"{p} returned an error that is usually temporary.", "Nothing.")


def explain_failure(message, diagnostic, profile, *, run_ends, resumable):
    """An error that stopped an agent for good. Returns (title, what, todo)."""
    message = str(message or "")
    diagnostic = diagnostic if isinstance(diagnostic, dict) else {}
    p = provider_label(profile)
    name = (profile or {}).get("name") or "this agent's profile"
    model = (profile or {}).get("model")
    status = _status(diagnostic)
    after = ("Then start a new run. This run ended as a technical failure and does not belong in the evaluation."
             if run_ends else "Then press Resume (after Pause, if the run is still going), so the agent takes part again." if resumable
             else "Then start a new run.")
    if diagnostic.get("action_required") or "Still refused after" in message:
        return (f"Still no credit at {p}",
                f"{p} refused every call for {ACTION_REQUIRED_WAIT_SECONDS // 60} minutes: no credit left or a spend "
                "limit reached. The lab stopped trying.",
                f"Add credit or raise the spend limit at {p}. {after}")
    if status in (401, 403):
        return (f"{p} rejected the API key",
                f"{p} answered with HTTP {status}: the key is wrong, expired or has no access to this model.",
                f"Check the key of the profile {name} under Models (or its environment variable). {after}")
    if status == 404:
        return (f"Model or address not found at {p}",
                f"{p} answered with HTTP 404: the model{f' {model}' if model else ''} or the base URL does not exist "
                "for this key.",
                f"Check the model name and base URL of the profile {name} under Models. {after}")
    if status == 402:
        return (f"Payment required at {p}", f"{p} answered with HTTP 402: no credit on the account.",
                f"Add credit at {p}. {after}")
    if status == 413:
        return (f"Request too large for {p}",
                f"{p} answered with HTTP 413: the conversation became longer than the model accepts.",
                f"Choose a model with a longer context or lower call_limit. {after}")
    if status in (400, 422):
        return (f"{p} rejected the request",
                f"{p} answered with HTTP {status}: the request was invalid for this model. Usual causes are a setting "
                "the model does not support (reasoning, service tier, token parameter, temperature) or a context "
                "that became too long.",
                f"The wire log (button Wire log) shows the exact answer. Fix the profile {name}. {after}")
    if isinstance(status, int) and status >= 400:
        return (f"Error at {p}", f"{p} answered with HTTP {status}, which the lab does not wait out.",
                f"Check the profile {name} and the wire log. {after}")
    if message.startswith("No key for"):
        return ("API key missing", f"The profile {name} has no API key, so no call was sent.",
                "Enter the key under Models or set the named environment variable (also in .env). " + after)
    if message.startswith("Complete the URL") or message == "Model profile not found":
        return ("Model profile incomplete", "The assigned model profile is missing or has no URL or model name.",
                "Complete the profile under Models and assign it to the agent. " + after)
    if "token limit" in message:
        return ("Answer cut off at the token limit",
                f"The model of {name} used up max_output_tokens before it finished its answer.",
                "Raise max_output_tokens in the design or lower the reasoning setting of the profile. " + after)
    if message.startswith("Response format incompatible") or message == "Responses API call did not complete.":
        return ("Answer in an unexpected format",
                f"The answer of {p} did not match the API format of the profile {name}.",
                "Check the provider format of the profile (Chat Completions, Responses or Anthropic Messages) and "
                "look at the answer in the wire log. " + after)
    if message == "Provider response too large":
        return ("Answer too large", f"{p} sent an answer larger than 4 MB, which the lab does not accept.",
                "Lower max_output_tokens. " + after)
    if message.startswith("Provider error in JSON"):
        return (f"Error reported by {p}", f"{p} answered with HTTP 200 but reported an error inside the answer.",
                "Look at the answer in the wire log and check the profile. " + after)
    if message.startswith("Call limit reached"):
        return ("Call limit reached", "This agent used all of its calls (call_limit).",
                "Nothing, if that is intended. For more calls, raise call_limit and start a new run.")
    if message.startswith("Budget bound is not configured"):
        return ("No price for the USD budget",
                "A USD budget is set, but the lab has no price for this model, so it cannot keep to the budget.",
                "Remove the USD budget or use a model with a known price. " + after)
    if message.startswith("Internal"):
        return ("Error in the lab itself",
                "Something unexpected went wrong inside the lab, not at the provider.",
                "Look at the console the server runs in and at the run events. Report it together with the run ID. "
                + after)
    return ("The agent stopped", "The agent stopped after an error the lab has no explanation for.",
            "Read the original message below and check the profile and the wire log. " + after)


def _parse(at):
    try:
        return datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return None


def _later(at, seconds):
    start = _parse(at)
    return (start + timedelta(seconds=seconds)).isoformat() if start and isinstance(seconds, (int, float)) else None


def run_issues(state):
    """The problems of a run that the operator should know about, newest state only, grouped by cause."""
    events = state.get("events") or []
    config = state.get("config") or {}
    profiles = state.get("provider_profiles") or {}
    agent_status = state.get("agent_status") or {}
    status = state.get("status")
    started = max((i for i, e in enumerate(events) if e.get("kind") == "started"), default=-1)
    recent = events[started + 1:]
    run_ends = state.get("finish_reason") == "agent_error"
    resumable = status not in ("complete", "stopped", "archived") and not run_ends
    issues = []
    for agent in config.get("agents") or []:
        mine = [e for e in recent if e.get("agent_id") == agent]
        profile = profiles.get(agent) or {}
        if status == "running" and agent_status.get(agent) == "rate_limited":
            wait = next((e for e in reversed(mine) if e.get("kind") == "retry_scheduled"), None)
            if wait:
                failure = next((e for e in reversed(mine[:mine.index(wait)]) if e.get("kind") == "model_error"), {})
                diagnostic = failure.get("diagnostic") or {}
                reason = wait.get("reason") or "retry"
                title, what, todo = explain_wait(reason, diagnostic, failure.get("provider") or profile)
                delay = wait.get("delay_seconds", 10 * (wait.get("attempt") or 1))
                issue = {"kind": "wait", "severity": "warning" if reason == "action_required" else "info",
                         "reason": reason, "title": title, "what": what, "continues": True, "todo": todo,
                         "original": failure.get("message"), "resume_at": _later(wait.get("at"), delay),
                         "agents": [agent]}
                if reason == "action_required":
                    # The window starts at the first refusal since the agent's last successful answer.
                    since = None
                    for e in reversed(mine[:mine.index(wait) + 1]):
                        if e.get("kind") == "model_response":
                            break
                        if e.get("kind") == "retry_scheduled" and e.get("reason") == "action_required":
                            since = e
                    issue["gives_up_at"] = _later((since or wait).get("at"), ACTION_REQUIRED_WAIT_SECONDS)
                issues.append(issue)
        error = next((e for e in reversed(mine) if e.get("kind") == "error"), None)
        if error and not any(e.get("kind") == "model_response" for e in mine[mine.index(error):]):
            diagnostic = error.get("diagnostic")
            if diagnostic is None:
                # Runs from before the error event carried its diagnostic: take it from the failed call.
                failure = next((e for e in reversed(mine[:mine.index(error)]) if e.get("kind") == "model_error"
                                and e.get("message") and str(error.get("message", "")).startswith(e["message"])), {})
                diagnostic = failure.get("diagnostic") or {}
            title, what, todo = explain_failure(error.get("message"), diagnostic, error.get("provider") or profile,
                                                run_ends=run_ends, resumable=resumable)
            consequence = ("The run ended with status error, because the design ends the run when an agent fails."
                           if run_ends else "The other agents go on without this agent."
                           if status == "running" and len(config.get("agents") or []) > 1 else "")
            issues.append({"kind": "stopped", "severity": "error", "title": title, "what": what,
                           "continues": False, "consequence": consequence, "todo": todo,
                           "original": error.get("message"), "agents": [agent]})
    if status == "error" and any(e.get("kind") == "internal_error" for e in recent):
        title, what, todo = explain_failure("Internal execution error", {}, {}, run_ends=False, resumable=False)
        issues.append({"kind": "run", "severity": "error", "title": title, "what": what, "continues": False,
                       "consequence": "The run stopped.", "todo": todo, "original": state.get("error"), "agents": []})
    return _group(issues)


def _group(issues):
    """One entry per cause: ten agents waiting on the same rate limit become one line."""
    grouped = {}
    for issue in issues:
        key = (issue["kind"], issue["title"], issue["what"], issue["todo"], issue.get("original"))
        if key not in grouped:
            grouped[key] = issue
            continue
        known = grouped[key]
        known["agents"] += issue["agents"]
        for field, pick in (("resume_at", min), ("gives_up_at", min)):
            values = [v for v in (known.get(field), issue.get(field)) if v]
            if values:
                known[field] = pick(values)
    order = {"error": 0, "warning": 1, "info": 2}
    return sorted(grouped.values(), key=lambda i: order[i["severity"]])


def request_help(message):
    """A hint for an error message the server answers a request with, or None when the message speaks for itself."""
    message = str(message or "")
    if message.startswith("No key for"):
        return "Enter the key under Models, or set the environment variable named in the profile (also in .env)."
    if message.startswith("Complete the URL") or message == "Model profile not found":
        return "Complete the model profile under Models and assign it to every agent."
    if message.startswith("Call limit reached"):
        return "This run has used its calls. Create a new run, for example with Copy run settings."
    if message == "Experiment finished":
        return "A finished run cannot go on. Create a new run, for example with Copy run settings."
    if message == "Execution is already in progress":
        return "The run is already running. Wait, or press Pause or Stop first."
    if message.startswith("Archived run is view-only"):
        return "Use Copy run settings to start a new run with the same configuration."
    return None


def package_help(error):
    """A hint for the error that stopped a package."""
    error = str(error or "")
    if not error:
        return None
    if error.startswith("Run without any successful model call"):
        return ("No call of this run reached a model, so the next runs would fail the same way. The package stopped. "
                "Open the run, fix the cause its error banner names, then press Continue package. The failed run "
                "stays in the package with status error and does not belong in the evaluation.")
    return "The package stopped. Fix the cause, then press Continue package."
