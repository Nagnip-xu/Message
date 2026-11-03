from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit, unquote

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("RELAY_DATA_DIR", str(ROOT / "data")))
DATA.mkdir(parents=True, exist_ok=True)


class Config:
    def __init__(self, path: Path | None = None):
        self.path = path or DATA / "settings.json"
        self.values = {"api_id": 0, "api_hash": "", "proxy": "", "catchup_limit": 500}
        if self.path.exists():
            self.values.update(json.loads(self.path.read_text(encoding="utf-8-sig")))

    def save(self, **values):
        self.values.update(values)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.values, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def public(self):
        return {"api_id": self.values["api_id"], "api_hash_configured": bool(self.values["api_hash"]),
                "proxy": self.values["proxy"], "catchup_limit": self.values["catchup_limit"]}

    def telegram_proxy(self):
        value = self.values["proxy"].strip()
        if not value:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"socks5", "socks4", "http"} or not parsed.hostname or not parsed.port:
            raise ValueError("代理格式应为 socks5://127.0.0.1:端口 或 http://127.0.0.1:端口")
        return {"proxy_type": parsed.scheme, "addr": parsed.hostname, "port": parsed.port,
                "username": unquote(parsed.username) if parsed.username else None,
                "password": unquote(parsed.password) if parsed.password else None, "rdns": True}
