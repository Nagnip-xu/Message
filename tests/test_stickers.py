import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types

from relay.config import Config
from relay.telegram import TelegramService
from tests.test_relay import make_route, store


def document_message(mime, attributes, text=""):
    now = datetime.now(timezone.utc)
    document = types.Document(id=501,access_hash=1,file_reference=b"",date=now,
        mime_type=mime,size=1000,dc_id=2,attributes=attributes)
    return types.Message(id=101,peer_id=types.PeerChannel(123456),date=now,message=text,
        media=types.MessageMediaDocument(document=document))


def sticker():
    return types.DocumentAttributeSticker(alt="😂",stickerset=types.InputStickerSetEmpty())


@pytest.mark.parametrize("mime,attributes", [
    ("image/webp",[sticker()]),
    ("application/x-tgsticker",[sticker()]),
    ("video/webm",[sticker(),types.DocumentAttributeVideo(duration=2,w=512,h=512)]),
    ("video/mp4",[types.DocumentAttributeAnimated()]),
    ("image/gif",[types.DocumentAttributeAnimated()]),
    ("application/x-tgsticker",[]),
])
@pytest.mark.parametrize("mode", ["new","edit","catchup"])
def test_stickers_and_captionless_animations_never_enqueue_or_fetch_senders(store,tmp_path,mime,attributes,mode):
    task = make_route(store)
    message = document_message(mime,attributes)
    if mode == "edit":
        message.edit_date = datetime.now(timezone.utc)
    sender_lookup = AsyncMock()
    message.get_sender = sender_lookup
    service = TelegramService(store,Config(tmp_path/"settings.json"))
    count = asyncio.run(service.ingest(task,message,SimpleNamespace(noforwards=False,forum=False),
        edited=mode=="edit",recovered=mode=="catchup"))
    assert count == 0
    assert not store.all("SELECT * FROM deliveries")
    assert not store.all("SELECT * FROM voice_jobs")
    sender_lookup.assert_not_awaited()


def test_animation_caption_is_preserved_without_attachment_notice(store,tmp_path):
    task = make_route(store)
    message = document_message("video/mp4",[types.DocumentAttributeAnimated()],"今天先观望")
    service = TelegramService(store,Config(tmp_path/"settings.json"))
    assert asyncio.run(service.ingest(task,message,SimpleNamespace(noforwards=False,forum=False))) == 1
    assert store.one("SELECT body FROM deliveries")["body"] == "今天先观望"


@pytest.mark.parametrize("mime", ["application/pdf","video/mp4","image/webp"])
def test_regular_files_videos_and_images_are_not_mistaken_for_stickers(store,tmp_path,mime):
    task = make_route(store)
    message = document_message(mime,[types.DocumentAttributeFilename(file_name="文件")])
    service = TelegramService(store,Config(tmp_path/"settings.json"))
    assert asyncio.run(service.ingest(task,message,SimpleNamespace(noforwards=False,forum=False))) == 1
    assert "含附件" in store.one("SELECT body FROM deliveries")["body"]


def test_inline_emoji_and_reply_text_are_preserved(store,tmp_path):
    task = make_route(store)
    now = datetime.now(timezone.utc)
    message = types.Message(id=101,peer_id=types.PeerChannel(123456),date=now,
        message="😂 我也这么觉得",reply_to=types.MessageReplyHeader(reply_to_msg_id=80))
    service = TelegramService(store,Config(tmp_path/"settings.json"))
    assert asyncio.run(service.ingest(task,message,SimpleNamespace(noforwards=False,forum=False))) == 1
    assert store.one("SELECT body FROM deliveries")["body"] == "😂 我也这么觉得"
