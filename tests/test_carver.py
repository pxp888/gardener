import json
import pytest
import httpx
from gardener.carver import (
    apply_replace_content,
    apply_remove_message,
    sanitize_carved_messages,
    run_garden_carver,
)
from gardener.config import AppConfig
from gardener.planner import GardenPlan


def test_apply_replace_content_success():
    messages = [
        {"role": "tool", "content": "Line 1\nBOILERPLATE TO REMOVE\nLine 3"}
    ]
    res = apply_replace_content(messages, 0, "BOILERPLATE TO REMOVE", "[Pruned]")
    assert "Success" in res
    assert messages[0]["content"] == "Line 1\n[Pruned]\nLine 3"


def test_apply_replace_content_not_found():
    messages = [
        {"role": "tool", "content": "Hello World"}
    ]
    res = apply_replace_content(messages, 0, "Nonexistent text", "")
    assert "Error" in res
    assert messages[0]["content"] == "Hello World"


def test_apply_replace_content_out_of_bounds():
    messages = [{"role": "user", "content": "hi"}]
    res = apply_replace_content(messages, 5, "hi", "bye")
    assert "Error" in res


def test_apply_remove_message():
    messages = [
        {"role": "user", "content": "Turn 1"},
        {"role": "assistant", "content": "Turn 1 answer"},
    ]
    res = apply_remove_message(messages, 0, "Dead-end question")
    assert "Success" in res
    assert messages[0].get("__removed__") is True


def test_sanitize_carved_messages_tool_pairing_safety():
    # Assistant has tool call c1; tool has tool result c1
    messages = [
        {"role": "user", "content": "Find users"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "db", "arguments": ""}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "huge 500 line db dump", "__removed__": True},
        {"role": "assistant", "content": "Found 2 users"},
    ]
    # Tool was marked __removed__, but assistant tool_call remains.
    # To satisfy OpenAI API invariant, the tool message must NOT be deleted, but its content pruned!
    cleaned = sanitize_carved_messages(messages)
    assert len(cleaned) == 4
    assert cleaned[2]["role"] == "tool"
    assert cleaned[2]["content"] == "[Output pruned by Gardener]"
    assert "__removed__" not in cleaned[2]


def test_sanitize_carved_messages_drop_paired_assistant_and_tool():
    # If assistant message with tool_calls is removed, the child tool result is also safely dropped
    messages = [
        {"role": "user", "content": "Dead end exploration"},
        {
            "role": "assistant",
            "content": "Trying dead end",
            "tool_calls": [{"id": "c2", "function": {"name": "test", "arguments": ""}}],
            "__removed__": True,
        },
        {"role": "tool", "tool_call_id": "c2", "content": "error trace"},
        {"role": "assistant", "content": "Moving on to real work"},
    ]
    cleaned = sanitize_carved_messages(messages)
    assert len(cleaned) == 2
    assert cleaned[0]["content"] == "Dead end exploration"
    assert cleaned[1]["content"] == "Moving on to real work"


@pytest.mark.anyio
async def test_run_garden_carver_multi_step_loop():
    # Test iterative carving: Step 1 calls replace_message_content, Step 2 calls finish
    step_count = 0

    def mock_carver_handler(request: httpx.Request):
        nonlocal step_count
        step_count += 1
        body = json.loads(request.content.decode("utf-8"))
        messages = body.get("messages", [])

        if step_count == 1:
            # Model decides to edit message 1
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "I will carve out the verbose database output.",
                                "tool_calls": [
                                    {
                                        "id": "edit_1",
                                        "type": "function",
                                        "function": {
                                            "name": "replace_message_content",
                                            "arguments": json.dumps({
                                                "message_index": 1,
                                                "target_content": "500 lines of verbose database dump",
                                                "replacement_content": "[Found 2 records]",
                                            }),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        elif step_count == 2:
            # Model receives tool result, thinks, and calls finish
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Context is now clean.",
                                "tool_calls": [
                                    {
                                        "id": "finish_1",
                                        "type": "function",
                                        "function": {
                                            "name": "finish",
                                            "arguments": json.dumps({"notes": "Carved out db dump"}),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )

        return httpx.Response(400)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_carver_handler))

    plan = GardenPlan(
        eligible_messages=[
            {"role": "user", "content": "Check database"},
            {"role": "tool", "tool_call_id": "c1", "content": "500 lines of verbose database dump"},
            {"role": "assistant", "content": "Database check done."},
        ],
        eligible_turns=1,
        estimated_tokens=50,
        cut_index=3,
        system_messages=[],
        protected_messages=[],
    )

    config = AppConfig()
    config.gardener.strategy = "carve"
    config.gardener.max_carve_steps = 4

    carved = await run_garden_carver(
        plan=plan,
        requested_model="test-model",
        config=config,
        client=client,
    )

    assert carved is not None
    assert step_count == 2
    assert carved[1]["content"] == "[Found 2 records]"
    assert carved[0]["content"] == "Check database"
    assert carved[2]["content"] == "Database check done."
