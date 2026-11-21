import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types

from relay.config import Config
from relay.store import Store
from relay.telegram import TelegramService
from tests.test_relay import FakeTelegram, client, make_route, store


def message(sender, *, text="交易信号", topic=0, forwarded=None):
    return types.Message(id=500, peer_id=types.PeerChannel(123456), date=datetime.now(timezone.utc),
                         message=text, from_id=sender, fwd_from=forwarded,
                         reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=topic) if topic else None)


def test_sender_filter_accepts_actual_account_and_applies_to_edits_and_recovery(store, tmp_path):
    task = make_route(store, topic_id=88, include="交易")
    task["sender_ids"] = "77,88"
    service = TelegramService(store, Config(tmp_path / "settings.json"))
    chat = SimpleNamespace(forum=True, noforwards=False, username=None)
    async def run():
        target = message(types.PeerUser(77), topic=88)
        assert await service.ingest(task, target, chat) == 1
        target.message = "交易信号修改"
        target.edit_date = datetime.now(timezone.utc)
        assert await service.ingest(task, target, chat, edited=True) == 1
        other = message(types.PeerUser(99), topic=88)
        other.id = 501
        assert await service.ingest(task, other, chat) == 0
        assert await service.ingest(task, other, chat, recovered=True) == 0
        recovered = message(types.PeerUser(88), topic=88)
        recovered.id = 502
        assert await service.ingest(task, recovered, chat, recovered=True) == 1
        wrong_topic = message(types.PeerUser(77), topic=99)
        assert await service.ingest(task, wrong_topic, chat) == 0
        excluded_text = message(types.PeerUser(77), topic=88, text="闲聊")
        assert await service.ingest(task, excluded_text, chat) == 0
    asyncio.run(run())
    assert [r["kind"] for r in store.all("SELECT kind FROM deliveries ORDER BY id")] == ["new", "edit", "catchup"]


@pytest.mark.parametrize("sender", [types.PeerUser(99), types.PeerChannel(77), None])
def test_forwarded_author_and_anonymous_post_signature_cannot_impersonate_target(store, tmp_path, sender):
    task = make_route(store)
    task["sender_ids"] = "77"
    m = message(sender, forwarded=types.MessageFwdHeader(date=datetime.now(timezone.utc), from_id=types.PeerUser(77)))
    m.post_author = "目标昵称"
    service = TelegramService(store, Config(tmp_path / "settings.json"))
    assert asyncio.run(service.ingest(task, m, SimpleNamespace(forum=False, noforwards=False))) == 0


def test_username_resolution_persists_identity_and_rejects_channels(store, tmp_path):
    async def run():
        service = TelegramService(store, Config(tmp_path / "settings.json"))
        get_entity = AsyncMock(side_effect=[types.User(id=77, first_name="目标", username="target"),
                                           types.User(id=77, first_name="目标", username="target")])
        service.require = AsyncMock(return_value=SimpleNamespace(get_entity=get_entity))
        assert await service.resolve_senders("@target, 77") == [{"id": 77, "name": "目标", "username": "target"}]
        get_entity.side_effect = None
        get_entity.return_value = types.Channel(id=77, title="频道", photo=types.ChatPhotoEmpty(), date=datetime.now(timezone.utc))
        with pytest.raises(ValueError, match="不是可用"):
            await service.resolve_senders("@channel")
        with pytest.raises(ValueError, match="昵称"):
            await service.resolve_senders("某人昵称")
        with pytest.raises(ValueError, match="20"):
            await service.resolve_senders(",".join(str(i) for i in range(1, 22)))
    asyncio.run(run())


def test_recent_sender_choices_are_unique_and_respect_topic(store, tmp_path):
    async def run():
        service = TelegramService(store, Config(tmp_path / "settings.json"))
        user = types.User(id=77, first_name="目标", username="target")
        m = message(types.PeerUser(77), topic=88)
        m._sender = user
        wrong = message(types.PeerUser(99), topic=99)
        wrong._sender = types.User(id=99, first_name="其他人")
        async def history(*args, **kwargs):
            assert kwargs["limit"] == 100
            for item in [m, m, wrong, message(types.PeerChannel(77), topic=88)]:
                yield item
        chat = types.Channel(id=123456, title="群", photo=types.ChatPhotoEmpty(), date=datetime.now(timezone.utc), forum=True)
        service.require = AsyncMock(return_value=SimpleNamespace(get_entity=AsyncMock(return_value=chat), iter_messages=history))
        assert await service.recent_senders(-100123456, 88) == [{"id": 77, "name": "目标", "username": "target"}]
    asyncio.run(run())


def test_sender_columns_migrate_legacy_tasks_without_changing_existing_routes(store):
    task = make_route(store)
    with store.db() as db:
        schema = db.execute("SELECT sql FROM sqlite_master WHERE name='tasks'").fetchone()[0]
        # Rebuild the pre-feature table with the same old columns and route.
        import re
        schema = re.sub(r',\s*sender_(?:ids|names) TEXT NOT NULL DEFAULT \'\'', '', schema)
        schema = schema.replace('CREATE TABLE tasks(', 'CREATE TABLE tasks_legacy(', 1)
        db.execute(schema)
        columns = ','.join(r[1] for r in db.execute('PRAGMA table_info(tasks)') if r[1] not in ('sender_ids', 'sender_names'))
        db.execute(f'INSERT INTO tasks_legacy ({columns}) SELECT {columns} FROM tasks')
        db.execute('DROP TABLE tasks')
        db.execute('ALTER TABLE tasks_legacy RENAME TO tasks')
    migrated = Store(store.path)
    row = migrated.one("SELECT * FROM tasks WHERE id=?", (task["id"],))
    assert row["sender_ids"] == row["sender_names"] == ""
    assert row["chat_id"] == task["chat_id"] and row["enabled"] == task["enabled"]


def test_task_api_validates_senders_preserves_omitted_filter_and_allows_clearing(client, store, monkeypatch):
    async def resolve(self, value):
        if value == "@missing": raise ValueError("无法识别发送人")
        return [{"id": 77, "name": "目标", "username": "target"}]
    monkeypatch.setattr(FakeTelegram, 'resolve_senders', resolve, raising=False)
    route = make_route(store)
    robot_id = route['robot_id']
    body = {"name": "仅目标", "chat_id": -100998877, "robot_id": robot_id, "sender_filter": "@target"}
    response = client.post('/api/tasks', json=body)
    assert response.status_code == 200
    task = response.json()
    assert task['sender_ids'] == '77' and task['sender_names'] == '目标'
    path = f"/api/tasks/{task['id']}"
    assert client.put(path, json={"name": "保留筛选"}).json()['sender_ids'] == '77'
    assert client.put(path, json={"name": "非法更新", "sender_filter": "@missing"}).status_code == 400
    assert store.one('SELECT name FROM tasks WHERE id=?', (task['id'],))['name'] == '保留筛选'
    assert client.post('/api/telegram/resolve-senders', json={"sender_filter": "@target"}).json()[0]['id'] == 77
    assert client.put(path, json={"name": "所有人", "sender_filter": ""}).json()['sender_ids'] == ''
