from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import random
import time
from collections import defaultdict, deque
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from .store import Store

TZ = ZoneInfo("Asia/Shanghai")


def validate_webhook(value: str):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != "open.feishu.cn" or not parsed.path.startswith("/open-apis/bot/v2/hook/") or not parsed.path.rsplit("/", 1)[-1] or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.query or parsed.fragment:
        raise ValueError("请输入飞书群自定义机器人的完整 HTTPS Webhook（open.feishu.cn/open-apis/bot/v2/hook/…）")
    return value


def split_text(text: str, byte_limit=7500):
    chunks, current, length = [], [], 0
    for char in text:
        size = len(char.encode("utf-8"))
        # Each line becomes a rich-text row; count JSON escaping and row overhead,
        # not just raw UTF-8, so many short lines cannot exceed Feishu's body cap.
        if char in '\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029':
            size = 50
        elif char in '\\"':
            size += 1
        elif ord(char) < 32:
            size = 6
        if length + size > byte_limit and current:
            chunks.append("".join(current))
            current, length = [], 0
        current.append(char)
        length += size
    if current:
        chunks.append("".join(current))
    return chunks or ["（无文字内容）"]


def signature(secret: str, timestamp: str):
    return base64.b64encode(hmac.new(f"{timestamp}\n{secret}".encode(), b"", hashlib.sha256).digest()).decode()


def format_time(timestamp):
    return datetime.fromtimestamp(timestamp, TZ).strftime("%m-%d %H:%M:%S")


def payload_for(job, secret="", now=None):
    now = now or time.time()
    label = {"new": "新消息", "edit": "消息更新", "catchup": "断线补发", "test": "连接测试"}[job["kind"]]
    delayed = job["kind"] != "test" and now - job["event_at"] > job["stale_minutes"] * 60
    if delayed:
        label = f"延迟补发 · {label}"
    suffix = f" · {job['part']}/{job['parts']}" if job["parts"] > 1 else ""
    content = [[{"tag": "text", "text": line or " "}] for line in job["body"].splitlines()]
    content.append([{"tag": "text", "text": " "}])
    if job.get("sender_name"):
        content.append([{"tag": "text", "text": f"发送人：{job['sender_name']}"}])
    content += [[{"tag": "text", "text": f"原消息：{format_time(job['original_at'])} (UTC+8)"}],
                [{"tag": "text", "text": f"转发：{format_time(now)} (UTC+8)"}]]
    if job["link"]:
        content.append([{"tag": "a", "text": "在 Telegram 查看原消息", "href": job["link"]}])
    payload = {"msg_type": "post", "content": {"post": {"zh_cn": {"title": f"【{label}】{job['source'][:120]}{suffix}", "content": content}}}}
    if secret:
        timestamp = str(int(now))
        payload.update(timestamp=timestamp, sign=signature(secret, timestamp))
    return payload


class DeliveryError(Exception):
    def __init__(self, message, *, retryable=True, uncertain=False):
        self.retryable = retryable
        self.uncertain = uncertain
        super().__init__(message)


