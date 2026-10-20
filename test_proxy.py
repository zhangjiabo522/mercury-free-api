import asyncio
import json
import os
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from protocol import ProxyError, sse_data, translate
from proxy import create_app, load_key
from upstream import BrowserUpstream


async def fragments(items):
    for item in items:
        yield item


def event(kind, **values):
    return "data: " + json.dumps({"type": kind, **values}, ensure_ascii=False) + "\n\n"


class FakeUpstream:
    ready = True

    def __init__(self, chunks=None, error=None):
        self.chunks = chunks if chunks is not None else [event("text-delta", delta="你好"), "data: [DONE]\n\n"]
        self.error = error
        self.payload = None
        self.active = False
        self.closed = False

    @asynccontextmanager
    async def open(self, payload):
        if self.error:
            raise self.error
        self.payload = payload
        self.active = True
        try:
            yield fragments(self.chunks)
        finally:
            self.active = False

    async def close(self):
        self.closed = True


def request(client, **values):
    body = {"model": "mercury-3", "messages": [{"role": "user", "content": "你好"}], **values}
    return client.post("/v1/chat/completions", headers={"Authorization": "Bearer test-key"}, json=body)


def make_app(upstream=None, api_key="test-key"):
    return create_app(upstream or FakeUpstream(), api_key, ":memory:")


def test_sse_fragmentation_and_multiline():
    async def run():
        source = "\ufeff: 心跳\r\ndata: {\r\ndata: \"type\":\"text-delta\",\r\ndata: \"delta\":\"中文\"}\r\n\r\ndata: [DONE]"
        values = [value async for value in sse_data(fragments(list(source)))]
        assert json.loads(values[0])["delta"] == "中文"
        assert values[1] == "[DONE]"
    asyncio.run(run())


def test_translate_reasoning_and_finish():
    async def run():
        data = [event("reasoning-start", id="r"), event("reasoning-delta", delta="思考"), event("text-delta", delta="回答"), event("finish", finishReason="length"), "data: [DONE]\n\n"]
        values = [value async for value in translate(fragments(data))]
        assert values[0].delta == {"reasoning_content": "思考"}
        assert values[1].delta == {"content": "回答"}
        assert values[2].finish_reason == "length"
    asyncio.run(run())


@pytest.mark.parametrize("data,code", [
    ([event("text-delta", delta="部分")], "upstream_stream_interrupted"),
    (["data: not-json\n\n"], "invalid_upstream_stream"),
    (["data: []\n\n"], "invalid_upstream_stream"),
    ([event("text-delta", delta=None)], "invalid_upstream_stream"),
    ([event("error", errorText="失败")], "upstream_generation_error"),
    ([event("abort")], "upstream_aborted"),
    (["data: [DONE]\n\n"], "empty_upstream_response"),
])
def test_bad_upstream_stream(data, code):
    async def run():
        with pytest.raises(ProxyError) as error:
            _ = [value async for value in translate(fragments(data))]
        assert error.value.code == code
    asyncio.run(run())


def test_non_stream_and_message_conversion():
    upstream = FakeUpstream([event("reasoning-delta", delta="想一想"), event("text-delta", delta="你好"), "data: [DONE]\n\n"])
    with TestClient(make_app(upstream)) as client:
        response = request(client, messages=[{"role": "developer", "content": "使用中文"}, {"role": "user", "content": [{"type": "text", "text": "你好"}]}], reasoning_effort="none", web_search=True)
        assert response.status_code == 200
        body = response.json()
        assert body["choices"][0]["message"] == {"role": "assistant", "content": "你好", "reasoning_content": "想一想"}
        assert body["usage"]["prompt_tokens"] > 0
        assert body["usage"]["completion_tokens"] > 0
        assert body["usage_estimated"] is True
        assert upstream.payload["reasoningEffort"] == "instant"
        assert upstream.payload["webSearchEnabled"] is True
        assert upstream.payload["messages"][0]["role"] == "system"
        assert upstream.payload["messages"][1]["parts"] == [{"type": "text", "text": "你好"}]
        assert not upstream.active
    assert upstream.closed


def test_stream_wire_format():
    upstream = FakeUpstream()
    with TestClient(make_app(upstream)) as client:
        response = request(client, stream=True, stream_options={"include_usage": True})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        values = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
        assert values[-1] == "[DONE]"
        chunks = [json.loads(value) for value in values[:-1]]
        assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
        assert chunks[1]["choices"][0]["delta"]["content"] == "你好"
        assert chunks[2]["choices"][0]["finish_reason"] == "stop"
        assert chunks[3]["usage"]["prompt_tokens"] > 0 and chunks[3]["choices"] == []
        assert chunks[3]["usage_estimated"] is True
        assert len({chunk["id"] for chunk in chunks}) == 1
        assert not upstream.active


