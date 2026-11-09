import asyncio
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from telethon import types

from relay.app import create_app
from relay.config import Config
from relay.feishu import Sender, payload_for, signature, split_text, validate_webhook
from relay.store import Store
from relay.telegram import TelegramService, matches_words, topic_id_for

WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/unit-test-only"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "test.sqlite3")


def make_route(store, *, enabled=True, edits=True, include="", exclude="", topic_id=0):
    now = time.time()
    robot_id = store.execute("INSERT INTO robots(name,webhook,secret,enabled,created_at) VALUES(?,?,?,?,?)", ("测试目标", WEBHOOK, "unit-secret", 1, now))
    task_id = store.execute("""INSERT INTO tasks(name,chat_id,chat_title,topic_id,topic_title,robot_id,
      enabled,edits,include_words,exclude_words,enabled_since,last_scan,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      ("测试任务", -100123456, "测试群", topic_id, "交易话题" if topic_id else "", robot_id, enabled, edits, include, exclude, now - 100, now - 100, now))
    return store.one("SELECT * FROM tasks WHERE id=?", (task_id,))


def queue(store, task, message=100, version="v1", kind="new", text="测试正文"):
    now = time.time()
    return store.enqueue(task, message, version, split_text(text), kind=kind, source="测试来源", link="", original_at=now, event_at=now, received_at=now)


def test_enqueue_is_atomic_and_deduplicates_versions(store):
    task = make_route(store)
    assert queue(store, task) == 1
    assert queue(store, task) == 0
    assert queue(store, task, version="v2", kind="edit") == 1
    assert len(store.all("SELECT * FROM deliveries")) == 2


def test_chunking_preserves_chinese_and_emoji():
    text = "开仓价格 1234.56 🚀\n" * 2000
    chunks = split_text(text)
    assert len(chunks) > 1
    assert "".join(chunks) == text
    assert all(len(c.encode()) <= 7500 for c in chunks)


def test_signature_matches_feishu_algorithm():
    expected = base64.b64encode(hmac.new(b"123456\nsecret", b"", hashlib.sha256).digest()).decode()
    assert signature("secret", "123456") == expected


@pytest.mark.parametrize("value", ["http://open.feishu.cn/open-apis/bot/v2/hook/abc", "https://evil.example/open-apis/bot/v2/hook/abc", "https://open.feishu.cn.evil.example/open-apis/bot/v2/hook/abc", "https://open.feishu.cn/open-apis/bot/v2/hook/abc?secret=x", "https://open.feishu.cn:123/open-apis/bot/v2/hook/abc"])
def test_only_actual_feishu_webhooks_allowed(value):
    with pytest.raises(ValueError):
        validate_webhook(value)


def test_queue_pauses_and_resumes_without_losing_work(store):
    task = make_route(store, enabled=False)
    queue(store, task)
    assert store.claim() is None
    store.execute("UPDATE tasks SET enabled=1")
    assert store.claim()["status"] == "pending"
    assert store.one("SELECT attempts FROM deliveries")["attempts"] == 1


def test_restarting_recovers_inflight_delivery(store):
    task = make_route(store)
    queue(store, task)
    store.claim()
    recovered = Store(store.path)
    recovered.mark_inflight_uncertain()
    assert recovered.one("SELECT status FROM deliveries")["status"] == "uncertain"
    assert recovered.claim() is None


def test_revision_and_chunk_order_survives_retries(store):
    task = make_route(store)
    queue(store, task, text="多" * 8000)
    queue(store, task, version="v2", kind="edit")
    first = store.claim()
    store.execute("UPDATE deliveries SET status='retry',next_attempt=? WHERE id=?", (time.time() + 60, first["id"]))
    assert store.claim() is None
    store.execute("UPDATE deliveries SET status='sent' WHERE id=?", (first["id"],))
    assert store.claim()["part"] == 2


def test_failed_destination_does_not_block_other_messages(store):
    task = make_route(store)
    queue(store, task)
    queue(store, task, message=101)
    first = store.claim()
    store.execute("UPDATE deliveries SET status='retry',next_attempt=? WHERE id=?", (time.time() + 60, first["id"]))
    assert store.claim()["message_id"] == 101


def test_feishu_success_requires_json_code_zero(store):
    async def run():
        task = make_route(store)
        queue(store, task)
        sender = Sender(store)
        await sender.client.aclose()
        sender.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 19021})))
        await sender.process_once()
        row = store.one("SELECT * FROM deliveries")
        assert row["status"] == "failed"
        assert "签名" in row["error"]
        assert row["sent_at"] is None
        await sender.close()
    asyncio.run(run())


def test_http_429_is_retried_and_then_sent(store):
    async def run():
        task = make_route(store)
        queue(store, task)
        responses = [httpx.Response(429), httpx.Response(200, json={"code": 0})]
        sender = Sender(store)
        await sender.client.aclose()
        sender.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: responses.pop(0)))
        await sender.process_once()
        assert store.one("SELECT status FROM deliveries")["status"] == "retry"
        store.execute("UPDATE deliveries SET next_attempt=0")
        await sender.process_once()
        row = store.one("SELECT * FROM deliveries")
        assert row["status"] == "sent" and row["attempts"] == 2
        await sender.close()
    asyncio.run(run())


def test_network_timeout_retains_queue_without_exposing_webhook(store):
    async def run():
        task = make_route(store)
        queue(store, task)
        def fail(request):
            raise httpx.ReadTimeout(f"secret URL: {WEBHOOK}", request=request)
        sender = Sender(store)
        await sender.client.aclose()
        sender.client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
        await sender.process_once()
        row = store.one("SELECT * FROM deliveries")
        assert row["status"] == "uncertain"
        assert WEBHOOK not in row["error"]
        await sender.close()
    asyncio.run(run())


def test_stale_label_uses_edit_time_not_original_time():
    now = time.time()
    job = dict(id=1, kind="edit", source="来源", body="止损已更新", message_id=20, parts=1, part=1, link="", original_at=now - 86400, event_at=now, stale_minutes=5)
    payload = payload_for(job, "secret", now)
    title = payload["content"]["post"]["zh_cn"]["title"]
    assert "延迟" not in title
    job["event_at"] = now - 301
    assert "延迟补发" in payload_for(job, now=now)["content"]["post"]["zh_cn"]["title"]


def test_post_separates_metadata_and_omits_internal_ids():
    now = time.time()
    job = dict(id=7, kind="new", source="来源", sender_name="测试发送者", body="信号正文", message_id=1727479, parts=1, part=1,
               link="https://t.me/c/123/1727479", original_at=now, event_at=now, stale_minutes=5)
    rows = payload_for(job, now=now)["content"]["post"]["zh_cn"]["content"]
    assert rows[0] == [{"tag": "text", "text": "信号正文"}]
    assert rows[1] == [{"tag": "text", "text": " "}]
    assert rows[2] == [{"tag": "text", "text": "发送人：测试发送者"}]
    assert "原消息：" in rows[3][0]["text"]
    assert "转发：" in rows[4][0]["text"]
    assert "来源编号" not in json.dumps(rows, ensure_ascii=False)
    assert "通知 #" not in json.dumps(rows, ensure_ascii=False)
    assert rows[5][0]["text"] == "在 Telegram 查看原消息"


def test_post_payload_respects_size_budget():
    now = time.time()
    job = dict(id=1, kind="new", source="来源" * 100, body=split_text('"\\开仓🚀' * 5000)[0], message_id=1, parts=10, part=1, link="https://t.me/c/123/456", original_at=now, event_at=now, stale_minutes=5)
    assert len(json.dumps(payload_for(job), ensure_ascii=False).encode()) < 20000


def test_thousands_of_lines_account_for_rich_text_row_overhead():
    now = time.time()
    text = ('"\\ETH\n' * 3000)
    chunks = split_text(text)
    assert ''.join(chunks) == text
    for part, chunk in enumerate(chunks, 1):
        job = dict(id=1, kind="new", source="来源", body=chunk, message_id=1, parts=len(chunks), part=part,
                   link="", original_at=now, event_at=now, stale_minutes=5)
        assert len(json.dumps(payload_for(job), ensure_ascii=False).encode()) < 20000


def test_rate_limited_robot_yields_to_another_destination(store):
    async def run():
        first_task = make_route(store)
        second_task = make_route(store)
        other_webhook = WEBHOOK + '-other'
        store.execute("UPDATE robots SET webhook=? WHERE id=?", (other_webhook, second_task['robot_id']))
        queue(store, first_task)
        queue(store, second_task)
        sender = Sender(store)
        await sender.client.aclose()
        sender.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"code": 0})))
        sender.recent[WEBHOOK].extend([time.monotonic()] * 4)
        await sender.process_once()
        assert store.one("SELECT status,attempts FROM deliveries WHERE id=1") == {"status": "pending", "attempts": 0}
        await sender.process_once()
        assert store.one("SELECT status FROM deliveries WHERE id=2")["status"] == "sent"
        await sender.close()
    asyncio.run(run())


def test_remote_server_error_is_held_for_reconciliation(store):
    async def run():
        task = make_route(store)
        queue(store, task)
        sender = Sender(store)
        await sender.client.aclose()
        sender.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(502)))
        await sender.process_once()
        row = store.one("SELECT * FROM deliveries")
        assert row["status"] == "uncertain"
        assert "结果未知" in row["error"]
        assert store.claim() is None
        await sender.close()
    asyncio.run(run())


def test_topic_header_distinguishes_reply_and_general():
    first = SimpleNamespace(reply_to=SimpleNamespace(forum_topic=True, reply_to_top_id=None, reply_to_msg_id=88))
    nested = SimpleNamespace(reply_to=SimpleNamespace(forum_topic=True, reply_to_top_id=88, reply_to_msg_id=99))
    general = SimpleNamespace(reply_to=None)
    assert topic_id_for(first, True) == 88
    assert topic_id_for(nested, True) == 88
    assert topic_id_for(general, True) == 1
    assert topic_id_for(general, False) == 0


def test_keyword_exclusion_has_precedence():
    assert matches_words("ETH 开仓", "btc，eth", "广告\n推广")
    assert not matches_words("ETH 开仓 广告", "ETH", "广告")
    assert matches_words("任意消息", "", "")


def test_ingest_topic_edit_and_protection(store, tmp_path):
    task = make_route(store, topic_id=88)
    config = Config(tmp_path / "settings.json")
    service = TelegramService(store, config)
    chat = SimpleNamespace(forum=True, noforwards=False, username=None)
    message = types.Message(id=100, peer_id=types.PeerChannel(123456), date=datetime.now(timezone.utc), message="ETH 开仓", reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=88))
    async def run():
        assert await service.ingest(task, message, chat) == 1
        assert await service.ingest(task, message, chat) == 0
        message.message = "ETH 止损更新"
        message.edit_date = datetime.now(timezone.utc)
        assert await service.ingest(task, message, chat, edited=True) == 1
        message.id = 101
        message.reply_to.reply_to_msg_id = 99
        assert await service.ingest(task, message, chat) == 0
        message.reply_to.reply_to_msg_id = 88
        message.noforwards = True
        assert await service.ingest(task, message, chat) == 0
        assert len(store.all("SELECT * FROM deliveries")) == 2
    asyncio.run(run())


def test_ingest_keeps_channel_post_author(store, tmp_path):
    task = make_route(store)
    service = TelegramService(store, Config(tmp_path / "settings.json"))
    chat = SimpleNamespace(noforwards=False, forum=False, username=None)
    message = types.Message(id=200, peer_id=types.PeerChannel(123456), date=datetime.now(timezone.utc),
                            message="频道信号", post=True, post_author="熬鹰管理员")
    assert asyncio.run(service.ingest(task, message, chat)) == 1
    assert store.one("SELECT sender_name FROM deliveries")["sender_name"] == "熬鹰管理员"


def test_ingest_keeps_group_sender_name_without_changing_body(store, tmp_path, monkeypatch):
    task = make_route(store)
    service = TelegramService(store, Config(tmp_path / "settings.json"))
    message = types.Message(id=201, peer_id=types.PeerChannel(123456), date=datetime.now(timezone.utc),
                            message="ETH 开仓", from_id=types.PeerUser(77))

    async def get_sender():
        return types.User(id=77, first_name="测试", last_name="成员")

    monkeypatch.setattr(message, "get_sender", get_sender)
    assert asyncio.run(service.ingest(task, message, SimpleNamespace(noforwards=False, forum=False, username=None))) == 1
    assert store.one("SELECT body,sender_name FROM deliveries") == {"body": "ETH 开仓", "sender_name": "测试 成员"}


def test_sender_lookup_failure_does_not_drop_message(store, tmp_path, monkeypatch):
    task = make_route(store)
    service = TelegramService(store, Config(tmp_path / "settings.json"))
    message = types.Message(id=202, peer_id=types.PeerChannel(123456), date=datetime.now(timezone.utc), message="信号正文")

    async def get_sender():
        raise ConnectionError("sender lookup unavailable")

    monkeypatch.setattr(message, "get_sender", get_sender)
    assert asyncio.run(service.ingest(task, message, SimpleNamespace(noforwards=False, forum=False, username=None))) == 1
    assert store.one("SELECT body,sender_name FROM deliveries") == {"body": "信号正文", "sender_name": ""}


def test_sender_column_migration_preserves_existing_deliveries(store):
    task = make_route(store)
    queue(store, task)
    # Recreate the legacy table without requiring SQLite 3.35's DROP COLUMN.
    with store.db() as db:
        schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='deliveries'").fetchone()[0]
        legacy_schema = schema.replace("CREATE TABLE deliveries(", "CREATE TABLE deliveries_legacy(", 1)
        legacy_schema = legacy_schema.replace("sender_name TEXT NOT NULL DEFAULT '', ", "", 1)
        db.execute(legacy_schema)
        columns = ",".join(row[1] for row in db.execute("PRAGMA table_info(deliveries)") if row[1] != "sender_name")
        db.execute(f"INSERT INTO deliveries_legacy ({columns}) SELECT {columns} FROM deliveries")
        db.execute("DROP TABLE deliveries")
        db.execute("ALTER TABLE deliveries_legacy RENAME TO deliveries")
    assert "sender_name" not in {row["name"] for row in store.all("PRAGMA table_info(deliveries)")}
    migrated = Store(store.path)
    assert migrated.one("SELECT body,status,sender_name FROM deliveries") == {"body": "测试正文", "status": "pending", "sender_name": ""}
    assert queue(migrated, task) == 0
    assert Store(store.path).one("SELECT COUNT(*) AS count FROM deliveries")["count"] == 1


def test_old_history_is_not_forwarded(store, tmp_path):
    task = make_route(store)
    service = TelegramService(store, Config(tmp_path / "settings.json"))
    message = types.Message(id=2, peer_id=types.PeerChannel(123456), date=datetime.fromtimestamp(task["enabled_since"] - 10, timezone.utc), message="过期历史")
    assert asyncio.run(service.ingest(task, message, SimpleNamespace(noforwards=False, forum=False, username=None), recovered=True)) == 0


class FakeTelegram:
    authorized = True
    recovery_lock = SimpleNamespace(locked=lambda: False)
    def status(self):
        return {"state": "ready", "connected": True, "authorized": True, "user": None, "last_error": ""}
    async def resolve_source(self, chat_id, topic_id):
        return {"chat_title": "测试群", "topic_title": "测试话题" if topic_id else "", "cursor": 100}
    async def close(self):
        pass
    async def reset(self):
        pass


@pytest.fixture
def client(store, tmp_path):
    config = Config(tmp_path / "settings.json")
    config.save(api_id=12345, api_hash="a" * 32)
    app = create_app(store, config, FakeTelegram(), start_workers=False)
    with TestClient(app) as client:
        yield client


def test_api_full_route_lifecycle(client, store):
    robot = client.post("/api/robots", json={"name": "目标", "webhook": WEBHOOK, "secret": "hidden"}).json()
    assert "webhook" not in robot and "secret" not in robot
    body = {"name": "信号", "chat_id": -100123456, "topic_id": 88, "robot_id": robot["id"]}
    response = client.post("/api/tasks", json=body)
    assert response.status_code == 200
    task = response.json()
    assert task["cursor"] == 100 and task["topic_title"] == "测试话题"
    assert client.post("/api/tasks", json=body).status_code == 409
    assert client.post(f"/api/tasks/{task['id']}/toggle").json()["enabled"] == 0
    assert client.post(f"/api/tasks/{task['id']}/toggle").json()["enabled"] == 1
    assert client.delete(f"/api/robots/{robot['id']}").status_code == 400
    assert client.delete(f"/api/tasks/{task['id']}").status_code == 200
    assert client.delete(f"/api/robots/{robot['id']}").status_code == 200


def test_api_test_message_queues_and_preserves_masked_secrets(client, store):
    robot = client.post("/api/robots", json={"name": "目标", "webhook": WEBHOOK, "secret": "hidden"}).json()
    response = client.post(f"/api/robots/{robot['id']}/test")
    assert response.status_code == 200
    assert store.one("SELECT kind,status FROM deliveries") == {"kind": "test", "status": "pending"}
    state = client.get("/api/state").json()
    assert state["settings"]["api_hash_configured"]
    assert "api_hash" not in state["settings"]
    assert "webhook" not in state["robots"][0]
    assert "hidden" not in client.get("/api/diagnostics").text


def test_cross_origin_mutations_are_rejected(client):
    assert client.post("/api/recover", headers={"Origin": "https://other.example"}).status_code == 403
    assert client.post("/api/recover", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_failed_retry_requires_active_task(client, store):
    task = make_route(store, enabled=False)
    queue(store, task)
    store.execute("UPDATE deliveries SET status='failed'")
    assert client.post("/api/deliveries/1/retry").status_code == 400
    store.execute("UPDATE tasks SET enabled=1")
    assert client.post("/api/deliveries/1/retry").status_code == 200
    assert store.one("SELECT status,attempts FROM deliveries") == {"status": "pending", "attempts": 0}


def test_uncertain_delivery_can_be_acknowledged_or_manually_retried(client, store):
    task = make_route(store)
    queue(store, task)
    queue(store, task, message=101)
    store.execute("UPDATE deliveries SET status='uncertain',error='请求结果未知'")

    assert client.post("/api/deliveries/1/acknowledge").status_code == 200
    acknowledged = store.one("SELECT status,sent_at,error FROM deliveries WHERE id=1")
    assert acknowledged["status"] == "sent" and acknowledged["sent_at"] is not None
    assert "核对后标记" in acknowledged["error"]
    assert client.post("/api/deliveries/1/retry").status_code == 400

    assert client.post("/api/deliveries/2/retry").status_code == 200
    retried = store.one("SELECT status,attempts,next_attempt,error FROM deliveries WHERE id=2")
    assert retried == {"status": "pending", "attempts": 0, "next_attempt": 0.0, "error": ""}


def test_acknowledge_rejects_delivery_without_uncertain_result(client, store):
    task = make_route(store)
    queue(store, task)
    assert client.post("/api/deliveries/1/acknowledge").status_code == 400


def test_deleted_tasks_cancel_pending_work(client, store):
    task = make_route(store)
    queue(store, task)
    assert client.delete(f"/api/tasks/{task['id']}").status_code == 200
    row = store.one("SELECT status,task_id FROM deliveries")
    assert row == {"status": "cancelled", "task_id": None}
    assert store.claim() is None


def test_api_pagination_and_search(client, store):
    task = make_route(store)
    for i in range(35):
        queue(store, task, message=i, text=f"ETH 测试 {i}")
    assert len(client.get("/api/deliveries").json()["items"]) == 30
    assert len(client.get("/api/deliveries?page=2").json()["items"]) == 5
    assert client.get("/api/deliveries?q=ETH").json()["total"] == 35
    assert client.get("/api/deliveries?q=BTC").json()["total"] == 0
    assert client.get("/api/deliveries?page=0").status_code == 422
