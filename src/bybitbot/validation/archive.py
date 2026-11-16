"""Self-contained validation evidence, integrity checks and safe archive output.

Source manifests contain raw UTF-8 text and its byte hash, independently of the
protocol. Export checks the frozen evidence; ``verify_sources`` separately checks
that the current product checkout still matches it before a run can continue.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import re
import secrets
import stat
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath

GENESIS_HASH = "0" * 64
_EVENT_FIELDS = ("sequence", "kind", "dedupe_key", "occurred_ms", "payload")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def json_value(value):
    """Use the sampling module's conversion without importing its DB dependencies."""
    return json.loads(
        json.dumps(
            value,
            default=lambda obj: obj.isoformat() if isinstance(obj, dt.datetime) else str(obj),
            sort_keys=True,
            allow_nan=False,
        )
    )


def _json_bytes(value) -> bytes:
    return json.dumps(
        json_value(value), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(value) -> str:
    """SHA256 with exactly the same canonical JSON rules as sampling.digest."""
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _directory_fd(path: Path) -> int:
    """Open an existing directory without following any symlink component."""
    path = Path(os.path.abspath(path))
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _source_paths(root: Path) -> set[str]:
    product = root / "src" / "bybitbot"
    # Check ancestors even if a linked directory would yield no Python files.
    descriptor = _directory_fd(product)
    os.close(descriptor)
    paths = {"pyproject.toml", "uv.lock"}

    def failed(error):
        raise error

    for parent, directories, files in os.walk(product, followlinks=False, onerror=failed):
        for name in directories:
            if (Path(parent) / name).is_symlink():
                raise ValueError("symlink in product sources")
        for name in files:
            if name.endswith(".py"):
                paths.add((Path(parent) / name).relative_to(root).as_posix())
    return paths


def _source_bytes(root: Path, relative: str) -> bytes:
    path = root / relative
    parent = _directory_fd(path.parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("source must be a regular file")
            return stream.read()
    finally:
        os.close(parent)


def freeze_sources() -> dict:
    """Freeze only src/bybitbot/**/*.py, pyproject.toml and uv.lock.

    ``_project_root`` is deliberately a small helper so tests can supply an
    isolated product tree. No working-directory, research or credential lookup
    participates in discovering the files.
    """
    try:
        root = _project_root()
        sources = {}
        for relative in sorted(_source_paths(root)):
            raw = _source_bytes(root, relative)
            sources[relative] = {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "content": raw.decode("utf-8"),
            }
        return {"sources": sources}
    except (OSError, UnicodeError) as exc:
        raise ValueError("cannot freeze product sources") from exc


def _is_hash(value) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _valid_source_path(path) -> bool:
    if not isinstance(path, str) or "\\" in path or "\x00" in path:
        return False
    parts = PurePosixPath(path).parts
    if not parts or PurePosixPath(path).as_posix() != path or ".." in parts:
        return False
    return path in {"pyproject.toml", "uv.lock"} or (
        len(parts) >= 3 and parts[:2] == ("src", "bybitbot") and path.endswith(".py")
    )


def _manifest_sources(manifest: dict) -> dict:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("sources"), dict):
        raise ValueError("malformed source manifest")
    sources = manifest["sources"]
    if not {"pyproject.toml", "uv.lock"} <= sources.keys():
        raise ValueError("missing required frozen sources")
    if not any(isinstance(path, str) and path.startswith("src/bybitbot/") for path in sources):
        raise ValueError("missing frozen product sources")
    for path, source in sources.items():
        if not _valid_source_path(path) or not isinstance(source, dict):
            raise ValueError("malformed source path or entry")
        content, expected = source.get("content"), source.get("sha256")
        if not isinstance(content, str) or not _is_hash(expected):
            raise ValueError("malformed frozen source")
        try:
            actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
        except UnicodeError as exc:
            raise ValueError("invalid UTF8 frozen source") from exc
        if actual != expected:
            raise ValueError(f"frozen source hash mismatch: {path}")
    return sources


def verify_sources(manifest: dict) -> None:
    """Reject malformed manifests and deleted, modified or newly added sources."""
    sources = _manifest_sources(manifest)
    try:
        root = _project_root()
        if _source_paths(root) != sources.keys():
            raise ValueError("product source set changed")
        for relative, source in sources.items():
            if _source_bytes(root, relative) != source["content"].encode("utf-8"):
                raise ValueError(f"product source changed: {relative}")
    except OSError as exc:
        raise ValueError("missing or unsafe product source") from exc


def event_hash(previous_hash: str, event: dict) -> str:
    """Hash the sequence and event body, excluding DB ids and stored hash fields."""
    if not _is_hash(previous_hash) or not isinstance(event, dict):
        raise ValueError("malformed event or previous hash")
    if any(field not in event for field in _EVENT_FIELDS):
        raise ValueError("missing event body fields")
    if type(event["sequence"]) is not int or event["sequence"] < 1:
        raise ValueError("invalid event sequence")
    if type(event["occurred_ms"]) is not int or event["occurred_ms"] < 0:
        raise ValueError("invalid event timestamp")
    if not all(isinstance(event[key], str) and event[key] for key in ("kind", "dedupe_key")):
        raise ValueError("invalid event identity")
    if not isinstance(event["payload"], dict):
        raise ValueError("invalid event payload")
    try:
        body = {"previous_hash": previous_hash}
        body.update({key: event[key] for key in _EVENT_FIELDS})
        return digest(body)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("malformed event JSON") from exc


def verify_chain(events: list[dict], expected_head: str) -> None:
    """Verify a complete ordered chain from sequence 1 and the all-zero genesis."""
    if not isinstance(events, list) or not _is_hash(expected_head):
        raise ValueError("malformed event chain")
    previous, seen = GENESIS_HASH, set()
    for sequence, event in enumerate(events, 1):
        if not isinstance(event, dict) or event.get("sequence") != sequence:
            raise ValueError("event sequence gap")
        if event.get("previous_hash") != previous:
            raise ValueError("event previous hash mismatch")
        actual = event_hash(previous, event)
        if event.get("event_hash") != actual:
            raise ValueError("event hash mismatch")
        if event["dedupe_key"] in seen:
            raise ValueError("duplicate event identity")
        seen.add(event["dedupe_key"])
        previous = actual
    if previous != expected_head:
        raise ValueError("event chain head mismatch")


def _nonzero_or_unknown(value) -> bool:
    try:
        amount = Decimal(str(value))
        return not amount.is_finite() or amount != 0
    except InvalidOperation:
        return True


def _summary(positions, observations, events) -> dict:
    bases = []
    for position in positions:
        state = position.get("state", {})
        if not isinstance(state, dict):
            raise ValueError("malformed position state")
        row = {key: position.get(key) for key in ("base", "phase", "cash", "fees", "reserved_risk")}
        row.update(
            long_qty=state.get("long_qty"),
            short_qty=state.get("short_qty"),
            fees_known=state.get("fees_known"),
            unknown_fees=state.get("fees_known") is not True,
            pending=state.get("pending"),
            accounting_error=state.get("accounting_error"),
        )
        row["unresolved_risk"] = (
            "unresolved" in str(row["phase"])
            or any(
                _nonzero_or_unknown(row[key])
                for key in ("long_qty", "short_qty", "reserved_risk")
            )
            or row["unknown_fees"]
            or bool(row["pending"] or row["accounting_error"] or state.get("unresolved_risk"))
        )
        bases.append(row)
    latencies = [
        {"sequence": event["sequence"], "base": event["payload"].get("base"),
         "latency_seconds": event["payload"].get("latency_seconds")}
        for event in events
        if event["kind"] == "execution" or "latency_seconds" in event["payload"]
    ]
    return {
        "bases": bases,
        "observation_count": len(observations),
        "observation_error_count": sum(row.get("status") == "error" for row in observations),
        "error_event_count": sum("error" in event["kind"] for event in events),
        "gap_count": sum(event["kind"] == "observation_gap" for event in events),
        "latencies": latencies,
        "unresolved_risk": any(row["unresolved_risk"] for row in bases),
    }


def _report(study, summary, *, final: bool) -> str:
    def cell(value):
        return json.dumps(value, ensure_ascii=False).replace("|", "\\|").replace("\n", " ")

    lines = [
        "# Final validation report" if final else "# Validation status",
        "",
        f"Run: {cell(study.get('run_id'))}",
        f"Status: {cell(study.get('status'))}",
        f"Chain head: {cell(study['chain_head'])}",
        f"Unresolved risk preserved: {str(summary['unresolved_risk']).lower()}",
        "",
        "| Base | Phase | Cash | Fees | Reserved risk | Long qty | Short qty | Unknown fees |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in summary["bases"]:
        lines.append("| " + " | ".join(cell(row[key]) for key in (
            "base",
            "phase",
            "cash",
            "fees",
            "reserved_risk",
            "long_qty",
            "short_qty",
            "unknown_fees",
        )) + " |")
    lines.extend([
        "",
        f"Observations: {summary['observation_count']}",
        f"Observation errors: {summary['observation_error_count']}",
        f"Error events: {summary['error_event_count']}",
        f"Observation gaps: {summary['gap_count']}",
        "",
        "Decision-to-fill latency seconds (null means unavailable):",
        "```json",
        json.dumps(summary["latencies"], sort_keys=True, ensure_ascii=False),
        "```",
        "",
        "Raw observations, position state and unresolved exposure are preserved in state.json.",
        "Cash is recorded cash flow, not a mark or an assumed liquidation of outstanding legs.",
        "An ended study does not imply that its exposure, pending orders or fees are resolved.",
        "",
    ])
    return "\n".join(lines)


def checkpoint_state(study: dict, positions: list[dict], observations: list[dict]) -> dict:
    """Return the journal checkpoint, excluding recursive evidence and chain head.

    All supplied study/position columns participate. Observation payloads are
    checked against snapshot_hash separately and are omitted only here, not from
    the exported state. Database row identities determine checkpoint ordering.
    """
    observation_fields = ("id", "study_id", "slot_ms", "observed_ms", "status", "snapshot_hash")
    try:
        return json_value({
            "study": {key: value for key, value in study.items()
                      if key not in {"evidence", "chain_head"}},
            "positions": sorted(positions, key=lambda row: row["id"]),
            "observations": [{key: row[key] for key in observation_fields}
                             for row in sorted(observations, key=lambda row: row["id"])],
        })
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("malformed checkpoint state") from exc


def archive_bytes(
    study: dict, positions: list[dict], observations: list[dict], events: list[dict]
) -> bytes:
    """Validate evidence and build a ZIP for authenticated HTTP delivery in memory."""
    if not isinstance(study, dict) or not isinstance(study.get("protocol"), dict):
        raise ValueError("malformed study protocol")
    if not isinstance(positions, list) or any(not isinstance(row, dict) for row in positions):
        raise ValueError("malformed positions")
    if not isinstance(observations, list):
        raise ValueError("malformed observations")
    evidence = study.get("evidence")
    sources = _manifest_sources(evidence)
    if "protocol_hash" in evidence and evidence["protocol_hash"] != digest(study["protocol"]):
        raise ValueError("protocol hash mismatch")
    verify_chain(events, study.get("chain_head"))
    for observation in observations:
        if not isinstance(observation, dict) or not isinstance(observation.get("payload"), dict):
            raise ValueError("malformed observation")
        if observation.get("snapshot_hash") != digest(observation["payload"]):
            raise ValueError("observation snapshot hash mismatch")
    # All exported structures are detached, including Decimal/datetime conversion.
    study, positions, observations, events = json_value([study, positions, observations, events])
    summary = _summary(positions, observations, events)
    checkpoint = checkpoint_state(study, positions, observations)
    checkpoint_hash = digest(checkpoint)
    if "checkpoint_hash" in evidence and evidence["checkpoint_hash"] != checkpoint_hash:
        raise ValueError("state checkpoint hash mismatch")
    checkpoints = [event for event in events if event["kind"] == "checkpoint"]
    if checkpoints and checkpoints[-1]["payload"].get("checkpoint_hash") != checkpoint_hash:
        raise ValueError("last ledger checkpoint hash mismatch")
    state = {"study": study, "positions": positions, "observations": observations,
             "checkpoint": checkpoint, "summary": summary}
    heartbeat = {key: study.get(key) for key in (
        "status", "heartbeat_ms", "last_observed_ms", "next_due_ms", "last_error", "chain_head"
    )}
    files = {
        "protocol.json": _json_bytes(study["protocol"]),
        "manifest.json": _json_bytes(evidence),
        "state.json": _json_bytes(state),
        "ledger.jsonl": b"".join(_json_bytes(event) + b"\n" for event in events),
        "heartbeat.json": _json_bytes(heartbeat),
        "STATUS.md": _report(study, summary, final=False).encode("utf-8"),
    }
    files.update({f"frozen/{path}": source["content"].encode("utf-8")
                  for path, source in sources.items()})
    if study.get("status") == "ended":
        files["FINAL_REPORT.md"] = _report(study, summary, final=True).encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            # Fixed timestamps make identical evidence produce identical archives.
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            archive.writestr(info, content)
    return output.getvalue()


def write_archive(directory, filename: str, data: bytes) -> Path:
    """Atomically publish to an existing directory; never replace differing data.

    Only a single filename is accepted. Every directory component and the target
    are opened without following symlinks; a hard-link publication provides atomic
    create-if-absent even if another writer creates the target concurrently.
    """
    if (
        not isinstance(filename, str)
        or filename in {"", ".", ".."}
        or "/" in filename or "\\" in filename or "\x00" in filename
        or not isinstance(data, bytes)
    ):
        raise ValueError("invalid archive filename or data")
    directory = Path(os.path.abspath(directory))
    # The optional disk helper must not publish back into the original records.
    research = _project_root() / "research"
    if directory == research or research in directory.parents:
        raise ValueError("archive destination must not be original research")
    parent = None
    temporary = None
    try:
        parent = _directory_fd(directory)
        try:
            target = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            pass
        else:
            with os.fdopen(target, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or stream.read() != data:
                    raise ValueError("archive target already exists with different data")
            return directory / filename
        # A random temporary name is opened relative to the pinned directory fd.
        for _ in range(100):
            candidate = f".validation-{secrets.token_hex(16)}.tmp"
            try:
                temporary_fd = os.open(
                    candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600, dir_fd=parent,
                )
            except FileExistsError:
                continue
            temporary = candidate
            break
        else:
            raise ValueError("cannot allocate archive temporary file")
        with os.fdopen(temporary_fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(
                temporary,
                filename,
                src_dir_fd=parent,
                dst_dir_fd=parent,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise ValueError("archive target appeared during publication") from exc
        os.fsync(parent)
        return directory / filename
    except OSError as exc:
        raise ValueError("unsafe or unavailable archive destination") from exc
    finally:
        if parent is not None:
            try:
                if temporary is not None:
                    os.unlink(temporary, dir_fd=parent)
            finally:
                os.close(parent)
