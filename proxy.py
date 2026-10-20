import hmac
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from admin import install_admin
from generation import Generation, with_heartbeats
from protocol import ChatRequest, Completion, ProxyError, encode_sse
from responses_api import ResponseBuilder, ResponseRequest
from storage import Store, TokenCounter
from upstream import BrowserUpstream


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
COUNTER = None


def load_key(directory=None):
    value = os.getenv("PROXY_API_KEY", "").strip()
    if value:
        return value
    directory = Path(directory or os.getenv("PROXY_DATA_DIR", str(ROOT / "data")))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "proxy-key.txt"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise RuntimeError("本地密钥文件为空，请删除 data/proxy-key.txt 后重启")
        return value
    value = "sk-" + secrets.token_urlsafe(32)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    print(f"本机接口密钥已保存至：{path}")
    return value


async def read_payload(request, model):
    raw = bytearray()
    async for block in request.stream():
        raw.extend(block)
        if len(raw) > 2097152:
            raise ProxyError("请求体不能超过 2MB", 413, "request_too_large")
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        invalid_request(exc)


def invalid_request(exc):
    first = exc.errors(include_input=False, include_url=False)[0]
    field = ".".join(str(part) for part in first["loc"])
    if first["type"] == "extra_forbidden":
        message = f"当前网页接口不支持参数：{field}，请删除后重试"
    elif first["type"] == "value_error":
        message = str(first.get("ctx", {}).get("error", "请求内容不正确"))
    else:
        message = f"参数格式不正确：{field or '请求体'}，仅支持 mercury-3 模型和文本消息"
    raise ProxyError(message, 400, "invalid_request", param=field or None) from exc


