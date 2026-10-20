import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager, suppress

import anyio
from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright

from protocol import ProxyError


FETCH_SCRIPT = """
async ({id, body, token, timeout}) => {
    window.inceptionProxyControllers ??= new Map();
    const controller = new AbortController();
    window.inceptionProxyControllers.set(id, controller);
    const timer = setTimeout(() => controller.abort(), timeout);
    const emit = event => window.inceptionProxyEmit(id, event);
    try {
        const response = await fetch('/api/chat', {
            method: 'POST',
            credentials: 'same-origin',
            headers: {'Content-Type': 'application/json', 'x-session-token': token},
            body: JSON.stringify(body),
            signal: controller.signal
        });
        const contentType = response.headers.get('content-type') || '';
        if (!response.ok || !contentType.includes('text/event-stream')) {
            await emit({kind: 'http_error', status: response.status, contentType,
                retryAfter: response.headers.get('retry-after')});
            return;
        }
        await emit({kind: 'headers'});
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        try {
            while (true) {
                const {done, value} = await reader.read();
                if (done) break;
                await emit({kind: 'data', text: decoder.decode(value, {stream: true})});
            }
            const tail = decoder.decode();
            if (tail) await emit({kind: 'data', text: tail});
            await emit({kind: 'done'});
        } finally {
            await reader.cancel().catch(() => {});
            reader.releaseLock();
        }
    } catch (error) {
        await emit({kind: 'failure', timeout: error.name === 'AbortError'});
    } finally {
        clearTimeout(timer);
        window.inceptionProxyControllers.delete(id);
    }
}
"""


