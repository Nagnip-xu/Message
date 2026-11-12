from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import datetime, timezone

from telethon import TelegramClient, events, errors, types, utils
from telethon.tl import functions

from .config import Config, DATA
from .feishu import split_text
from .store import Store


def topic_id_for(message, is_forum=False):
    header = getattr(message, "reply_to", None)
    if header and getattr(header, "forum_topic", False):
        return header.reply_to_top_id or header.reply_to_msg_id or 1
    return 1 if is_forum else 0


def matches_words(text, include_words, exclude_words):
    def words(value):
        return [word.strip().casefold() for word in re.split(r"[,，\n]", value) if word.strip()]
    folded = text.casefold()
    included, excluded = words(include_words), words(exclude_words)
    return (not included or any(word in folded for word in included)) and not any(word in folded for word in excluded)


def error_text(exc):
    if isinstance(exc, errors.FloodWaitError):
        return f"Telegram 要求等待 {exc.seconds} 秒，请稍后重试"
    known = {
        "PhoneNumberInvalidError": "手机号格式不正确，请包含国家区号，例如 +86…",
        "PhoneCodeInvalidError": "验证码不正确，请重新输入",
        "PhoneCodeExpiredError": "验证码已过期，请重新获取",
        "PasswordHashInvalidError": "两步验证密码不正确",
        "ApiIdInvalidError": "API ID 或 API Hash 不正确",
        "PhoneNumberBannedError": "该手机号被 Telegram 限制，请检查账号状态",
        "AuthKeyUnregisteredError": "登录会话已失效，请重新登录",
        "SessionRevokedError": "登录会话已撤销，请重新登录",
        "AuthKeyDuplicatedError": "此会话在其他位置重复使用，请退出后重新登录",
        "ChatAdminRequiredError": "没有读取此内容所需的权限",
        "ChannelPrivateError": "无法访问该群组或频道，请确认账号仍是成员",
        "TimeoutError": "连接 Telegram 超时，请检查网络或配置代理",
        "ConnectionError": "Telegram 连接失败，请检查网络或代理",
    }
    return known.get(type(exc).__name__, f"Telegram 请求失败（{type(exc).__name__}），请检查连接后重试")


