import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telethon import types

from relay.config import Config
from relay.feishu import Sender, payload_for, signature, split_text
from relay.images import ImageError, ImageUploader, ImageWorker, MAX_IMAGE_BYTES, test_png as png
from relay.store import Store
from relay.telegram import TelegramService
from tests.test_relay import client, make_route, queue, store

CHAT = SimpleNamespace(noforwards=False,forum=False,username=None)


def picture(*,text='',size=1000,document=False):
    now = datetime.now(timezone.utc)
    if document:
        media=types.MessageMediaDocument(document=types.Document(id=999,access_hash=1,file_reference=b'',date=now,
            mime_type='image/png',size=size,dc_id=2,attributes=[types.DocumentAttributeFilename(file_name='photo.png')]))
    else:
        media=types.MessageMediaPhoto(photo=types.Photo(id=999,access_hash=1,file_reference=b'',date=now,dc_id=2,
            sizes=[types.PhotoSize(type='x',w=160,h=100,size=size)]))
    return types.Message(id=101,peer_id=types.PeerChannel(123456),date=now,message=text,media=media)


async def enqueue_image(store,tmp_path,task=None,message=None,**kwargs):
    task=task or make_route(store)
    service=TelegramService(store,Config(tmp_path/'tg-settings.json'))
    assert await service.ingest(task,message or picture(),CHAT,**kwargs)==1
    return task


def configured(tmp_path):
    config=Config(tmp_path/'settings.json')
    config.save(feishu_app_id='cli_test',feishu_app_secret='private-app-secret')
    return config


async def mock_uploader(config,handler=None):
    uploader=ImageUploader(config)
    await uploader.client.aclose()
    def respond(request):
        if request.url.path.endswith('/internal'):
            return httpx.Response(200,json={'code':0,'tenant_access_token':'private-token','expire':7200})
        return httpx.Response(200,json={'code':0,'data':{'image_key':'img_test'}})
    uploader.client=httpx.AsyncClient(transport=httpx.MockTransport(handler or respond))
    return uploader


@pytest.mark.parametrize('document',[False,True])
def test_image_ingest_dedup_and_keeps_text_delivery_immediate(store,tmp_path,document):
    async def run():
        task=make_route(store)
        message=picture(text='交易图',document=document)
        service=TelegramService(store,Config(tmp_path/'config.json'))
        assert await service.ingest(task,message,CHAT)==1
        assert await service.ingest(task,message,CHAT)==0
        assert store.one('SELECT caption FROM image_jobs')['caption']=='交易图'
        assert not store.all('SELECT * FROM deliveries')
        queue(store,task,message=102,text='即时文字')
        assert store.claim()['body']=='即时文字'
    asyncio.run(run())


def test_image_sender_topic_protection_and_keywords_checked_before_queue(store,tmp_path):
    async def run():
        task=make_route(store,include='开仓',topic_id=88)
        task['sender_ids']='77'
        m=picture(text='开仓')
        m.from_id=types.PeerUser(99)
        m.reply_to=types.MessageReplyHeader(forum_topic=True,reply_to_msg_id=88)
        service=TelegramService(store,Config(tmp_path/'config.json'))
        assert await service.ingest(task,m,CHAT)==0
        m.from_id=types.PeerUser(77)
        m.reply_to.reply_to_msg_id=99
        assert await service.ingest(task,m,CHAT)==0
        m.reply_to.reply_to_msg_id=88
        m.noforwards=True
        assert await service.ingest(task,m,CHAT)==0
        m.noforwards=False
        m.message='无关键词'
        assert await service.ingest(task,m,CHAT)==0
        assert not store.all('SELECT * FROM image_jobs')
        m.message='开仓'
        assert await service.ingest(task,m,CHAT)==1
    asyncio.run(run())


def test_image_disabled_retains_attachment_notice(store,tmp_path):
    async def run():
        task=make_route(store)
        task['forward_images']=0
        await enqueue_image(store,tmp_path,task,picture(text='配文'))
        assert not store.all('SELECT * FROM image_jobs')
        assert '含图片' in store.one('SELECT body FROM deliveries')['body']
    asyncio.run(run())


