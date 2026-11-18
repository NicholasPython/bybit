"""命令行：分离 worker/web/迁移/凭据初始化（ENV-01 / SEC-01）。"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import logging
import os
import secrets
import signal
from pathlib import Path

from bybitbot.db.session import Database
from bybitbot.settings.loader import load_config


def init_admin(path: Path, *, password_file: Path | None = None, username: str = "admin"):
    if path.exists():
        raise RuntimeError("管理凭据文件已存在，拒绝覆盖；轮换请指定新的路径")
    if password_file and password_file.exists():
        raise RuntimeError("密码输出文件已存在，拒绝覆盖")
    password = secrets.token_urlsafe(24) if password_file else getpass.getpass("新的管理密码：")
    if len(password) < 14:
        raise RuntimeError("管理密码至少 14 个字符")
    if not password_file and password != getpass.getpass("再次输入："):
        raise RuntimeError("两次密码不一致")
    salt = secrets.token_bytes(32)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000).hex()
    auth = {
        "username": username,
        "password_hash": f"pbkdf2_sha256$600000${salt.hex()}${digest}",
        "session_secret": secrets.token_urlsafe(48),
    }
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        json.dump(auth, stream)
    if password_file:
        password_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(
            password_file, "x", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)
        ) as stream:
            # 不写结尾换行：从文件里整行复制到登录框时，换行会被当成密码的一部分，
            # 服务端按字面比对，于是报"用户名或密码错误"。
            stream.write(password)
    print(f"管理身份已创建：{path}；密码未输出到日志")


async def worker(config_path):
    from bybitbot.service import TradingService

    config = load_config(config_path)
    database = Database(config.profile.db_dsn)
    service = TradingService(config, database, config_path=config_path)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, service.stop_event.set)
    try:
        await service.run()
    finally:
        await database.dispose()


def main():
    parser = argparse.ArgumentParser(description="Bybit 现货与单腿期权运行服务")
    parser.add_argument("--config", default=os.environ.get("BYBITBOT_CONFIG", "config/config.yaml"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="运行唯一交易 worker")
    sub.add_parser("check", help="验证配置，输出无密摘要")
    sub.add_parser("migrate", help="应用数据库版本迁移")
    web = sub.add_parser("web", help="管理页，只读查询及控制命令入队")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=18080)
    web.add_argument("--auth", default=os.environ.get("BYBITBOT_ADMIN_AUTH", "secrets/admin.json"))
    web.add_argument(
        "--insecure-local-cookie",
        action="store_true",
        help="仅本机 HTTP 开发时允许不带 Secure 的 Cookie",
    )
    admin = sub.add_parser("init-admin", help="创建 PBKDF2 管理身份")
    admin.add_argument("--path", type=Path, default=Path("secrets/admin.json"))
    admin.add_argument("--username", default="admin")
    admin.add_argument("--generate-password-file", type=Path)
    sandbox = sub.add_parser("sandbox", help="建立或验收本地隔离 paper 系统，不访问交易所")
    sandbox.add_argument("action", choices=("init", "verify"))
    sandbox.add_argument("--directory", type=Path, default=Path("var/paper-system"))
    sandbox.add_argument("--dsn", default=os.environ.get("BYBITBOT_SANDBOX_DSN"))
    sandbox.add_argument("--template", type=Path, default=Path("config/config.example.yaml"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if args.command == "init-admin":
        init_admin(args.path, password_file=args.generate_password_file, username=args.username)
        return
    if args.command == "sandbox":
        from bybitbot.sandbox import initialize, verify

        if args.action == "init":
            if not args.dsn:
                parser.error("sandbox init 需要 --dsn 或 BYBITBOT_SANDBOX_DSN（本机 PostgreSQL）")
            result = asyncio.run(initialize(args.directory, dsn=args.dsn, template=args.template))
        else:
            result = asyncio.run(verify(args.directory))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    config = load_config(args.config)
    if args.command == "check":
        print(
            json.dumps(
                {
                    "environment": config.active_environment.value,
                    "strategies": [
                        {"name": s.name, "enabled": s.enabled} for s in config.strategies
                    ],
                    "live_allowed": config.live_gates.live_allowed,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "migrate":
        from alembic.config import Config

        from alembic import command

        os.environ["BYBITBOT_DB_DSN"] = config.profile.db_dsn
        command.upgrade(Config("alembic.ini"), "head")
    elif args.command == "run":
        asyncio.run(worker(args.config))
    elif args.command == "web":
        import uvicorn

        from bybitbot.web.app import create_app

        if args.host != "127.0.0.1":
            raise RuntimeError("管理页仅绑定本机；使用 Tailscale Serve 提供 HTTPS")
        application = create_app(
            config,
            Database(config.profile.db_dsn),
            args.auth,
            secure_cookies=not args.insecure_local_cookie,
        )
        uvicorn.run(
            application,
            host=args.host,
            port=args.port,
            access_log=False,
            forwarded_allow_ips="127.0.0.1",
        )


if __name__ == "__main__":
    main()
