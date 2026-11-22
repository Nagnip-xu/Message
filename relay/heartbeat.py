from __future__ import annotations

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

INTERVAL = 24 * 60 * 60
TZ = ZoneInfo("Asia/Shanghai")


def slot_for(now):
    local = datetime.fromtimestamp(now, TZ)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class Heartbeat:
    def __init__(self, store, config, telegram):
        self.store, self.config, self.telegram = store, config, telegram

    def process_once(self, now=None):
        now = time.time() if now is None else now
        if not self.config.values["heartbeat_enabled"]:
            return 0
        scheduled_at = slot_for(now)
        tg = self.telegram.status()
        health_at = tg.get("last_health_at")
        healthy = bool(tg.get("authorized") and tg.get("connected") and tg.get("state") == "ready"
                       and health_at and now - health_at < 90)
        warnings = tg.get("recovery_warnings", [])
        queued = 0
        for robot in self.store.all("SELECT id FROM robots WHERE enabled=1"):
            robot_id = robot["id"]
            with self.store.db() as db:
                tasks = db.execute("SELECT COUNT(*) FROM tasks WHERE enabled=1 AND robot_id=?", (robot_id,)).fetchone()[0]
                sent = db.execute("SELECT COUNT(*) FROM deliveries WHERE robot_id=? AND kind IN ('new','edit','catchup') AND status='sent' AND sent_at>?", (robot_id, now - INTERVAL)).fetchone()[0]
                counts = dict(db.execute("SELECT status,COUNT(*) FROM deliveries WHERE robot_id=? AND kind!='heartbeat' GROUP BY status", (robot_id,)))
            pending = sum(counts.get(k, 0) for k in ("pending", "sending", "retry"))
            failed, uncertain = counts.get("failed", 0), counts.get("uncertain", 0)
            issues = []
            if not healthy:
                issues.append("Telegram 未通过在线检查")
            if not tasks:
                issues.append("没有启用的监听任务")
            if warnings:
                issues.append("消息恢复有异常")
            if failed or uncertain:
                issues.append(f"{failed} 条失败、{uncertain} 条待核对")
            if issues:
                body = f"每日状态：需要检查，{'，'.join(issues)}。\n过去 24 小时已推送 {sent} 条，详情请查看管理面板。"
            else:
                body = f"每日状态：运行正常，正在监听 {tasks} 个任务。\n过去 24 小时已推送 {sent} 条消息。"
            if pending:
                body = body.rstrip("。") + f"（{pending} 条待发送）。"
            if self.store.enqueue_heartbeat(robot_id, body, now=now, scheduled_at=scheduled_at):
                queued += 1
        if queued:
            self.store.log(f"已生成运行状态提醒：{queued} 个飞书机器人")
        return queued

    async def run(self):
        while True:
            try:
                self.process_once()
            except Exception as exc:
                self.store.log(f"运行状态提醒生成失败（{type(exc).__name__}）", "error")
            # Daily at Beijing midnight, independent of startup time.
            now = time.time()
            await asyncio.sleep(min(30, max(.1, slot_for(now) + INTERVAL - now)))
