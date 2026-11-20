from __future__ import annotations

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

INTERVAL = 4 * 60 * 60
TZ = ZoneInfo("Asia/Shanghai")


def stamp(value):
    return datetime.fromtimestamp(value, TZ).strftime("%m-%d %H:%M:%S") if value else "暂无记录"


class Heartbeat:
    def __init__(self, store, config, telegram):
        self.store, self.config, self.telegram = store, config, telegram

    def process_once(self, now=None):
        now = time.time() if now is None else now
        if not self.config.values["heartbeat_enabled"]:
            return 0
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
                last_sent = db.execute("SELECT MAX(sent_at) FROM deliveries WHERE robot_id=? AND kind IN ('new','edit','catchup') AND status='sent'", (robot_id,)).fetchone()[0]
                counts = dict(db.execute("SELECT status,COUNT(*) FROM deliveries WHERE robot_id=? AND kind!='heartbeat' GROUP BY status", (robot_id,)))
            pending = sum(counts.get(k, 0) for k in ("pending", "sending", "retry"))
            failed, uncertain = counts.get("failed", 0), counts.get("uncertain", 0)
            state = "运行正常" if healthy and tasks and not warnings and not (failed or uncertain) else "需要检查"
            connection = "已通过在线请求检查" if healthy else "未通过在线检查，监听暂未确认正常"
            lines = [f"{state} · 每 4 小时状态提醒", f"Telegram：{connection}",
                     f"最近在线检查：{stamp(health_at)}", f"启用的监听任务：{tasks} 个",
                     f"过去 4 小时成功推送：{sent} 条", f"最近消息推送：{stamp(last_sent)}",
                     f"发送队列：{pending} 条待发送，{failed} 条失败，{uncertain} 条待核对"]
            if not sent:
                lines.append("过去 4 小时没有成功推送记录；请结合上面的连接和队列状态判断。")
            if tg.get("last_error"):
                lines.append(f"连接异常：{tg['last_error']}")
            lines.extend(warnings[:3])
            if not tasks:
                lines.append("此机器人没有已启用的监听任务，请在管理面板检查任务开关。")
            lines.append("这是运行状态快照；飞书接口接受消息不代表手机已弹出通知。")
            if self.store.enqueue_heartbeat(robot_id, "\n".join(lines), now=now, interval=INTERVAL):
                queued += 1
        if queued:
            self.store.log(f"已生成运行状态提醒：{queued} 个飞书机器人")
        return queued

    async def run(self):
        # Give Telegram a moment to reconnect and start recovery before the first report.
        await asyncio.sleep(30)
        while True:
            try:
                self.process_once()
            except Exception as exc:
                self.store.log(f"运行状态提醒生成失败（{type(exc).__name__}）", "error")
            await asyncio.sleep(30)
