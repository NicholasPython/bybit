from __future__ import annotations

import datetime as dt
import json

import httpx

from bybitbot.clock import now_utc
from bybitbot.db.models import Alert
from bybitbot.ops.alerts import AlertManager
from bybitbot.ops.notifications import deliver_alerts


async def test_delivery_retry_dedupe_and_resolution(database, tmp_path):
    manager = AlertManager(database.sessionmaker, "paper")
    file = tmp_path / "notification.json"
    file.write_text(json.dumps({"url": "https://example.invalid/private-notification-token"}))
    file.chmod(0o600)
    alert = await manager.raise_alert(alert_key="test", severity="CRITICAL", title="测试")
    calls = []

    def fail(request):
        calls.append(request)
        return httpx.Response(503)

    await deliver_alerts(manager, (str(file),), transport=httpx.MockTransport(fail))
    async with database.sessionmaker() as session:
        current = await session.get(Alert, alert.id)
        assert current.delivery_attempts == 1
        assert "private-notification-token" not in current.last_delivery_error
        current.context = {
            **current.context,
            "last_delivery_at": (now_utc() - dt.timedelta(seconds=10)).isoformat(),
        }
        await session.commit()
    await deliver_alerts(
        manager, (str(file),), transport=httpx.MockTransport(lambda request: httpx.Response(200))
    )
    assert not await manager.undelivered()
    repeat = await manager.raise_alert(alert_key="test", severity="CRITICAL", title="测试")
    assert repeat.first_seen_at == alert.first_seen_at
    assert repeat.status == "DELIVERED"
    await manager.resolve("test")
    reopened = await manager.raise_alert(alert_key="test", severity="CRITICAL", title="测试")
    assert reopened.status == "ACTIVE"
    assert reopened.context["delivered_channels"] == {}
    assert len(reopened.context["history"]) == 2