@pytest.mark.parametrize("stream", [False, True])
def test_upstream_failure_keeps_status_before_stream(stream):
    upstream = FakeUpstream(error=ProxyError("稍后重试", 429, "upstream_rate_limit", retry_after="60"))
    with TestClient(make_app(upstream)) as client:
        response = request(client, stream=stream)
        assert response.status_code == 429
        assert response.headers["retry-after"] == "60"
        assert response.json()["error"]["code"] == "upstream_rate_limit"


def test_mid_stream_error_has_no_success_finish():
    upstream = FakeUpstream([event("text-delta", delta="部分"), event("error", errorText="生成失败")])
    with TestClient(make_app(upstream)) as client:
        response = request(client, stream=True)
        chunks = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
        assert chunks[-1]["error"]["code"] == "upstream_generation_error"
        assert all(chunk.get("choices", [{}])[0].get("finish_reason") != "stop" for chunk in chunks)
        assert not upstream.active


@pytest.mark.parametrize("stream", [False, True])
def test_initial_protocol_error_is_json(stream):
    upstream = FakeUpstream([event("error", errorText="失败")])
    with TestClient(make_app(upstream)) as client:
        response = request(client, stream=stream)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "upstream_generation_error"
        assert not upstream.active


def test_auth_models_health_and_openapi():
    with TestClient(make_app()) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/v1/models").status_code == 401
        response = client.get("/v1/models", headers={"Authorization": "Bearer test-key"})
        assert response.json()["data"][0]["id"] == "mercury-3"
        schema = client.get("/openapi.json").json()
        assert "Message" in schema["components"]["schemas"]
        assert schema["paths"]["/v1/chat/completions"]["post"]["security"] == [{"ProxyKey": []}]
        assert schema["paths"]["/v1/responses"]["post"]["security"] == [{"ProxyKey": []}]


@pytest.mark.parametrize("values", [
    {"model": "invented-model"},
    {"messages": []},
    {"messages": [{"role": "assistant", "content": "你好"}]},
    {"messages": [{"role": "user", "content": " "}]},
    {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}]}]},
    {"tools": []},
    {"temperature": 0.2},
    {"stream": "false"},
    {"n": 2},
])
def test_unsupported_requests(values):
    upstream = FakeUpstream()
    with TestClient(make_app(upstream)) as client:
        response = request(client, **values)
        assert response.status_code == 400
        assert "error" in response.json()
        assert upstream.payload is None


def test_large_body_and_invalid_json():
    with TestClient(make_app()) as client:
        headers = {"Authorization": "Bearer test-key", "Content-Type": "application/json"}
        assert client.post("/v1/chat/completions", headers=headers, content=b"x" * 2097153).status_code == 413
        assert client.post("/v1/chat/completions", headers=headers, content="{").status_code == 400


def test_local_key_is_stable_and_private(tmp_path, monkeypatch):
    monkeypatch.delenv("PROXY_API_KEY", raising=False)
    monkeypatch.setenv("PROXY_DATA_DIR", str(tmp_path))
    first = load_key()
    assert first == load_key()
    assert first.startswith("sk-")
    if os.name != "nt":
        assert (tmp_path / "proxy-key.txt").stat().st_mode & 0o777 == 0o600


def test_client_disconnect_releases_stream():
    class WaitingUpstream(FakeUpstream):
        @asynccontextmanager
        async def open(self, payload):
            self.active = True

            async def chunks():
                yield event("text-delta", delta="开始")
                await asyncio.Event().wait()

            try:
                yield chunks()
            finally:
                await asyncio.sleep(0)
                self.active = False

    async def run():
        upstream = WaitingUpstream()
        app = make_app(upstream)
        body = json.dumps({"messages": [{"role": "user", "content": "测试"}], "stream": True}).encode()
        sent_request = False
        disconnect = asyncio.Event()

        async def receive():
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                disconnect.set()

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "method": "POST", "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions", "root_path": "", "scheme": "http", "query_string": b"", "headers": [(b"authorization", b"Bearer test-key"), (b"content-type", b"application/json")], "client": ("127.0.0.1", 1234), "server": ("localhost", 8000), "http_version": "1.1"}
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(app(scope, receive, send), timeout=3)
            assert not upstream.active

    asyncio.run(run())


def responses_request(client, values=None):
    body = {"model": "mercury-3", "input": "你好", **(values or {})}
    return client.post("/v1/responses", headers={"Authorization": "Bearer test-key"}, json=body)


def test_responses_non_stream_and_retrieve():
    upstream = FakeUpstream([event("reasoning-delta", delta="想一想"), event("text-delta", delta="你好"), "data: [DONE]\n\n"])
    with TestClient(make_app(upstream)) as client:
        response = responses_request(client, {"instructions": "使用中文", "metadata": {"source": "test"}})
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "response"
        assert body["status"] == "completed"
        assert body["output"][-1]["content"][0]["text"] == "你好"
        assert body["output"][0]["summary"][0]["text"] == "想一想"
        assert body["usage"]["input_tokens"] > 0
        assert body["usage"]["output_tokens"] > 0
        retrieved = client.get(f"/v1/responses/{body['id']}", headers={"Authorization": "Bearer test-key"})
        assert retrieved.json()["id"] == body["id"]
        items = client.get(f"/v1/responses/{body['id']}/input_items", headers={"Authorization": "Bearer test-key"}).json()
        assert items["data"][0]["role"] == "user"
        deleted = client.delete(f"/v1/responses/{body['id']}", headers={"Authorization": "Bearer test-key"})
        assert deleted.json()["deleted"] is True
        assert client.get(f"/v1/responses/{body['id']}", headers={"Authorization": "Bearer test-key"}).status_code == 404


