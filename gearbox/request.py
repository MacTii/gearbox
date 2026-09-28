"""Inspecting and rewriting Messages API request bodies."""
from __future__ import annotations

import copy
import hashlib
import json
import re

REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def new_user_prompt(body: dict) -> str | None:
    """Text of the latest user message, or None when this request is an agent-loop continuation
    (last message holds only tool_result blocks) or contains nothing but injected reminders.
    Trailing mid-conversation system messages (Claude Code appends them after the prompt) are skipped."""
    messages = [m for m in body.get("messages") or [] if m.get("role") != "system"]
    if not messages or messages[-1].get("role") != "user":
        return None
    text = REMINDER_RE.sub("", _text_of(messages[-1].get("content", ""))).strip()
    return text or None


def demote_system_messages(body: dict) -> dict:
    """Turn mid-conversation system messages into user-turn reminders for models that reject
    role:"system" inside messages. Deterministic, so the rewritten prefix stays cacheable."""
    out = []
    for m in body.get("messages") or []:
        if m.get("role") != "system":
            out.append(m)
            continue
        text = _text_of(m.get("content", "")).strip()
        if not text:
            continue  # e.g. effort-only control messages
        block = {"type": "text", "text": f"<system-reminder>\n{text}\n</system-reminder>"}
        prev = out[-1] if out else None
        if prev and prev.get("role") == "user":
            content = prev["content"]
            content = [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
            out[-1] = {**prev, "content": content + [block]}
        else:
            out.append({"role": "user", "content": [block]})
    return {**body, "messages": out}


def is_fresh_conversation(body: dict) -> bool:
    return sum(1 for m in body.get("messages") or [] if m.get("role") == "user") <= 1


def session_key(body: dict, header_session: str | None = None) -> str:
    """Session id from the x-claude-code-session-id header or metadata.user_id
    (JSON with session_id in current Claude Code, "..._session_<id>" in older versions);
    otherwise a hash of the first user message."""
    if header_session:
        return header_session
    uid = (body.get("metadata") or {}).get("user_id")
    if uid:
        try:
            sid = json.loads(uid).get("session_id")
            if sid:
                return sid
        except (ValueError, AttributeError):
            pass
        m = re.search(r"session_([0-9a-f-]+)", uid)
        return m.group(1) if m else uid
    messages = body.get("messages") or []
    first = next((m for m in messages if m.get("role") == "user"), None)
    seed = _text_of(first.get("content", ""))[:2000] if first else ""
    return "h:" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def estimate_tokens(raw: bytes) -> int:
    return len(raw) // 4


def strip_fields(body: dict, paths: list[str]) -> dict:
    out = copy.deepcopy(body)
    for path in paths:
        *parents, leaf = path.split(".")
        node = out
        for p in parents:
            node = node.get(p) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict):
            node.pop(leaf, None)
    if isinstance(out.get("output_config"), dict) and not out["output_config"]:
        out.pop("output_config")
    return out


def dumps(body: dict) -> bytes:
    return json.dumps(body, ensure_ascii=False).encode("utf-8")
