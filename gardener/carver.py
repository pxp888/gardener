"""Gardener context carver: surgical in-place editing harness."""

import copy
import json
import logging
from typing import Any, Dict, List, Optional
import httpx

from gardener.config import AppConfig
from gardener.planner import GardenPlan, estimate_tokens
from gardener.prompt import (
    CARVER_SYSTEM_PROMPT,
    CARVER_TOOLS,
    build_carver_user_request,
)

logger = logging.getLogger("gardener.carver")


def apply_replace_content(
    messages: List[Dict[str, Any]],
    message_index: int,
    target_content: str,
    replacement_content: str,
) -> str:
    """Surgically replace exact target content within a message."""
    if message_index < 0 or message_index >= len(messages):
        return f"Error: message_index {message_index} out of range (0 to {len(messages) - 1})."

    msg = messages[message_index]
    content = msg.get("content")
    if not isinstance(content, str):
        content = json.dumps(content) if content is not None else ""

    if target_content not in content:
        # Check if stripping leading/trailing whitespace helps match
        if target_content.strip() and target_content.strip() in content:
            target_content = target_content.strip()
        else:
            return (
                f"Error: target_content not found in Message #{message_index}. "
                "Ensure target_content matches characters in the message verbatim."
            )

    new_content = content.replace(target_content, replacement_content, 1)
    msg["content"] = new_content
    chars_delta = len(content) - len(new_content)
    return (
        f"Success: Updated Message #{message_index}. "
        f"Reduced by {chars_delta} chars ({len(content)} -> {len(new_content)})."
    )


def apply_remove_message(
    messages: List[Dict[str, Any]],
    message_index: int,
    reason: str = "",
) -> str:
    """Mark a message for removal."""
    if message_index < 0 or message_index >= len(messages):
        return f"Error: message_index {message_index} out of range."

    messages[message_index]["__removed__"] = True
    role = messages[message_index].get("role", "unknown")
    return f"Success: Message #{message_index} ({role}) marked for removal ({reason or 'no reason'})."


def sanitize_carved_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Clean up and validate message list while strictly preserving OpenAI API invariants.

    Invariants:
    1. Every assistant `tool_calls` must have matching `role: 'tool'` messages with identical `tool_call_id`.
    2. If a tool message was marked removed but its assistant call remains, replace content with a pruned marker.
    3. If an assistant call message is dropped, drop its subsequent tool results too.
    """
    cleaned: List[Dict[str, Any]] = []
    removed_tool_call_ids = set()

    # Pass 1: Identify removed assistant tool_calls
    for msg in messages:
        if msg.get("__removed__"):
            tcs = msg.get("tool_calls")
            if tcs and isinstance(tcs, list):
                for tc in tcs:
                    tc_id = tc.get("id")
                    if tc_id:
                        removed_tool_call_ids.add(tc_id)

    # Pass 2: Filter and sanitize
    for msg in messages:
        msg_copy = copy.deepcopy(msg)
        is_removed = msg_copy.pop("__removed__", False)
        role = msg_copy.get("role")

        if role == "tool":
            tc_id = msg_copy.get("tool_call_id")
            # If the calling assistant message was removed, drop this tool result too
            if tc_id and tc_id in removed_tool_call_ids:
                continue

            # If this tool message was marked removed but caller still exists,
            # we MUST keep the tool message to satisfy OpenAI API schemas, but empty its payload
            if is_removed:
                msg_copy["content"] = "[Output pruned by Gardener]"
            elif not msg_copy.get("content"):
                msg_copy["content"] = "[Empty output]"

            cleaned.append(msg_copy)
            continue

        if is_removed:
            continue

        # Drop empty user or assistant messages that have no tool calls
        if not msg_copy.get("content") and not msg_copy.get("tool_calls"):
            continue

        cleaned.append(msg_copy)

    return cleaned


async def run_garden_carver(
    plan: GardenPlan,
    requested_model: str,
    config: AppConfig,
    client_auth_header: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Execute iterative surgical carving on eligible messages."""
    model = config.gardener.model or requested_model
    base_url = (config.gardener.base_url or config.upstream.base_url).rstrip("/")
    api_key = config.gardener.api_key or config.upstream.api_key

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    elif client_auth_header:
        headers["Authorization"] = client_auth_header

    # Create working scratchpad copy
    staged_messages = copy.deepcopy(plan.eligible_messages)
    tokens_before = estimate_tokens(staged_messages)

    initial_prompt = build_carver_user_request(staged_messages)
    gardener_conversation: List[Dict[str, Any]] = [
        {"role": "system", "content": CARVER_SYSTEM_PROMPT},
        {"role": "user", "content": initial_prompt},
    ]

    url = f"{base_url}/chat/completions"

    should_close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=60.0)
        should_close_client = True

    try:
        logger.info(
            f"🌿 Starting Context Carver on {len(staged_messages)} messages "
            f"(~{tokens_before} tokens) with model '{model}'..."
        )

        total_edits = 0
        for step in range(config.gardener.max_carve_steps):
            payload = {
                "model": model,
                "messages": gardener_conversation,
                "tools": CARVER_TOOLS,
                "tool_choice": "auto",
                "temperature": 0.2,
                "stream": False,
            }

            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()

            data = resp.json()
            choices = data.get("choices", [])
            if not choices:
                break

            response_msg = choices[0].get("message", {})
            tool_calls = response_msg.get("tool_calls") or []

            # Append assistant turn to gardener dialogue
            gardener_conversation.append(response_msg)

            if not tool_calls:
                logger.debug(f"Gardener finished thinking at step {step + 1}.")
                break

            finished = False
            for tc in tool_calls:
                func_name = tc.get("function", {}).get("name")
                raw_args = tc.get("function", {}).get("arguments", "{}")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:
                    args = {}

                if func_name == "finish":
                    finished = True
                    result_content = "Gardening finished. Changes submitted."
                elif func_name == "replace_message_content":
                    msg_idx = args.get("message_index", -1)
                    target = args.get("target_content", "")
                    replacement = args.get("replacement_content", "")
                    result_content = apply_replace_content(staged_messages, msg_idx, target, replacement)
                    total_edits += 1
                elif func_name == "remove_message":
                    msg_idx = args.get("message_index", -1)
                    reason = args.get("reason", "")
                    result_content = apply_remove_message(staged_messages, msg_idx, reason)
                    total_edits += 1
                else:
                    result_content = f"Unknown tool: {func_name}"

                gardener_conversation.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "content": result_content,
                })

            if finished:
                logger.info(f"🌿 Gardener signaled finish at step {step + 1}.")
                break

        final_messages = sanitize_carved_messages(staged_messages)
        tokens_after = estimate_tokens(final_messages)
        saved = max(0, tokens_before - tokens_after)
        pct = (saved / tokens_before * 100) if tokens_before > 0 else 0
        logger.info(
            f"🌿 Carving complete: {total_edits} edit(s) applied. "
            f"Tokens: {tokens_before} -> {tokens_after} ({saved} tokens saved, {pct:.1f}% reduction)."
        )

        return final_messages

    except Exception as e:
        logger.error(f"Context carver failed: {e}", exc_info=True)
        return None
    finally:
        if should_close_client:
            await client.aclose()
