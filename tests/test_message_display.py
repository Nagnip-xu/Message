import json
import time

import pytest

from relay.feishu import payload_for, signature
from relay.store import DISPLAY_FIELDS, Store
from tests.test_relay import client, make_route, queue, store


def message(**values):
    now = time.time()
    return dict(kind="new", source="测试群 / 话题", body="加不加仓呢", sender_name="测试成员",
                link="https://t.me/c/123/456", original_at=now, event_at=now,
                stale_minutes=5, parts=1, part=1, **values)


def test_default_is_exact_body_and_preserves_whitespace_and_signature():
    job = message()
    job["body"] = "加不加仓呢\n\n第二段 <原文> & 内容\n"
    now = time.time()
    result = payload_for(job, "secret", now)
    assert result == {"msg_type": "text", "content": {"text": job["body"]},
                      "timestamp": str(int(now)), "sign": signature("secret", str(int(now)))}


@pytest.mark.parametrize("field,expected", list(zip(DISPLAY_FIELDS,
    ["【新消息】测试群 / 话题", "发送人：测试成员", "原消息：", "转发：", "在 Telegram 查看原消息"])))
def test_individual_display_options_do_not_add_other_metadata(field, expected):
    job = message(**{field: True})
    result = payload_for(job)
    assert result["msg_type"] == "post"
    post = result["content"]["post"]["zh_cn"]
    rendered = json.dumps(post, ensure_ascii=False)
    assert expected in rendered and "加不加仓呢" in rendered
    for other, text in [("show_sender", "发送人："), ("show_original_time", "原消息："),
                        ("show_forward_time", "转发："), ("show_link", "在 Telegram 查看原消息")]:
        if other != field:
            assert text not in rendered
    if field != "show_title":
        assert "title" not in post and "测试群" not in rendered
    else:
        assert post["content"] == [[{"tag": "text", "text": job["body"]}]]


def test_missing_sender_and_link_do_not_create_empty_metadata():
    job = message(show_sender=True, show_link=True)
    job.update(sender_name="", link="")
    assert payload_for(job) == {"msg_type": "text", "content": {"text": job["body"]}}


def test_compact_edits_catchup_delays_and_chunks_remain_identifiable():
    job = message()
    job["kind"] = "edit"
    assert payload_for(job)["content"]["text"] == "【消息更新】加不加仓呢"
    job["kind"] = "catchup"
    assert payload_for(job)["content"]["text"] == "【断线补发】加不加仓呢"
    job.update(kind="new", parts=2, part=1)
    assert "1/2" in payload_for(job)["content"]["text"]
    job.update(parts=1, event_at=time.time() - 600)
    rendered = str(payload_for(job))
    assert "延迟补发" in rendered and "原消息：" in rendered
    assert "发送人：" not in rendered and "转发：" not in rendered


@pytest.mark.parametrize("kind", ["heartbeat", "test"])
def test_system_messages_never_inherit_task_metadata_or_delayed_labels(kind):
    job = message(**dict.fromkeys(DISPLAY_FIELDS, True))
    job.update(kind=kind, body="简短状态内容", event_at=0)
    assert payload_for(job) == {"msg_type": "text", "content": {"text": "简短状态内容"}}


def test_display_settings_apply_when_claiming_existing_queue(store):
    task = make_route(store)
    queue(store, task)
    store.execute("UPDATE tasks SET show_sender=1,show_link=1 WHERE id=?", (task["id"],))
    job = store.claim()
    assert job["show_sender"] == job["show_link"] == 1
    assert job["show_title"] == job["show_forward_time"] == 0
    store.execute("UPDATE deliveries SET status='retry',next_attempt=0")
    store.execute("UPDATE tasks SET show_sender=0,show_link=0")
    assert payload_for(store.claim())["msg_type"] == "text"


def test_task_api_persists_independent_flags_preserves_omitted_and_allows_disabling(client, store):
    task = make_route(store)
    body = {"name": "精简任务", "chat_id": -100998877, "robot_id": task["robot_id"]}
    result = client.post("/api/tasks", json=body)
    assert result.status_code == 200
    saved = result.json()
    assert all(saved[field] == 0 for field in DISPLAY_FIELDS)
    path = f"/api/tasks/{saved['id']}"
    saved = client.put(path, json={"name": "自选显示", "show_sender": True, "show_original_time": True, "show_reply_names": True}).json()
    assert saved["show_sender"] == saved["show_original_time"] == 1
    assert saved["show_title"] == saved["show_forward_time"] == saved["show_link"] == 0
    preserved = client.put(path, json={"name": "保留显示设置"}).json()
    assert preserved["show_sender"] == preserved["show_reply_names"] == 1
    assert client.put(path, json={"name": "关闭回复名称", "show_reply_names": False}).json()["show_reply_names"] == 0
    assert client.put(path, json={"name": "关闭发送人", "show_sender": False}).json()["show_sender"] == 0
    other = store.one("SELECT * FROM tasks WHERE id=?", (task["id"],))
    assert all(other[field] == 0 for field in DISPLAY_FIELDS)
    # New tasks may also explicitly opt in during creation.
    body.update(chat_id=-100887766, show_title=True, show_link=True, show_reply_names=True)
    created = client.post("/api/tasks", json=body).json()
    assert created["show_title"] == created["show_link"] == created["show_reply_names"] == 1


def test_display_migration_preserves_existing_routes_sender_rules_and_queue(store):
    task = make_route(store, edits=False, include="开仓")
    store.execute("UPDATE tasks SET sender_ids='77',sender_names='目标成员' WHERE id=?", (task["id"],))
    queue(store, task)
    with store.db() as db:
        schema = db.execute("SELECT sql FROM sqlite_master WHERE name='tasks'").fetchone()[0]
        import re
        schema = re.sub(r',\s*show_\w+ INTEGER NOT NULL DEFAULT 0', '', schema)
        db.execute(schema.replace('CREATE TABLE tasks(', 'CREATE TABLE tasks_legacy(', 1))
        columns = ','.join(row[1] for row in db.execute('PRAGMA table_info(tasks)') if row[1] not in DISPLAY_FIELDS)
        db.execute(f'INSERT INTO tasks_legacy ({columns}) SELECT {columns} FROM tasks')
        # Table rebuild needs FK checks disabled outside the transaction for queued rows.
        db.commit()
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('DROP TABLE tasks')
        db.execute('ALTER TABLE tasks_legacy RENAME TO tasks')
    migrated = Store(store.path)
    saved = migrated.one("SELECT * FROM tasks")
    assert saved["sender_ids"] == '77' and saved["include_words"] == '开仓' and saved["edits"] == 0
    assert saved["chat_id"] == task["chat_id"] and saved["enabled"] == 1
    assert all(saved[field] == 0 for field in DISPLAY_FIELDS)
    assert migrated.claim()["body"] == "测试正文"
