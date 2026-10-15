import pytest
from gardener.planner import find_clean_cut_index, plan_garden, extract_existing_summary


def test_no_garden_when_short_history():
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    plan = plan_garden(messages, protected_turns=5, min_tokens=10)
    assert plan is None


def test_clean_cut_boundary():
    # 3 assistant turns with protected_turns=2 -> should cut before turn 2
    messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "Turn 1 request"},
        {"role": "assistant", "content": "Turn 1 answer"},
        {"role": "user", "content": "Turn 2 request"},
        {"role": "assistant", "content": "Turn 2 answer"},
        {"role": "user", "content": "Turn 3 request"},
        {"role": "assistant", "content": "Turn 3 answer"},
    ]
    plan = plan_garden(messages, protected_turns=2, min_tokens=5)
    assert plan is not None
    assert plan.eligible_turns == 1
    # Turn 1 should be eligible
    assert len(plan.eligible_messages) == 2
    assert plan.eligible_messages[0]["content"] == "Turn 1 request"
    assert plan.eligible_messages[1]["content"] == "Turn 1 answer"
    # Protected tail should contain Turn 2 and Turn 3
    assert len(plan.protected_messages) == 4
    assert plan.protected_messages[0]["content"] == "Turn 2 request"


def test_tool_call_integrity():
    # Ensure assistant with tool_call and following tool result are not severed
    messages = [
        {"role": "system", "content": "Sys"},
        {"role": "user", "content": "Find files"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "ls", "arguments": ""}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "file1.txt\nfile2.txt"},
        {"role": "assistant", "content": "Found files."},
        {"role": "user", "content": "Next step"},
        {"role": "assistant", "content": "Done."},
    ]
    # protected_turns = 1 -> keep only the last assistant turn ("Done.")
    plan = plan_garden(messages, protected_turns=1, min_tokens=5)
    assert plan is not None
    # All earlier messages (user -> assistant tool call -> tool result -> assistant response)
    # should be eligible together without severing
    assert plan.protected_messages[0]["content"] == "Next step"
    assert plan.protected_messages[1]["content"] == "Done."


def test_extract_existing_summary():
    messages = [
        {"role": "system", "content": "<gardener-state>\nPrevious notes\n</gardener-state>"},
        {"role": "user", "content": "Keep going"},
    ]
    summary, cleaned = extract_existing_summary(messages)
    assert summary == "Previous notes"
    assert len(cleaned) == 1
    assert cleaned[0]["content"] == "Keep going"

def test_agent_multistep_tool_loop_cut():
    messages = [
        {"role": "system", "content": "You are a helpful coding agent."},
        {"role": "user", "content": "Refactor the module"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "ls"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "file1.py, file2.py"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "function": {"name": "read_file"}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "def old(): pass"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c3", "function": {"name": "edit_file"}}]},
        {"role": "tool", "tool_call_id": "c3", "content": "ok"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c4", "function": {"name": "run_tests"}}]},
        {"role": "tool", "tool_call_id": "c4", "content": "passed"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c5", "function": {"name": "git_commit"}}]},
        {"role": "tool", "tool_call_id": "c5", "content": "committed"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c6", "function": {"name": "git_push"}}]},
        {"role": "tool", "tool_call_id": "c6", "content": "pushed"},
        {"role": "assistant", "content": "All done!"},
    ]
    plan = plan_garden(messages, protected_turns=5, min_tokens=5)
    assert plan is not None
    assert plan.eligible_turns == 2
    assert len(plan.eligible_messages) == 5
    assert plan.eligible_messages[0]["content"] == "Refactor the module"
    assert plan.eligible_messages[1]["tool_calls"][0]["id"] == "c1"
    assert plan.eligible_messages[2]["tool_call_id"] == "c1"
    assert plan.eligible_messages[3]["tool_calls"][0]["id"] == "c2"
    assert plan.eligible_messages[4]["tool_call_id"] == "c2"

    assert plan.protected_messages[0]["tool_calls"][0]["id"] == "c3"
    assert plan.protected_messages[-1]["content"] == "All done!"
