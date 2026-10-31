"""凭据装载与脱敏 —— SEC-01。

约束（需求文档 SEC-01）：
    密钥仅供交易适配器使用，通过受限配置文件或密钥服务注入；
    不进 Git、镜像、业务表、URL 或日志。
    启动只记录脱敏身份和权限摘要。

因此：
    * 凭据文件权限必须不超过 0600，且必须由当前用户拥有；
    * :class:`ApiCredentials` 不实现 ``__repr__``/``__str__`` 输出明文，
      避免被 ``logging`` 的 ``%s`` 意外打出来；
    * 纸面（paper）环境根本不打开凭据文件（ENV-01）。
"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class CredentialError(Exception):
    """凭据缺失或权限不合规。"""


@dataclass(frozen=True)
class ApiCredentials:
    """Bybit API 凭据。

    只提供 ``masked_summary()`` 给日志用；不提供任何会泄漏明文的
    ``__repr__``/``__str__``。
    """

    api_key: str
    api_secret: str
    #: 可选：demo / testnet 环境可能使用独立的 key。
    label: str = "default"

    def __repr__(self) -> str:
        return f"ApiCredentials(key={self.api_key[:6]}***, label={self.label!r})"

    __str__ = __repr__

    def masked_summary(self) -> dict[str, str]:
        """脱敏身份摘要，供启动日志与 OPS-01 展示（SEC-01）。"""
        return {
            "api_key_prefix": f"{self.api_key[:6]}***",
            "label": self.label,
        }


def load_credentials(path: Path | str, *, require_permissions: bool = True) -> ApiCredentials:
    """从受限文件读取凭据。

    Args:
        path: JSON 文件路径，含 ``api_key`` 与 ``api_secret``。
        require_permissions: 是否检查文件权限（生产环境应保持 True）。

    Raises:
        CredentialError: 文件不存在、权限过宽或字段缺失。
    """
    cred_path = Path(path)
    if not cred_path.exists():
        raise CredentialError(f"凭据文件不存在：{cred_path}")

    if require_permissions:
        st = cred_path.stat()
        mode = stat.S_IMODE(st.st_mode)
        if mode & 0o077:
            raise CredentialError(f"凭据文件权限过宽：{cred_path} 是 {mode:04o}，应收紧到 0600")
        if st.st_uid != os.getuid():
            raise CredentialError(f"凭据文件不属于当前用户：{cred_path}（uid={st.st_uid}）")

    try:
        data = json.loads(cred_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialError(f"凭据文件无法读取：{cred_path}：{exc}") from exc

    api_key = data.get("api_key")
    api_secret = data.get("api_secret")
    if not api_key or not api_secret:
        raise CredentialError(f"凭据文件缺少 api_key / api_secret：{cred_path}")

    creds = ApiCredentials(
        api_key=str(api_key),
        api_secret=str(api_secret),
        label=str(data.get("label") or cred_path.stem),
    )
    logger.info("已装载凭据（脱敏）：%s", creds.masked_summary())
    return creds


def redact(text: str, secrets: list[str]) -> str:
    """把已知的明文密钥从任意文本里抹掉。

    用于日志与导出的兜底脱敏（SEC-02：日志及导出内容脱敏）。
    """
    if not text:
        return text
    out = text
    for secret in secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "***REDACTED***")
    return out


__all__ = ["ApiCredentials", "CredentialError", "load_credentials", "redact"]