def test_feishu_image_payloads_caption_metadata_signature_and_reply(store,tmp_path):
    async def run():
        await enqueue_image(store,tmp_path)
        job=store.claim_image()
        assert store.finish_image(job,[''],image_key='img_one')==1
        delivery=store.claim()
        now=time.time()
        payload=payload_for(delivery,'secret',now)
        assert payload=={'msg_type':'image','content':{'image_key':'img_one'},
            'timestamp':str(int(now)),'sign':signature('secret',str(int(now)))}
        delivery['body']='这张图说明趋势'
        post=payload_for(delivery)['content']['post']['zh_cn']
        assert 'title' not in post
        assert post['content']==[[{'tag':'text','text':'这张图说明趋势'}],[{'tag':'img','image_key':'img_one'}]]
        delivery.update(reply_context='↳ 回复张三：看这张图',sender_name='李四',show_sender=1,show_link=1)
        post=payload_for(delivery)['content']['post']['zh_cn']
        text=str(post)
        assert '↳ 回复张三' in text and '李四：这张图说明趋势' in text
        assert text.count('李四')==1 and '发送人：' not in text
        assert 'image_key' in text and '在 Telegram 查看原消息' in text
    asyncio.run(run())


def test_image_chunked_caption_attaches_picture_once_and_preserves_all_text(store,tmp_path):
    async def run():
        caption='长配文'*4000
        await enqueue_image(store,tmp_path,message=picture(text=caption))
        job=store.claim_image()
        store.finish_image(job,split_text(caption),image_key='img_long')
        rows=store.all('SELECT * FROM deliveries ORDER BY id')
        assert ''.join(r['body'] for r in rows)==caption
        assert sum(bool(r['image_key']) for r in rows)==1
        assert all(len(json.dumps(payload_for(r),ensure_ascii=False).encode())<20000 for r in rows)
    asyncio.run(run())


def test_upload_token_reuse_multipart_and_clear_errors(tmp_path):
    async def run():
        captured=[]
        def respond(request):
            captured.append(request)
            if request.url.path.endswith('/internal'):
                assert json.loads(request.read())=={'app_id':'cli_test','app_secret':'private-app-secret'}
                return httpx.Response(200,json={'code':0,'tenant_access_token':'private-token','expire':7200})
            assert request.headers['Authorization']=='Bearer private-token'
            assert 'multipart/form-data' in request.headers['Content-Type']
            body=request.read()
            assert b'name="image_type"' in body and b'message' in body and b'\x89PNG' in body
            return httpx.Response(200,json={'code':0,'data':{'image_key':'img_uploaded'}})
        uploader=await mock_uploader(configured(tmp_path),respond)
        path=tmp_path/'test.png';path.write_bytes(png())
        try:
            assert await uploader.upload(path)=='img_uploaded'
            assert await uploader.upload(path)=='img_uploaded'
            assert sum(r.url.path.endswith('/internal') for r in captured)==1
            uploader.config.save(feishu_app_id='cli_changed',feishu_app_secret='changed')
            # Changing credentials must not reuse the old tenant token.
            await uploader.client.aclose()
            uploader.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'code':99991661,'msg':'private-secret-echo'})))
            with pytest.raises(ImageError) as exc:await uploader.upload(path)
            assert 'private-secret-echo' not in str(exc.value)
        finally:await uploader.close()
    asyncio.run(run())


@pytest.mark.parametrize('response,retryable',[
    (httpx.Response(200,json={'code':99991672,'msg':'private-app-secret'}),False),
    (httpx.Response(200,json={'code':99991400}),True),
    (httpx.Response(503,text='private-app-secret'),True),
    (httpx.Response(200,text='invalid-json'),True),
    (httpx.Response(200,json=['unexpected']),True),
])
def test_upload_errors_are_sanitized(tmp_path,response,retryable):
    async def run():
        uploader=await mock_uploader(configured(tmp_path),lambda request:response)
        path=tmp_path/'test.png';path.write_bytes(png())
        try:
            with pytest.raises(ImageError) as exc:await uploader.upload(path)
            assert exc.value.retryable==retryable
            assert 'private-app-secret' not in str(exc.value)
        finally:await uploader.close()
    asyncio.run(run())