def create_app(upstream=None, api_key=None, data_dir=None):
    @asynccontextmanager
    async def lifespan(app):
        global COUNTER
        directory = data_dir or os.getenv("PROXY_DATA_DIR", str(ROOT / "data"))
        key = api_key if api_key is not None else load_key(directory)
        app.state.store = Store(directory, key, os.getenv("PROXY_TIMEZONE", "Asia/Shanghai"))
        if COUNTER is None:
            COUNTER = TokenCounter()
        app.state.counter = COUNTER
        app.state.upstream = upstream if upstream is not None else BrowserUpstream()
        app.state.upstream.timeout = int(app.state.store.get("upstream_timeout"))
        try:
            yield
        finally:
            try:
                await app.state.upstream.close()
            finally:
                app.state.store.close()

    app = FastAPI(title="Mercury 控制台与模型代理", description="Inception 网页模型的 OpenAI 兼容文本接口；用量为本地分词估算。", lifespan=lifespan)
    install_admin(app, ROOT)
    schemas = {}
    definitions = {}
    for model in (ChatRequest, ResponseRequest):
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        definitions.update(schema.pop("$defs", {}))
        schemas[model.__name__] = schema

    @app.exception_handler(ProxyError)
    async def proxy_error(request, exc):
        headers = {"Retry-After": exc.retry_after} if isinstance(exc.retry_after, str) and exc.retry_after.isdigit() else None
        return JSONResponse(exc.body(), status_code=exc.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(ProxyError("请求格式不正确，请检查填写内容", 400, "invalid_request").body(), status_code=400)

    async def authenticate(request: Request):
        scheme, _, credential = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(credential.encode(), request.app.state.store.get("api_key").encode()):
            raise ProxyError("接口密钥不正确，请在 Authorization 中填写 Bearer 加空格再加本机密钥", 401, "invalid_api_key")

    @app.get("/health", summary="检查服务状态")
    async def health():
        return {"status": "ok", "browser_ready": app.state.upstream.ready}

    @app.get("/v1/models", dependencies=[Depends(authenticate)], summary="列出模型")
    async def models():
        return {"object": "list", "data": [{"id": "mercury-3", "object": "model", "created": 0, "owned_by": "inceptionlabs"}]}

    @app.post("/v1/chat/completions", dependencies=[Depends(authenticate)], summary="发送文本对话", openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": schemas["ChatRequest"]}}}})
    async def chat(request: Request):
        payload = await read_payload(request, ChatRequest)
        if "reasoning_effort" not in payload.model_fields_set:
            payload.reasoning_effort = app.state.store.get("reasoning_effort")
        if "web_search" not in payload.model_fields_set:
            payload.web_search = app.state.store.get("web_search") == "true"
        run = await Generation(app, payload, "/v1/chat/completions").open()
        completion = Completion(payload.model)
        if not payload.stream:
            try:
                async for event in run.iterate():
                    pass
                body = completion.full(run.text, run.thinking, run.finish_reason)
                body.update(usage=run.chat_usage(), usage_estimated=True, usage_method="cl100k_base")
                return body
            except BaseException as exc:
                run.fail(exc)
                raise
            finally:
                await run.close()

        async def stream():
            iterator = with_heartbeats(run.iterate())
            try:
                yield encode_sse(completion.chunk({"role": "assistant", "content": ""}))
                async for event in iterator:
                    if event is None:
                        yield ": 保持连接\n\n"
                    else:
                        yield encode_sse(completion.chunk(event.delta or {}, event.finish_reason))
                if payload.stream_options and payload.stream_options.include_usage:
                    body = completion.chunk({})
                    body.update(choices=[], usage=run.chat_usage(), usage_estimated=True, usage_method="cl100k_base")
                    yield encode_sse(body)
                yield "data: [DONE]\n\n"
            except ProxyError as exc:
                run.fail(exc)
                yield encode_sse(exc.body())
                yield "data: [DONE]\n\n"
            finally:
                with anyio.CancelScope(shield=True):
                    await iterator.aclose()
                    await run.close()

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/v1/responses", dependencies=[Depends(authenticate)], summary="创建 Responses 文本响应", openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": schemas["ResponseRequest"]}}}})
    async def responses(request: Request):
        payload = await read_payload(request, ResponseRequest)
        try:
            chat_payload, history = payload.chat(app.state.store)
        except ValidationError as exc:
            invalid_request(exc)
        run = await Generation(app, chat_payload, "/v1/responses").open()
        builder = ResponseBuilder(payload, run)
        if not payload.stream:
            try:
                async for event in run.iterate():
                    pass
                body = builder.full()
                if payload.store:
                    app.state.store.save_response(body, builder.history(history))
                return body
            except BaseException as exc:
                run.fail(exc)
                raise
            finally:
                await run.close()

        async def stream():
            iterator = with_heartbeats(run.iterate())
            try:
                for value in builder.start():
                    yield value
                async for event in iterator:
                    if event is None:
                        yield ": 保持连接\n\n"
                    else:
                        for value in builder.delta(event):
                            yield value
                for value in builder.finish():
                    yield value
                body = builder.full()
                if payload.store:
                    app.state.store.save_response(body, builder.history(history))
                yield builder.event("response.completed" if body["status"] == "completed" else "response.incomplete", response=body)
            except ProxyError as exc:
                run.fail(exc)
                body = builder.full("failed", {"code": exc.code, "message": str(exc)})
                if payload.store:
                    app.state.store.save_response(body, history)
                yield builder.event("response.failed", response=body)
            finally:
                with anyio.CancelScope(shield=True):
                    await iterator.aclose()
                    await run.close()

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/v1/responses/{response_id}", dependencies=[Depends(authenticate)], summary="读取已保存响应")
    async def retrieve(response_id: str):
        return app.state.store.response(response_id)[0]

    @app.delete("/v1/responses/{response_id}", dependencies=[Depends(authenticate)], summary="删除已保存响应")
    async def delete(response_id: str):
        app.state.store.delete_response(response_id)
        return {"id": response_id, "object": "response", "deleted": True}

    @app.get("/v1/responses/{response_id}/input_items", dependencies=[Depends(authenticate)], summary="读取响应输入")
    async def input_items(response_id: str):
        body, history = app.state.store.response(response_id)
        history = history[:-1] if body["status"] in {"completed", "incomplete"} else history
        items = [{"id": "item_" + str(index), "type": "message", "role": item["role"], "content": [{"type": "output_text" if item["role"] == "assistant" else "input_text", "text": item["content"]}]} for index, item in enumerate(history)]
        return {"object": "list", "data": items, "first_id": items[0]["id"] if items else None, "last_id": items[-1]["id"] if items else None, "has_more": False}

    original_openapi = app.openapi

    def openapi():
        schema = original_openapi()
        schema.setdefault("components", {}).setdefault("schemas", {}).update(definitions)
        schema["components"]["securitySchemes"] = {"ProxyKey": {"type": "http", "scheme": "bearer"}}
        for path, operations in schema["paths"].items():
            if path.startswith("/v1/"):
                for operation in operations.values():
                    operation["security"] = [{"ProxyKey": []}]
        return schema

    app.openapi = openapi
    return app


app = create_app()


if __name__ == "__main__":
    print("控制台：http://127.0.0.1:" + os.getenv("PROXY_PORT", "8000") + "/admin")
    print("兼容接口：http://127.0.0.1:" + os.getenv("PROXY_PORT", "8000") + "/v1")
    uvicorn.run(app, host=os.getenv("PROXY_HOST", "127.0.0.1"), port=int(os.getenv("PROXY_PORT", "8000")))
