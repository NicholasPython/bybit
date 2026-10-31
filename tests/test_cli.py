import json
import stat

import pytest

from bybitbot.cli import init_admin


def test_admin_generation_permissions_no_overwrite(tmp_path, capsys):
    auth = tmp_path / "admin.json"
    password = tmp_path / "admin-password.txt"
    init_admin(auth, password_file=password)
    data = json.loads(auth.read_text())
    assert data["password_hash"].startswith("pbkdf2_sha256$600000$")
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600
    assert stat.S_IMODE(password.stat().st_mode) == 0o600
    assert password.read_text().strip() not in capsys.readouterr().out
    with pytest.raises(RuntimeError, match="已存在"):
        init_admin(auth, password_file=password)