def test_responses_previous_response_and_no_store():
    upstream = FakeUpstream()
    with TestClient(make_app(upstream)) as client:
        first = responses_request(client).json()
        second = responses_request(client, {"input": "继续", "previous_response_id": first["id"]}).json()
        assert [item["role"] for item in upstream.payload["messages"]] == ["user", "assistant", "user"]
        assert second["previous_response_id"] == first["id"]
        no_store = responses_request(client, {"input": "临时", "store": False}).json()
        assert no_store["store"] is False
        assert client.get(f"/v1/responses/{no_store['id']}", headers={"Authorization": "Bearer test-key"}).status_code == 404


def test_responses_stream_events():
    upstream = FakeUpstream([event("reasoning-delta", delta="想"), event("text-delta", delta="你"), event("text-delta", delta="好"), "data: [DONE]\n\n"])
    with TestClient(make_app(upstream)) as client:
        response = responses_request(client, {"stream": True})
        assert response.status_code == 200
        lines = response.text.splitlines()
        events = [line[7:] for line in lines if line.startswith("event: ")]
        assert events[0] == "response.created"
        assert "response.output_text.delta" in events
        assert "response.reasoning_summary_text.delta" in events
        assert events[-1] == "response.completed"
        completed = json.loads([line[6:] for line in lines if line.startswith("data: ")][-1])
        assert completed["response"]["output"][-1]["content"][0]["text"] == "你好"
        assert completed["sequence_number"] == len(events) - 1


def test_admin_login_settings_and_key_rotation(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-admin-password")
    with TestClient(make_app()) as client:
        assert client.get("/admin").status_code == 200
        assert client.get("/api/admin/overview").status_code == 401
        login = client.post("/api/admin/login", headers={"x-admin-request": "1"}, json={"password": "test-admin-password"})
        assert login.status_code == 200
        assert client.get("/api/admin/session").json()["authenticated"] is True
        settings = client.get("/api/admin/settings").json()
        assert settings["api_key_preview"].endswith("-key")
        changed = client.put("/api/admin/settings", headers={"x-admin-request": "1"}, json={"api_key": "sk-new-test-key-123456", "reasoning_effort": "low", "web_search": True, "upstream_timeout": 120})
        assert changed.status_code == 200
        assert client.get("/v1/models", headers={"Authorization": "Bearer test-key"}).status_code == 401
        assert client.get("/v1/models", headers={"Authorization": "Bearer sk-new-test-key-123456"}).status_code == 200
        assert client.get("/api/admin/api-key").json()["api_key"] == "sk-new-test-key-123456"


def test_admin_usage_summary_and_filters(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-admin-password")
    with TestClient(make_app()) as client:
        request(client)
        request(client, stream=True)
        client.post("/api/admin/login", headers={"x-admin-request": "1"}, json={"password": "test-admin-password"})
        overview = client.get("/api/admin/overview").json()
        assert overview["summary"]["requests"] == 2
        assert overview["summary"]["success"] == 2
        assert overview["summary"]["input_tokens"] > 0
        assert overview["summary"]["output_tokens"] > 0
        assert overview["summary"]["avg_latency_ms"] is not None
        assert len(overview["requests"]) == 2
        assert sum(item["requests"] for item in overview["hourly"]) == 2
        assert client.get("/api/admin/overview?status=error").json()["total"] == 0


def test_admin_csrf_and_password_rate_limit(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-admin-password")
    with TestClient(make_app()) as client:
        assert client.post("/api/admin/login", json={"password": "test-admin-password"}).status_code == 403
        for _ in range(10):
            assert client.post("/api/admin/login", headers={"x-admin-request": "1"}, json={"password": "wrong"}).status_code == 401
        assert client.post("/api/admin/login", headers={"x-admin-request": "1"}, json={"password": "test-admin-password"}).status_code == 429


def test_upstream_close_tolerates_closed_browser_transport():
    class ClosedBrowser:
        async def close(self):
            raise RuntimeError("transport closed")

    class ClosedPlaywright:
        async def stop(self):
            raise RuntimeError("transport closed")

    async def run():
        upstream = BrowserUpstream()
        upstream.browser = ClosedBrowser()
        upstream.playwright = ClosedPlaywright()
        await upstream.close()
        assert upstream.browser is None
        assert upstream.playwright is None

    asyncio.run(run())
