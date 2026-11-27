"""Run the existing localhost development app against a fresh browser-test database."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import uvicorn

ROOT = Path(__file__).resolve().parents[1]


def main():
    outputs = ROOT / "frontend" / "test-results"
    outputs.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(outputs).free < 256 * 1024 * 1024:
        raise RuntimeError("浏览器集成测试至少需要 256 MB 可用空间")
    run = outputs / f"backend-{time.time_ns()}"
    run.mkdir()
    database = run / "purrai.db"
    os.environ["PURRAI_DEV_DATABASE"] = str(database)
    environment = {**os.environ, "PURRAI_DATABASE_URL": f"sqlite+aiosqlite:///{database.as_posix()}",
                   "PURRAI_BOT_TOKEN": "", "PURRAI_CHAT_API_KEY": "", "PYTHONUTF8": "1"}
    subprocess.run([sys.executable, "-m", "purrai.cli", "init-db"], cwd=ROOT, env=environment, check=True)
    sys.path.insert(0, str(ROOT / "scripts"))
    uvicorn.run("local_dev:create_local_app", factory=True, host="127.0.0.1", port=8877, log_level="warning")


if __name__ == "__main__":
    main()
