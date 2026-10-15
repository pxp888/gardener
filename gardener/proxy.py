"""FastAPI proxy server for turn-based context gardening."""

import copy
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from gardener.config import AppConfig, load_config
from gardener.planner import SessionManager, derive_session_id, plan_garden
from gardener.summarizer import run_garden_summarizer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("gardener.proxy")


def create_app(config: Optional[AppConfig] = None) -> FastAPI:
    if config is None:
        config = load_config()

    session_manager = SessionManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Create shared HTTP client with keep-alive
        app.state.http_client = httpx.AsyncClient(timeout=120.0)
        yield
        await app.state.http_client.aclose()

    app = FastAPI(
        title="Gardener Proxy",
        description="Transparent context compaction proxy for OpenAI-compatible APIs",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.session_manager = session_manager

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "gardener": {
                "turns_frequency": config.gardener.turns,
                "protected_turns": config.gardener.protected_turns,
                "model": config.gardener.model or "mirror-main-agent",
            },
            "upstream": config.upstream.base_url,
        }

    @app.get("/v1/models")
    async def list_models(request: Request):
        """Pass through models listing from upstream provider."""
        client: httpx.AsyncClient = app.state.http_client
        upstream_url = f"{config.upstream.base_url.rstrip('/')}/models"

        headers = {}
        auth = request.headers.get("authorization") or (
            f"Bearer {config.upstream.api_key}" if config.upstream.api_key else None
        )
        if auth:
            headers["Authorization"] = auth

        try:
            resp = await client.get(upstream_url, headers=headers)
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "application/json"),
            )
        except Exception as e:
            logger.error(f"Failed to fetch upstream models: {e}")
            raise HTTPException(status_code=502, detail=f"Upstream provider error: {str(e)}")

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        client: httpx.AsyncClient = app.state.http_client

        try:
            body: Dict[str, Any] = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        requested_model = body.get("model", "")
        raw_messages = body.get("messages", [])

        if not isinstance(raw_messages, list) or len(raw_messages) == 0:
            raise HTTPException(status_code=400, detail="Missing or invalid 'messages' array")

        headers_dict = {k.lower(): v for k, v in request.headers.items()}
        session_id = derive_session_id(headers_dict, body)
        session = session_manager.get_or_create(session_id)

        # Count current assistant turns
        current_assistant_count = sum(1 for m in raw_messages if m.get("role") == "assistant")

        # Track turn progress
        if current_assistant_count > session.last_seen_assistant_count:
            turn_delta = current_assistant_count - session.last_seen_assistant_count
            session.turns_since_garden += turn_delta
            session.last_seen_assistant_count = current_assistant_count

        client_auth = request.headers.get("authorization")

        # Check if gardening is due
        should_garden = (
            session.turns_since_garden >= config.gardener.turns
            or (session.previous_summary is None and current_assistant_count > config.gardener.protected_turns)
        )

        modified_body = copy.deepcopy(body)

        if should_garden:
            plan = plan_garden(
                messages=raw_messages,
                protected_turns=config.gardener.protected_turns,
                min_tokens=config.gardener.min_tokens_to_garden,
                previous_summary=session.previous_summary,
            )

            if plan:
                logger.info(
                    f"🌿 [session: {session_id}] Folding {plan.eligible_turns} turn(s) older than "
                    f"last {config.gardener.protected_turns} turns..."
                )
                summary = await run_garden_summarizer(
                    plan=plan,
                    requested_model=requested_model,
                    config=config,
                    client_auth_header=client_auth,
                    client=client,
                )

                if summary:
                    session.previous_summary = summary
                    session.turns_since_garden = 0

                    state_block = f"<gardener-state>\n{summary}\n</gardener-state>"

                    if config.gardener.state_injection_mode == "user_assistant":
                        state_messages = [
                            {"role": "user", "content": f"Context update:\n{state_block}"},
                            {"role": "assistant", "content": "Understood. Proceeding with updated context."},
                        ]
                    else:
                        # Default "system" mode: injected as structured system message
                        state_messages = [{"role": "system", "content": state_block}]

                    # Assembled messages: Base System -> Gardener State -> Protected Tail
                    modified_messages = plan.system_messages + state_messages + plan.protected_messages
                    modified_body["messages"] = modified_messages
                    logger.info(
                        f"🌿 Injected gardener state. Message count reduced from "
                        f"{len(raw_messages)} to {len(modified_messages)}."
                    )
            else:
                logger.debug(f"[session: {session_id}] Turn criteria met, but insufficient aged-out tokens to garden.")

        # Forward to upstream LLM provider
        upstream_url = f"{config.upstream.base_url.rstrip('/')}/chat/completions"
        upstream_headers = {"Content-Type": "application/json"}

        # Upstream auth priority: config.upstream.api_key -> client Authorization header
        if config.upstream.api_key:
            upstream_headers["Authorization"] = f"Bearer {config.upstream.api_key}"
        elif client_auth:
            upstream_headers["Authorization"] = client_auth

        is_streaming = modified_body.get("stream", False)

        try:
            if is_streaming:
                return await _stream_upstream(client, upstream_url, upstream_headers, modified_body)
            else:
                resp = await client.post(
                    upstream_url,
                    headers=upstream_headers,
                    json=modified_body,
                    timeout=180.0,
                )
                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"),
                )
        except httpx.RequestError as e:
            logger.error(f"Error forwarding request to upstream: {e}")
            raise HTTPException(status_code=502, detail=f"Failed to communicate with upstream provider: {str(e)}")

    async def _stream_upstream(
        client: httpx.AsyncClient,
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
    ) -> StreamingResponse:
        req = client.build_request("POST", url, headers=headers, json=payload, timeout=180.0)
        upstream_resp = await client.send(req, stream=True)

        async def stream_generator() -> AsyncGenerator[bytes, None]:
            try:
                async for chunk in upstream_resp.aiter_bytes():
                    yield chunk
            finally:
                await upstream_resp.aclose()

        return StreamingResponse(
            stream_generator(),
            status_code=upstream_resp.status_code,
            media_type=upstream_resp.headers.get("content-type", "text/event-stream"),
        )

    return app
