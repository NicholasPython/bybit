"""Private, revision-checked edits to the top-level spread YAML section only."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.nodes import MappingNode, ScalarNode, SequenceNode
from yaml.tokens import AliasToken, AnchorToken, BlockEndToken, DocumentEndToken, StreamEndToken

from bybitbot.settings.loader import AppConfig, ConfigError, load_config
from bybitbot.settings.models import Environment, SpreadSettings

#: 模型级校验无法定位到具体字段时，用消息前缀补一个安全字段名。
_FIELD_HINTS: tuple[tuple[str, str], ...] = (
    ("必须满足 z_exit < z_entry < z_stop", "z_exit"),
    ("配对的两条腿不能是同一个合约", "symbol_b"),
    ("pair_key 必须等于", "pair_key"),
    ("pair_key 不能重复", "pair_key"),
    ("名义资金与裸敞口上限必须使用同一币种", "max_unhedged_notional"),
    ("裸敞口上限必须小于单组名义上限", "max_unhedged_notional"),
    ("超时与恢复窗口必须为正", "passive_timeout_ms"),
    ("滑点容忍度不能为负", "max_slippage_bps"),
    ("对冲系数必须为正的有限数", "hedge_ratio"),
    ("Z-Score 阈值必须为非负数", "z_entry"),
    ("套利阈值必须为有限数", "z_entry"),
    ("自动信号阈值必须为有限数", "window"),
    ("影子盘阈值必须为有限数", "observation_days"),
)
#: 任何形似连接串的片段都不得出现在面向用户的错误里。
_SECRET_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+")
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


class ShadowConfigError(ValueError):
    """A deliberately public error, with no configuration values or paths."""


class RevisionConflict(ShadowConfigError):
    pass


class ReadOnlyConfig(ShadowConfigError):
    pass


def _safe_message(message: str) -> str:
    """把校验消息压成不含任何提交值的形式。"""
    text = str(message).replace("Value error, ", "").strip()
    if "：" in text:
        # 我们自己的消息用全角冒号引出被回显的值；只保留冒号前的说明。
        text = text.split("：", 1)[0]
        text = _QUOTED.sub("", text)
    text = _SECRET_PATTERN.sub("[已隐藏]", text)
    return text.strip()


def _path(parts: tuple[Any, ...], allowed: set[str] | None) -> str:
    """拼出 ``pairs[0].z_exit`` 形式的路径；未知字段名一律隐藏。"""
    path = ""
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        elif allowed is None or part in allowed:
            path = f"{path}.{part}" if path else str(part)
        else:
            return ""
    return path


def field_errors(exc: ValidationError, *, allowed: set[str] | None = None) -> list[str]:
    """把 pydantic 错误转成 ``路径: 说明``，只含字段路径与安全消息。"""
    problems: list[str] = []
    for error in exc.errors():
        loc = tuple(error.get("loc", ()))
        if error.get("type") == "extra_forbidden":
            problems.append("请求字段不被接受")
            continue
        message = _safe_message(error.get("msg", ""))
        path = _path(loc, allowed)
        if not path:
            problems.append(message or "请求字段无效")
            continue
        if len(loc) == 1 or (len(loc) == 2 and isinstance(loc[1], int)):
            # 模型级校验只给出模型位置；用安全前缀补上具体字段。
            for prefix, field in _FIELD_HINTS:
                if message.startswith(prefix):
                    path = f"{path}.{field}"
                    break
        problems.append(f"{path}: {message}" if message else path)
    return problems


@dataclass(frozen=True)
class SavedConfig:
    config: AppConfig
    revision: str
    writable: bool
    read_only_reason: str | None = None

    def public(self) -> dict[str, Any]:
        spread = self.config.spread
        return {
            "revision": self.revision,
            "writable": self.writable,
            "read_only_reason": self.read_only_reason,
            "shadow": spread.shadow.model_dump(mode="json"),
            "auto": spread.auto.model_dump(mode="json"),
            "pairs": [pair.model_dump(mode="json") for pair in spread.pairs],
            "poll_seconds": self.config.runtime.poll_seconds,
        }


def _revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(path: Path) -> bytes:
    # O_NOFOLLOW also closes the lstat/open symlink race for the file itself.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ShadowConfigError("配置必须是普通文件。")
        return stream.read()


def _document(data: bytes) -> tuple[str, dict[str, Any], tuple[int, int] | None]:
    """Locate the section without round-tripping text belonging to other sections."""
    try:
        text = data.decode("utf-8")
        tokens = list(yaml.scan(text))
        if any(isinstance(token, (AliasToken, AnchorToken)) for token in tokens):
            raise ShadowConfigError("配置含 YAML 锚点或别名，不能安全编辑。")
        root = yaml.compose(text, Loader=yaml.SafeLoader)
        if not isinstance(root, MappingNode) or root.flow_style:
            raise ShadowConfigError("配置顶层必须是块映射。")

        def check(node: Any) -> None:
            if isinstance(node, MappingNode):
                keys = set()
                for key, value in node.value:
                    if not isinstance(key, ScalarNode) or key.tag != "tag:yaml.org,2002:str":
                        raise ShadowConfigError("配置映射键必须是字符串。")
                    if key.value in keys:
                        raise ShadowConfigError("配置含重复 YAML 键，不能安全编辑。")
                    keys.add(key.value)
                    check(value)
            elif isinstance(node, SequenceNode):
                for value in node.value:
                    check(value)

        check(root)
        span = None
        for key, value in root.value:
            if key.value != "spread":
                continue
            if key.start_mark.column != 0:
                raise ShadowConfigError("spread 必须是独立的顶层配置段。")
            # Mapping end marks consume following comments. Use the last actual
            # token instead, retaining every trailing comment outside the section.
            ends = [
                token.end_mark.index
                for token in tokens
                if key.start_mark.index <= token.start_mark.index < value.end_mark.index
                and not isinstance(token, (BlockEndToken, DocumentEndToken, StreamEndToken))
            ]
            end = max(ends)
            line_end = text.find("\n", end)
            # Block scalars may already end at the beginning of the next line.
            if end > 0 and text[end - 1] == "\n":
                line_end = end - 1
            span = (key.start_mark.index, len(text) if line_end < 0 else line_end + 1)
        raw = yaml.safe_load(text)
        return text, raw, span
    except (UnicodeError, yaml.YAMLError):
        raise ShadowConfigError("配置不是可安全编辑的 UTF-8 YAML。") from None


@contextmanager
def _private_file(path: Path, data: bytes, *, prefix: str):
    fd, name = tempfile.mkstemp(prefix=prefix, dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        yield temporary
    finally:
        temporary.unlink(missing_ok=True)


def _validate(path: Path, env_override: Environment | str | None) -> AppConfig:
    try:
        return load_config(path, env_override=env_override)
    except (ConfigError, ValueError, TypeError, KeyError):
        # ConfigError includes raw Pydantic input, possibly an unrelated DSN.
        raise ShadowConfigError("整份配置校验失败；请在服务器侧检查配置。") from None


def _read_only_reason(config: AppConfig) -> str | None:
    if config.active_environment is not Environment.PAPER:
        return "仅 paper 环境允许编辑影子盘配置。"
    if config.spread.enabled or config.spread.auto.enabled:
        return "执行面 spread.enabled / auto.enabled 必须先在服务器侧关闭。"
    return None


def read_shadow_config(
    path: Path | str, *, env_override: Environment | str | None = None
) -> SavedConfig:
    path = Path(path)
    data = _read(path)
    _document(data)
    # Validate the exact bytes that were read, even during an atomic replacement.
    with _private_file(path, data, prefix=".shadow-read-") as candidate:
        config = _validate(candidate, env_override)
    reason = _read_only_reason(config)
    if reason is None and not os.access(path.parent, os.W_OK | os.X_OK):
        reason = "配置目录不可写；请在服务器侧维护配置。"
    return SavedConfig(config, _revision(data), reason is None, reason)


@contextmanager
def _locked(path: Path):
    # Never unlink the lock: all writers must lock the same stable inode.
    lock = path.with_name(path.name + ".shadow.lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
            raise ShadowConfigError("配置锁不是安全的普通文件。")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def write_spread(
    path: Path | str,
    changes: dict[str, Any],
    *,
    revision: str,
    env_override: Environment | str | None = None,
) -> dict[str, Any]:
    """Merge form fields, validate a private candidate, back up, then replace."""
    path = Path(path)
    with _locked(path):
        original = _read(path)
        original_mode = stat.S_IMODE(path.stat().st_mode)
        if _revision(original) != revision:
            raise RevisionConflict("配置已被修改，请重新加载后再保存。")
        text, raw, span = _document(original)
        with _private_file(path, original, prefix=".shadow-read-") as current:
            config = _validate(current, env_override)
        reason = _read_only_reason(config)
        if reason:
            raise ReadOnlyConfig(reason)
        spread = copy.deepcopy(raw.get("spread") or {})
        existing = {pair["pair_key"]: pair for pair in spread.get("pairs", [])}
        spread["pairs"] = [
            {**existing.get(pair["pair_key"], {"enabled": False}), **pair}
            for pair in changes["pairs"]
        ]
        spread["auto"] = {**(spread.get("auto") or {}), **changes["auto"]}
        spread["shadow"] = {**(spread.get("shadow") or {}), **changes["shadow"]}
        try:
            validated = SpreadSettings.model_validate(spread)
        except ValidationError as exc:
            problems = field_errors(exc)
            raise ShadowConfigError(
                "配置无效：" + "；".join(problems[:6]) if problems else "配置无效。"
            ) from None
        if validated.shadow.enabled and not validated.pairs:
            raise ShadowConfigError("shadow.enabled: 开启时至少需要一个配对。")
        if validated.auto.sample_interval_seconds < config.runtime.poll_seconds:
            raise ShadowConfigError(
                f"auto.sample_interval_seconds: 不能短于 runtime.poll_seconds"
                f"（{config.runtime.poll_seconds} 秒）。"
            )
        section = yaml.safe_dump({"spread": spread}, allow_unicode=True, sort_keys=False)
        if span is None:
            # Insert before document-end markers, preserving all original text.
            root = yaml.compose(text, Loader=yaml.SafeLoader)
            insertion = root.end_mark.index
            section = ("\n" if insertion and text[insertion - 1] != "\n" else "") + section
            candidate_text = text[:insertion] + section + text[insertion:]
        else:
            candidate_text = text[: span[0]] + section + text[span[1] :]
        candidate_data = candidate_text.encode("utf-8")
        with _private_file(path, candidate_data, prefix=".shadow-write-") as candidate:
            _validate(candidate, env_override)
            # Also detect non-cooperating external editors before installation.
            if _read(path) != original:
                raise RevisionConflict("配置已被修改，请重新加载后再保存。")
            backup_fd, backup_name = tempfile.mkstemp(
                prefix=path.name + ".bak-shadow-", dir=path.parent
            )
            backup = Path(backup_name)
            try:
                with os.fdopen(backup_fd, "wb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
                candidate.chmod(original_mode)
                os.replace(candidate, path)
            except BaseException:
                backup.unlink(missing_ok=True)
                raise
        return {"revision": _revision(candidate_data), "backup": backup.name}