def test_full_image_pipeline_cleans_files_caches_and_sends_once(store,tmp_path):
    async def run():
        task=await enqueue_image(store,tmp_path,message=picture(text='配文'))
        message=picture(text='配文')
        paths=[]
        async def download(message,*,file,progress_callback):
            paths.append(Path(file))
            Path(file).write_bytes(png());progress_callback(len(png()),len(png()))
            return file
        tg_client=SimpleNamespace(get_entity=AsyncMock(return_value=CHAT),get_messages=AsyncMock(return_value=message),
            download_media=AsyncMock(side_effect=download))
        tg=SimpleNamespace(status=lambda:{'authorized':True,'connected':True},require=AsyncMock(return_value=tg_client))
        uploader=await mock_uploader(configured(tmp_path))
        worker=ImageWorker(store,tg,uploader);worker.temp_root=tmp_path/'image-tmp';worker.cleanup()
        sender=Sender(store);await sender.client.aclose()
        payloads=[]
        def receive(request):
            payloads.append(json.loads(request.read()));return httpx.Response(200,json={'code':0})
        sender.client=httpx.AsyncClient(transport=httpx.MockTransport(receive))
        try:
            assert await worker.process_once()
            assert not any(p.exists() for p in paths)
            assert not list(worker.temp_root.iterdir())
            assert store.one('SELECT status FROM image_jobs')['status']=='done'
            assert await sender.process_once()
            assert not await sender.process_once()
            assert payloads[0]['content']['post']['zh_cn']['content'][1][0]['image_key']=='img_test'
            assert '配文' in str(payloads[0])
            # Same photo routed to another robot only needs one upload.
            second=make_route(store)
            await enqueue_image(store,tmp_path,second,message)
            assert await worker.process_once()
            assert len(paths)==1
        finally:await sender.close();await uploader.close()
    asyncio.run(run())


@pytest.mark.parametrize('stage',['download','upload'])
def test_failure_and_cancellation_remove_temporary_images(store,tmp_path,stage):
    async def run():
        await enqueue_image(store,tmp_path)
        paths=[]
        async def download(message,*,file,progress_callback):
            paths.append(Path(file));Path(file).write_bytes(png())
            if stage=='download':raise TimeoutError()
            return file
        tg_client=SimpleNamespace(get_entity=AsyncMock(return_value=CHAT),get_messages=AsyncMock(return_value=picture()),download_media=download)
        tg=SimpleNamespace(status=lambda:{'authorized':True,'connected':True},require=AsyncMock(return_value=tg_client))
        uploader=await mock_uploader(configured(tmp_path))
        uploader.upload=AsyncMock(side_effect=asyncio.CancelledError())
        worker=ImageWorker(store,tg,uploader);worker.temp_root=tmp_path/'image-tmp'
        try:
            if stage=='upload':
                with pytest.raises(asyncio.CancelledError):await worker.process_once()
            else:assert await worker.process_once()
            assert paths and not any(p.exists() for p in paths)
            assert not list(worker.temp_root.iterdir())
            assert store.one('SELECT status FROM image_jobs')['status']=='retry'
            assert not worker.busy
        finally:await uploader.close()
    asyncio.run(run())


def test_unconfigured_upload_preserves_caption_and_link_without_download(store,tmp_path):
    async def run():
        await enqueue_image(store,tmp_path,message=picture(text='有价值的配文'))
        tg=SimpleNamespace(status=lambda:{'authorized':True,'connected':True},require=AsyncMock())
        uploader=await mock_uploader(Config(tmp_path/'unconfigured.json'))
        worker=ImageWorker(store,tg,uploader)
        try:
            assert await worker.process_once()
            tg.require.assert_not_awaited()
            row=store.one('SELECT * FROM deliveries')
            assert '有价值的配文' in row['body'] and '尚未配置' in row['body'] and 'https://t.me/c/' in row['body']
            assert not row['image_key']
            assert store.one('SELECT status FROM image_jobs')['status']=='fallback'
        finally:await uploader.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure',['protected','changed','oversize','missing'])
