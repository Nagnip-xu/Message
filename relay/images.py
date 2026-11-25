from __future__ import annotations

import asyncio
import contextlib
import shutil
import struct
import tempfile
import time
import zlib
from pathlib import Path

import httpx
from telethon import types

from .config import DATA
from .feishu import split_text

MAX_IMAGE_BYTES = 10 * 1024 * 1024


class ImageError(Exception):
    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def test_png():
    """Small visible test picture, generated locally without dependencies."""
    def chunk(kind, data):
        return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    rows = b''.join(b'\0'+b'\x08\x7d\x76'*160 for _ in range(100))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',160,100,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(rows))+chunk(b'IEND',b'')


class ImageUploader:
    def __init__(self, config):
        self.config = config
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(45,connect=8),trust_env=False)
        self.token_value, self.token_until, self.token_pair = "", 0, None
        self.token_lock = asyncio.Lock()
        self.test_root = DATA/'image-test-tmp'

    def configured(self):
        return bool(self.config.values.get("feishu_app_id") and self.config.values.get("feishu_app_secret"))

    def public(self):
        return {"configured":self.configured(),"app_id":self.config.values.get("feishu_app_id",""),
                "secret_configured":bool(self.config.values.get("feishu_app_secret")),
                "verified":bool(self.config.values.get("feishu_image_verified",False)) and self.configured()}

    async def token(self, pair):
        async with self.token_lock:
            if pair == self.token_pair and self.token_until > time.time():
                return self.token_value
            data = await self.request('/auth/v3/tenant_access_token/internal',json={'app_id':pair[0],'app_secret':pair[1]})
            token = data.get('tenant_access_token')
            if not isinstance(token,str) or not token:
                raise ImageError('飞书未返回访问凭证，请检查应用是否已启用')
            self.token_value, self.token_pair = token, pair
            self.token_until = time.time()+max(0,min(float(data.get('expire',7200))-120,7200))
            return token

    async def request(self, path, **kwargs):
        try:
            response = await self.client.post('https://open.feishu.cn/open-apis'+path,**kwargs)
        except httpx.HTTPError:
            raise ImageError('飞书图片上传网络异常，将重试',retryable=True) from None
        if response.status_code != 200:
            raise ImageError(f'飞书图片上传 HTTP {response.status_code}',retryable=response.status_code==429 or response.status_code>=500)
        try:
            data = response.json()
        except ValueError:
            raise ImageError('飞书图片接口返回无法解析的结果',retryable=True) from None
        if not isinstance(data,dict) or not isinstance(data.get('code'),int):
            raise ImageError('飞书图片接口返回未知结果',retryable=True)
        code = data.get('code')
        if code != 0:
            if code in {99991663,99991664,99991668}:
                self.token_until = 0
            known = {99991661:'应用凭证无效，请检查 App ID 和 App Secret',
                     99991672:'应用权限不足，请开通 im:resource 并发布应用',
                     234007:'请先开启自建应用的机器人能力并发布',
                     234001:'图片格式或大小不符合飞书要求'}
            reason = known.get(code,'请检查应用凭证、机器人能力、im:resource 权限及应用发布状态')
            raise ImageError(f'飞书图片接口错误 {code}：{reason}',retryable=code in {99991400,9499,99991663,99991664,99991668})
        return data

    async def upload(self, path):
        pair = (self.config.values.get('feishu_app_id',''),self.config.values.get('feishu_app_secret',''))
        if not all(pair):
            raise ImageError('尚未配置飞书图片上传应用')
        path = Path(path)
        if not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
            raise ImageError('图片为空或超过 10MB，未上传')
        with path.open('rb') as file:
            data = await self.request('/im/v1/images',headers={'Authorization':'Bearer '+await self.token(pair)},
                data={'image_type':'message'},files={'image':(path.name,file,'application/octet-stream')})
        key = data.get('data',{}).get('image_key')
        if not isinstance(key,str) or not key:
            raise ImageError('飞书未返回 image_key',retryable=True)
        return key

    async def test(self):
        pair = (self.config.values.get('feishu_app_id'),self.config.values.get('feishu_app_secret'))
        self.test_root.mkdir(mode=0o700,parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='image-test-',dir=self.test_root) as work:
            path = Path(work)/'test.png'
            path.write_bytes(test_png())
            key = await self.upload(path)
        if pair != (self.config.values.get('feishu_app_id'),self.config.values.get('feishu_app_secret')):
            raise ImageError('应用配置已改变，请重新测试')
        self.config.save(feishu_image_verified=True)
        return key

    async def close(self):
        await self.client.aclose()


