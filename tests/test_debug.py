import json
import pytest
from pathlib import Path
from gardener.debug import format_messages_markdown, dump_compaction_debug


def test_format_messages_markdown():
    messages = [
        {"role": "user", "content": "Hello gardener"},
        {
            "role": "assistant",
            "content": "Running tool",
            "tool_calls": [{"id": "t1", "function": {"name": "read", "arguments": "{\"path\": \"file.txt\"}"}}],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "file content line 1\nline 2"},
    ]
    md = format_messages_markdown("Test Context", messages, {"Strategy": "carve"})
    assert "# Test Context" in md
    assert "**Strategy**: carve" in md
    assert "[0] `USER`" in md
    assert "Hello gardener" in md
    assert "`read({\"path\": \"file.txt\"})` [id: `t1`]" in md
    assert "file content line 1" in md


def test_dump_compaction_debug(tmp_path):
    before = [
        {"role": "user", "content": "Check DB"},
        {"role": "tool", "tool_call_id": "c1", "content": "500 lines of DB output"},
    ]
    after = [
        {"role": "user", "content": "Check DB"},
        {"role": "tool", "tool_call_id": "c1", "content": "[Pruned DB output]"},
    ]

    dump_compaction_debug(before, after, strategy="carve", output_dir=str(tmp_path))

    before_file = tmp_path / "before.md"
    after_file = tmp_path / "after.md"

    assert before_file.is_file()
    assert after_file.is_file()

    before_text = before_file.read_text(encoding="utf-8")
    after_text = after_file.read_text(encoding="utf-8")

    assert "# Context Before Compaction" in before_text
    assert "500 lines of DB output" in before_text

    assert "# Context After Compaction" in after_text
    assert "[Pruned DB output]" in after_text
    assert "Tokens Saved" in after_text
