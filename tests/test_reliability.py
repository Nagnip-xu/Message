import asyncio
import contextlib
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types

from relay.config import Config
from relay.feishu import Sender, payload_for
from relay.heartbeat import Heartbeat, INTERVAL, slot_for
from relay.store import Store
from relay.telegram import TelegramService
from tests.test_relay import client, make_route, queue


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "relay.sqlite3")


def online(now):
    return SimpleNamespace(status=lambda: {"authorized": True, "connected": True, "state": "ready",
                                          "last_health_at": now, "recovery_warnings": []})


def test_heartbeat_without_messages_is_durable_and_does_not_repeat_after_restart(store, tmp_path):
    now = slot_for(time.time())
    make_route(store)
    config = Config(tmp_path / "settings.json")
    assert Heartbeat(store, config, online(now)).process_once(now) == 1
    assert Heartbeat(Store(store.path), config, online(now)).process_once(now + 100) == 0
    first = store.one("SELECT * FROM deliveries")
    assert first["kind"] == "heartbeat" and "运行正常" in first["body"]
    assert "成功推送：0 条" in first["body"]
    assert Heartbeat(store, config, online(now + INTERVAL)).process_once(now + INTERVAL) == 1
    assert store.one("SELECT status FROM deliveries WHERE id=?", (first["id"],))["status"] == "cancelled"
    assert store.one("SELECT COUNT(*) AS n FROM deliveries WHERE status='pending'")["n"] == 1


def test_heartbeat_continues_while_telegram_is_unhealthy_and_ignores_disabled_robots(store, tmp_path):
    now = slot_for(time.time())
    first = make_route(store)
    second = make_route(store)
    store.execute("UPDATE robots SET enabled=0 WHERE id=?", (second["robot_id"],))
    config = Config(tmp_path / "settings.json")
    tg = SimpleNamespace(status=lambda: {"authorized": True, "connected": True, "state": "ready",
                                         "last_health_at": now - 200, "recovery_warnings": []})
    assert Heartbeat(store, config, tg).process_once(now) == 1
    row = store.one("SELECT * FROM deliveries")
    assert row["robot_id"] == first["robot_id"]
    assert "需要检查" in row["body"] and "未通过在线检查" in row["body"]
    config.save(heartbeat_enabled=False)
    assert Heartbeat(store, config, tg).process_once(now + INTERVAL) == 0


def test_heartbeat_is_not_blocked_by_uncertain_robot_test_and_uses_real_sender(store, tmp_path):
    import httpx

    async def run():
        task = make_route(store)
        now = slot_for(time.time())
        store.execute("""INSERT INTO deliveries(robot_id,source,kind,body,original_at,event_at,received_at,status)
                        VALUES(?,'test','test','unknown',?,?,?,'uncertain')""", (task["robot_id"], now, now, now))
        Heartbeat(store, Config(tmp_path / "settings.json"), online(now)).process_once(now)
        captured = []
        def respond(request):
            captured.append(request.read())
            return httpx.Response(200, json={"code": 0})
        sender = Sender(store)
        await sender.client.aclose()
        sender.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        assert await sender.process_once()
        assert store.one("SELECT status FROM deliveries WHERE kind='heartbeat'")["status"] == "sent"
        assert len(captured) == 1 and "运行状态" in captured[0].decode()
        await sender.close()
    asyncio.run(run())


def test_heartbeat_counts_messages_and_renders_snapshot_time(store, tmp_path):
    now = slot_for(time.time())
    task = make_route(store)
    queue(store, task)
    store.execute("UPDATE deliveries SET status='sent',sent_at=?", (now - 10,))
    Heartbeat(store, Config(tmp_path / "settings.json"), online(now)).process_once(now)
    row = store.one("SELECT * FROM deliveries WHERE kind='heartbeat'")
    payload = payload_for(row, now=now)
    text = str(payload)
    assert "成功推送：1 条" in text and "检查时间" in text and "原消息：" not in text


def test_heartbeat_settings_does_not_reset_telegram(client):
    assert client.put("/api/heartbeat", json={"enabled": False}).status_code == 200
    assert client.get("/api/state").json()["settings"]["heartbeat_enabled"] is False


