import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types

from relay.config import Config
from relay.store import Store
from relay.telegram import TelegramService
from relay.voice import VoiceRejected, VoiceUnavailable, VoiceWorker
from tests.test_relay import client, make_route, queue, store


def speech(*, message_id=101, sender=77, duration=9, media_id=901, text=""):
    doc=types.Document(id=media_id,access_hash=1,file_reference=b"",date=datetime.now(timezone.utc),
        mime_type="audio/ogg",size=1000,dc_id=2,
        attributes=[types.DocumentAttributeAudio(duration=duration,voice=True)])
    return types.Message(id=message_id,peer_id=types.PeerChannel(123456),from_id=types.PeerUser(sender),
        date=datetime.now(timezone.utc),message=text,media=types.MessageMediaDocument(document=doc))


def voice_task(store, **kwargs):
    task=make_route(store,**kwargs)
    store.execute("UPDATE tasks SET transcribe_voice=1 WHERE id=?",(task["id"],))
    return store.one("SELECT * FROM tasks WHERE id=?",(task["id"],))


async def enqueue_async(store,task,message=None,**kwargs):
    service=TelegramService(store,Config(store.path.parent/'settings.json'))
    chat=SimpleNamespace(noforwards=False,forum=False,username=None)
    return await service.ingest(task,message or speech(),chat,**kwargs)


def enqueue(store,task,message=None,**kwargs):
    return asyncio.run(enqueue_async(store,task,message,**kwargs))


def worker(store):
    telegram=SimpleNamespace(status=lambda:{"authorized":True,"connected":True})
    return VoiceWorker(store,telegram)


def test_voice_ingest_durable_dedup_and_does_not_block_text_delivery(store):
    task=voice_task(store)
    assert enqueue(store,task)==1
    assert enqueue(store,task)==0
    assert store.one('SELECT COUNT(*) AS n FROM deliveries')['n']==0
    assert store.one('SELECT COUNT(*) AS n FROM voice_jobs')['n']==1
    queue(store,task,message=102,text='文字即时消息')
    assert store.claim()['body']=='文字即时消息'
    w=worker(store)
    w.recognize_job=AsyncMock(return_value='今天先观望。')
    assert asyncio.run(w.process_once())
    row=store.one("SELECT * FROM deliveries WHERE message_id=101")
    assert row['body']=='语音转文字（9秒）：今天先观望。'
    assert row['sender_name']=='' and row['kind']=='new'
    assert not asyncio.run(w.process_once())
    assert store.one('SELECT status FROM voice_jobs')['status']=='done'


def test_voice_disabled_uses_existing_attachment_notice(store):
    task=make_route(store)
    assert enqueue(store,task)==1
    assert not store.all('SELECT * FROM voice_jobs')
    assert '附件' in store.one('SELECT body FROM deliveries')['body']


def test_voice_uses_member_topic_and_protection_rules_before_download(store):
    task=voice_task(store,topic_id=88)
    task['sender_ids']='77'
    msg=speech(sender=99)
    msg.reply_to=types.MessageReplyHeader(forum_topic=True,reply_to_msg_id=88)
    assert enqueue(store,task,msg)==0
    msg=speech(sender=77)
    msg.reply_to=types.MessageReplyHeader(forum_topic=True,reply_to_msg_id=99)
    assert enqueue(store,task,msg)==0
    msg.reply_to.reply_to_msg_id=88
    msg.noforwards=True
    assert enqueue(store,task,msg)==0
    assert not store.all('SELECT * FROM voice_jobs')


@pytest.mark.parametrize('include,exclude,text,expected',[('开仓','','现在开仓',True),('开仓','','今天观望',False),('','广告','广告消息',False)])
def test_voice_keywords_apply_to_actual_transcript(store,include,exclude,text,expected):
    task=voice_task(store,include=include,exclude=exclude)
    assert enqueue(store,task)==1
    w=worker(store);w.recognize_job=AsyncMock(return_value=text)
    asyncio.run(w.process_once())
    assert bool(store.all('SELECT * FROM deliveries'))==expected
    assert store.one('SELECT status FROM voice_jobs')['status']==('done' if expected else 'filtered')


