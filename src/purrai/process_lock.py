from pathlib import Path


class ProcessLock:
    """OS-owned file lock; an interrupted process cannot leave a stale lock behind."""

    def __init__(self, database_url):
        self.path = Path(database_url.split("///", 1)[-1] + ".runtime-lock")
        self.handle = None

    def acquire(self):
        import os

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("此数据库已有一个 PurrAI 进程运行；请保持单 worker，或先停止服务。") from exc

    def release(self):
        if self.handle:
            self.handle.close()
            self.handle = None
