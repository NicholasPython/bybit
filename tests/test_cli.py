import hashlib
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


def test_admin_password_file_has_no_trailing_newline(tmp_path):
    """密码文件不能带结尾换行。

    从文件里整行复制到登录框时，换行会变成密码的一部分；服务端按字面
    比对哈希，于是正确密码也会被判成"用户名或密码错误"。
    """
    auth = tmp_path / "admin.json"
    password = tmp_path / "admin-password.txt"
    init_admin(auth, password_file=password)

    raw = password.read_bytes()
    assert not raw.endswith(b"\n")
    assert raw == raw.strip()

    # 文件内容必须能直接通过 admin.json 的哈希校验。
    data = json.loads(auth.read_text())
    algorithm, rounds, salt_hex, digest = data["password_hash"].split("$")
    assert algorithm == "pbkdf2_sha256"
    computed = hashlib.pbkdf2_hmac(
        "sha256", raw, bytes.fromhex(salt_hex), int(rounds)
    ).hex()
    assert computed == digest
