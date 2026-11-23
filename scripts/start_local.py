"""Windows development entry: real API, isolated demo database, Vite and Python reload."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ChildProcesses:
    """Windows closes this job handle even if the user closes the console window."""
    def __init__(self):
        self.processes, self.logs, self.job = [], {}, None
        if os.name != "nt":
            return
        from ctypes import wintypes as w

        class Limits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64), ("flags", w.DWORD),
                ("min_working", ctypes.c_size_t), ("max_working", ctypes.c_size_t), ("active", w.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", Limits), ("io", ctypes.c_uint64 * 6), ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.CreateJobObjectW.argtypes = [w.LPVOID, w.LPCWSTR]
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.job = self.kernel.CreateJobObjectW(None, None)
        info = ExtendedLimits()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.job or not self.kernel.SetInformationJobObject(self.job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())

    def start(self, command, cwd, env, log):
        if log not in self.logs:
            self.logs[log] = log.open("a", encoding="utf-8")
        stream = self.logs[log]
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
        self.processes = [item for item in self.processes if item.poll() is None] + [process]
        if self.job and not self.kernel.AssignProcessToJobObject(self.job, int(process._handle)):
            process.terminate()
            raise ctypes.WinError(ctypes.get_last_error())
        return process

    def close(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                if os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
        if self.job:
            self.kernel.CloseHandle(self.job)
            self.job = None
        for stream in self.logs.values():
            stream.close()


def status(url):
    try:
        with HTTP.open(url + "/__local/status", timeout=0.6) as response:
            data = json.load(response)
        return data.get("mode") == "local-development" and Path(data.get("project", "")).resolve() == ROOT
    except (OSError, ValueError):
        return False


def require_port(port):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


def source_version():
    result = []
    for folder in (ROOT / "src", ROOT / "scripts"):
        for file in sorted(folder.rglob("*.py")):
            try:
                item = file.stat()
                result.append((file, item.st_mtime_ns, item.st_size))
            except FileNotFoundError:
                pass  # The next poll sees the completed rename/save.
    return tuple(result)


def wait_ready(url, process, log):
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"进程启动失败，请查看：{log}\n{log.read_text(encoding='utf-8', errors='replace')[-2500:]}")
        if status(url):
            return
        time.sleep(0.2)
    raise RuntimeError(f"启动超时，请查看：{log}")


def main():
    parser = argparse.ArgumentParser(description="PurrAI 本机测试后台：前后端热更新，不连接真实群")
    parser.add_argument("--api-port", type=int, default=8765)
    parser.add_argument("--web-port", type=int, default=5188)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--check", action="store_true", help="只检查依赖和端口，不启动或修改数据")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "local-dev")
    args = parser.parse_args()
    if not 1 <= args.api_port <= 65535 or not 1 <= args.web_port <= 65535 or args.api_port == args.web_port:
        parser.error("前后端端口须不同，且在 1–65535 之间")
    os.chdir(ROOT)
    node = os.environ.get("PURRAI_NODE") or ("D:/Tools/NodeJS/node.exe" if Path("D:/Tools/NodeJS/node.exe").is_file() else shutil.which("node"))
    vite = ROOT / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
    if not node or not Path(node).is_file() or not vite.is_file():
        raise RuntimeError("未找到 Node.js 或网页依赖。请按 docs/OPERATIONS.md 准备本机环境；启动器不会自动安装。")
    from purrai.db import schema_head
    schema_head()  # Check the existing Python environment without touching any database.
    api, web = f"http://127.0.0.1:{args.api_port}", f"http://127.0.0.1:{args.web_port}"
    if status(api) and status(web):
        print(f"本机测试后台已经在运行：{web}")
        if not args.check and not args.no_browser:
            webbrowser.open(web)
        return
    for port in (args.api_port, args.web_port):
        try:
            require_port(port)
        except OSError as exc:
            raise RuntimeError(f"端口 {port} 已被占用或不可用。关闭原启动窗口，或用 --api-port / --web-port 换端口。") from exc
    data_dir = args.data_dir.resolve()
    database = data_dir / "purrai.db"
    if database.exists():
        import sqlite3
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "groups" in tables and connection.execute("SELECT count(*) FROM groups").fetchone()[0]:
                marker = connection.execute("SELECT value FROM meta WHERE key='local-dev:seed:v1'").fetchone() if "meta" in tables else None
                if not marker:
                    raise RuntimeError("所选目录包含非本机测试数据，请另选空目录；未修改任何数据库。")
    print(f"网页：{web}\n本机测试数据：{database}\nPython：{sys.executable}\nNode.js：{node}")
    if args.check:
        print("检查通过。前后端均使用当前源码，不需要打包或 Telegram 登录。")
        return
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "PURRAI_DATABASE_URL": f"sqlite+aiosqlite:///{database.as_posix()}", "PURRAI_DEV_DATABASE": str(database),
        "PURRAI_BACKUP_DIR": str(data_dir / "backups"), "PURRAI_DEV_API_URL": api}
    data_dir.mkdir(parents=True, exist_ok=True)
    # Run migration outside the source checkout so Settings never reads its real .env.
    env = {key: value for key, value in env.items() if not key.startswith("PURRAI_") or key in (
        "PURRAI_DATABASE_URL", "PURRAI_DEV_DATABASE", "PURRAI_BACKUP_DIR", "PURRAI_DEV_API_URL")}
    subprocess.run([sys.executable, "-m", "purrai.cli", "init-db"], cwd=data_dir, env=env, check=True)
    log_dir = data_dir / "logs" / time.strftime("%Y%m%d-%H%M%S")
    log_dir.mkdir(parents=True, exist_ok=True)
    children = ChildProcesses()
    try:
        command = [sys.executable, "-u", "-m", "uvicorn", "scripts.local_dev:create_local_app", "--factory", "--host", "127.0.0.1",
                   "--port", str(args.api_port)]
        version = source_version()
        backend = children.start(command,
            ROOT, env, log_dir / "backend.log")
        wait_ready(api, backend, log_dir / "backend.log")
        frontend = children.start([node, str(vite), "--host", "127.0.0.1", "--port", str(args.web_port), "--strictPort"],
            ROOT / "frontend", env, log_dir / "frontend.log")
        wait_ready(web, frontend, log_dir / "frontend.log")
        print(f"\n已启动：{web}\n修改网页或后端代码会自动更新；测试设置在重启后保留。\n此窗口保持运行，按 Ctrl+C 或关闭窗口停止前后端。\n日志：{log_dir}", flush=True)
        if not args.no_browser:
            webbrowser.open(web)
        failed = False
        while frontend.poll() is None:
            time.sleep(0.5)
            latest = source_version()
            if latest != version:
                version, failed = latest, False
                print("Python 代码已变化，正在重启本机后端……", flush=True)
                if backend.poll() is None:
                    # Uvicorn's Windows reload broadcasts CTRL_C_EVENT. Terminating
                    # only this owned process keeps Vite and the launcher alive.
                    backend.terminate()
                    backend.wait(timeout=5)
                backend = children.start(command, ROOT, env, log_dir / "backend.log")
            if backend.poll() is not None and not failed:
                failed = True
                print(f"后端暂未启动成功，请修正代码后保存，会再次启动。日志：{log_dir / 'backend.log'}", flush=True)
        raise RuntimeError(f"开发服务已退出，请查看日志：{log_dir}")
    except KeyboardInterrupt:
        print("\n正在停止本机测试后台……", flush=True)
    finally:
        children.close()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"\n启动未完成：{error}", file=sys.stderr)
        sys.exit(1)
