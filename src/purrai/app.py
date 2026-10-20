from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from purrai.api import install_api
from purrai.auth import install_auth
from purrai.config import Settings
from purrai.db import Database
from purrai.management import ManagementError
from purrai.process_lock import ProcessLock
from purrai.runtime import Runtime
from purrai.service import Service
from purrai.telegram import TelegramGateway


def create_app(settings: Settings | None = None, db=None, telegram=None, start_runtime=True) -> FastAPI:
    settings = settings or Settings()
    database = db or Database(settings.database_url)
    gateway = telegram or TelegramGateway(settings.bot_token, settings.proxy_url)
    service = Service(database, gateway, settings)
    runtime = Runtime(database, gateway, service)

    @asynccontextmanager
    async def lifespan(app):
        process_lock = ProcessLock(settings.database_url)
        try:
            try:
                schema = await database.schema_health()
            except Exception as exc:
                raise RuntimeError("数据库检查失败，请检查配置并运行 purrai init-db") from exc
            if not schema["ready"]:
                raise RuntimeError("数据库结构与当前程序不兼容，请先运行 purrai init-db")
            if start_runtime:
                process_lock.acquire()
                runtime.start()
            yield
        finally:
            await runtime.stop()
            process_lock.release()
            if db is None:
                await database.close()

    app = FastAPI(title="PurrAI", version="0.1.0", lifespan=lifespan)
    app.state.settings, app.state.db, app.state.tg = settings, database, gateway
    app.state.service, app.state.runtime = service, runtime
    app.state.management = runtime.management
    from purrai.traffic import RateLimit

    public_limit, login_limit = RateLimit(120, 600), RateLimit(5, 30)

    @app.middleware("http")
    async def throttle(request, call_next):
        peer = request.client.host if request.client else "unknown"
        if not public_limit.allow(peer) or (
            request.url.path.startswith("/auth/telegram/") and not login_limit.allow(peer)
        ):
            return JSONResponse({"detail": "请求过于频繁，请稍后重试"}, status_code=429, headers={"Retry-After": "60"})
        return await call_next(request)

    @app.exception_handler(ManagementError)
    async def management_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=error.status)

    install_auth(app)
    install_api(app)

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        try:
            database_ready = (await database.schema_health())["ready"]
        except Exception:
            database_ready = False
        runtime_ready = not start_runtime or bool(runtime.tasks) and all(not task.done() for task in runtime.tasks)
        operational = database_ready and runtime_ready and gateway.connected and bool(settings.bot_token)
        return JSONResponse(
            {
                "status": "ready" if operational else "database_incompatible" if not database_ready else "runtime_unavailable" if not runtime_ready else "configuration_required",
                "database_ready": database_ready,
                "runtime_ready": runtime_ready,
                "bot_configured": bool(settings.bot_token),
                "login_configured": settings.login_ready,
                "telegram_connected": gateway.connected,
            },
            status_code=200 if operational else 503,
        )

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str):
        if path.startswith(("api/", "auth/", "health/")):
            raise HTTPException(404, "接口不存在")
        root = settings.frontend_dist.resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise HTTPException(404)
        if target.is_file():
            return FileResponse(target)
        index = root / "index.html"
        if index.is_file() and not path.startswith("assets/"):
            return FileResponse(index)
        if path:
            raise HTTPException(404)
        return HTMLResponse(
            "<html lang='zh-CN'><meta charset='utf-8'><title>PurrAI</title>"
            "<h1>PurrAI</h1><p>请先在 frontend 目录执行 npm ci 和 npm run build。</p></html>",
            status_code=503,
        )

    return app
