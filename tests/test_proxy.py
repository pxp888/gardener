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

            # Check if this is a carver request
            if body.get("tools"):
                # If tool response received in messages, finish
                if any(m.get("role") == "tool" for m in messages):
                    return httpx.Response(
                        200,
                        json={
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": "Carving finished.",
                                        "tool_calls": [
                                            {
                                                "id": "fin",
                                                "type": "function",
                                                "function": {"name": "finish", "arguments": "{}"},
                                            }
                                        ],
                                    }
                                }
                            ]
                        },
                    )
                # First step: carve message 2
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "Carving boilerplate.",
                                    "tool_calls": [
                                        {
                                            "id": "c_edit",
                                            "type": "function",
                                            "function": {
                                                "name": "replace_message_content",
                                                "arguments": json.dumps({
                                                    "message_index": 3,
                                                    "target_content": "skeleton.",
                                                    "replacement_content": "skeleton (carved).",
                                                }),
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )

            # If this is a summary gardener request
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
                    b"data: {\"choices\":[{\"delta\":{\"content\":\"Hello from mock!\"}}]}\n\ndata: [DONE]\n\n"
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


def test_summarize_strategy_triggered(mock_upstream):
    handler, captured = mock_upstream
    config = AppConfig()
    config.gardener.strategy = "summarize"
    config.gardener.turns = 1
    config.gardener.protected_turns = 2
    config.gardener.min_tokens_to_garden = 5

    app = create_app(config)

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
            headers={"x-session-id": "test-session-summarize"},
        )
        assert resp.status_code == 200
        assert len(captured) == 2

        gardener_req = json.loads(captured[0].content.decode("utf-8"))
        assert "<conversation>" in gardener_req["messages"][0]["content"]

        main_req = json.loads(captured[1].content.decode("utf-8"))
        sent_messages = main_req["messages"]

        state_msg = sent_messages[1]
        assert state_msg["role"] == "system"
        assert "<gardener-state>" in state_msg["content"]
        assert "Checked records" in state_msg["content"]


def test_carve_strategy_triggered(mock_upstream):
    handler, captured = mock_upstream
    config = AppConfig()
    config.gardener.strategy = "carve"
    config.gardener.turns = 1
    config.gardener.protected_turns = 2
    config.gardener.min_tokens_to_garden = 5
    config.gardener.max_carve_steps = 4

    app = create_app(config)

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
            headers={"x-session-id": "test-session-carve"},
        )
        assert resp.status_code == 200

        # We expect:
        # Request 1: Carver step 1 (emits replace_message_content)
        # Request 2: Carver step 2 (emits finish)
        # Request 3: Main agent call with surgically carved messages
        assert len(captured) == 3

        main_req = json.loads(captured[2].content.decode("utf-8"))
        sent_messages = main_req["messages"]

        # In carving mode, there is NO synthetic <gardener-state> system block!
        assert not any("<gardener-state>" in m.get("content", "") for m in sent_messages)

        # The edited content in the history was modified in-place!
        assert any("skeleton (carved)" in m.get("content", "") for m in sent_messages)

        # Original user request and protected tail remain verbatim!
        assert sent_messages[1]["content"] == "Check records"
        assert sent_messages[-1]["content"] == "What is next?"


def test_debug_dumps_written_by_proxy(mock_upstream, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    handler, captured = mock_upstream
    config = AppConfig()
    config.gardener.strategy = "carve"
    config.gardener.turns = 1
    config.gardener.protected_turns = 2
    config.gardener.min_tokens_to_garden = 5
    config.gardener.debug = True

    app = create_app(config)

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
            headers={"x-session-id": "test-debug-dumps"},
        )
        assert resp.status_code == 200

    before_f = tmp_path / "before.md"
    after_f = tmp_path / "after.md"
    assert before_f.is_file(), "before.md should be written when debug=True"
    assert after_f.is_file(), "after.md should be written when debug=True"
    assert "Context Before Compaction" in before_f.read_text(encoding="utf-8")
    assert "Context After Compaction" in after_f.read_text(encoding="utf-8")