class ImageWorker:
    def __init__(self, store, telegram, uploader):
        self.store,self.telegram,self.uploader = store,telegram,uploader
        self.busy = False
        self.temp_root = DATA/'image-tmp'

    def status(self):
        pending = self.store.one("SELECT COUNT(*) AS n FROM image_jobs WHERE status IN ('pending','processing','retry')")['n']
        return {**self.uploader.public(),"busy":self.busy,"pending":pending,"max_mb":10}

    def cleanup(self):
        # Only this worker owns this directory; run once before accepting work after restart.
        for directory in (self.temp_root,self.uploader.test_root):
            directory.mkdir(mode=0o700,parents=True,exist_ok=True)
            for path in directory.glob('image-*'):
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)

    async def upload_job(self,job):
        if not self.uploader.configured():
            raise ImageError('尚未配置飞书图片上传应用')
        client = await self.telegram.require()
        chat = await asyncio.wait_for(client.get_entity(job['chat_id']),timeout=20)
        message = await asyncio.wait_for(client.get_messages(chat,ids=job['message_id']),timeout=20)
        if not isinstance(message,types.Message) or getattr(chat,'noforwards',False) or message.noforwards:
            raise ImageError('原图片已删除、无法访问或开启了内容保护')
        photo = getattr(message.media,'photo',None)
        document = getattr(message.media,'document',None)
        media_key = 'photo:'+str(photo.id) if photo else 'document:'+str(document.id) if document else ''
        if media_key != job['media_key']:
            raise ImageError('原图片已变更，未上传旧版本')
        size = document.size if document else max((getattr(s,'size',0) or max(getattr(s,'sizes',[]) or [0]) for s in photo.sizes),default=0)
        if size > MAX_IMAGE_BYTES:
            raise ImageError('图片超过 10MB，未上传')
        app_id = self.uploader.config.values['feishu_app_id']
        cached = self.store.one('SELECT image_key FROM image_cache WHERE app_id=? AND media_key=? AND created_at>?',
            (app_id,media_key,time.time()-7*86400))
        if cached:
            return cached['image_key']
        self.temp_root.mkdir(mode=0o700,parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='image-',dir=self.temp_root) as work:
            ext = {'image/png':'.png','image/webp':'.webp','image/bmp':'.bmp'}.get(getattr(document,'mime_type',''),'.jpg')
            def progress(current,total):
                if current>MAX_IMAGE_BYTES or (total and total>MAX_IMAGE_BYTES):
                    raise ImageError('图片超过 10MB，未上传')
            path = await asyncio.wait_for(client.download_media(message,file=str(Path(work)/('image'+ext)),progress_callback=progress),timeout=120)
            if not path or not Path(path).is_file():
                raise ImageError('Telegram 图片下载未完成',retryable=True)
            key = await self.uploader.upload(path)
        self.store.execute('INSERT INTO image_cache VALUES(?,?,?,?) ON CONFLICT(app_id,media_key) DO UPDATE SET image_key=excluded.image_key,created_at=excluded.created_at',
            (app_id,media_key,key,time.time()))
        self.store.execute('DELETE FROM image_cache WHERE created_at<?',(time.time()-7*86400,))
        return key

    async def process_once(self):
        tg = self.telegram.status()
        if not tg.get('authorized') or not tg.get('connected'):
            return False
        job = self.store.claim_image()
        if not job:
            return False
        self.busy = True
        try:
            key = await self.upload_job(job)
            self.store.finish_image(job,split_text(job['caption']) if job['caption'] else [''],image_key=key)
        except asyncio.CancelledError:
            self.store.execute("UPDATE image_jobs SET status='retry',next_attempt=0 WHERE id=? AND status='processing'",(job['id'],))
            raise
        except Exception as exc:
            retryable = not isinstance(exc,ImageError) or exc.retryable
            reason = str(exc) if isinstance(exc,ImageError) else f'图片处理失败（{type(exc).__name__}）'
            if retryable and job['attempts'] < 3:
                self.store.execute("UPDATE image_jobs SET status='retry',next_attempt=?,error=? WHERE id=?",
                    (time.time()+min(60,5*2**job['attempts']),reason,job['id']))
            else:
                body = (job['caption']+'\n' if job['caption'] else '')+f'［图片未转发：{reason}］'
                if job['link']:
                    body += '\n'+job['link']
                self.store.finish_image(job,split_text(body),error=reason)
                self.store.log('图片未转发：'+reason,'warning')
        finally:
            self.busy = False
        return True

    async def run(self):
        self.cleanup()
        while True:
            try:
                if not await self.process_once():
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.log(f'图片处理队列异常（{type(exc).__name__}）','error')
                await asyncio.sleep(5)