def test_invalid_original_rejected_before_download(store,tmp_path,failure):
    async def run():
        await enqueue_image(store,tmp_path)
        message=picture(size=MAX_IMAGE_BYTES+1 if failure=='oversize' else 1000)
        if failure=='protected':message.noforwards=True
        if failure=='changed':message.media.photo.id=555
        tg_client=SimpleNamespace(get_entity=AsyncMock(return_value=CHAT),get_messages=AsyncMock(return_value=None if failure=='missing' else message),download_media=AsyncMock())
        uploader=await mock_uploader(configured(tmp_path))
        worker=ImageWorker(store,SimpleNamespace(require=AsyncMock(return_value=tg_client)),uploader)
        try:
            with pytest.raises(ImageError):await worker.upload_job(store.claim_image())
            tg_client.download_media.assert_not_awaited()
        finally:await uploader.close()
    asyncio.run(run())


def test_retry_exhaustion_is_explicit_and_secrets_not_logged(store,tmp_path):
    async def run():
        await enqueue_image(store,tmp_path)
        uploader=await mock_uploader(configured(tmp_path))
        worker=ImageWorker(store,SimpleNamespace(status=lambda:{'authorized':True,'connected':True}),uploader)
        worker.upload_job=AsyncMock(side_effect=RuntimeError('private-app-secret'))
        try:
            for i in range(3):
                assert await worker.process_once()
                store.execute('UPDATE image_jobs SET next_attempt=0')
            assert not await worker.process_once()
            assert '图片未转发' in store.one('SELECT body FROM deliveries')['body']
            assert 'private-app-secret' not in str(store.all('SELECT * FROM system_logs'))
        finally:await uploader.close()
    asyncio.run(run())


def test_restart_cleanup_and_job_recovery_are_safe_and_atomic(store,tmp_path):
    async def run():
        await enqueue_image(store,tmp_path)
        job=store.claim_image()
        uploader=await mock_uploader(configured(tmp_path))
        worker=ImageWorker(store,SimpleNamespace(),uploader);worker.temp_root=tmp_path/'image-tmp'
        stale=worker.temp_root/'image-interrupted';stale.mkdir(parents=True);(stale/'image.jpg').write_bytes(png())
        keep=worker.temp_root/'unrelated.txt';keep.write_text('keep')
        try:
            worker.cleanup()
            assert not stale.exists() and keep.exists()
            restarted=Store(store.path);restarted.mark_inflight_uncertain()
            assert restarted.one('SELECT status FROM image_jobs')['status']=='retry'
            new=restarted.claim_image()
            assert restarted.finish_image(new,[''],image_key='img_retry')==1
            assert restarted.finish_image(job,[''],image_key='img_duplicate')==0
            assert len(restarted.all('SELECT * FROM deliveries'))==1
        finally:await uploader.close()
    asyncio.run(run())


def test_paused_tasks_cancel_image_processing_and_offline_preserves_attempts(store,tmp_path):
    async def run():
        await enqueue_image(store,tmp_path)
        uploader=await mock_uploader(configured(tmp_path))
        tg=SimpleNamespace(status=lambda:{'authorized':False,'connected':False})
        worker=ImageWorker(store,tg,uploader)
        try:
            assert not await worker.process_once()
            assert store.one('SELECT attempts FROM image_jobs')['attempts']==0
            job=store.claim_image()
            store.execute('UPDATE tasks SET forward_images=0')
            assert store.finish_image(job,[''],image_key='img_no_send')==0
            assert not store.all('SELECT * FROM deliveries')
        finally:await uploader.close()
    asyncio.run(run())


def test_api_image_settings_preserve_secret_validate_and_never_return_secrets(client,store,monkeypatch):
    response=client.put('/api/image-settings',json={'app_id':'cli_test','app_secret':'private-app-secret'})
    assert response.status_code==200
    assert response.json()['configured'] and not response.json()['verified']
    assert 'private-app-secret' not in response.text
    assert 'private-app-secret' not in client.get('/api/state').text
    assert 'private-app-secret' not in client.get('/api/diagnostics').text
    assert client.put('/api/image-settings',json={'app_id':'cli_test'}).json()['secret_configured']
    assert client.put('/api/image-settings',json={'app_id':'cli_other'}).status_code==400
    assert client.put('/api/image-settings',json={'app_id':'wrong-id','app_secret':'x'}).status_code==422
    assert client.put('/api/image-settings',json={'clear':True}).json()['configured'] is False


