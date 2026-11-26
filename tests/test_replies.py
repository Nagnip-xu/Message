import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types

from relay.config import Config
from relay.feishu import payload_for
from relay.store import Store
from relay.telegram import TelegramService
from relay.voice import VoiceWorker
from tests.test_relay import make_route, queue, store
from tests.test_voice import speech, voice_task


def reply_pair(*, text="先等等，暂时别动。", original_text="现在可以加仓吗？"):
    now = datetime.now(timezone.utc)
    original = types.Message(id=80,peer_id=types.PeerChannel(123456),from_id=types.PeerUser(99),date=now,
        message=original_text)
    original.get_sender = AsyncMock(return_value=types.User(id=99,first_name="张三"))
    reply = types.Message(id=101,peer_id=types.PeerChannel(123456),from_id=types.PeerUser(77),date=now,message=text,
        reply_to=types.MessageReplyHeader(reply_to_msg_id=80))
    reply.get_sender = AsyncMock(return_value=types.User(id=77,first_name="李四"))
    reply.get_reply_message = AsyncMock(return_value=original)
    return reply, original


def service_for(store, tmp_path):
    return TelegramService(store,Config(tmp_path/"settings.json"))


CHAT = SimpleNamespace(noforwards=False,forum=False,username=None)


def test_reply_format_exact_body_only_and_durable_after_restart(store,tmp_path):
    task = make_route(store)
    reply, original = reply_pair()
    original.reply_to = types.MessageReplyHeader(reply_to_msg_id=40)
    original.get_reply_message = AsyncMock()
    service = service_for(store,tmp_path)
    assert asyncio.run(service.ingest(task,reply,CHAT)) == 1
    job = Store(store.path).claim()
    assert job['body'] == '先等等，暂时别动。'
    payload = payload_for(job)
    assert payload == {'msg_type':'text','content':{'text':'↳ 回复：现在可以加仓吗？\n先等等，暂时别动。'}}
    original.get_reply_message.assert_not_awaited()
    original.message = '被回复的原文修改了'
    assert asyncio.run(service.ingest(task,reply,CHAT)) == 0
    reply.get_reply_message.assert_awaited_once()


def test_sender_filter_matches_reply_author_and_keyword_filter_does_not_use_quote(store,tmp_path):
    task = make_route(store,include='加仓')
    task['sender_ids'] = '77'
    reply, original = reply_pair()
    service = service_for(store,tmp_path)
    assert asyncio.run(service.ingest(task,reply,CHAT)) == 0
    reply.get_reply_message.assert_not_awaited()
    task['include_words'] = ''
    assert asyncio.run(service.ingest(task,reply,CHAT)) == 1
    assert '张三' in store.one('SELECT reply_context FROM deliveries')['reply_context']
    task2 = make_route(store)
    task2['sender_ids'] = '99'
    assert asyncio.run(service.ingest(task2,reply,CHAT)) == 0


@pytest.mark.parametrize('reason',['deleted','denied','protected'])
def test_missing_or_protected_original_never_drops_current_reply_or_leaks_quote(store,tmp_path,reason):
    task = make_route(store)
    reply, original = reply_pair(original_text='不能泄露的原文')
    if reason == 'deleted':reply.get_reply_message.return_value = None
    if reason == 'denied':reply.get_reply_message.side_effect = ValueError('private error')
    if reason == 'protected':original.noforwards = True
    assert asyncio.run(service_for(store,tmp_path).ingest(task,reply,CHAT)) == 1
    text = payload_for(store.claim())['content']['text']
    assert '先等等，暂时别动。' in text
    assert '不能泄露的原文' not in text and 'private error' not in text
    assert '无法引用' in text if reason == 'protected' else '不可读取' in text


def test_reply_lookup_timeout_preserves_reply(store,tmp_path,monkeypatch):
    async def run():
        task = make_route(store)
        reply, original = reply_pair()
        async def slow():await asyncio.Event().wait()
        reply.get_reply_message = slow
        real_wait_for = asyncio.wait_for
        async def bounded(awaitable, timeout):return await real_wait_for(awaitable,timeout=.01)
        monkeypatch.setattr('relay.telegram.asyncio.wait_for',bounded)
        assert await service_for(store,tmp_path).ingest(task,reply,CHAT) == 1
        assert '原消息不可读取' in payload_for(store.claim())['content']['text']
    asyncio.run(run())