def test_retry_persists_and_terminal_failure_provides_link(store):
    task=voice_task(store)
    enqueue(store,task)
    w=worker(store);w.recognize_job=AsyncMock(side_effect=RuntimeError('private details'))
    for attempt in range(3):
        assert asyncio.run(w.process_once())
        store.execute('UPDATE voice_jobs SET next_attempt=0')
    row=store.one('SELECT * FROM deliveries')
    assert '转写失败' in row['body'] and 'https://t.me/c/' in row['body']
    assert 'private details' not in str(store.all('SELECT * FROM system_logs'))
    assert 'private details' not in row['body']
    assert not asyncio.run(w.process_once())


def test_restart_retries_transcription_and_atomic_finish_cannot_duplicate(store):
    task=voice_task(store);enqueue(store,task)
    job=store.claim_voice()
    restarted=Store(store.path)
    restarted.mark_inflight_uncertain()
    assert restarted.one('SELECT status FROM voice_jobs')['status']=='retry'
    new_job=restarted.claim_voice()
    assert restarted.finish_voice(new_job,['转写结果'])==1
    assert restarted.finish_voice(job,['重复结果'])==0
    assert len(restarted.all('SELECT * FROM deliveries'))==1


def test_task_disable_or_voice_disable_cancels_processing_results(store):
    task=voice_task(store);enqueue(store,task)
    job=store.claim_voice()
    store.execute('UPDATE tasks SET transcribe_voice=0')
    assert store.finish_voice(job,['不应发送'])==0
    assert not store.all('SELECT * FROM deliveries')
    task=voice_task(store);enqueue(store,task)
    store.execute('UPDATE robots SET enabled=0 WHERE id=?',(task['robot_id'],))
    assert store.claim_voice() is None
    assert store.one('SELECT status FROM voice_jobs WHERE task_id=?',(task['id'],))['status']=='cancelled'


def test_offline_does_not_consume_voice_attempts(store):
    task=voice_task(store);enqueue(store,task)
    w=VoiceWorker(store,SimpleNamespace(status=lambda:{'connected':False,'authorized':False}))
    assert not asyncio.run(w.process_once())
    assert store.one('SELECT attempts FROM voice_jobs')['attempts']==0


def test_download_cache_and_privacy_cleanup(store,tmp_path,monkeypatch):
    async def run():
        task=voice_task(store);await enqueue_async(store,task)
        job=store.claim_voice()
        msg=speech()
        async def download(message,*,file,progress_callback):
            progress_callback(5,5)
            Path(file).write_bytes(b'audio')
            return file
        client=SimpleNamespace(get_entity=AsyncMock(return_value=SimpleNamespace(noforwards=False)),
            get_messages=AsyncMock(return_value=msg),download_media=AsyncMock(side_effect=download))
        w=VoiceWorker(store,SimpleNamespace(require=AsyncMock(return_value=client)))
        w.status=lambda:{'ready':True}
        w.transcribe=AsyncMock(return_value='开仓信号')
        monkeypatch.setattr('relay.voice.DATA',tmp_path)
        assert await w.recognize_job(job)=='开仓信号'
        assert not list((tmp_path/'voice-tmp').iterdir())
        assert await w.recognize_job(job)=='开仓信号'
        assert client.download_media.await_count==1
        msg.noforwards=True
        with pytest.raises(VoiceRejected):await w.recognize_job(job)
    asyncio.run(run())


def test_download_exception_removes_temporary_audio(store,tmp_path,monkeypatch):
    async def run():
        task=voice_task(store);await enqueue_async(store,task)
        job=store.claim_voice()
        client=SimpleNamespace(get_entity=AsyncMock(return_value=SimpleNamespace(noforwards=False)),
            get_messages=AsyncMock(return_value=speech()))
        async def download(message,*,file,progress_callback):
            Path(file).write_bytes(b'partial')
            raise TimeoutError()
        client.download_media=download
        w=VoiceWorker(store,SimpleNamespace(require=AsyncMock(return_value=client)))
        w.status=lambda:{'ready':True}
        monkeypatch.setattr('relay.voice.DATA',tmp_path)
        with pytest.raises(TimeoutError):await w.recognize_job(job)
        assert not list((tmp_path/'voice-tmp').iterdir())
    asyncio.run(run())


