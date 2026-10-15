"""Gardener system prompt and conversation formatting."""

from typing import Any, Dict, List, Optional

GARDENER_PROMPT = """You are the Context Gardener for a coding agent. Your responsibility is to reduce the agent's conversation context while preserving everything necessary to continue its current objective correctly.

Review the eligible historical conversation and produce a concise replacement that allows the main agent to resume work without repeating investigations, losing important discoveries, or undoing successful changes.

Prioritize preserving:

1. The current objective, requirements, acceptance criteria, and constraints.

2. Current implementation state, including relevant files, functions, interfaces, and successful changes.

3. Important discoveries, including failed searches, negative findings, environmental limitations, and rejected approaches.

4. Decisions already made and their rationale.

5. Outstanding work, unresolved questions, blockers, and the next useful steps.

6. Relevant user instructions and preferences.

Remove verbose tool outputs, irrelevant exploration, redundant explanations, superseded plans, and boilerplate that can safely be regenerated.

Do not treat a failed investigation as worthless. Preserve its conclusion whenever it prevents repeated work or constrains future decisions. Prefer a short factual statement over a transcript of the investigation.

Do not invent facts, infer that an operation succeeded when only an attempt is recorded, or claim that files contain particular code unless the available context supports that claim.

The newest protected turns are outside your editing scope. Preserve their contents exactly.

Produce a compact, factual context replacement. Optimize for the main agent's ability to continue working, not for a comprehensive historical narrative. When uncertain whether information is essential, preserve it in a concise form rather than deleting it.

Operational rules:
- Keep your internal reasoning/thinking concise. Do not overthink; proceed directly to extracting and updating the state.
- Your replacement will be shown to the main agent as its session state, in place of the summarized history.
- Output only the replacement context: no preamble, no closing remarks, no markdown code fences."""


def serialize_messages(messages: List[Dict[str, Any]]) -> str:
    """Format messages array into clean, structured text for the gardener."""
    formatted_parts: List[str] = []

    for msg in messages:
        role = msg.get("role", "unknown").upper()
        content = msg.get("content")

        # Handle content being list of blocks (multimodal / OpenAI standard)
        if isinstance(content, list):
            text_pieces = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_pieces.append(item.get("text", ""))
                elif isinstance(item, str):
                    text_pieces.append(item)
            text_content = "\n".join(text_pieces)
        elif content is None:
            text_content = ""
        else:
            text_content = str(content)

        # Include tool calls if present on assistant
        tool_calls = msg.get("tool_calls")
        if tool_calls and isinstance(tool_calls, list):
            call_reprs = []
            for tc in tool_calls:
                func = tc.get("function", {})
                call_reprs.append(f"Call {func.get('name')}({func.get('arguments', '')})")
            if text_content:
                text_content += f"\n[Tool Calls]: {'; '.join(call_reprs)}"
            else:
                text_content = f"[Tool Calls]: {'; '.join(call_reprs)}"

        # Include tool results
        if role == "TOOL":
            tool_id = msg.get("tool_call_id", "")
            formatted_parts.append(f"[TOOL RESULT (id: {tool_id})]:\n{text_content}")
        else:
            formatted_parts.append(f"[{role}]:\n{text_content}")

    return "\n\n".join(formatted_parts)


def build_gardener_user_request(
    eligible_messages: List[Dict[str, Any]],
    previous_summary: Optional[str] = None,
    custom_instructions: Optional[str] = None,
) -> str:
    """Assemble the gardener prompt for summarization."""
    conversation_text = serialize_messages(eligible_messages)

    parts: List[str] = [GARDENER_PROMPT]

    if previous_summary:
        parts.append(
            "The conversation below is newer than this previous state note. Merge it into an updated state; "
            "do not repeat verbatim what is already covered unless it changed.\n<previous-context>\n"
            + previous_summary.strip()
            + "\n</previous-context>"
        )

    if custom_instructions and custom_instructions.strip():
        parts.append(f"Additional focus requested by user: {custom_instructions.strip()}")

    parts.append(f"<conversation>\n{conversation_text}\n</conversation>")

    return "\n\n".join(parts)