def test_quote_is_single_line_and_bounded_and_unknown_sender_keeps_text(store,tmp_path):
    task = make_route(store)
    reply, original = reply_pair(original_text='加仓\n\n'+'很长的原文'*100)
    original.get_sender.side_effect = ValueError()
    assert asyncio.run(service_for(store,tmp_path).ingest(task,reply,CHAT)) == 1
    context = store.one('SELECT reply_context FROM deliveries')['reply_context']
    assert context.startswith('↳ 回复匿名用户：加仓 ')
    assert context.endswith('…') and len(context) < 240 and '\n' not in context


@pytest.mark.parametrize('topic_header',[
    types.MessageReplyHeader(forum_topic=True,reply_to_msg_id=88),
    types.MessageReplyHeader(forum_topic=True,reply_to_msg_id=88,reply_to_top_id=88),
])
def test_forum_topic_membership_is_not_a_reply_quote(store,tmp_path,topic_header):
    task = make_route(store,topic_id=88)
    reply, original = reply_pair()
    reply.reply_to = topic_header
    assert asyncio.run(service_for(store,tmp_path).ingest(task,reply,CHAT)) == 1
    reply.get_reply_message.assert_not_awaited()
    assert store.one('SELECT reply_context FROM deliveries')['reply_context'] == ''


def test_actual_reply_in_topic_keeps_quote(store,tmp_path):
    task = make_route(store,topic_id=88)
    reply, original = reply_pair()
    reply.reply_to = types.MessageReplyHeader(forum_topic=True,reply_to_msg_id=80,reply_to_top_id=88)
    assert asyncio.run(service_for(store,tmp_path).ingest(task,reply,CHAT)) == 1
    assert store.one('SELECT reply_context FROM deliveries')['reply_context'].startswith('↳ 回复张三：')


def test_cross_chat_protected_original_is_not_quoted(store,tmp_path):
    task = make_route(store)
    reply, original = reply_pair(original_text='另一个群的受保护原文')
    original.peer_id = types.PeerChannel(999999)
    original.get_chat = AsyncMock(return_value=SimpleNamespace(noforwards=True))
    assert asyncio.run(service_for(store,tmp_path).ingest(task,reply,CHAT)) == 1
    assert '无法引用' in store.one('SELECT reply_context FROM deliveries')['reply_context']


def test_reply_context_is_cached_across_tasks_but_not_forever(store,tmp_path):
    service = service_for(store,tmp_path)
    reply, original = reply_pair()
    task1, task2 = make_route(store), make_route(store)
    assert asyncio.run(service.ingest(task1,reply,CHAT)) == 1
    assert asyncio.run(service.ingest(task2,reply,CHAT)) == 1
    reply.get_reply_message.assert_awaited_once()
    service.reply_cache = {key:(time.time()-61,value[1]) for key,value in service.reply_cache.items()}
    reply.id = 102
    assert asyncio.run(service.ingest(task1,reply,CHAT)) == 1
    assert reply.get_reply_message.await_count == 2


def test_voice_reply_context_survives_transcription_without_affecting_keywords(store,tmp_path):
    async def run():
        task = voice_task(store,include='观望')
        reply, original = reply_pair()
        voice = speech()
        voice.reply_to = reply.reply_to
        voice.get_reply_message = reply.get_reply_message
        voice.get_sender = reply.get_sender
        assert await service_for(store,tmp_path).ingest(task,voice,CHAT) == 1
        job = store.one('SELECT * FROM voice_jobs')
        assert job['caption'] == '' and job['reply_context'] == '↳ 回复张三：现在可以加仓吗？'
        worker = VoiceWorker(store,SimpleNamespace(status=lambda:{'authorized':True,'connected':True}))
        worker.recognize_job = AsyncMock(return_value='今天先观望。')
        assert await worker.process_once()
        text = payload_for(store.claim())['content']['text']
        assert text == '↳ 回复：现在可以加仓吗？\n语音转文字（9秒）：今天先观望。'
    asyncio.run(run())