class BrowserUpstream:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.token = None
        self.token_at = 0
        self.lock = asyncio.Lock()
        self.session_lock = asyncio.Lock()
        self.queues = {}
        self.timeout = float(os.getenv("UPSTREAM_TIMEOUT", "180"))
        self.slots = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENCY", "1")))
        if self.timeout <= 0 or int(os.getenv("MAX_CONCURRENCY", "1")) < 1:
            raise ValueError("超时和并发数必须大于零")

    @property
    def ready(self):
        return bool(self.browser and self.browser.is_connected() and self.page and not self.page.is_closed())

    async def receive(self, source, request_id, event):
        queue = self.queues.get(request_id)
        if queue is not None:
            await queue.put(event)

    async def ensure_started(self):
        async with self.lock:
            if self.ready:
                return
            await self.close()
            try:
                self.playwright = await async_playwright().start()
                options = {"headless": os.getenv("BROWSER_HEADLESS", "1").lower() not in {"0", "false", "no"}}
                executable = os.getenv("BROWSER_EXECUTABLE_PATH")
                if executable:
                    options["executable_path"] = executable
                self.browser = await self.playwright.chromium.launch(**options)
                self.context = await self.browser.new_context(locale="zh-CN", timezone_id=os.getenv("PROXY_TIMEZONE", "Asia/Shanghai"))
                self.page = await self.context.new_page()
                await self.page.expose_binding("inceptionProxyEmit", self.receive)
                await self.page.goto("https://chat.inceptionlabs.ai/", wait_until="domcontentloaded", timeout=45000)
                await self.page.locator("textarea").wait_for(state="attached", timeout=60000 if not options["headless"] else 20000)
            except BrowserError as exc:
                await self.close()
                raise ProxyError("浏览器未能打开聊天页。请确认已执行 python -m playwright install chromium；若网站要求手动验证，可设置 BROWSER_HEADLESS=0 后重试。", code="browser_unavailable") from exc

    async def session_token(self, force=False):
        async with self.session_lock:
            if self.token and time.monotonic() - self.token_at < 720 and not force:
                return self.token
            try:
                result = await self.page.evaluate("""async () => {
                    const response = await fetch('/api/session', {signal: AbortSignal.timeout(30000)});
                    if (!response.ok) return {status: response.status};
                    try { return {status: response.status, data: await response.json()}; }
                    catch { return {status: response.status}; }
                }""")
            except BrowserError as exc:
                raise ProxyError("获取网站会话失败，请稍后重试", code="session_unavailable") from exc
            token = result.get("data", {}).get("token")
            if not isinstance(token, str) or not token:
                if result.get("status") == 429:
                    raise ProxyError("网站限制了会话请求频率，请稍后重试", 429, "upstream_rate_limit")
                raise ProxyError("网站未返回有效会话，可能需要手动打开浏览器完成验证", code="session_unavailable")
            self.token = token
            self.token_at = time.monotonic()
            return token

    async def pump(self, request_id, payload, token):
        try:
            await self.page.evaluate(FETCH_SCRIPT, {"id": request_id, "body": payload, "token": token, "timeout": int(self.timeout * 1000)})
        except BrowserError:
            await self.receive(None, request_id, {"kind": "failure", "timeout": False})

    async def next_event(self, queue):
        try:
            event = await asyncio.wait_for(queue.get(), timeout=self.timeout + 2)
        except asyncio.TimeoutError as exc:
            raise ProxyError("等待网站回复超时", 504, "upstream_timeout") from exc
        if event["kind"] == "failure":
            if event.get("timeout"):
                raise ProxyError("网站回复超时", 504, "upstream_timeout")
            raise ProxyError("浏览器与网站的连接中断", code="upstream_connection_error")
        return event

    async def cleanup_request(self, request_id, queue, task):
        self.queues.pop(request_id, None)
        while not queue.empty():
            queue.get_nowait()
        if not task.done() and self.page and not self.page.is_closed():
            with suppress(BrowserError, asyncio.TimeoutError):
                await asyncio.wait_for(self.page.evaluate("id => window.inceptionProxyControllers?.get(id)?.abort()", request_id), timeout=2)
        task.cancel()
        with suppress(asyncio.CancelledError, BrowserError):
            await task

    @asynccontextmanager
    async def open(self, payload):
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=5)
        except asyncio.TimeoutError as exc:
            raise ProxyError("代理正在处理其他请求，请稍后重试", 429, "proxy_busy", retry_after="5") from exc
        try:
            await self.ensure_started()
            for attempt in range(2):
                token = await self.session_token(force=attempt > 0)
                request_id = uuid.uuid4().hex
                queue = asyncio.Queue(maxsize=8)
                self.queues[request_id] = queue
                task = asyncio.create_task(self.pump(request_id, payload, token))
                try:
                    first = await self.next_event(queue)
                    if first["kind"] == "http_error":
                        status = first["status"]
                        if status in {401, 403} and attempt == 0:
                            continue
                        if status == 429:
                            raise ProxyError("网站限制了请求频率，请稍后重试", 429, "upstream_rate_limit", retry_after=first.get("retryAfter"))
                        if "text/html" in first.get("contentType", ""):
                            raise ProxyError("网站返回了验证页面，请手动打开浏览器检查", code="upstream_verification_required")
                        raise ProxyError(f"网站返回异常状态：{status}", code="upstream_http_error")
                    if first["kind"] != "headers":
                        raise ProxyError("网站响应缺少有效的流式响应头", code="invalid_upstream_stream")

                    async def chunks():
                        while True:
                            event = await self.next_event(queue)
                            if event["kind"] == "done":
                                return
                            if event["kind"] != "data":
                                raise ProxyError("网站返回了未知响应", code="invalid_upstream_stream")
                            yield event["text"]

                    yield chunks()
                    return
                finally:
                    with anyio.CancelScope(shield=True):
                        await self.cleanup_request(request_id, queue, task)
        finally:
            self.slots.release()

    async def close(self):
        if self.browser:
            with suppress(BrowserError):
                await self.browser.close()
        if self.playwright:
            with suppress(BrowserError):
                await self.playwright.stop()
        self.page = self.context = self.browser = self.playwright = None
        self.token = None
        self.token_at = 0