def test_api_upload_test_and_robot_image_test_use_image_payload(client,store,monkeypatch,tmp_path):
    uploaded=[]
    async def upload(self,path):
        uploaded.append(Path(path));assert Path(path).read_bytes().startswith(b'\x89PNG')
        return 'img_api_test'
    monkeypatch.setattr(ImageUploader,'upload',upload)
    client.put('/api/image-settings',json={'app_id':'cli_test','app_secret':'private-app-secret'})
    result=client.post('/api/image-settings/test')
    assert result.status_code==200 and result.json()['verified']
    assert not uploaded[-1].exists()
    assert not store.all('SELECT * FROM deliveries')
    task=make_route(store)
    result=client.post(f"/api/robots/{task['robot_id']}/test-image")
    assert result.status_code==200
    row=store.one('SELECT * FROM deliveries')
    assert row['image_key']=='img_api_test' and row['kind']=='test'
    assert 'img_api_test' in str(payload_for(row))
    assert client.put('/api/image-settings',json={'app_id':'cli_new','app_secret':'new-secret'}).json()['verified'] is False


def test_task_image_api_defaults_and_omitted_setting_preserved(client,store):
    task=make_route(store)
    saved=client.post('/api/tasks',json={'name':'图片任务','chat_id':-100889977,'robot_id':task['robot_id']}).json()
    assert saved['forward_images']==1
    path=f"/api/tasks/{saved['id']}"
    assert client.put(path,json={'name':'关闭','forward_images':False}).json()['forward_images']==0
    assert client.put(path,json={'name':'保留'}).json()['forward_images']==0
    assert client.put(path,json={'name':'开启','forward_images':True}).json()['forward_images']==1


def test_image_schema_migration_preserves_old_tasks_and_pending_delivery(store,tmp_path):
    task=make_route(store)
    queue(store,task,text='保留待发送正文')
    with store.db() as db:
        import re
        # Rebuild old tables without relying on SQLite DROP COLUMN support.
        db.commit();db.execute('PRAGMA foreign_keys=OFF')
        for table,column,pattern in [('tasks','forward_images',r',\s*forward_images INTEGER NOT NULL DEFAULT 1'),
                                     ('deliveries','image_key',r",\s*image_key TEXT NOT NULL DEFAULT ''")]:
            schema=db.execute('SELECT sql FROM sqlite_master WHERE name=?',(table,)).fetchone()[0]
            schema=re.sub(pattern,'',schema)
            db.execute(schema.replace(f'CREATE TABLE {table}',f'CREATE TABLE {table}_legacy',1))
            columns=','.join(r[1] for r in db.execute(f'PRAGMA table_info({table})') if r[1]!=column)
            db.execute(f'INSERT INTO {table}_legacy ({columns}) SELECT {columns} FROM {table}')
            db.execute(f'DROP TABLE {table}');db.execute(f'ALTER TABLE {table}_legacy RENAME TO {table}')
        db.execute('DROP TABLE image_jobs');db.execute('DROP TABLE image_cache')
    migrated=Store(store.path)
    saved=migrated.one('SELECT * FROM tasks')
    assert saved['forward_images']==1 and saved['chat_id']==task['chat_id'] and saved['enabled']==task['enabled']
    job=migrated.claim()
    assert job['body']=='保留待发送正文' and job['image_key']==''


def test_upload_size_limit_never_sends_oversize_file(tmp_path):
    async def run():
        calls=[]
        uploader=await mock_uploader(configured(tmp_path),lambda request:calls.append(request))
        path=tmp_path/'oversize.png'
        with path.open('wb') as file:file.truncate(MAX_IMAGE_BYTES+1)
        try:
            with pytest.raises(ImageError,match='10MB'):await uploader.upload(path)
            assert not calls
        finally:await uploader.close()
    asyncio.run(run())
