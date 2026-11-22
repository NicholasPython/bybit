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

    inventory = {"bybitbot_paper": {"at": 900, "path": None, "verified": True}}
    state = {}
    for now in (1000, 1030):
        state = WATCH["check_once"](
            state,
            healthy=False,
            inventory=inventory,
            expected=["bybitbot_paper"],
            now=now,
            send=send,
        )
        assert not notices
    for now in (1060, 1090, 1120):
        state = WATCH["check_once"](
            state,
            healthy=False,
            inventory=inventory,
            expected=["bybitbot_paper"],
            now=now,
            send=send,
        )
    assert len(notices) == 1
    state = WATCH["check_once"](
        state,
        healthy=True,
        inventory=inventory,
        expected=["bybitbot_paper"],
        now=1150,
        send=send,
    )
    assert len(notices) == 2
    assert state["consecutive_failures"] == 0
    assert not state["down_notified"]


def test_offsite_backup_staleness_is_independent_of_health():
    notices = []
    state = WATCH["check_once"](
        {},
        healthy=True,
        inventory={"bybitbot_paper": {"at": 1, "path": None, "verified": True}},
        expected=["bybitbot_paper"],
        now=90000,
        send=lambda message: notices.append(message) or True,
    )
    assert state["backup_problems"]
    assert len(notices) == 1
    state = WATCH["check_once"](
        state,
        healthy=True,
        inventory={"bybitbot_paper": {"at": 90000, "path": None, "verified": True}},
        expected=["bybitbot_paper"],
        now=90001,
        send=lambda message: notices.append(message) or True,
    )
    assert not state["backup_problems"]
    assert not state["backup_notified"]


def test_backup_problems_cover_missing_stale_and_unverified():
    now = 1_000_000.0
    inventory = {
        "good": {"at": now, "path": None, "verified": True},
        "stale": {"at": now - 90000, "path": None, "verified": True},
        "broken": {"at": now, "path": None, "verified": False},
    }
    problems = WATCH["backup_problems"](inventory, ["good", "stale", "broken", "absent"], now)
    assert any("absent" in problem for problem in problems)
    assert any("stale" in problem for problem in problems)
    assert any("broken" in problem for problem in problems)
    assert not any(problem.startswith("good") for problem in problems)


def test_backup_inventory_is_per_database_and_reports_verification(tmp_path):
    path = tmp_path / "bybitbot-20261031T105346Z-bybitbot_paper.dump.gpg"
    path.write_bytes(b"synthetic-encrypted-bytes")
    inventory = WATCH["backup_inventory"](tmp_path, verify=False)
    assert inventory["bybitbot_paper"]["at"] == 1793444026
    assert not inventory["bybitbot_paper"]["verified"]
    # 每个库各算一份，不允许用一个库的时间戳覆盖另一个库。
    other = tmp_path / "bybitbot-20261118T023318Z-bybitbot_live.dump.gpg"
    other.write_bytes(b"synthetic-encrypted-bytes")
    inventory = WATCH["backup_inventory"](tmp_path, verify=False)
    assert set(inventory) == {"bybitbot_paper", "bybitbot_live"}
    assert inventory["bybitbot_live"]["at"] > inventory["bybitbot_paper"]["at"]


def test_backup_file_name_parsing():
    stamp, database = WATCH["parse_backup_name"]("bybitbot-20261031T105346Z-bybitbot_live.dump.gpg")
    assert database == "bybitbot_live"
    assert stamp.isoformat() == "2026-10-31T10:53:46+00:00"
    assert WATCH["parse_backup_name"]("not-a-backup.txt") == (None, None)


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
