import json
import pytest
import httpx
from fastapi.testclient import TestClient
from gardener.config import AppConfig
from gardener.proxy import create_app


@pytest.fixture
def mock_upstream():
    captured_requests = []

    def handler(request: httpx.Request):
        captured_requests.append(request)
        url_path = request.url.path

        if url_path.endswith("/models"):
            return httpx.Response(
                200,
                json={"data": [{"id": "gpt-4o", "object": "model"}]},
            )

        if url_path.endswith("/chat/completions"):
            body = json.loads(request.content.decode("utf-8"))
            messages = body.get("messages", [])

            # If this is a gardener request
            if len(messages) == 1 and "<conversation>" in messages[0].get("content", ""):
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "- Checked records: found 2 users\n- Created server.py",
                                }
                            }
                        ]
                    },
                )

            # Normal assistant response
            if body.get("stream"):
                chunk = (
                    b'data: {"choices":[{"delta":{"content":"Hello from mock!"}}]}\n\n'
                    b"data: [DONE]\n\n"
                )
                return httpx.Response(
                    200,
                    content=chunk,
                    headers={"Content-Type": "text/event-stream"},
                )

            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": "Hello from mock!"}}
                    ]
                },
            )

        return httpx.Response(404)

    return handler, captured_requests


def test_models_passthrough(mock_upstream):
    handler, captured = mock_upstream
    config = AppConfig()
    app = create_app(config)

    with TestClient(app) as client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        resp = client.get("/v1/models")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"][0]["id"] == "gpt-4o"


def test_gardening_triggered(mock_upstream):
    handler, captured = mock_upstream
    config = AppConfig()
    # Garden every 1 turn, keep last 2 turns
    config.gardener.turns = 1
    config.gardener.protected_turns = 2
    config.gardener.min_tokens_to_garden = 5

    app = create_app(config)

    # 4 assistant turns -> 2 older turns should be folded into gardener state note
    conversation = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "Check records"},
        {"role": "assistant", "content": "Checking records... done."},
        {"role": "user", "content": "Create server.py"},
        {"role": "assistant", "content": "Created server.py skeleton."},
        {"role": "user", "content": "Add health endpoint"},
        {"role": "assistant", "content": "Added /health endpoint."},
        {"role": "user", "content": "Run tests"},
        {"role": "assistant", "content": "All tests pass."},
        {"role": "user", "content": "What is next?"},
    ]

    with TestClient(app) as client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": conversation},
            headers={"x-session-id": "test-session-1"},
        )
        assert resp.status_code == 200

        # We expect 2 requests captured:
        # 1. Gardener completion call to generate summary
        # 2. Main agent call with modified messages
        assert len(captured) == 2

        gardener_req = json.loads(captured[0].content.decode("utf-8"))
        assert "<conversation>" in gardener_req["messages"][0]["content"]

        main_req = json.loads(captured[1].content.decode("utf-8"))
        sent_messages = main_req["messages"]

        # Verify that <gardener-state> was injected
        state_msg = sent_messages[1]
        assert state_msg["role"] == "system"
        assert "<gardener-state>" in state_msg["content"]
        assert "Checked records" in state_msg["content"]

        # Verify that protected tail (last 2 assistant turns) is intact
        assert any("All tests pass" in m.get("content", "") for m in sent_messages)
        assert sent_messages[-1]["content"] == "What is next?"
