from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import time
from datetime import datetime, timezone

from telethon import TelegramClient, events, errors, types, utils
from telethon.tl import functions

from .config import Config, DATA
from .feishu import split_text
from .store import Store

HEALTH_TIMEOUT = 15
HISTORY_TIMEOUT = 45
HISTORY_PAGE_WAIT = 3


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
        self.last_health_at = None
        self.running = True
        self.connection_lock = asyncio.Lock()
        self.recovery_lock = asyncio.Lock()
        self.ingest_lock = asyncio.Lock()
        self.dialog_cache = None
        self.protected_warned = set()
        self.recovery_warnings = {}
        self.recovery_backlog = False
        self.reply_cache = {}

    def status(self):
        transport_connected = bool(self.client and self.client.is_connected())
        connected = bool(transport_connected and self.last_health_at and time.time() - self.last_health_at < 90)
        state = self.state
        if self.authorized:
            state = "ready" if connected and state == "ready" else "reconnecting"
        return {"state": state, "connected": connected, "authorized": self.authorized,
                "user": self.user, "last_error": self.last_error, "last_event_at": self.last_event_at,
                "last_recovery_at": self.last_recovery_at, "last_health_at": self.last_health_at,
                "recovery_warnings": list(self.recovery_warnings.values())}

    async def get_client(self):
        async with self.connection_lock:
            if not self.config.values["api_id"] or not self.config.values["api_hash"]:
                self.state = "needs_config"
                raise ValueError("请先保存 Telegram API ID 和 API Hash")
            if self.client is None:
                self.client = TelegramClient(DATA / "telegram", int(self.config.values["api_id"]),
                    self.config.values["api_hash"], proxy=self.config.telegram_proxy(),
                    device_model="Message Relay", app_version="1.1.3", system_lang_code="zh-CN",
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
                await asyncio.wait_for(self.client.disconnect(), timeout=10)
                self.client = None
            self.authorized, self.user, self.dialog_cache = False, None, None
            self.phone, self.phone_code_hash = None, None
            self.state, self.last_error = "connecting", ""
            self.last_health_at, self.last_recovery_at = None, None

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
        me = await asyncio.wait_for(self.client.get_me(), timeout=20)
        self.authorized, self.state, self.last_error = True, "ready", ""
        self.user = {"id": me.id, "name": " ".join(filter(None, [me.first_name, me.last_name])), "username": me.username}
        self.phone, self.phone_code_hash = None, None
        self.last_health_at = time.time()
        self.store.log("Telegram 账号已连接")

    async def probe(self, client):
        # is_connected() only describes the socket; an RPC verifies the actual route.
        await asyncio.wait_for(client(functions.PingRequest(ping_id=int(time.time() * 1000))), timeout=HEALTH_TIMEOUT)
        self.last_health_at = time.time()
        self.state, self.last_error = "ready", ""

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

    async def resolve_senders(self, value):
        entries = list(dict.fromkeys(part.strip() for part in re.split(r"[,，\s]+", value) if part.strip()))
        if len(entries) > 20:
            raise ValueError("每个任务最多指定 20 个发送人")
        if not entries:
            return []
        client = await self.require()
        resolved = {}
        for entry in entries:
            if re.fullmatch(r"[1-9]\d{0,18}", entry):
                target = int(entry)
            elif re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,31}", entry):
                target = entry
            else:
                raise ValueError("发送人请填写 @用户名 或正整数用户 ID，多个用逗号分隔；不支持昵称或手机号")
            try:
                user = await asyncio.wait_for(client.get_entity(target), timeout=20)
            except (ValueError, errors.UsernameInvalidError, errors.UsernameNotOccupiedError):
                raise ValueError(f"无法识别发送人 {entry}，请检查 @用户名，或先刷新最近发言人再选择用户 ID") from None
            if not isinstance(user, types.User) or getattr(user, "deleted", False):
                raise ValueError(f"{entry} 不是可用的 Telegram 用户；不能指定群组或频道")
            name = utils.get_display_name(user) or user.username or str(user.id)
            resolved[user.id] = {"id": user.id, "name": name, "username": user.username}
        return list(resolved.values())

    async def recent_senders(self, chat_id, topic_id=0):
        client = await self.require()
        async def read():
            chat = await client.get_entity(chat_id)
            if not isinstance(chat, (types.Chat, types.Channel)):
                raise ValueError("仅支持群组和频道")
            messages = [m async for m in client.iter_messages(chat, limit=100)]
            users = {}
            for message in messages:
                if not isinstance(message, types.Message) or not isinstance(message.from_id, types.PeerUser):
                    continue
                if topic_id and topic_id_for(message, bool(getattr(chat, "forum", False))) != topic_id:
                    continue
                if message.from_id.user_id in users:
                    continue
                user = message.sender
                if not isinstance(user, types.User):
                    continue
                users[user.id] = {"id": user.id, "name": utils.get_display_name(user) or str(user.id), "username": user.username}
            return list(users.values())
        return await asyncio.wait_for(read(), timeout=30)

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
            chat = await asyncio.wait_for(event.get_chat(), timeout=20)
            async with self.ingest_lock:
                for task in tasks:
                    await self.ingest(task, event.message, chat, edited=edited)
        except Exception as exc:
            self.store.log(f"处理 Telegram 更新失败：{error_text(exc)}", "error")

    async def reply_context_for(self, message, chat):
        header = getattr(message, "reply_to", None)
        if not isinstance(header, types.MessageReplyHeader) or not header.reply_to_msg_id:
            return ""
        # Forum membership is encoded as a reply to the topic root, not a quoted message.
        if header.forum_topic and header.reply_to_msg_id == (header.reply_to_top_id or header.reply_to_msg_id):
            return ""
        key = (utils.get_peer_id(header.reply_to_peer_id or message.peer_id), header.reply_to_msg_id)
        cached = self.reply_cache.get(key)
        if cached and time.time() - cached[0] < 60:
            return cached[1]
        unavailable = "↳ 回复：原消息不可读取"
        try:
            original = await asyncio.wait_for(message.get_reply_message(), timeout=3)
            if not isinstance(original, types.Message):
                return unavailable
            if original.noforwards:
                return "↳ 回复：原消息开启内容保护，无法引用"
            if utils.get_peer_id(original.peer_id) != utils.get_peer_id(message.peer_id):
                origin_chat = await asyncio.wait_for(original.get_chat(), timeout=2)
                if origin_chat is None:
                    return unavailable
                if getattr(origin_chat, "noforwards", False):
                    return "↳ 回复：原消息开启内容保护，无法引用"
            name = (original.post_author or "").strip()
            if not name:
                try:
                    sender = await asyncio.wait_for(original.get_sender(), timeout=2)
                    name = utils.get_display_name(sender) if sender else ""
                except Exception:
                    name = ""
            name = " ".join((name or ("频道发布" if original.post else "匿名用户")).split())[:60]
            text = " ".join((original.message or "").split())
            if not text:
                text = "［图片］" if original.photo else "［语音］" if original.voice else "［贴纸］" if original.sticker else "［附件或无文字消息］"
            if len(text) > 200:
                text = text[:200] + "…"
            result = f"↳ 回复{name}：{text}"
            if len(self.reply_cache) >= 256:
                self.reply_cache.pop(next(iter(self.reply_cache)))
            self.reply_cache[key] = (time.time(), result)
            return result
        except Exception:
            # Missing access, deletion or a slow lookup must never drop the current reply.
            return unavailable

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
        sender_ids = task.get("sender_ids", "")
        if sender_ids:
            # Match the actual sending account, never display names or forwarded authors.
            if not isinstance(message.from_id, types.PeerUser) or str(message.from_id.user_id) not in sender_ids.split(","):
                return 0
        original_at = message.date.timestamp()
        edit_at = message.edit_date.timestamp() if message.edit_date else 0
        event_at = edit_at if edited else original_at
        if event_at < task["enabled_since"] or (edited and not task["edits"]):
            return 0
        body = message.message or ""
        media = getattr(message, "media", None)
        document = getattr(media, "document", None)
        attributes = getattr(document, "attributes", [])
        decorative = any(isinstance(a, (types.DocumentAttributeSticker, types.DocumentAttributeAnimated))
                         for a in attributes) or getattr(document, "mime_type", "") == "application/x-tgsticker"
        if decorative and not body.strip():
            # Static / animated / video stickers and captionless GIFs carry no signal text.
            # Skip before sender lookup so they also add no attachment placeholders.
            return 0
        voice = next((a for a in getattr(document, "attributes", [])
                      if isinstance(a, types.DocumentAttributeAudio) and a.voice), None)
        transcribe = voice is not None and task.get("transcribe_voice", False)
        photo = getattr(media, "photo", None)
        image = bool(photo or (not decorative and getattr(document,"mime_type","") in
                     {"image/jpeg","image/png","image/webp","image/bmp"})) and task.get("forward_images", True)
        if media and not isinstance(media, types.MessageMediaWebPage) and not transcribe and not decorative and not image:
            label = "图片" if isinstance(media, types.MessageMediaPhoto) else "附件"
            body += f"\n\n［含{label}，原文件未转发］"
        # Voice keywords must be evaluated on the transcription, not the attachment placeholder.
        if not transcribe and ((not body.strip() and not image) or not matches_words(body, task["include_words"], task["exclude_words"])):
            return 0
        sender_name = (getattr(message, "post_author", None) or "").strip()
        if not sender_name:
            try:
                sender = await asyncio.wait_for(message.get_sender(), timeout=5)
            except Exception:
                sender = None
            if sender:
                sender_name = " ".join(filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]))
                sender_name = sender_name or getattr(sender, "title", "") or getattr(sender, "username", "")
        if not sender_name and getattr(message, "post", False):
            sender_name = "频道发布"
        media_id = getattr(getattr(media, "photo", None), "id", None) or getattr(getattr(media, "document", None), "id", None)
        version = hashlib.sha256(f"{body}\0{media_id}".encode()).hexdigest()
        if self.store.one("SELECT 1 FROM seen WHERE task_id=? AND message_id=? AND version=?", (task["id"],message.id,version)):
            return 0
        reply_context = await self.reply_context_for(message, chat)
        username = getattr(chat, "username", None)
        peer_id = utils.get_peer_id(message.peer_id)
        if username:
            link = f"https://t.me/{username}/{message.id}"
        elif str(peer_id).startswith("-100"):
            link = f"https://t.me/c/{str(peer_id)[4:]}/{message.id}"
        else:
            link = ""
        source = task["chat_title"] + (f" / {task['topic_title']}" if task["topic_id"] else "")
        if image:
            return self.store.enqueue_image(task,message.id,version,media_key=("photo:" if photo else "document:")+str(media_id),
                source=source,sender_name=sender_name,link=link,caption=body,reply_context=reply_context,
                kind="edit" if edited else ("catchup" if recovered else "new"),
                original_at=original_at,event_at=event_at,received_at=time.time())
        if transcribe:
            return self.store.enqueue_voice(task, message.id, version, media_key=str(document.id),
                source=source, sender_name=sender_name, link=link, caption=body,
                duration=int(voice.duration or 0), byte_size=document.size or 0,
                kind="edit" if edited else ("catchup" if recovered else "new"),
                original_at=original_at,event_at=event_at,received_at=time.time(),reply_context=reply_context)
        return self.store.enqueue(task, message.id, version, split_text(body),
            kind="edit" if edited else ("catchup" if recovered else "new"), source=source, sender_name=sender_name, link=link,
            original_at=original_at, event_at=event_at, received_at=time.time(),reply_context=reply_context)

    async def recover(self):
        if self.recovery_lock.locked():
            return
        async with self.recovery_lock:
            self.recovery_backlog = False
            client = await self.require()
            tasks = self.store.all("SELECT * FROM tasks WHERE enabled=1 AND catch_up=1")
            grouped = {}
            for task in tasks:
                grouped.setdefault(task["chat_id"], []).append(task)
            for chat_id, chat_tasks in grouped.items():
                retry_key = f"telegram-history-retry:{chat_id}"
                retry = self.store.one("SELECT value FROM runtime_state WHERE key=?", (retry_key,))
                if retry and retry["value"] > time.time():
                    continue
                try:
                    scan_started = time.time()
                    chat = await asyncio.wait_for(client.get_entity(chat_id), timeout=20)
                    limit = self.config.values["catchup_limit"]
                    min_cursor = min(t["cursor"] for t in chat_tasks)
                    history_timeout = HISTORY_TIMEOUT + (limit // 100) * HISTORY_PAGE_WAIT
                    async def read_new():
                        result = []
                        async for m in client.iter_messages(chat, min_id=min_cursor, reverse=True, limit=limit, wait_time=HISTORY_PAGE_WAIT):
                            self.last_health_at = time.time()
                            result.append(m)
                        return result
                    messages = await asyncio.wait_for(read_new(), timeout=history_timeout)
                    self.last_health_at = time.time()
                    if len(messages) >= limit:
                        self.recovery_backlog = True
                        warning = f"{chat_tasks[0]['chat_title']} 存在补发积压，正在按顺序分批恢复（每批最多 {limit} 条）"
                        if self.recovery_warnings.get(chat_id) != warning:
                            self.store.log(warning, "warning")
                        self.recovery_warnings[chat_id] = warning
                    else:
                        self.recovery_warnings.pop(chat_id, None)
                    # Old messages may have been edited without producing a new ID.
                    recent = []
                    if any(t["edits"] for t in chat_tasks):
                        async def read_recent():
                            result = []
                            async for m in client.iter_messages(chat, limit=limit, wait_time=HISTORY_PAGE_WAIT):
                                self.last_health_at = time.time()
                                result.append(m)
                            return result
                        recent = await asyncio.wait_for(read_recent(), timeout=history_timeout)
                        self.last_health_at = time.time()
                    async with self.ingest_lock:
                        for task in chat_tasks:
                            for message in messages:
                                if not isinstance(message, types.Message):
                                    continue
                                if message.id > task["cursor"]:
                                    await self.ingest(task, message, chat, recovered=True)
                            for message in reversed(recent):
                                if isinstance(message, types.Message) and message.id <= task["cursor"] and message.edit_date and message.edit_date.timestamp() >= task["last_scan"] - 3:
                                    await self.ingest(task, message, chat, edited=True, recovered=True)
                            cursor = max((m.id for m in messages), default=task["cursor"])
                            self.store.execute("UPDATE tasks SET cursor=MAX(cursor,?),last_scan=? WHERE id=?", (cursor, scan_started, task["id"]))
                    self.store.execute("DELETE FROM runtime_state WHERE key=?", (retry_key,))
                except Exception as exc:
                    if isinstance(exc, errors.FloodWaitError):
                        # Persist the server's cooldown across restarts; leave real-time updates active.
                        self.store.execute("INSERT INTO runtime_state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                           (retry_key, time.time() + exc.seconds + 15))
                    warning = f"{chat_tasks[0]['chat_title']} 补发检查失败：{error_text(exc)}"
                    if self.recovery_warnings.get(chat_id) != warning:
                        self.store.log(warning, "warning")
                    self.recovery_warnings[chat_id] = warning
                    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
                        raise
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
                    if await asyncio.wait_for(client.is_user_authorized(), timeout=20):
                        await self.mark_authorized()
                    elif self.state not in {"code_sent", "password_required"}:
                        self.state, self.last_error = "login_required", ""
                if self.authorized:
                    if self.last_health_at is None or time.time() - self.last_health_at >= 30:
                        await self.probe(client)
                    if self.recovery_backlog or self.last_recovery_at is None or time.time() - self.last_recovery_at > 60:
                        await self.recover()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = error_text(exc)
                if message != self.last_error:
                    self.store.log(message, "warning")
                self.last_error, self.state = message, "offline"
                if not isinstance(exc, errors.FloodWaitError):
                    # Discard Telethon's stalled RPC/reconnection state, retaining the session on disk.
                    async with self.connection_lock:
                        old_client, self.client = self.client, None
                        self.authorized = False
                        self.last_health_at, self.last_recovery_at = None, None
                        if old_client:
                            with contextlib.suppress(Exception):
                                await asyncio.wait_for(old_client.disconnect(), timeout=10)
                if isinstance(exc, (errors.AuthKeyUnregisteredError, errors.SessionRevokedError)):
                    self.authorized = False
                if isinstance(exc, errors.FloodWaitError):
                    await asyncio.sleep(min(exc.seconds, 300))
            await asyncio.sleep(5)

    async def close(self):
        self.running = False
        if self.client:
            await self.client.disconnect()