def test_rich_message_does_not_repeat_inline_reply_speaker(store,tmp_path):
    task = make_route(store)
    reply, original = reply_pair()
    assert asyncio.run(service_for(store,tmp_path).ingest(task,reply,CHAT)) == 1
    job = store.claim()
    job['show_sender'] = True
    job['show_reply_names'] = True
    job['show_forward_time'] = True
    rendered = str(payload_for(job))
    assert rendered.count('李四') == 1 and '发送人：' not in rendered
    assert '转发：' in rendered


def test_reply_name_toggle_applies_to_existing_queue_without_changing_quote_or_body(store,tmp_path):
    task = make_route(store)
    reply, original = reply_pair(text='正文：继续观望',original_text='原文：现在呢？')
    original.get_sender.return_value.first_name = '这是一个特别特别长的被引用作者名称'
    reply.get_sender.return_value.first_name = '这是一个特别特别长的当前发言人名称'
    assert asyncio.run(service_for(store,tmp_path).ingest(task,reply,CHAT)) == 1
    store.execute('UPDATE tasks SET show_reply_names=1 WHERE id=?',(task['id'],))
    job = store.claim()
    assert payload_for(job)['content']['text'] == '↳ 回复这是一个特别特别长的被引…：原文：现在呢？\n这是一个特别特别长的当前…：正文：继续观望'
    store.execute("UPDATE deliveries SET status='retry',next_attempt=0")
    store.execute('UPDATE tasks SET show_reply_names=0,show_sender=1 WHERE id=?',(task['id'],))
    job = Store(store.path).claim()
    assert payload_for(job)['content']['text'] == '↳ 回复：原文：现在呢？\n正文：继续观望'
    # Multi-part replies repeat neither the quote nor names when hidden.
    job.update(part=2,parts=2)
    assert payload_for(job)['content']['text'] == '【新消息 · 2/2】正文：继续观望'


def test_name_option_does_not_change_regular_messages_or_unavailable_quote():
    from tests.test_message_display import message
    job = message(show_reply_names=True)
    assert payload_for(job)['content']['text'] == '加不加仓呢'
    job.update(reply_context='↳ 回复：原消息不可读取')
    assert payload_for(job)['content']['text'] == '↳ 回复：原消息不可读取\n测试成员：加不加仓呢'
    job['show_reply_names'] = False
    assert payload_for(job)['content']['text'] == '↳ 回复：原消息不可读取\n加不加仓呢'


def test_reply_migration_keeps_old_pending_text_and_voice_jobs(store,tmp_path):
    task = voice_task(store)
    queue(store,task,text='迁移前待发送的文字')
    assert asyncio.run(service_for(store,tmp_path).ingest(task,speech(),CHAT)) == 1
    with store.db() as db:
        # The deployment's SQLite predates DROP COLUMN. Build the legacy schema explicitly.
        import re
        for table in ('deliveries','voice_jobs'):
            schema = db.execute('SELECT sql FROM sqlite_master WHERE name=?',(table,)).fetchone()[0]
            schema = re.sub(r",\s*reply_context TEXT NOT NULL DEFAULT ''",'',schema)
            db.execute(schema.replace(f'CREATE TABLE {table}',f'CREATE TABLE {table}_legacy',1))
            columns = ','.join(row[1] for row in db.execute(f'PRAGMA table_info({table})') if row[1] != 'reply_context')
            db.execute(f'INSERT INTO {table}_legacy ({columns}) SELECT {columns} FROM {table}')
            db.execute(f'DROP TABLE {table}')
            db.execute(f'ALTER TABLE {table}_legacy RENAME TO {table}')
    migrated = Store(store.path)
    delivery = migrated.claim()
    assert delivery['body'] == '迁移前待发送的文字' and delivery['reply_context'] == ''
    voice = migrated.claim_voice()
    assert voice['message_id'] == 101 and voice['reply_context'] == ''
