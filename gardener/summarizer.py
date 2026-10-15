"""Gardener summarizer execution."""

import logging
from typing import Any, Dict, Optional
import httpx

from gardener.config import AppConfig
from gardener.planner import GardenPlan
from gardener.prompt import build_gardener_user_request

logger = logging.getLogger("gardener.summarizer")


async def run_garden_summarizer(
    plan: GardenPlan,
    requested_model: str,
    config: AppConfig,
    client_auth_header: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[str]:
    """Execute a single-turn completion with the gardener model to produce the state note."""
    model = config.gardener.model or requested_model
    base_url = (config.gardener.base_url or config.upstream.base_url).rstrip("/")
    api_key = config.gardener.api_key or config.upstream.api_key

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    elif client_auth_header:
        headers["Authorization"] = client_auth_header

    prompt = build_gardener_user_request(
        eligible_messages=plan.eligible_messages,
        previous_summary=plan.previous_summary,
    )

    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "temperature": 0.2,
        "stream": False,
    }

    url = f"{base_url}/chat/completions"

    should_close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=60.0)
        should_close_client = True

    try:
        logger.info(
            f"🌿 Running gardener with model '{model}' on {plan.eligible_turns} turn(s) "
            f"(~{plan.estimated_tokens} tokens)..."
        )
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()

        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            logger.warning("Gardener call returned no choices.")
            return None

        message = choices[0].get("message", {})
        summary = message.get("content", "").strip()

        if not summary:
            logger.warning("Gardener produced empty content.")
            return None

        logger.info(f"🌿 Compaction complete. Summary length: {len(summary)} chars.")
        return summary

    except Exception as e:
        logger.error(f"Gardener call failed: {e}", exc_info=True)
        return None
    finally:
        if should_close_client:
            await client.aclose()
