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
    """Assemble the gardener prompt matching Pi's buildGardenerRequest."""
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
