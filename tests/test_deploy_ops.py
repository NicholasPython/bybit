import plistlib
import runpy
import sys
from pathlib import Path

WATCH = runpy.run_path(str(Path(__file__).parents[1] / "deploy" / "watch_nas.py"))
INSTALL = runpy.run_path(str(Path(__file__).parents[1] / "deploy" / "install_watch.py"))


def test_independent_monitor_three_misses_one_alert_and_recovery():
    notices = []

    def send(message):
        notices.append(message)
        return True

    state = {}
    for now in (1000, 1030):
        state = WATCH["check_once"](state, healthy=False, backup_at=900, now=now, send=send)
        assert not notices
    for now in (1060, 1090, 1120):
        state = WATCH["check_once"](state, healthy=False, backup_at=900, now=now, send=send)
    assert len(notices) == 1
    state = WATCH["check_once"](state, healthy=True, backup_at=900, now=1150, send=send)
    assert len(notices) == 2
    assert state["consecutive_failures"] == 0
    assert not state["down_notified"]


def test_offsite_backup_staleness_is_independent_of_health():
    notices = []
    state = WATCH["check_once"](
        {},
        healthy=True,
        backup_at=1,
        now=90000,
        send=lambda message: notices.append(message) or True,
    )
    assert state["backup_overdue"]
    assert len(notices) == 1
    state = WATCH["check_once"](
        state,
        healthy=True,
        backup_at=90000,
        now=90001,
        send=lambda message: notices.append(message) or True,
    )
    assert not state["backup_overdue"]
    assert not state["backup_notified"]


def test_backup_age_uses_dump_time_instead_of_copy_time(tmp_path):
    path = tmp_path / "bybitbot-20261031T105346Z.dump.gpg"
    path.write_bytes(b"synthetic-encrypted-bytes")
    assert WATCH["newest_backup"](tmp_path) == 1793444026


def test_installer_keeps_jobs_outside_protected_desktop(tmp_path):
    directory = tmp_path / "monitor"
    agents = tmp_path / "agents"
    paths = INSTALL["install"](
        directory, agents, Path(sys.executable), health_url="https://example.invalid/health"
    )
    assert len(paths) == 2
    for path in paths:
        definition = plistlib.loads(path.read_bytes())
        arguments = definition["ProgramArguments"]
        # launchd 无法可靠访问受系统隐私保护的目录，脚本必须装在标准位置。
        assert str(directory) in " ".join(arguments)
        assert "Desktop" not in " ".join(arguments)
        assert "example.invalid" in " ".join(arguments)
        assert definition["RunAtLoad"] is True
        assert definition["StartInterval"] in {30, 1800}
        assert path.stat().st_mode & 0o077 == 0
    assert (directory / "watch_nas.py").exists()
    assert (directory / "state").stat().st_mode & 0o077 == 0