CARVER_SYSTEM_PROMPT = """You are the Context Carver for a coding agent.
Your responsibility is to reduce conversation bloat while keeping critical code, findings, user constraints, and architectural decisions 100% intact.

Instead of writing a broad summary from scratch, you have surgical editing tools to CARVE OUT the fluff from the historical conversation:
1. `replace_message_content(message_index, target_content, replacement_content)`:
   - Replaces exact substrings in a message with a concise replacement, or with "" to delete.
   - Use this to prune large tool outputs (file dumps, logs, compiler output) down to just their essential findings, or to trim rambling explanations.
   - `target_content` must match the existing message text verbatim.
2. `remove_message(message_index, reason)`:
   - Removes an entire message when a turn was an aborted dead-end or superseded confusion.
3. `finish(notes)`:
   - Call this when all unnecessary fluff has been carved out.

Carving Guidelines:
- PRESERVE:
  * Working code snippets, function signatures, and interface definitions verbatim.
  * Explicit user requirements, preferences, and constraints.
  * Final conclusions of investigations (e.g. 'auth handled in middleware.py').
  * Active TODOs, unresolved blockers, and current architectural decisions.
- CARVE OUT:
  * Verbose tool results (large file dumps, grep outputs with dozens of matches, repetitive test logs).
  * Redundant boilerplate that can easily be regenerated.
  * Aborted investigations and dead-ends (replace with a 1-sentence finding or remove).

You may think step-by-step and make multiple edits across rounds. When the context is lean and clean, call finish."""

CARVER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "replace_message_content",
            "description": "Surgically replace an exact substring within a message's content. Use an empty string '' as replacement_content to delete, or provide a concise summary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_index": {
                        "type": "integer",
                        "description": "The zero-based index of the message to modify (as labeled in the conversation)."
                    },
                    "target_content": {
                        "type": "string",
                        "description": "The exact character substring to find and replace in the message."
                    },
                    "replacement_content": {
                        "type": "string",
                        "description": "The replacement text (can be '' to delete, or a concise factual note)."
                    }
                },
                "required": ["message_index", "target_content", "replacement_content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remove_message",
            "description": "Remove an entire message from the conversation (e.g. an irrelevant dead-end turn).",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_index": {
                        "type": "integer",
                        "description": "The zero-based index of the message to remove."
                    },
                    "reason": {
                        "type": "string",
                        "description": "Brief explanation of why this message is being removed."
                    }
                },
                "required": ["message_index"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Signal that context carving is complete and the remaining context is clean and minimal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "notes": {
                        "type": "string",
                        "description": "Brief summary of what was carved or preserved."
                    }
                }
            }
        }
    }
]


def format_messages_for_carver(messages: List[Dict[str, Any]]) -> str:
    """Format messages array with explicit indices for surgical editing."""
    parts: List[str] = []
    for idx, msg in enumerate(messages):
        role = msg.get("role", "unknown").upper()
        content = msg.get("content")
        if isinstance(content, list):
            text_pieces = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_pieces.append(item.get("text", ""))
                elif isinstance(item, str):
                    text_pieces.append(item)
            text_content = "\n".join(text_pieces)
        elif content is None:
            text_content = ""
        else:
            text_content = str(content)

        header = f"=== [Message Index: {idx} | Role: {role}"
        tool_id = msg.get("tool_call_id")
        if tool_id:
            header += f" | tool_call_id: {tool_id}"
        header += " ] ==="

        tool_calls = msg.get("tool_calls")
        tc_info = ""
        if tool_calls and isinstance(tool_calls, list):
            calls_repr = []
            for tc in tool_calls:
                func = tc.get("function", {})
                calls_repr.append(f"{func.get('name')}({func.get('arguments', '')}) [id: {tc.get('id', '')}]")
            tc_info = f"[Tool Calls]: {'; '.join(calls_repr)}\n"

        parts.append(f"{header}\n{tc_info}{text_content}".rstrip())
    return "\n\n".join(parts)


def build_carver_user_request(
    eligible_messages: List[Dict[str, Any]],
    custom_instructions: Optional[str] = None,
) -> str:
    """Assemble the prompt requesting surgical carving of eligible messages."""
    formatted = format_messages_for_carver(eligible_messages)
    req = [
        f"Review the eligible conversation messages below (Indices 0 to {len(eligible_messages)-1}). Use your editing tools to carve out boilerplate, verbose tool dumps, and dead-ends while preserving code, constraints, and findings verbatim.",
        f"<eligible-conversation count={len(eligible_messages)}>\n{formatted}\n</eligible-conversation>"
    ]
    if custom_instructions and custom_instructions.strip():
        req.append(f"Custom user focus: {custom_instructions.strip()}")
    return "\n\n".join(req)
