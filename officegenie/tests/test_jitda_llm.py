"""Jitda OpenAI-compatible API adapter tests; never make an external request."""

import io
import json
import urllib.error
import urllib.request

from google.genai import types

from agent import DEFAULT_MODEL, OfficeGenieAgent, _openai_messages


class FakeResponse:
    def __init__(self, body: dict):
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return self.body


def test_default_model_is_jitda_chat():
    assert DEFAULT_MODEL == "jitda-chat"


def test_llm_credentials_are_not_forwarded_to_mcp_process(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-only-credential")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("GEMINI_API_KEY", "old-test-credential")
    monkeypatch.setenv("GOOGLE_API_KEY", "old-test-credential")
    agent = OfficeGenieAgent()
    try:
        assert bool(agent._api_key)
        assert not {
            "LLM_API_KEY",
            "LLM_BASE_URL",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
        } & set(agent._env)
    finally:
        agent.close()


def test_chat_completion_uses_server_auth_and_maps_tool_calls(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["authorization"] = request.get_header("Authorization")
        seen["payload"] = json.loads(request.data.decode("utf-8"))
        seen["timeout"] = timeout
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "search_policy",
                                        "arguments": '{"query":"점심 식대 한도"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    test_credential = "test-only-credential"
    agent = OfficeGenieAgent(
        api_key=test_credential,
        base_url="https://llm.example.test/v1/",
    )
    try:
        response = agent._run(
            agent._generate(
                contents=[
                    types.Content(
                        role="user", parts=[types.Part(text="식대 기준을 알려줘")]
                    )
                ],
                system="시스템 안내",
                decls=[
                    {
                        "name": "search_policy",
                        "description": "규정 검색",
                        "parameters_json_schema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                    }
                ],
            )
        )

        assert seen["url"] == "https://llm.example.test/v1/chat/completions"
        assert seen["authorization"] == f"Bearer {test_credential}"
        assert seen["timeout"] > 0
        assert seen["payload"]["model"] == "jitda-chat"
        assert seen["payload"]["messages"] == [
            {"role": "system", "content": "시스템 안내"},
            {"role": "user", "content": "식대 기준을 알려줘"},
        ]
        assert test_credential not in json.dumps(seen["payload"])
        assert response.function_calls[0].name == "search_policy"
        assert response.function_calls[0].args == {"query": "점심 식대 한도"}
        assert response.candidates[0].content.parts[0].function_call.id == "call-1"
    finally:
        agent.close()


def test_tool_result_history_maps_to_openai_tool_message():
    history = [
        types.Content(role="user", parts=[types.Part(text="규정을 찾아줘")]),
        types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        id="call-1",
                        name="search_policy",
                        args={"query": "점심 식대"},
                    )
                )
            ],
        ),
        types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id="call-1",
                        name="search_policy",
                        response={"result": {"ok": True, "code": "OK"}},
                    )
                )
            ],
        ),
    ]

    messages = _openai_messages(history, "시스템 안내")

    assert messages[0] == {"role": "system", "content": "시스템 안내"}
    assert messages[1] == {"role": "user", "content": "규정을 찾아줘"}
    assert messages[2]["role"] == "assistant"
    assert messages[2]["tool_calls"][0]["id"] == "call-1"
    assert messages[3]["role"] == "tool"
    assert messages[3]["tool_call_id"] == "call-1"
    assert json.loads(messages[3]["content"]) == {"result": {"ok": True, "code": "OK"}}


def test_http_error_redacts_api_credential(monkeypatch):
    credential = "test-only-credential"

    def fail_urlopen(request, timeout):
        body = json.dumps(
            {"error": {"message": f"temporary failure; echoed {credential}"}}
        ).encode("utf-8")
        raise urllib.error.HTTPError(
            request.full_url,
            503,
            "Service Unavailable",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    agent = OfficeGenieAgent(api_key=credential, base_url="https://llm.example.test/v1")
    try:
        try:
            agent._run(agent._generate(contents=[], system="", decls=[]))
        except Exception as exc:
            assert "HTTP 503" in str(exc)
            assert credential not in str(exc)
            assert "[REDACTED]" in str(exc)
        else:
            raise AssertionError("Expected the mock 503 response to fail")
    finally:
        agent.close()
