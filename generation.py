import asyncio
import time
import uuid
from contextlib import suppress

import anyio

from protocol import translate


class Generation:
    def __init__(self, app, payload, endpoint):
        self.app = app
        self.payload = payload
        self.endpoint = endpoint
        self.id = uuid.uuid4().hex
        self.created_at = time.time()
        self.started = time.perf_counter()
        self.first_token_ms = None
        self.status = "cancelled"
        self.error_code = None
        self.content = []
        self.reasoning = []
        self.final_content = None
        self.tool_calls = []
        self.finish_reason = "stop"
        self.resource = None
        self.events = None
        self.entered = False
        self.closed = False
        self.input_tokens = app.state.counter.messages(payload.messages)

    async def open(self):
        self.resource = self.app.state.upstream.open(self.payload.upstream(str(self.app.state.store.timezone)))
        try:
            chunks = await self.resource.__aenter__()
            self.entered = True
            self.events = translate(chunks)
            self.first = await anext(self.events)
            self.accept(self.first)
        except BaseException as exc:
            self.fail(exc)
            await self.close()
            raise
        return self

    def accept(self, event):
        if event.delta:
            if self.first_token_ms is None:
                self.first_token_ms = (time.perf_counter() - self.started) * 1000
            self.content.append(event.delta.get("content", ""))
            self.reasoning.append(event.delta.get("reasoning_content", ""))
        if event.finish_reason:
            self.finish_reason = event.finish_reason

    async def iterate(self):
        yield self.first
        async for event in self.events:
            self.accept(event)
            yield event
        self.status = "success"

    def fail(self, exc):
        if not isinstance(exc, asyncio.CancelledError):
            self.status = "error"
            self.error_code = getattr(exc, "code", "internal_error")

    @property
    def text(self):
        return "".join(self.content)

    @property
    def thinking(self):
        return "".join(self.reasoning)

    def counts(self):
        counter = self.app.state.counter
        reasoning = counter.count(self.thinking)
        return self.input_tokens, counter.count(self.text) + reasoning, reasoning

    def chat_usage(self):
        input_tokens, output_tokens, reasoning_tokens = self.counts()
        return {"prompt_tokens": input_tokens, "completion_tokens": output_tokens, "total_tokens": input_tokens + output_tokens, "completion_tokens_details": {"reasoning_tokens": reasoning_tokens}}

    def response_usage(self):
        input_tokens, output_tokens, reasoning_tokens = self.counts()
        return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens, "output_tokens_details": {"reasoning_tokens": reasoning_tokens}}

    async def close(self):
        if self.closed:
            return
        self.closed = True
        with anyio.CancelScope(shield=True):
            try:
                if self.events:
                    await self.events.aclose()
            finally:
                try:
                    if self.entered:
                        await self.resource.__aexit__(None, None, None)
                finally:
                    input_tokens, output_tokens, reasoning_tokens = self.counts()
                    self.app.state.store.record({"id": self.id, "created_at": self.created_at, "endpoint": self.endpoint, "model": self.payload.model, "status": self.status, "input_tokens": input_tokens, "output_tokens": output_tokens, "reasoning_tokens": reasoning_tokens, "latency_ms": (time.perf_counter() - self.started) * 1000, "first_token_ms": self.first_token_ms, "error_code": self.error_code, "stream": int(self.payload.stream)})


async def with_heartbeats(iterator):
    pending = None
    try:
        while True:
            pending = asyncio.create_task(anext(iterator))
            while True:
                done, _ = await asyncio.wait({pending}, timeout=15)
                if done:
                    break
                yield None
            try:
                yield pending.result()
            except StopAsyncIteration:
                return
    finally:
        with anyio.CancelScope(shield=True):
            if pending and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
            await iterator.aclose()
