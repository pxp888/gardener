"""Garden planning: turn counting, cut point calculation, and session tracking."""

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class GardenPlan:
    eligible_messages: List[Dict[str, Any]]
    eligible_turns: int
    estimated_tokens: int
    cut_index: int
    system_messages: List[Dict[str, Any]]
    protected_messages: List[Dict[str, Any]]
    previous_summary: Optional[str] = None


class SessionState:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.previous_summary: Optional[str] = None
        self.turns_since_garden: int = 0
        self.last_seen_assistant_count: int = 0
        self.updated_at: float = time.time()


class SessionManager:
    """In-memory session manager with TTL-based cleanup."""

    def __init__(self, ttl_seconds: float = 7200.0):
        self.sessions: Dict[str, SessionState] = {}
        self.ttl_seconds = ttl_seconds

    def get_or_create(self, session_id: str) -> SessionState:
        now = time.time()
        # Clean expired sessions if collection grows
        if len(self.sessions) > 500:
            expired = [k for k, v in self.sessions.items() if now - v.updated_at > self.ttl_seconds]
            for k in expired:
                del self.sessions[k]

        if session_id not in self.sessions:
            self.sessions[session_id] = SessionState(session_id)
        session = self.sessions[session_id]
        session.updated_at = now
        return session


def derive_session_id(headers: Dict[str, str], body: Dict[str, Any]) -> str:
    """Determine a stable session identifier from headers or payload contents."""
    # 1. Custom session headers
    for h in ("x-session-id", "session-id", "x-conversation-id", "conversation-id"):
        if h in headers and headers[h].strip():
            return headers[h].strip()

    # 2. Body user field (standard OpenAI field often used as user/session id)
    user_field = body.get("user")
    if user_field and isinstance(user_field, str) and user_field.strip():
        return f"user:{user_field.strip()}"

    # 3. Hash of initial prompt (system + first user message)
    messages = body.get("messages", [])
    if messages:
        prefix_sample = []
        for m in messages[:3]:
            prefix_sample.append(f"{m.get('role')}:{m.get('content')}")
        digest = hashlib.sha256("||".join(prefix_sample).encode("utf-8")).hexdigest()[:16]
        return f"conv:{digest}"

    return "default-session"


def estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    """Rough token estimate (~4 chars per token)."""
    chars = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            chars += len(json.dumps(c))
        tc = m.get("tool_calls")
        if tc:
            chars += len(json.dumps(tc))
    return max(1, chars // 4)


def find_clean_cut_index(messages: List[Dict[str, Any]], protected_turns: int) -> Optional[int]:
    """Find the index where the protected tail begins.

    Ensures the cut never splits an assistant tool_calls block from its corresponding tool results.
    """
    assistant_indices: List[int] = [
        i for i, m in enumerate(messages) if m.get("role") == "assistant"
    ]

    if len(assistant_indices) < protected_turns:
        return None

    # The K-th assistant from the end
    target_assistant_idx = assistant_indices[-protected_turns]

    # If this assistant was immediately preceded by an intermediate user prompt,
    # attach that user prompt to the protected turn (unless it is the session origin at index 0).
    if target_assistant_idx > 1 and messages[target_assistant_idx - 1].get("role") == "user":
        return target_assistant_idx - 1

    return target_assistant_idx


def extract_existing_summary(messages: List[Dict[str, Any]]) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Check if an earlier <gardener-state> block already exists in messages.

    If found, extracts it as the base previous_summary so we don't re-summarize it.
    """
    clean_messages: List[Dict[str, Any]] = []
    extracted_summary: Optional[str] = None

    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str) and "<gardener-state>" in content and "</gardener-state>" in content:
            start = content.find("<gardener-state>") + len("<gardener-state>")
            end = content.find("</gardener-state>")
            extracted_summary = content[start:end].strip()
        else:
            clean_messages.append(msg)

    return extracted_summary, clean_messages


def plan_garden(
    messages: List[Dict[str, Any]],
    protected_turns: int,
    min_tokens: int,
    previous_summary: Optional[str] = None,
) -> Optional[GardenPlan]:
    """Compute garden plan.

    Returns None if:
    - Insufficient assistant turns aged out
    - Estimated aging-out tokens below min_tokens
    """
    # Separate leading system / developer messages
    system_messages: List[Dict[str, Any]] = []
    content_messages: List[Dict[str, Any]] = []

    for m in messages:
        if m.get("role") in ("system", "developer") and not content_messages:
            system_messages.append(m)
        else:
            content_messages.append(m)

    if not content_messages:
        return None

    # Check for existing summary block in content messages
    found_summary, cleaned_content = extract_existing_summary(content_messages)
    active_previous_summary = previous_summary or found_summary

    cut_idx = find_clean_cut_index(cleaned_content, protected_turns)
    if cut_idx is None or cut_idx <= 0:
        return None

    eligible_messages = cleaned_content[:cut_idx]
    protected_messages = cleaned_content[cut_idx:]

    eligible_turns = sum(1 for m in eligible_messages if m.get("role") == "assistant")
    if eligible_turns == 0:
        return None

    est_tokens = estimate_tokens(eligible_messages)
    if est_tokens < min_tokens:
        return None

    return GardenPlan(
        eligible_messages=eligible_messages,
        eligible_turns=eligible_turns,
        estimated_tokens=est_tokens,
        cut_index=cut_idx,
        system_messages=system_messages,
        protected_messages=protected_messages,
        previous_summary=active_previous_summary,
    )
