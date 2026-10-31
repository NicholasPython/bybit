"""受限文件注入通知目的地；重试状态落库，不把 webhook 密钥存入业务表。"""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from bybitbot.clock import now_utc
from bybitbot.db.models import Alert


def endpoint_from_file(reference: str) -> str:
    path = Path(reference)
    info = path.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_mode & 0o077
        or info.st_uid not in {0, os.getuid()}
    ):
        raise ValueError("通知配置必须是当前用户拥有的受限文件")
    data = json.loads(path.read_text())
    address = data.get("url", "")
    parts = urlsplit(address)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("通知目的地必须为 HTTPS")
    return address


async def deliver_alerts(manager, channels: tuple[str, ...], *, transport=None) -> None:
    for alert in await manager.undelivered():
        context = dict(alert.context or {})
        previous = context.get("last_delivery_at")
        if previous:
            delay = min(300, 2 ** min(alert.delivery_attempts, 8))
            if (now_utc() - dt.datetime.fromisoformat(previous)).total_seconds() < delay:
                continue
        if not channels:
            continue
        sent = dict(context.get("delivered_channels", {}))
        failures = []
        async with httpx.AsyncClient(timeout=3, transport=transport, trust_env=False) as client:
            for reference in channels:
                if sent.get(reference):
                    continue
                try:
                    endpoint = endpoint_from_file(reference)
                    response = await client.post(
                        endpoint,
                        json={
                            "id": alert.id,
                            "environment": manager.environment,
                            "severity": alert.severity,
                            "title": alert.title,
                            "detail": alert.detail,
                            "first_seen_at": alert.first_seen_at.isoformat(),
                        },
                    )
                    response.raise_for_status()
                    sent[reference] = now_utc().isoformat()
                except Exception as exc:
                    # 异常消息可能包含 webhook URL，持久化仅保留异常类型。
                    failures.append(type(exc).__name__)
        async with manager.sessionmaker() as session:
            current = await session.get(Alert, alert.id)
            current.context = {**(current.context or {}), "delivered_channels": sent}
            await session.commit()
        if failures:
            await manager.mark_delivery_failed(alert.id, ",".join(failures))
        else:
            await manager.mark_delivered(alert.id)