class Sender:
    def __init__(self, store: Store):
        self.store = store
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(15, connect=8), trust_env=False)
        self.recent = defaultdict(deque)
        self.running = True

    def rate_delay(self, key):
        history = self.recent[key]
        now = time.monotonic()
        while history and history[0] <= now - 60:
            history.popleft()
        return max((history[-4] + 1.05 - now) if len(history) >= 4 else 0,
                   (history[0] + 60.05 - now) if len(history) >= 90 else 0, 0)

    async def rate_limit(self, key):
        history = self.recent[key]
        while True:
            delay = self.rate_delay(key)
            if delay <= 0:
                history.append(time.monotonic())
                return
            await asyncio.sleep(delay)

    async def send(self, job):
        await self.rate_limit(job["webhook"])
        started = time.monotonic()
        try:
            response = await self.client.post(validate_webhook(job["webhook"]), json=payload_for(job, job["secret"]))
        except httpx.ConnectError as exc:
            raise DeliveryError(f"飞书无法建立连接（{type(exc).__name__}），将重试") from None
        except httpx.ConnectTimeout as exc:
            raise DeliveryError(f"飞书连接超时（{type(exc).__name__}），将重试") from None
        except httpx.PoolTimeout as exc:
            raise DeliveryError(f"飞书本地连接池超时（{type(exc).__name__}），将重试") from None
        except httpx.TransportError as exc:
            raise DeliveryError(f"{type(exc).__name__}：请求结果未知，请先核对飞书群，避免重复通知", retryable=False, uncertain=True) from None
        except httpx.HTTPError as exc:
            raise DeliveryError(f"飞书请求异常（{type(exc).__name__}），请先核对飞书群", retryable=False, uncertain=True) from None
        if response.status_code != 200:
            if response.status_code >= 500:
                raise DeliveryError(f"飞书 HTTP {response.status_code}，请求结果未知；请先核对飞书群", retryable=False, uncertain=True)
            raise DeliveryError(f"飞书 HTTP {response.status_code}", retryable=response.status_code == 429)
        try:
            data = response.json()
        except ValueError:
            raise DeliveryError("飞书返回了无法解析的响应，请先核对飞书群", retryable=False, uncertain=True) from None
        code = data.get("code") if "code" in data else data.get("StatusCode")
        if not isinstance(code, int):
            raise DeliveryError("飞书返回了未知响应，请先核对飞书群", retryable=False, uncertain=True)
        if code != 0:
            known = {19024: "关键词校验失败，请检查机器人关键词规则", 19021: "签名校验失败，请检查签名密钥和系统时间", 19022: "IP 白名单校验失败", 19001: "机器人 Webhook 无效", 19002: "机器人已停用", 9499: "请求频率受限", 11232: "请求频率受限"}
            raise DeliveryError(f"飞书错误 {code}：{known.get(code, '请检查机器人设置及安全规则')}", retryable=code in {9499, 11232, 99991400, 230020})
        return int((time.monotonic() - started) * 1000)

    async def process_once(self):
        job = self.store.claim()
        if not job:
            return False
        delay = self.rate_delay(job["webhook"])
        if delay > 0:
            # Yield this destination's slot so another robot is not stuck behind it.
            self.store.execute("UPDATE deliveries SET status='pending',attempts=attempts-1,next_attempt=? WHERE id=?", (time.time() + delay, job["id"]))
            return True
        try:
            latency = await self.send(job)
            self.store.execute("UPDATE deliveries SET status='sent',sent_at=?,error='',latency_ms=? WHERE id=?", (time.time(), latency, job["id"]))
        except asyncio.CancelledError:
            self.store.execute("UPDATE deliveries SET status='uncertain',next_attempt=0,error='程序停止时请求结果未知。请先在飞书群核对，避免重复通知。' WHERE id=?", (job["id"],))
            raise
        except (DeliveryError, ValueError) as exc:
            retryable = isinstance(exc, DeliveryError) and exc.retryable
            status = "uncertain" if isinstance(exc, DeliveryError) and exc.uncertain else "retry" if retryable and job["attempts"] < 8 else "failed"
            delay = min(300, 2 ** job["attempts"]) + random.random()
            self.store.execute("UPDATE deliveries SET status=?,next_attempt=?,error=? WHERE id=?", (status, 0 if status == "uncertain" else time.time() + delay, str(exc), job["id"]))
        except Exception as exc:
            self.store.execute("UPDATE deliveries SET status='failed',error=? WHERE id=?", (f"发送异常：{type(exc).__name__}", job["id"]))
        return True

    async def run(self):
        while self.running:
            if not await self.process_once():
                await asyncio.sleep(.3)

    async def close(self):
        self.running = False
        await self.client.aclose()
