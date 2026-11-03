from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from telethon import errors

from .config import ROOT, Config
from .feishu import Sender, validate_webhook
from .store import Store
from .telegram import TelegramService, error_text


class SettingsInput(BaseModel):
    api_id: int = Field(gt=0)
    api_hash: str | None = None
    proxy: str = ""
    catchup_limit: int = Field(default=500, ge=100, le=5000)

    @field_validator("api_hash")
    @classmethod
    def hash_valid(cls, value):
        import re
        if value is not None and not re.fullmatch(r"[0-9a-fA-F]{32}", value):
            raise ValueError("API Hash 应为 32 位十六进制字符串")
        return value


class PhoneInput(BaseModel):
    phone: str


class VerifyInput(BaseModel):
    code: str = ""
    password: str = ""


class RobotInput(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    webhook: str
    secret: str = Field(default="", max_length=300)
    enabled: bool = True

    @field_validator("webhook")
    @classmethod
    def webhook_valid(cls, value):
        return validate_webhook(value.strip())


class RobotUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    webhook: str | None = None
    secret: str | None = Field(default=None, max_length=300)
    enabled: bool = True


class TaskInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    chat_id: int
    topic_id: int = Field(default=0, ge=0)
    robot_id: int
    edits: bool = True
    include_words: str = Field(default="", max_length=1000)
    exclude_words: str = Field(default="", max_length=1000)
    stale_minutes: int = Field(default=5, ge=1, le=1440)
    catch_up: bool = True


class TaskUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    edits: bool = True
    include_words: str = Field(default="", max_length=1000)
    exclude_words: str = Field(default="", max_length=1000)
    stale_minutes: int = Field(default=5, ge=1, le=1440)
    catch_up: bool = True


def public_robot(robot):
    return {"id": robot["id"], "name": robot["name"], "enabled": bool(robot["enabled"]),
            "webhook_hint": "…" + robot["webhook"][-6:], "secret_configured": bool(robot["secret"])}


def create_app(store=None, config=None, telegram=None, sender=None, start_workers=True):
    store = store or Store()
    config = config or Config()
    telegram = telegram or TelegramService(store, config)
    sender = sender or Sender(store)

    @asynccontextmanager
    async def lifespan(app):
        workers = []
        if start_workers:
            store.mark_inflight_uncertain()
            workers = [asyncio.create_task(telegram.run()), asyncio.create_task(sender.run())]
            store.log("消息中继服务已启动")
        yield
        for task in workers:
            task.cancel()
        for task in workers:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await telegram.close()
        await sender.close()

    app = FastAPI(title="消息中继", version="1.0.1", lifespan=lifespan)
    app.state.store, app.state.telegram = store, telegram

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            return JSONResponse({"detail": "本地管理面板只接受本机访问"}, 403)
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("sec-fetch-site") == "cross-site" or (origin and (urlsplit(origin).netloc != request.url.netloc or urlsplit(origin).scheme != request.url.scheme)):
                return JSONResponse({"detail": "跨站请求已拒绝，请从本机管理页面操作"}, 403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, 400)

    @app.exception_handler(errors.RPCError)
    async def telegram_error(request, exc):
        return JSONResponse({"detail": error_text(exc)}, 429 if isinstance(exc, errors.FloodWaitError) else 400)

    @app.exception_handler(TimeoutError)
    async def timeout_error(request, exc):
        return JSONResponse({"detail": error_text(exc)}, 504)

    @app.exception_handler(ConnectionError)
    async def connection_error(request, exc):
        return JSONResponse({"detail": error_text(exc)}, 503)

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(request, exc):
        return JSONResponse({"detail": "此来源、话题和机器人已存在监听任务，请勿重复添加"}, 409)

    def robot_by_id(robot_id):
        robot = store.one("SELECT * FROM robots WHERE id=?", (robot_id,))
        if not robot:
            raise HTTPException(404, "机器人不存在")
        return robot

    def task_by_id(task_id):
        task = store.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise HTTPException(404, "监听任务不存在")
        return task

    @app.get("/api/health")
    async def health():
        return {"ok": True, "app": "telegram-feishu-relay", "version": "1.0.1"}

    @app.get("/api/state")
    async def state():
        tasks = store.all("""SELECT t.*,r.name AS robot_name,r.enabled AS robot_enabled,
          (SELECT COUNT(*) FROM deliveries d WHERE d.task_id=t.id AND status IN ('pending','sending','retry')) AS pending,
          (SELECT MAX(sent_at) FROM deliveries d WHERE d.task_id=t.id AND status='sent') AS last_sent_at
          FROM tasks t JOIN robots r ON r.id=t.robot_id ORDER BY t.id DESC""")
        return {"telegram": telegram.status(), "stats": store.stats(), "settings": config.public(),
                "robots": [public_robot(r) for r in store.all("SELECT * FROM robots ORDER BY id DESC")],
                "tasks": tasks, "system_logs": store.all("SELECT * FROM system_logs ORDER BY id DESC LIMIT 15"),
                "server_time": time.time()}

    @app.put("/api/settings")
    async def settings(value: SettingsInput):
        if telegram.authorized and value.api_id != config.values["api_id"]:
            raise ValueError("更换 Telegram 应用前请先退出当前账号")
        values = value.model_dump(exclude_none=True)
        candidate = Config(config.path)
        candidate.values.update(values)
        candidate.telegram_proxy()
        if not candidate.values["api_hash"]:
            raise ValueError("请填写 API Hash")
        config.save(**values)
        await telegram.reset()
        store.log("Telegram 连接配置已更新")
        return config.public()

    @app.post("/api/telegram/code")
    async def code(value: PhoneInput):
        return await telegram.send_code(value.phone.strip())

    @app.post("/api/telegram/verify")
    async def verify(value: VerifyInput):
        return await telegram.verify(value.code.replace(" ", ""), value.password)

    @app.post("/api/telegram/reconnect")
    async def reconnect():
        await telegram.reset()
        return {"ok": True}

    @app.post("/api/telegram/logout")
    async def logout():
        await telegram.logout()
        return {"ok": True}

    @app.get("/api/telegram/dialogs")
    async def dialogs(refresh: bool = False):
        return await telegram.dialogs(refresh)

    @app.get("/api/telegram/topics/{chat_id}")
    async def topics(chat_id: int, q: str = ""):
        return await telegram.topics(chat_id, q)

    @app.post("/api/robots")
    async def create_robot(value: RobotInput):
        robot_id = store.execute("INSERT INTO robots(name,webhook,secret,enabled,created_at) VALUES(?,?,?,?,?)",
                                 (value.name.strip(), value.webhook, value.secret.strip(), value.enabled, time.time()))
        store.log(f"已添加飞书机器人：{value.name.strip()}")
        return public_robot(robot_by_id(robot_id))

    @app.put("/api/robots/{robot_id}")
    async def update_robot(robot_id: int, value: RobotUpdate):
        old = robot_by_id(robot_id)
        webhook = validate_webhook(value.webhook.strip()) if value.webhook is not None else old["webhook"]
        secret = value.secret.strip() if value.secret is not None else old["secret"]
        store.execute("UPDATE robots SET name=?,webhook=?,secret=?,enabled=? WHERE id=?", (value.name.strip(), webhook, secret, value.enabled, robot_id))
        return public_robot(robot_by_id(robot_id))

    @app.delete("/api/robots/{robot_id}")
    async def delete_robot(robot_id: int):
        robot_by_id(robot_id)
        if store.one("SELECT id FROM tasks WHERE robot_id=? LIMIT 1", (robot_id,)):
            raise ValueError("请先删除使用此机器人的监听任务")
        # Preserve delivery audit rows. The UI can pause a robot with delivery history.
        if store.one("SELECT id FROM deliveries WHERE robot_id=? LIMIT 1", (robot_id,)):
            raise ValueError("该机器人已有发送记录，请使用停用以保留历史记录")
        store.execute("DELETE FROM robots WHERE id=?", (robot_id,))
        return {"ok": True}

    @app.post("/api/robots/{robot_id}/test")
    async def test_robot(robot_id: int):
        robot = robot_by_id(robot_id)
        if not robot["enabled"]:
            raise ValueError("请先启用此机器人")
        now = time.time()
        job_id = store.execute("""INSERT INTO deliveries(robot_id,source,kind,body,original_at,event_at,received_at)
          VALUES(?,?,'test',?,?,?,?)""", (robot_id, "消息中继", "飞书机器人连接测试。\n收到此消息表示机器人已接受推送。\n请同时确认手机通知、声音及群免打扰设置。", now, now, now))
        return {"id": job_id, "message": "测试消息已排队，请在发送记录和飞书群中确认结果"}

    @app.post("/api/tasks")
    async def create_task(value: TaskInput):
        robot_by_id(value.robot_id)
        source = await telegram.resolve_source(value.chat_id, value.topic_id)
        now = int(time.time())
        task_id = store.execute("""INSERT INTO tasks(name,chat_id,chat_title,topic_id,topic_title,robot_id,
            edits,include_words,exclude_words,stale_minutes,catch_up,enabled_since,cursor,last_scan,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (value.name.strip(), value.chat_id, source["chat_title"], value.topic_id, source["topic_title"],
            value.robot_id, value.edits, value.include_words, value.exclude_words, value.stale_minutes,
            value.catch_up, now, source["cursor"], now, now))
        store.log(f"监听任务已启用：{value.name.strip()}，从现在开始接收新消息")
        return task_by_id(task_id)

    @app.put("/api/tasks/{task_id}")
    async def update_task(task_id: int, value: TaskUpdate):
        task_by_id(task_id)
        store.execute("UPDATE tasks SET name=?,edits=?,include_words=?,exclude_words=?,stale_minutes=?,catch_up=? WHERE id=?",
                     (value.name.strip(), value.edits, value.include_words, value.exclude_words, value.stale_minutes, value.catch_up, task_id))
        return task_by_id(task_id)

    @app.post("/api/tasks/{task_id}/toggle")
    async def toggle_task(task_id: int):
        task = task_by_id(task_id)
        if task["enabled"]:
            store.execute("UPDATE tasks SET enabled=0 WHERE id=?", (task_id,))
            store.log(f"监听任务已暂停：{task['name']}")
        else:
            source = await telegram.resolve_source(task["chat_id"], task["topic_id"])
            now = int(time.time())
            store.execute("UPDATE tasks SET enabled=1,enabled_since=?,cursor=?,last_scan=? WHERE id=?", (now, source["cursor"], now, task_id))
            store.log(f"监听任务已恢复：{task['name']}；暂停期间消息不自动补发")
        return task_by_id(task_id)

    @app.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: int):
        task = task_by_id(task_id)
        with store.db() as db:
            db.execute("UPDATE deliveries SET status='cancelled',error='监听任务已删除' WHERE task_id=? AND status IN ('pending','retry')", (task_id,))
            db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        store.log(f"监听任务已删除：{task['name']}")
        return {"ok": True}

    @app.get("/api/deliveries")
    async def deliveries(status: str = "", q: str = "", page: int = Query(1, ge=1), limit: int = Query(30, ge=1, le=100)):
        clauses, args = [], []
        if status:
            clauses.append("d.status=?")
            args.append(status)
        if q:
            clauses.append("(d.body LIKE ? OR d.source LIKE ?)")
            args += [f"%{q}%", f"%{q}%"]
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        total = store.one(f"SELECT COUNT(*) AS count FROM deliveries d {where}", args)["count"]
        rows = store.all(f"SELECT d.*,r.name AS robot_name FROM deliveries d JOIN robots r ON r.id=d.robot_id {where} ORDER BY d.id DESC LIMIT ? OFFSET ?", args + [limit, (page - 1) * limit])
        return {"items": rows, "total": total, "page": page, "limit": limit}

    @app.post("/api/deliveries/{delivery_id}/retry")
    async def retry(delivery_id: int):
        row = store.one("SELECT * FROM deliveries WHERE id=?", (delivery_id,))
        if not row:
            raise HTTPException(404, "发送记录不存在")
        if row["status"] not in {"failed", "retry", "uncertain"}:
            raise ValueError("只有失败、等待重试或待核对的消息可以手动重试")
        if not robot_by_id(row["robot_id"])["enabled"]:
            raise ValueError("请先启用目标机器人")
        if row["task_id"] and not task_by_id(row["task_id"])["enabled"]:
            raise ValueError("请先恢复对应监听任务")
        store.execute("UPDATE deliveries SET status='pending',attempts=0,next_attempt=0,error='' WHERE id=?", (delivery_id,))
        return {"ok": True}

    @app.post("/api/deliveries/{delivery_id}/acknowledge")
    async def acknowledge(delivery_id: int):
        row = store.one("SELECT * FROM deliveries WHERE id=?", (delivery_id,))
        if not row:
            raise HTTPException(404, "发送记录不存在")
        if row["status"] != "uncertain":
            raise ValueError("只有发送结果未知的通知需要核对")
        now = time.time()
        store.execute("UPDATE deliveries SET status='sent',sent_at=?,error='用户在飞书群核对后标记已接收' WHERE id=?", (now, delivery_id))
        return {"ok": True}

    @app.post("/api/recover")
    async def recover():
        await telegram.require()
        if telegram.recovery_lock.locked():
            return {"message": "补发检查正在进行"}
        asyncio.create_task(telegram.recover())
        return {"message": "已开始检查近期可恢复消息，结果请查看运行记录"}

    @app.post("/api/shutdown")
    async def shutdown(request: Request):
        server = getattr(request.app.state, "server", None)
        if server is None:
            raise HTTPException(503, "无法安全关闭后台服务")
        server.should_exit = True
        store.log("收到安全停止请求，正在排空后台任务")
        return {"ok": True}

    @app.get("/api/diagnostics")
    async def diagnostics():
        return Response(store.export(), media_type="application/json", headers={"Content-Disposition": 'attachment; filename="relay-diagnostics.json"'})

    static = ROOT / "frontend" / "dist"
    if static.exists():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/{path:path}")
    async def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "接口不存在")
        if not (static / "index.html").exists():
            return JSONResponse({"detail": "前端尚未构建。请执行 frontend 中的 npm install 和 npm run build。"}, 503)
        return FileResponse(static / "index.html")

    return app


app = create_app()