@pytest.mark.parametrize("hour", [0, 4, 8, 12, 16, 20])
def test_fixed_heartbeat_all_daily_boundaries_and_midnight(store, tmp_path, hour):
    from zoneinfo import ZoneInfo
    now = datetime(2026, 11, 22, hour, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    assert slot_for(now + 14399) == now
    assert slot_for(now + INTERVAL) == now + INTERVAL
    make_route(store)
    config = Config(tmp_path / "settings.json")
    service = Heartbeat(store, config, online(now))
    assert service.process_once(now - 1) == 0
    assert service.process_once(now) == 1
    assert Heartbeat(Store(store.path), config, online(now)).process_once(now + 20) == 0
    assert service.process_once(now + INTERVAL) == 1


def test_fixed_schedule_ignores_legacy_rolling_times_and_waits_after_restart(store, tmp_path):
    now = slot_for(time.time())
    first, second = make_route(store), make_route(store)
    config = Config(tmp_path / "settings.json")
    store.execute("INSERT INTO runtime_state VALUES(?,?)", (f"heartbeat:{first['robot_id']}", now - 100))
    store.execute("INSERT INTO runtime_state VALUES(?,?)", (f"heartbeat:{second['robot_id']}", now - 7200))
    assert Heartbeat(store, config, online(now)).process_once(now + 3600) == 0
    assert not store.all("SELECT * FROM deliveries")
    assert Heartbeat(Store(store.path), config, online(now)).process_once(now + INTERVAL + 5) == 2
    assert len(store.all("SELECT * FROM deliveries")) == 2
    # Multiple missed times produce no off-schedule catch-up reports.
    assert Heartbeat(Store(store.path), config, online(now)).process_once(now + INTERVAL * 4 + 100) == 0
    assert Heartbeat(Store(store.path), config, online(now)).process_once(now + INTERVAL * 5) == 2


def test_fixed_schedule_reenable_waits_until_next_boundary(store, tmp_path):
    now = slot_for(time.time())
    make_route(store)
    config = Config(tmp_path / "settings.json")
    config.save(heartbeat_enabled=False)
    service = Heartbeat(store, config, online(now))
    assert service.process_once(now) == 0
    config.save(heartbeat_enabled=True)
    assert service.process_once(now + 120) == 0
    assert service.process_once(now + INTERVAL + 30) == 1


def test_connected_socket_with_stale_health_is_not_reported_ready(store, tmp_path):
    service = TelegramService(store, Config(tmp_path / "settings.json"))
    service.client = SimpleNamespace(is_connected=lambda: True)
    service.authorized, service.state = True, "ready"
    service.last_health_at = time.time() - 91
    assert not service.status()["connected"]
    assert service.status()["state"] == "reconnecting"
    service.last_health_at = time.time()
    assert service.status()["state"] == "ready"


def test_recovery_drains_oldest_messages_across_multiple_batches_without_skipping(store, tmp_path):
    async def run():
        task = make_route(store, edits=False)
        store.execute("UPDATE tasks SET cursor=100 WHERE id=?", (task["id"],))
        config = Config(tmp_path / "settings.json")
        config.values["catchup_limit"] = 2
        service = TelegramService(store, config)
        messages = [types.Message(id=i, peer_id=types.PeerChannel(123456), date=datetime.now(timezone.utc),
                                  message=f"message {i}") for i in range(101, 106)]
        chat = SimpleNamespace(noforwards=False, forum=False, username=None)
        calls = []
        async def history(_, *, min_id, reverse, limit):
            calls.append((min_id, reverse))
            for message in [m for m in messages if m.id > min_id][:limit]:
                yield message
        client = SimpleNamespace(get_entity=AsyncMock(return_value=chat), iter_messages=history)
        service.require = AsyncMock(return_value=client)
        await service.recover()
        assert service.recovery_backlog
        assert store.one("SELECT cursor FROM tasks")["cursor"] == 102
        await service.recover()
        await service.recover()
        assert not service.recovery_backlog and not service.recovery_warnings
        assert calls == [(100, True), (102, True), (104, True)]
        assert [r["message_id"] for r in store.all("SELECT message_id FROM deliveries ORDER BY id")] == list(range(101, 106))
        await service.recover()
        assert len(store.all("SELECT id FROM deliveries")) == 5
    asyncio.run(run())


def test_recovery_timeout_preserves_cursor_and_surfaces_warning(store, tmp_path, monkeypatch):
    monkeypatch.setattr("relay.telegram.HISTORY_TIMEOUT", .02)
    async def run():
        task = make_route(store, edits=False)
        store.execute("UPDATE tasks SET cursor=100 WHERE id=?", (task["id"],))
        service = TelegramService(store, Config(tmp_path / "settings.json"))
        async def stuck_history(*args, **kwargs):
            await asyncio.Event().wait()
            yield None
        client = SimpleNamespace(get_entity=AsyncMock(return_value=SimpleNamespace()), iter_messages=stuck_history)
        service.require = AsyncMock(return_value=client)
        with pytest.raises(TimeoutError):
            await service.recover()
        assert store.one("SELECT cursor FROM tasks")["cursor"] == 100
        assert service.recovery_warnings and service.last_recovery_at is None
    asyncio.run(run())


def test_hung_telegram_rpc_discards_client_for_automatic_reconnect(store, tmp_path, monkeypatch):
    monkeypatch.setattr("relay.telegram.HEALTH_TIMEOUT", .02)
    async def run():
        config = Config(tmp_path / "settings.json")
        config.values["api_id"] = 123
        service = TelegramService(store, config)
        disconnected = asyncio.Event()
        class StuckClient:
            def is_connected(self): return True
            async def __call__(self, request): await asyncio.Event().wait()
            async def disconnect(self): disconnected.set()
        client = StuckClient()
        service.client, service.authorized, service.state = client, True, "ready"
        service.get_client = AsyncMock(return_value=client)
        worker = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(disconnected.wait(), timeout=1)
            assert service.client is None and not service.authorized
            assert service.state == "offline" and "超时" in service.last_error
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError): await worker
    asyncio.run(run())
