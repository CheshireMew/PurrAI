import argparse
import asyncio
from pathlib import Path

from purrai.config import Settings
from purrai.db import Database


def main():
    parser = argparse.ArgumentParser(description="PurrAI Telegram 群管理服务")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="执行数据库迁移")
    run = sub.add_parser("run", help="以单 worker 启动后台和机器人")
    run.add_argument("--host")
    run.add_argument("--port", type=int)
    sub.add_parser("backup", help="生成 SQLite 一致性备份")
    sub.add_parser("doctor", help="检查运行配置，不输出密钥")
    access = sub.add_parser("groups", help="列出、批准或撤销私用群接入")
    changes = access.add_mutually_exclusive_group()
    changes.add_argument("--approve", type=int, metavar="GROUP_ID")
    changes.add_argument("--revoke", type=int, metavar="GROUP_ID")
    restore = sub.add_parser("restore", help="停服后恢复数据库；自动先保留当前数据库备份")
    restore.add_argument("source", type=Path)
    args = parser.parse_args()
    settings = Settings()
    if args.command == "init-db":
        from alembic import command
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from purrai.process_lock import ProcessLock
        import sqlite3

        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        database_file = Path(settings.database_url.split("///", 1)[-1])
        lock = ProcessLock(settings.database_url)
        lock.acquire()
        try:
            if database_file.is_file() and database_file.stat().st_size:
                with sqlite3.connect(database_file) as connection:
                    table = connection.execute(
                        "SELECT name FROM sqlite_master WHERE name='alembic_version'"
                    ).fetchone()
                    current = (
                        connection.execute("SELECT version_num FROM alembic_version").fetchone()
                        if table
                        else None
                    )
                if current and current[0] != ScriptDirectory.from_config(config).get_current_head():

                    async def before_upgrade():
                        database = Database(settings.database_url)
                        try:
                            return await database.backup(settings.backup_dir)
                        finally:
                            await database.close()

                    print(f"升级前备份：{asyncio.run(before_upgrade())}")
            command.upgrade(config, "head")
            print("PurrAI 数据库迁移完成。")
        finally:
            lock.release()
    elif args.command == "run":
        import uvicorn
        from purrai.app import create_app

        uvicorn.run(
            create_app(settings),
            host=args.host or settings.host,
            port=args.port or settings.port,
            workers=1,
            access_log=False,
        )
    elif args.command == "backup":

        async def backup():
            database = Database(settings.database_url)
            try:
                path = await database.backup(settings.backup_dir)
                print(f"备份已保存：{path}")
            finally:
                await database.close()

        asyncio.run(backup())
    elif args.command == "groups":
        import json
        from sqlalchemy import select
        from purrai.management import Management
        from purrai.models import Group
        from purrai.process_lock import ProcessLock
        from purrai.telegram import TelegramGateway

        async def group_access():
            database = Database(settings.database_url)
            gateway = TelegramGateway("")
            lock = ProcessLock(settings.database_url)
            try:
                if not (await database.schema_health())["ready"]:
                    raise RuntimeError("请先执行 purrai init-db")
                if args.approve is not None or args.revoke is not None:
                    lock.acquire()
                    management = Management(database, gateway, settings=settings)
                    await management.set_group_access(args.approve if args.approve is not None else args.revoke,
                                                      args.approve is not None)
                async with database.sessions() as session:
                    rows = (await session.scalars(select(Group).order_by(Group.id))).all()
                    print(json.dumps([{"id": row.id, "title": row.title, "approved": row.approved,
                                       "enabled": row.enabled} for row in rows], ensure_ascii=False, indent=2))
            finally:
                lock.release()
                await gateway.close()
                await database.close()

        asyncio.run(group_access())
    elif args.command == "doctor":
        print(f"Python 环境：可用\nBot Token：{'已配置' if settings.bot_token else '未配置'}")
        print(f"网页登录（可选）：{'已配置' if settings.login_ready else '未配置，仍可通过 Bot 管理'}")
        print(
            f"超级管理员（可选）：{'已配置' if settings.superadmin_ids else '未配置；群管理员仍可管理本群，私聊 /id 获取自己的数字 ID'}"
        )
        print(
            f"网页 HTTPS：{'是' if settings.public_url.startswith('https://') else '否（网页仅供本地检查，不影响 Bot）'}"
        )
        print(f"前端构建：{'存在' if (settings.frontend_dist / 'index.html').exists() else '尚未构建'}")
        print(f"群聊天 API（可选）：{'已配置，模型 ' + settings.chat_model if settings.chat_ready else '未配置；不影响验证和群管理'}")
        print("私用接入：仅限明确批准的群；使用 purrai groups 查看和批准")
        print(f"每日模型请求：全局 {settings.chat_daily_calls}、每群 {settings.chat_group_daily_calls}、每人 {settings.chat_user_daily_calls}；全局 Token 预算 {settings.chat_daily_tokens}")
    elif args.command == "restore":
        import sqlite3
        from purrai.process_lock import ProcessLock

        if not args.source.is_file():
            parser.error("指定备份文件不存在")
        lock = ProcessLock(settings.database_url)
        lock.acquire()
        try:

            async def preserve():
                database = Database(settings.database_url)
                try:
                    return await database.backup(settings.backup_dir)
                finally:
                    await database.close()

            backup = asyncio.run(preserve())
            with sqlite3.connect(args.source.resolve().as_uri() + "?mode=ro", uri=True) as source:
                if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    parser.error("备份数据库完整性检查失败")
                if not source.execute(
                    "SELECT name FROM sqlite_master WHERE name='alembic_version'"
                ).fetchone():
                    parser.error("指定文件不是带迁移记录的 PurrAI 备份")
                target = settings.database_url.split("///", 1)[-1]
                with sqlite3.connect(target) as destination:
                    source.backup(destination)
            print(f"数据库已恢复；恢复前的数据库保留于：{backup}")
        finally:
            lock.release()


if __name__ == "__main__":
    main()
