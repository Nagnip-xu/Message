"""Isolated UI integration fixture. Never uses the user's account or webhook."""
import asyncio
import os
import uuid
from pathlib import Path
from datetime import datetime, timezone

os.environ["RELAY_DATA_DIR"] = str(Path(__file__).resolve().parents[1] / "artifacts" / f"ui-test-{uuid.uuid4().hex}")

import httpx
from telethon import types
from relay.app import create_app
from relay.config import Config
from relay.feishu import Sender
from relay.store import Store
from relay.telegram import TelegramService


class FixtureTelegram(TelegramService):
    def __init__(self, store, config):
        super().__init__(store, config)
        self.authorized = True
        self.user = {"id": 1, "name": "集成测试账号（测试数据）"}

    def status(self):
        return {"state": "ready", "connected": True, "authorized": True, "user": self.user, "last_error": "", "last_event_at": self.last_event_at, "last_recovery_at": None, "recovery_warnings": []}

    async def dialogs(self, refresh=False):
        return [{"id": -100123456, "title": "集成测试群（测试数据）", "forum": True, "kind": "group", "protected": False, "username": None}]

    async def topics(self, chat_id, query=""):
        return [{"id": 88, "title": "交易信号（测试数据）", "closed": False}, {"id": 99, "title": "闲聊（测试数据）", "closed": False}]

    async def resolve_source(self, chat_id, topic_id):
        return {"chat_title": "集成测试群（测试数据）", "topic_title": "交易信号（测试数据）" if topic_id else "", "cursor": 100}

    async def run(self):
        await asyncio.Event().wait()

    async def require(self):
        return self


store = Store()
config = Config()
config.save(api_id=12345, api_hash="a" * 32)
telegram = FixtureTelegram(store, config)
sender = Sender(store)
# The test webhook is intercepted in memory, no HTTP request leaves the process.
sender.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 0})))
app = create_app(store, config, telegram, sender)


@app.post("/__test__/reset")
async def reset_fixture():
    with store.db() as db:
        for table in ("seen", "deliveries", "tasks", "robots"):
            db.execute(f"DELETE FROM {table}")
    return {"ok": True}


@app.post("/__test__/ingest")
async def ingest_fixture():
    from types import SimpleNamespace
    chat = SimpleNamespace(forum=True, noforwards=False, username=None)
    message = types.Message(id=101, peer_id=types.PeerChannel(123456), date=datetime.now(timezone.utc), message="测试信号 ETH 开仓（模拟数据）", reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=88))
    count = 0
    for task in store.all("SELECT * FROM tasks WHERE enabled=1"):
        count += await telegram.ingest(task, message, chat)
        count += await telegram.ingest(task, message, chat)  # Deliberate duplicate.
        message.message = "测试信号 ETH 止损更新（模拟数据）"
        message.edit_date = datetime.now(timezone.utc)
        count += await telegram.ingest(task, message, chat, edited=True)
        message.id = 102
        message.reply_to.reply_to_msg_id = 99
        count += await telegram.ingest(task, message, chat)  # Wrong topic.
    return {"enqueued": count}
