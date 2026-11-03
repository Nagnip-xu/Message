"""Run a single worker: Telegram's session and durable queue have one owner."""
import os
import sys
from pathlib import Path

import uvicorn

if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    os.chdir(root)
    data = Path(os.environ.get("RELAY_DATA_DIR", str(root / "data")))
    data.mkdir(parents=True, exist_ok=True)
    lock = (data / "server.lock").open("a+b")
    lock.seek(0)
    if not lock.read(1):
        lock.write(b"0")
        lock.flush()
    lock.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("Message Relay is already running for this data directory.")
    (data / "server.pid").write_text(str(os.getpid()), encoding="ascii")
    try:
        from relay.app import app
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8765, workers=1, log_level="warning", access_log=False))
        app.state.server = server
        server.run()
    finally:
        pid_file = data / "server.pid"
        if pid_file.exists() and pid_file.read_text(encoding="ascii") == str(os.getpid()):
            pid_file.unlink()
