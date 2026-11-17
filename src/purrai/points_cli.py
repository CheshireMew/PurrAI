"""Offline points operations. Does not start Telegram or contact external services."""
import asyncio
import json
from pathlib import Path

from purrai.db import Database
from purrai.errors import ManagementError
from purrai.points import PointsLedger
from purrai.points_schemas import PointsAdjustment, PointsIdentity, PointsImportInput
from purrai.process_lock import ProcessLock


def add_points_parser(sub):
    parser = sub.add_parser("points", help="本地积分账本（不连接 Telegram）")
    parser.add_argument("--group", required=True, type=int, help="群数字 ID")
    commands = parser.add_subparsers(dest="points_command", required=True)
    importing = commands.add_parser("import", help="导入 JSON 历史积分，批次防重复")
    importing.add_argument("file", type=Path)
    importing.add_argument("--preview", action="store_true")
    listing = commands.add_parser("list", help="排行榜与汇总")
    listing.add_argument("--page", type=int, default=1)
    listing.add_argument("--query", default="")
    history = commands.add_parser("history", help="查看积分流水")
    history.add_argument("account")
    for name in ("adjust", "bind"):
        change = commands.add_parser(name, help="调整积分" if name == "adjust" else "绑定或纠正身份")
        change.add_argument("account")
        change.add_argument("--version", type=int, required=True)
        change.add_argument("--reason", required=True)
        change.add_argument("--request-id", required=True)
        if name == "adjust":
            change.add_argument("--delta", type=int, required=True)
        else:
            change.add_argument("--user-id", type=int)
            change.add_argument("--name", required=True)


def run_points(args, settings):
    async def run():
        db = Database(settings.database_url)
        lock = ProcessLock(settings.database_url)
        try:
            if not (await db.schema_health())["ready"]:
                raise ManagementError(409, "请先执行数据库迁移")
            ledger = PointsLedger(db)
            action = args.points_command
            if action in ("import", "adjust", "bind"):
                lock.acquire()
            if action == "import":
                body = PointsImportInput.model_validate_json(args.file.read_text(encoding="utf-8-sig"))
                result = await ledger.import_data(args.group, body, preview=args.preview)
            elif action == "list":
                if args.page < 1:
                    raise ValueError("页码必须大于零")
                result = await ledger.page(args.group, query=args.query, page_number=args.page)
            elif action == "history":
                result = await ledger.history(args.group, args.account)
            else:
                if not 8 <= len(args.request_id) <= 80:
                    raise ValueError("请求编号长度须为 8 至 80")
                common = {"reason": args.reason, "version": args.version}
                body = (PointsIdentity(**common, user_id=args.user_id, display_name=args.name) if action == "bind"
                        else PointsAdjustment(**common, delta=args.delta))
                result = await ledger.change(args.group, args.account, body, 0, args.request_id, identity=action == "bind")
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            lock.release()
            await db.close()
    try:
        asyncio.run(run())
    except (ManagementError, ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1) from None