def test_cancellation_requeues_safely(store):
    async def run():
        task=voice_task(store);await enqueue_async(store,task)
        w=worker(store)
        started=asyncio.Event()
        async def stuck(job):
            started.set();await asyncio.Event().wait()
        w.recognize_job=stuck
        process=asyncio.create_task(w.process_once())
        await started.wait()
        process.cancel()
        with pytest.raises(asyncio.CancelledError):await process
        assert store.one('SELECT status FROM voice_jobs')['status']=='retry'
        assert not w.busy and not store.all('SELECT * FROM deliveries')
    asyncio.run(run())


def test_subprocess_timeout_and_cancellation_are_bounded(store):
    async def run():
        w=worker(store)
        with pytest.raises(TimeoutError):
            await w.command(sys.executable,'-c','import time;time.sleep(10)',timeout=.1)
        result=await w.command(sys.executable,'-c',"print('ok')",timeout=5)
        assert result.strip()==b'ok'
    asyncio.run(run())


@pytest.mark.parametrize('error',[VoiceRejected('未识别到清晰语音'),VoiceUnavailable()])
def test_permanent_voice_failures_are_explicit_and_not_silent(store,error):
    task=voice_task(store);enqueue(store,task)
    w=worker(store);w.recognize_job=AsyncMock(side_effect=error)
    asyncio.run(w.process_once())
    assert '请到 Telegram 收听' in store.one('SELECT body FROM deliveries')['body']
    assert store.one('SELECT attempts FROM voice_jobs')['attempts']==1


def test_voice_api_default_and_preserves_omitted_setting(client,store):
    task=make_route(store)
    response=client.post('/api/tasks',json={'name':'语音任务','robot_id':task['robot_id'],'chat_id':-100887766})
    assert response.status_code==200
    saved=response.json();assert saved['transcribe_voice']==1
    path=f"/api/tasks/{saved['id']}"
    assert client.put(path,json={'name':'关闭','transcribe_voice':False}).json()['transcribe_voice']==0
    assert client.put(path,json={'name':'保持设置'}).json()['transcribe_voice']==0
    assert client.put(path,json={'name':'开启','transcribe_voice':True}).json()['transcribe_voice']==1
    assert client.get('/api/state').json()['voice']['engine']=='SenseVoice · CPU'


def test_oversized_and_changed_voice_are_rejected_before_download(store):
    async def run():
        task=voice_task(store);await enqueue_async(store,task)
        job=store.claim_voice()
        msg=speech(duration=601)
        client=SimpleNamespace(get_entity=AsyncMock(return_value=SimpleNamespace(noforwards=False)),
            get_messages=AsyncMock(return_value=msg),download_media=AsyncMock())
        w=VoiceWorker(store,SimpleNamespace(require=AsyncMock(return_value=client)))
        job['duration']=601
        with pytest.raises(VoiceRejected,match='10 分钟'):await w.recognize_job(job)
        job['duration']=9
        msg.media.document.id=999
        with pytest.raises(VoiceRejected,match='变更'):await w.recognize_job(job)
        client.download_media.assert_not_awaited()
    asyncio.run(run())


def test_transcription_to_signed_feishu_delivery_preserves_text_and_dedup(store):
    import httpx
    import json
    from relay.feishu import Sender, signature
    async def run():
        task=voice_task(store);await enqueue_async(store,task)
        w=worker(store);w.recognize_job=AsyncMock(return_value='今天先观望。')
        await w.process_once()
        captured=[]
        def receive(request):
            captured.append(json.loads(request.read()))
            return httpx.Response(200,json={'code':0})
        sender=Sender(store)
        await sender.client.aclose()
        sender.client=httpx.AsyncClient(transport=httpx.MockTransport(receive))
        try:
            assert await sender.process_once()
            assert not await sender.process_once()
            assert captured[0]['msg_type']=='text'
            assert captured[0]['content']['text']=='语音转文字（9秒）：今天先观望。'
            assert captured[0]['sign']==signature('unit-secret',captured[0]['timestamp'])
            assert store.one('SELECT status FROM deliveries')['status']=='sent'
        finally:await sender.close()
    asyncio.run(run())