class TelegramService:
    def __init__(self, store: Store, config: Config):
        self.store, self.config = store, config
        self.client = None
        self.authorized = False
        self.state = "connecting" if config.values["api_id"] else "needs_config"
        self.last_error = ""
        self.user = None
        self.phone = None
        self.phone_code_hash = None
        self.last_event_at = None
        self.last_recovery_at = None
        self.running = True
        self.connection_lock = asyncio.Lock()
        self.recovery_lock = asyncio.Lock()
        self.ingest_lock = asyncio.Lock()
        self.dialog_cache = None
        self.protected_warned = set()
        self.recovery_warnings = {}

    def status(self):
        connected = bool(self.client and self.client.is_connected())
        state = self.state
        if self.authorized:
            state = "ready" if connected else "reconnecting"
        return {"state": state, "connected": connected, "authorized": self.authorized,
                "user": self.user, "last_error": self.last_error, "last_event_at": self.last_event_at,
                "last_recovery_at": self.last_recovery_at, "recovery_warnings": list(self.recovery_warnings.values())}

    async def get_client(self):
        async with self.connection_lock:
            if not self.config.values["api_id"] or not self.config.values["api_hash"]:
                self.state = "needs_config"
                raise ValueError("请先保存 Telegram API ID 和 API Hash")
            if self.client is None:
                self.client = TelegramClient(DATA / "telegram", int(self.config.values["api_id"]),
                    self.config.values["api_hash"], proxy=self.config.telegram_proxy(),
                    device_model="Message Relay", app_version="1.1.0", system_lang_code="zh-CN",
                    connection_retries=1, request_retries=1, timeout=8,
                    flood_sleep_threshold=0, auto_reconnect=True, sequential_updates=True)
                self.client.add_event_handler(self.on_new, events.NewMessage())
                self.client.add_event_handler(self.on_edit, events.MessageEdited())
            if not self.client.is_connected():
                self.state = "connecting"
                await asyncio.wait_for(self.client.connect(), timeout=15)
            return self.client

    async def require(self):
        client = await self.get_client()
        if not self.authorized:
            raise ValueError("请先登录 Telegram 账号")
        return client

    async def reset(self):
        async with self.connection_lock:
            if self.client:
                await self.client.disconnect()
                self.client = None
            self.authorized, self.user, self.dialog_cache = False, None, None
            self.phone, self.phone_code_hash = None, None
            self.state, self.last_error = "connecting", ""

    async def send_code(self, phone):
        if self.authorized:
            raise ValueError("当前账号已登录；切换账号请先退出")
        if not re.fullmatch(r"\+[1-9]\d{6,14}", phone):
            raise ValueError("请输入带国家区号的手机号，例如 +8613800000000")
        client = await self.get_client()
        result = await client.send_code_request(phone)
        self.phone, self.phone_code_hash = phone, result.phone_code_hash
        self.state, self.last_error = "code_sent", ""
        return {"state": "code_sent", "message": "验证码已请求，请检查 Telegram 客户端或短信。验证码发送渠道由 Telegram 决定。"}

    async def verify(self, code="", password=""):
        client = await self.get_client()
        try:
            if password:
                await client.sign_in(password=password)
            else:
                if not self.phone_code_hash or not self.phone:
                    raise ValueError("请先获取验证码")
                await client.sign_in(phone=self.phone, code=code, phone_code_hash=self.phone_code_hash)
        except errors.SessionPasswordNeededError:
            self.state = "password_required"
            return {"state": self.state}
        await self.mark_authorized()
        return {"state": "ready"}

    async def mark_authorized(self):
        me = await self.client.get_me()
        self.authorized, self.state, self.last_error = True, "ready", ""
        self.user = {"id": me.id, "name": " ".join(filter(None, [me.first_name, me.last_name])), "username": me.username}
        self.phone, self.phone_code_hash = None, None
        self.store.log("Telegram 账号已连接")

    async def logout(self):
        if self.client:
            await self.client.log_out()
            self.client = None
        # Old peer IDs must not silently become routes for another account.
        self.store.execute("UPDATE tasks SET enabled=0")
        self.authorized, self.user, self.dialog_cache = False, None, None
        self.state, self.last_error = "login_required", ""
        self.store.log("Telegram 已退出，所有监听任务已暂停")

    async def dialogs(self, refresh=False):
        client = await self.require()
        if self.dialog_cache and not refresh and time.time() - self.dialog_cache[0] < 60:
            return self.dialog_cache[1]
        result = []
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if not isinstance(entity, (types.Channel, types.Chat)):
                continue
            if getattr(entity, "left", False) or getattr(entity, "deactivated", False):
                continue
            result.append({"id": dialog.id, "title": dialog.name, "forum": bool(getattr(entity, "forum", False)),
                           "kind": "channel" if getattr(entity, "broadcast", False) else "group",
                           "protected": bool(getattr(entity, "noforwards", False)), "username": getattr(entity, "username", None)})
        result.sort(key=lambda d: (not d["forum"], d["title"].casefold()))
        self.dialog_cache = (time.time(), result)
        return result

    async def topics(self, chat_id, query=""):
        client = await self.require()
        entity = await client.get_entity(chat_id)
        if not getattr(entity, "forum", False):
            return []
        output, offset_id, offset_topic, offset_date = {}, 0, 0, None
        while True:
            result = await client(functions.messages.GetForumTopicsRequest(
                peer=entity, offset_date=offset_date, offset_id=offset_id, offset_topic=offset_topic, limit=100, q=query or None))
            valid = [t for t in result.topics if isinstance(t, types.ForumTopic)]
            added = [t for t in valid if t.id not in output]
            for topic in added:
                output[topic.id] = {"id": topic.id, "title": topic.title, "closed": bool(topic.closed)}
            if not added or len(result.topics) < 100 or len(output) >= result.count:
                break
            last = valid[-1]
            offset_id, offset_topic, offset_date = last.top_message, last.id, last.date
        return list(output.values())

    async def resolve_source(self, chat_id, topic_id):
        client = await self.require()
        entity = await client.get_entity(chat_id)
        if not isinstance(entity, (types.Channel, types.Chat)):
            raise ValueError("仅支持群组和频道")
        if getattr(entity, "noforwards", False):
            raise ValueError("该来源开启了内容保护，本程序不转发受保护内容")
        topic_title = ""
        if topic_id:
            topics = await self.topics(chat_id)
            found = next((t for t in topics if t["id"] == topic_id), None)
            if not found:
                raise ValueError("话题不存在或无法读取，请刷新后重新选择")
            topic_title = found["title"]
        latest = await client.get_messages(entity, limit=1)
        cursor = latest[0].id if latest else 0
        return {"chat_title": entity.title, "topic_title": topic_title, "cursor": cursor}

    async def on_new(self, event):
        await self.on_event(event, False)

    async def on_edit(self, event):
        await self.on_event(event, True)

    async def on_event(self, event, edited):
        if not self.authorized:
            return
        try:
            tasks = self.store.all("SELECT * FROM tasks WHERE enabled=1 AND chat_id=?", (event.chat_id,))
            if not tasks:
                return
            self.last_event_at = time.time()
            chat = await event.get_chat()
            async with self.ingest_lock:
                for task in tasks:
                    await self.ingest(task, event.message, chat, edited=edited)
        except Exception as exc:
            self.store.log(f"处理 Telegram 更新失败：{error_text(exc)}", "error")

    async def ingest(self, task, message, chat, *, edited=False, recovered=False):
        if not isinstance(message, types.Message) or not message.date:
            return 0
        if getattr(chat, "noforwards", False) or getattr(message, "noforwards", False):
            if task["chat_id"] not in self.protected_warned:
                self.protected_warned.add(task["chat_id"])
                self.store.log(f"{task['chat_title']} 开启了内容保护，已跳过转发", "warning")
            return 0
        if task["topic_id"] and topic_id_for(message, bool(getattr(chat, "forum", False))) != task["topic_id"]:
            return 0
        original_at = message.date.timestamp()
        edit_at = message.edit_date.timestamp() if message.edit_date else 0
        event_at = edit_at if edited else original_at
        if event_at < task["enabled_since"] or (edited and not task["edits"]):
            return 0
        body = message.message or ""
        media = getattr(message, "media", None)
        if media and not isinstance(media, types.MessageMediaWebPage):
            label = "图片" if isinstance(media, types.MessageMediaPhoto) else "附件"
            body += f"\n\n［含{label}，原文件未转发］"
        if not body.strip() or not matches_words(body, task["include_words"], task["exclude_words"]):
            return 0
        sender_name = (getattr(message, "post_author", None) or "").strip()
        if not sender_name:
            try:
                sender = await message.get_sender()
            except Exception:
                sender = None
            if sender:
                sender_name = " ".join(filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]))
                sender_name = sender_name or getattr(sender, "title", "") or getattr(sender, "username", "")
        if not sender_name and getattr(message, "post", False):
            sender_name = "频道发布"
        media_id = getattr(getattr(media, "photo", None), "id", None) or getattr(getattr(media, "document", None), "id", None)
        version = hashlib.sha256(f"{body}\0{media_id}".encode()).hexdigest()
        username = getattr(chat, "username", None)
        peer_id = utils.get_peer_id(message.peer_id)
        if username:
            link = f"https://t.me/{username}/{message.id}"
        elif str(peer_id).startswith("-100"):
            link = f"https://t.me/c/{str(peer_id)[4:]}/{message.id}"
        else:
            link = ""
        source = task["chat_title"] + (f" / {task['topic_title']}" if task["topic_id"] else "")
        return self.store.enqueue(task, message.id, version, split_text(body),
            kind="edit" if edited else ("catchup" if recovered else "new"), source=source, sender_name=sender_name, link=link,
            original_at=original_at, event_at=event_at, received_at=time.time())

    async def recover(self):
        if self.recovery_lock.locked():
            return
        async with self.recovery_lock:
            client = await self.require()
            tasks = self.store.all("SELECT * FROM tasks WHERE enabled=1 AND catch_up=1")
            grouped = {}
            for task in tasks:
                grouped.setdefault(task["chat_id"], []).append(task)
            for chat_id, chat_tasks in grouped.items():
                try:
                    scan_started = time.time()
                    chat = await client.get_entity(chat_id)
                    limit = self.config.values["catchup_limit"]
                    messages = [m async for m in client.iter_messages(chat, limit=limit)]
                    if not messages:
                        continue
                    min_cursor = min(t["cursor"] for t in chat_tasks)
                    if len(messages) >= limit and messages[-1].id > min_cursor and min_cursor:
                        warning = f"{chat_tasks[0]['chat_title']} 的恢复窗口超过 {limit} 条，可能存在未恢复消息；请检查原群或提高补发扫描上限"
                        if self.recovery_warnings.get(chat_id) != warning:
                            self.store.log(warning, "warning")
                        self.recovery_warnings[chat_id] = warning
                    else:
                        self.recovery_warnings.pop(chat_id, None)
                    async with self.ingest_lock:
                        for task in chat_tasks:
                            for message in reversed(messages):
                                if not isinstance(message, types.Message):
                                    continue
                                if message.id > task["cursor"]:
                                    await self.ingest(task, message, chat, recovered=True)
                                elif message.edit_date and message.edit_date.timestamp() >= task["last_scan"] - 3:
                                    await self.ingest(task, message, chat, edited=True, recovered=True)
                            self.store.execute("UPDATE tasks SET cursor=MAX(cursor,?),last_scan=? WHERE id=?", (messages[0].id, scan_started, task["id"]))
                except Exception as exc:
                    self.store.log(f"{chat_tasks[0]['chat_title']} 补发检查失败：{error_text(exc)}", "warning")
            self.last_recovery_at = time.time()

    async def run(self):
        while self.running:
            try:
                if not self.config.values["api_id"]:
                    self.state = "needs_config"
                    await asyncio.sleep(3)
                    continue
                client = await self.get_client()
                if not self.authorized:
                    if await client.is_user_authorized():
                        await self.mark_authorized()
                    elif self.state not in {"code_sent", "password_required"}:
                        self.state, self.last_error = "login_required", ""
                if self.authorized:
                    self.last_error = ""
                    if self.last_recovery_at is None or time.time() - self.last_recovery_at > 60:
                        await self.recover()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = error_text(exc)
                if message != self.last_error:
                    self.store.log(message, "warning")
                self.last_error, self.state = message, "offline"
                if isinstance(exc, (errors.AuthKeyUnregisteredError, errors.SessionRevokedError)):
                    self.authorized = False
                if isinstance(exc, errors.FloodWaitError):
                    await asyncio.sleep(min(exc.seconds, 300))
            await asyncio.sleep(5)

    async def close(self):
        self.running = False
        if self.client:
            await self.client.disconnect()
