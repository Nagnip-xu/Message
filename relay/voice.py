from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from telethon import types

from .config import DATA, ROOT
from .feishu import split_text
from .telegram import matches_words

MAX_SECONDS = 600
MAX_BYTES = 25 * 1024 * 1024


class VoiceUnavailable(Exception):
    pass


class VoiceRejected(Exception):
    pass


class VoiceWorker:
    def __init__(self, store, telegram):
        self.store, self.telegram = store, telegram
        self.python = os.environ.get("MESSAGE_ASR_PYTHON", "")
        self.model = os.environ.get("MESSAGE_ASR_MODEL", "")
        self.tokens = os.environ.get("MESSAGE_ASR_TOKENS", "")
        self.ffmpeg = os.environ.get("MESSAGE_FFMPEG", "ffmpeg")
        self.busy = False

    def status(self):
        ready = all(value and Path(value).is_file() for value in (self.python, self.model, self.tokens)) and bool(shutil.which(self.ffmpeg))
        pending = self.store.one("SELECT COUNT(*) AS n FROM voice_jobs WHERE status IN ('pending','processing','retry')")["n"]
        return {"ready": ready, "busy": self.busy, "pending": pending, "max_seconds": MAX_SECONDS,
                "engine": "SenseVoice · CPU"}

    def cleanup(self):
        directory = DATA / "voice-tmp"
        directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        # Interrupted recognition cannot retain audio indefinitely after a restart.
        for path in directory.glob("voice-*"):
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)

    async def command(self, *args, timeout):
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env={**os.environ,"OMP_NUM_THREADS":"1","OPENBLAS_NUM_THREADS":"1","MKL_NUM_THREADS":"1"})
        try:
            out, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.communicate()
            raise
        if process.returncode:
            raise RuntimeError("voice subprocess failed")
        return out

    async def transcribe(self, audio, directory):
        wav = Path(directory) / "audio.wav"
        await self.command(self.ffmpeg,"-nostdin","-v","error","-y","-i",str(audio),
            "-t",str(MAX_SECONDS + 1),"-ac","1","-ar","16000","-c:a","pcm_s16le",str(wav),timeout=45)
        import wave
        with wave.open(str(wav),"rb") as stream:
            if stream.getnframes() / stream.getframerate() > MAX_SECONDS:
                raise VoiceRejected("语音超过 10 分钟，未转写")
        raw = await self.command(self.python,str(ROOT / "relay/asr_cli.py"),str(wav),
            "--model",self.model,"--tokens",self.tokens,timeout=600)
        text = json.loads(raw.decode("utf-8"))["text"].strip()
        if not text:
            raise VoiceRejected("未识别到清晰语音")
        return text

    async def recognize_job(self, job):
        client = await self.telegram.require()
        chat = await asyncio.wait_for(client.get_entity(job["chat_id"]), timeout=20)
        message = await asyncio.wait_for(client.get_messages(chat,ids=job["message_id"]),timeout=20)
        if not isinstance(message,types.Message) or getattr(chat,"noforwards",False) or getattr(message,"noforwards",False):
            raise VoiceRejected("原语音已删除、无法访问或开启了内容保护")
        document = getattr(getattr(message,"media",None),"document",None)
        if not document or str(document.id) != job["media_key"]:
            raise VoiceRejected("原语音已变更，未转写旧版本")
        if job["duration"] > MAX_SECONDS or document.size > MAX_BYTES:
            raise VoiceRejected("语音超过 10 分钟或 25MB，未转写")
        cached = self.store.one("SELECT text FROM voice_cache WHERE media_key=?", (job["media_key"],))
        if cached:
            return cached["text"]
        if not self.status()["ready"]:
            raise VoiceUnavailable()
        directory = DATA / "voice-tmp"
        directory.mkdir(mode=0o700,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="voice-", dir=directory) as work:
            def progress(current, total):
                if current > MAX_BYTES or (total and total > MAX_BYTES):
                    raise VoiceRejected("语音超过 25MB，未转写")
            path = await asyncio.wait_for(client.download_media(message,file=str(Path(work)/"source.ogg"),progress_callback=progress),timeout=180)
            if not path or not Path(path).is_file():
                raise RuntimeError("voice download incomplete")
            if Path(path).stat().st_size > MAX_BYTES:
                raise VoiceRejected("语音超过 25MB，未转写")
            text = await self.transcribe(path,work)
        self.store.execute("INSERT INTO voice_cache VALUES(?,?,?) ON CONFLICT(media_key) DO UPDATE SET text=excluded.text,created_at=excluded.created_at",
                           (job["media_key"],text,time.time()))
        self.store.execute("DELETE FROM voice_cache WHERE created_at<?", (time.time()-7*86400,))
        return text

    async def process_once(self):
        # Wait for reconnect instead of exhausting download retries while offline.
        tg = self.telegram.status()
        if not tg.get("authorized") or not tg.get("connected"):
            return False
        job = self.store.claim_voice()
        if not job:
            return False
        self.busy = True
        try:
            text = await self.recognize_job(job)
            current = self.store.one("SELECT include_words,exclude_words FROM tasks WHERE id=?", (job["task_id"],))
            filtered = bool(current and not matches_words(job["caption"] + "\n" + text,current["include_words"],current["exclude_words"]))
            body = f"语音转文字（{job['duration']}秒）：{text}"
            if job["caption"]:
                body = job["caption"] + "\n" + body
            self.store.finish_voice(job, split_text(body), filtered=filtered)
        except asyncio.CancelledError:
            self.store.execute("UPDATE voice_jobs SET status='retry',next_attempt=0 WHERE id=? AND status='processing'", (job["id"],))
            raise
        except Exception as exc:
            permanent = isinstance(exc,(VoiceRejected,VoiceUnavailable))
            if not permanent and job["attempts"] < 3:
                self.store.execute("UPDATE voice_jobs SET status='retry',next_attempt=?,error=? WHERE id=?",
                    (time.time()+min(60,5*2**job["attempts"]),f"语音处理失败（{type(exc).__name__}），等待重试",job["id"]))
            else:
                reason = str(exc) if isinstance(exc,VoiceRejected) else "转写服务未配置" if isinstance(exc,VoiceUnavailable) else "语音转写失败"
                body = f"收到一条 {job['duration']} 秒的语音（{reason}，请到 Telegram 收听）。"
                # Failure links remain accessible even with compact message metadata disabled.
                if job["link"]:
                    body += "\n" + job["link"]
                self.store.finish_voice(job,split_text(body))
                self.store.execute("UPDATE voice_jobs SET error=? WHERE id=?", (reason,job["id"]))
                self.store.log(f"语音转写未完成：{reason}","warning")
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
                self.store.log(f"语音转写队列异常（{type(exc).__name__}）","error")
                await asyncio.sleep(5)
