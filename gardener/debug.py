"""Debug utilities for dumping context before and after compaction."""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from gardener.planner import estimate_tokens

logger = logging.getLogger("gardener.debug")


def format_messages_markdown(
    title: str,
    messages: List[Dict[str, Any]],
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Render a conversation messages array as clean, readable Markdown."""
    tokens = estimate_tokens(messages)
    lines: List[str] = [f"# {title}", ""]

    meta = {
        "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Total Messages": len(messages),
        "Estimated Tokens": f"~{tokens:,}",
    }
    if metadata:
        meta.update(metadata)

    lines.append("## Summary")
    lines.append("")
    for k, v in meta.items():
        lines.append(f"- **{k}**: {v}")
    lines.append("")
    lines.append("---")
    lines.append("")

    lines.append("## Messages")
    lines.append("")
    for idx, msg in enumerate(messages):
        role = msg.get("role", "unknown").upper()
        header = f"### [{idx}] `{role}`"
        tool_id = msg.get("tool_call_id")
        if tool_id:
            header += f" (id: `{tool_id}`)"
        lines.append(header)
        lines.append("")

        tool_calls = msg.get("tool_calls")
        if tool_calls and isinstance(tool_calls, list):
            lines.append("**Tool Calls:**")
            for tc in tool_calls:
                func = tc.get("function", {})
                fn_name = func.get("name", "")
                fn_args = func.get("arguments", "")
                tc_id = tc.get("id", "")
                lines.append(f"- `{fn_name}({fn_args})` [id: `{tc_id}`]")
            lines.append("")

        content = msg.get("content")
        if content is None or content == "":
            lines.append("_<empty content>_")
        elif isinstance(content, list):
            lines.append("```json")
            lines.append(json.dumps(content, indent=2))
            lines.append("```")
        else:
            lines.append(str(content))

        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def dump_compaction_debug(
    before_messages: List[Dict[str, Any]],
    after_messages: List[Dict[str, Any]],
    strategy: str,
    output_dir: str = ".",
) -> None:
    """Write before.md and after.md files to disk."""
    try:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        tok_before = estimate_tokens(before_messages)
        tok_after = estimate_tokens(after_messages)
        saved = max(0, tok_before - tok_after)
        pct = (saved / tok_before * 100) if tok_before > 0 else 0

        before_meta = {
            "Strategy": strategy,
            "Compaction State": "Before Compaction",
        }
        after_meta = {
            "Strategy": strategy,
            "Compaction State": "After Compaction",
            "Messages Reduction": f"{len(before_messages)} -> {len(after_messages)}",
            "Tokens Saved": f"~{saved:,} ({pct:.1f}% reduction)",
        }

        before_md = format_messages_markdown("Context Before Compaction", before_messages, before_meta)
        after_md = format_messages_markdown("Context After Compaction", after_messages, after_meta)

        (out_path / "before.md").write_text(before_md, encoding="utf-8")
        (out_path / "after.md").write_text(after_md, encoding="utf-8")

        logger.info(f"🌿 Debug context written to {out_path / "before.md"} and {out_path / "after.md"}")
    except Exception as e:
        logger.warning(f"Failed to write debug markdown files: {e}")
