"""Pure evidence integrity and filesystem export, without DB or exchange access."""

import copy
import datetime as dt
import hashlib
import io
import json
import os
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

from bybitbot.validation import archive
from bybitbot.validation.sampling import digest as sampling_digest


@pytest.fixture
def product(tmp_path, monkeypatch):
    root = tmp_path / "product"
    (root / "src/bybitbot/validation").mkdir(parents=True)
    files = {
        "src/bybitbot/__init__.py": b"# product\r\n",
        "src/bybitbot/validation/example.py": "VALUE = '冻结'\n".encode(),
        "pyproject.toml": b"[project]\nname = 'example'\n",
        "uv.lock": b"version = 1\n",
    }
    for relative, data in files.items():
        (root / relative).write_bytes(data)
    (root / "research").mkdir()
    (root / "research/record.py").write_text("original research must remain untouched\n")
    (root / ".env").write_text("not a product source\n")
    (root / "src/bybitbot/irrelevant.json").write_text("{}\n")
    monkeypatch.setattr(archive, "_project_root", lambda: root)
    return root, files


def append_event(events, kind, payload):
    previous = events[-1]["event_hash"] if events else archive.GENESIS_HASH
    row = {
        "id": len(events) + 10,
        "study_id": 1,
        "sequence": len(events) + 1,
        "previous_hash": previous,
        "kind": kind,
        "dedupe_key": f"{kind}:{len(events) + 1}",
        "occurred_ms": 1000 + len(events),
        "payload": payload,
    }
    row["event_hash"] = archive.event_hash(previous, row)
    events.append(row)
    return row


@pytest.fixture
def evidence(product):
    study = {
        "id": 1,
        "run_id": "cohort",
        "environment": "paper",
        "account_uid": "test-account",
        "policy": "strong_up",
        "direction": "Call",
        "protocol": {"sampling_seconds": 900, "budget": "25"},
        "bases": ["BTC", "ETH"],
        "status": "ended",
        "started_ms": 1000,
        "end_ms": 5000,
        "heartbeat_ms": 5001,
        "last_observed_ms": 4000,
        "next_due_ms": 900000,
        "last_error": "sample_failed:TimeoutError",
        "evidence": archive.freeze_sources(),
    }
    positions = [
        {
            "id": 2, "study_id": 1, "base": "ETH", "phase": "closed", "cash": "2.5",
            "fees": "0.1", "reserved_risk": "0",
            "state": {"long_qty": "0", "short_qty": "0", "fees_known": True},
        },
        {
            "id": 1, "study_id": 1, "base": "BTC", "phase": "study_ended_unresolved",
            "cash": "-8.4", "fees": "0.2", "reserved_risk": "10",
            "state": {
                "long_qty": "0.01", "short_qty": "0.005", "fees_known": False,
                "pending": {"purpose": "close_short", "signal_id": "close-1"},
                "accounting_error": "unknown_fee_asset",
            },
        },
    ]
    observations = [
        {"id": 2, "study_id": 1, "slot_ms": 3000, "observed_ms": 4000,
         "status": "error", "payload": {"error": "sample_failed:TimeoutError"}},
        {"id": 1, "study_id": 1, "slot_ms": 1000, "observed_ms": 1001,
         "status": "ok", "payload": {"bases": {"BTC": {"bid": "1.2"}}, "raw": ["原始", 1]}},
    ]
    for row in observations:
        row["snapshot_hash"] = sampling_digest(row["payload"])
    events = []
    append_event(events, "started", {"protocol": study["protocol"]})
    append_event(events, "execution", {"base": "BTC", "latency_seconds": "0.125"})
    append_event(events, "execution", {"base": "BTC", "latency_seconds": None})
    append_event(events, "observation_gap", {"base": "BTC", "gap_seconds": "2999"})
    append_event(events, "sample_error", {"error": study["last_error"]})
    checkpoint_hash = sampling_digest(archive.checkpoint_state(study, positions, observations))
    study["evidence"].update(
        protocol_hash=sampling_digest(study["protocol"]), checkpoint_hash=checkpoint_hash
    )
    append_event(events, "checkpoint", {"checkpoint_hash": checkpoint_hash})
    study["chain_head"] = events[-1]["event_hash"]
    return study, positions, observations, events


def unzip(data):
    with zipfile.ZipFile(io.BytesIO(data)) as result:
        return {name: result.read(name) for name in result.namelist()}


def test_digest_is_sampling_compatible():
    value = {"unicode": "保留", "amount": Decimal("1.2300"),
             "at": dt.datetime(2026, 11, 16, tzinfo=dt.UTC), "nested": [None, True, 1.25]}
    assert archive.digest(value) == sampling_digest(value)
    with pytest.raises(ValueError):
        archive.digest({"nan": float("nan")})


def test_freeze_exact_bytes_and_product_sources_only(product, monkeypatch):
    root, files = product
    original = archive._source_bytes
    reads = []

    def recording_read(source_root, relative):
        reads.append(relative)
        return original(source_root, relative)

    monkeypatch.setattr(archive, "_source_bytes", recording_read)
    monkeypatch.chdir(root / "research")
    manifest = archive.freeze_sources()
    assert set(reads) == set(files)
    assert set(manifest["sources"]) == set(files)
    assert json.loads(json.dumps(manifest)) == manifest
    for path, raw in files.items():
        assert manifest["sources"][path] == {
            "sha256": hashlib.sha256(raw).hexdigest(), "content": raw.decode("utf-8")
        }
    archive.verify_sources(manifest)


@pytest.mark.parametrize("change", ["changed", "deleted", "added"])
def test_verify_detects_checkout_drift(product, change):
    root, _ = product
    manifest = archive.freeze_sources()
    path = root / "src/bybitbot/validation/example.py"
    if change == "changed":
        path.write_text("VALUE = 'modified'\n")
    elif change == "deleted":
        path.unlink()
    else:
        (root / "src/bybitbot/new_module.py").write_text("NEW = True\n")
    with pytest.raises(ValueError):
        archive.verify_sources(manifest)


@pytest.mark.parametrize("change", ["content", "hash", "missing", "traversal", "type", "utf8"])
def test_verify_detects_malformed_manifest(product, change):
    manifest = archive.freeze_sources()
    source = manifest["sources"]["pyproject.toml"]
    if change == "content":
        source["content"] += "tamper"
    elif change == "hash":
        source["sha256"] = "not-a-hash"
    elif change == "missing":
        del manifest["sources"]["uv.lock"]
    elif change == "traversal":
        manifest["sources"]["src/bybitbot/../../research/record.py"] = source
    elif change == "type":
        manifest["sources"] = []
    else:
        source["content"] = "\ud800"
    with pytest.raises(ValueError):
        archive.verify_sources(manifest)


@pytest.mark.parametrize("directory_link", [False, True])
def test_sources_refuse_symlinks_without_reading_target(product, directory_link):
    root, _ = product
    if directory_link:
        (root / "src/bybitbot/linked").symlink_to(root / "research", target_is_directory=True)
    else:
        (root / "src/bybitbot/linked.py").symlink_to(root / "research/record.py")
    with pytest.raises(ValueError):
        archive.freeze_sources()


def test_invalid_utf8_source_is_refused(product):
    root, _ = product
    (root / "uv.lock").write_bytes(b"\xff")
    with pytest.raises(ValueError):
        archive.freeze_sources()


def test_event_chain_binds_sequence_and_body_but_not_database_ids():
    events = []
    event = append_event(events, "sample", {"amount": Decimal("1.20"), "raw": "观察"})
    expected = sampling_digest({key: event[key] for key in (
        "sequence", "previous_hash", "kind", "dedupe_key", "occurred_ms", "payload"
    )})
    assert event["event_hash"] == expected
    event["id"], event["study_id"] = 999, 999
    archive.verify_chain(events, expected)
    archive.verify_chain([], archive.GENESIS_HASH)


@pytest.mark.parametrize("change", ["body", "hash", "previous", "gap", "drop", "reorder", "head"])
def test_chain_detects_tamper_and_gaps(change):
    events = []
    append_event(events, "started", {})
    append_event(events, "sample", {"snapshot_hash": "a" * 64})
    expected_head = events[-1]["event_hash"]
    if change == "body":
        events[0]["payload"]["tampered"] = True
    elif change == "hash":
        events[0]["event_hash"] = "f" * 64
    elif change == "previous":
        events[1]["previous_hash"] = archive.GENESIS_HASH
    elif change == "gap":
        events[1]["sequence"] = 3
    elif change == "drop":
        events.pop()
    elif change == "reorder":
        events.reverse()
    else:
        expected_head = archive.GENESIS_HASH
    with pytest.raises(ValueError):
        archive.verify_chain(events, expected_head)


@pytest.mark.parametrize("field,value", [
    ("sequence", True), ("sequence", 0), ("occurred_ms", -1),
    ("kind", ""), ("dedupe_key", None), ("payload", []),
])
def test_event_hash_rejects_malformed_body(field, value):
    events = []
    row = append_event(events, "sample", {})
    row[field] = value
    with pytest.raises(ValueError):
        archive.event_hash(archive.GENESIS_HASH, row)


def test_chain_rejects_duplicate_dedupe_keys():
    events = []
    first = append_event(events, "sample", {})
    second = append_event(events, "sample", {})
    second["dedupe_key"] = first["dedupe_key"]
    second["event_hash"] = archive.event_hash(first["event_hash"], second)
    with pytest.raises(ValueError, match="duplicate"):
        archive.verify_chain(events, second["event_hash"])


def test_checkpoint_exact_shape_and_id_order(evidence):
    study, positions, observations, _ = evidence
    actual = archive.checkpoint_state(study, positions, observations)
    assert actual == {
        "study": {key: value for key, value in study.items()
                  if key not in {"evidence", "chain_head"}},
        "positions": sorted(positions, key=lambda row: row["id"]),
        "observations": [{key: value for key, value in row.items() if key != "payload"}
                         for row in sorted(observations, key=lambda row: row["id"])],
    }
    assert "payload" not in actual["observations"][0]
    assert archive.digest(actual) == study["evidence"]["checkpoint_hash"]


def test_complete_export_exact_evidence_and_unresolved_truth(evidence, product):
    study, positions, observations, events = evidence
    original = copy.deepcopy(evidence)
    data = archive.archive_bytes(*evidence)
    assert data == archive.archive_bytes(*evidence)
    files = unzip(data)
    expected_names = {
        "protocol.json", "manifest.json", "state.json", "ledger.jsonl", "heartbeat.json",
        "STATUS.md", "FINAL_REPORT.md",
    } | {f"frozen/{path}" for path in product[1]}
    assert files.keys() == expected_names
    assert json.loads(files["protocol.json"]) == study["protocol"]
    assert json.loads(files["manifest.json"]) == study["evidence"]
    assert [json.loads(line) for line in files["ledger.jsonl"].splitlines()] == events
    for path, raw in product[1].items():
        assert files[f"frozen/{path}"] == raw
    state = json.loads(files["state.json"])
    assert state["study"] == study
    assert state["positions"] == positions
    assert state["observations"] == observations
    assert state["checkpoint"] == archive.checkpoint_state(study, positions, observations)
    summary = state["summary"]
    assert summary["unresolved_risk"] is True
    assert summary["observation_count"] == 2
    assert summary["observation_error_count"] == summary["error_event_count"] == 1
    assert summary["gap_count"] == 1
    btc = next(row for row in summary["bases"] if row["base"] == "BTC")
    assert btc["phase"] == "study_ended_unresolved"
    assert (btc["cash"], btc["fees"], btc["reserved_risk"]) == ("-8.4", "0.2", "10")
    assert (btc["long_qty"], btc["short_qty"], btc["unknown_fees"]) == ("0.01", "0.005", True)
    assert summary["latencies"][0]["latency_seconds"] == "0.125"
    assert summary["latencies"][1]["latency_seconds"] is None
    heartbeat = json.loads(files["heartbeat.json"])
    assert heartbeat["heartbeat_ms"] == 5001
    assert heartbeat["last_error"] == study["last_error"]
    for name in ("STATUS.md", "FINAL_REPORT.md"):
        report = files[name].decode()
        assert "Unresolved risk preserved: true" in report
        assert "study_ended_unresolved" in report
        assert '"0.01"' in report and '"0.005"' in report
        assert "Unknown fees" in report and "null" in report
    assert evidence == original


@pytest.mark.parametrize("status", ["running", "stopped", "ending"])
def test_final_report_only_when_ended(evidence, status):
    study, _, _, events = evidence
    study["status"] = status
    study["evidence"].pop("checkpoint_hash")
    events.pop()
    study["chain_head"] = events[-1]["event_hash"]
    assert "FINAL_REPORT.md" not in unzip(archive.archive_bytes(*evidence))


def test_historical_frozen_sources_export_after_checkout_upgrade(evidence, product):
    root, files = product
    (root / "src/bybitbot/new.py").write_text("ADDED_AFTER_RUN = True\n")
    (root / "pyproject.toml").write_text("changed after run\n")
    with pytest.raises(ValueError):
        archive.verify_sources(evidence[0]["evidence"])
    result = unzip(archive.archive_bytes(*evidence))
    assert result["frozen/pyproject.toml"] == files["pyproject.toml"]
    assert "frozen/src/bybitbot/new.py" not in result


@pytest.mark.parametrize("change", ["payload", "hash", "event", "protocol", "state", "metadata",
                                     "rehash_observation", "manifest", "ledger_checkpoint"])
def test_export_refuses_corrupt_evidence(evidence, change):
    study, positions, observations, events = evidence
    if change == "payload":
        observations[0]["payload"]["error"] = "tampered"
    elif change == "hash":
        observations[0]["snapshot_hash"] = archive.GENESIS_HASH
    elif change == "event":
        events[0]["payload"]["tamper"] = True
    elif change == "protocol":
        study["protocol"]["budget"] = "999"
    elif change == "state":
        positions[0]["cash"] = "999"
    elif change == "metadata":
        observations[0]["status"] = "ok"
    elif change == "rehash_observation":
        observations[0]["payload"]["tampered"] = True
        observations[0]["snapshot_hash"] = archive.digest(observations[0]["payload"])
    elif change == "manifest":
        study["evidence"]["sources"]["uv.lock"]["content"] = "tampered"
    else:
        study["evidence"].pop("checkpoint_hash")
        events[-1]["payload"]["checkpoint_hash"] = archive.GENESIS_HASH
        events[-1]["event_hash"] = archive.event_hash(events[-1]["previous_hash"], events[-1])
        study["chain_head"] = events[-1]["event_hash"]
    with pytest.raises(ValueError):
        archive.archive_bytes(*evidence)


def test_write_archive_atomic_idempotent_and_never_overwrites(tmp_path):
    data = b"archive data"
    target = archive.write_archive(tmp_path, "evidence.zip", data)
    inode = target.stat().st_ino
    assert target.read_bytes() == data
    assert archive.write_archive(tmp_path, "evidence.zip", data) == target
    assert target.stat().st_ino == inode
    with pytest.raises(ValueError, match="different"):
        archive.write_archive(tmp_path, "evidence.zip", b"different")
    assert target.read_bytes() == data
    assert target.stat().st_ino == inode
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("filename", ["../escape.zip", "/tmp/escape.zip", "a/b.zip", "a\\b.zip",  # noqa: S108 - 恶意路径用例，不会真正落盘
                                      ".", "..", "", "invalid\x00.zip"])
def test_write_refuses_traversal_without_mutation(tmp_path, filename):
    before = set(tmp_path.iterdir())
    with pytest.raises(ValueError):
        archive.write_archive(tmp_path, filename, b"archive")
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize("exists", [False, True])
def test_write_refuses_symlink_target_without_mutation(tmp_path, exists):
    destination = tmp_path / "destination"
    if exists:
        destination.write_bytes(b"same")
    target = tmp_path / "archive.zip"
    target.symlink_to(destination)
    before = set(tmp_path.iterdir())
    with pytest.raises(ValueError):
        archive.write_archive(tmp_path, "archive.zip", b"same")
    assert target.is_symlink()
    assert set(tmp_path.iterdir()) == before
    assert destination.read_bytes() == b"same" if exists else not destination.exists()


def test_write_refuses_directory_symlinks(tmp_path):
    actual = tmp_path / "actual"
    (actual / "nested").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError):
        archive.write_archive(link / "nested", "archive.zip", b"data")
    assert list((actual / "nested").iterdir()) == []


def test_write_never_targets_original_research(product):
    root, _ = product
    research = root / "research"
    before = {path.name: path.read_bytes() for path in research.iterdir()}
    with pytest.raises(ValueError, match="research"):
        archive.write_archive(research, "new.zip", b"data")
    assert {path.name: path.read_bytes() for path in research.iterdir()} == before


def test_write_refuses_nonregular_target(tmp_path):
    (tmp_path / "directory.zip").mkdir()
    os.mkfifo(tmp_path / "fifo.zip")
    for name in ("directory.zip", "fifo.zip"):
        with pytest.raises(ValueError):
            archive.write_archive(tmp_path, name, b"data")
    assert {path.name for path in tmp_path.iterdir()} == {"directory.zip", "fifo.zip"}


def test_failed_publication_cleans_temporary_file(tmp_path, monkeypatch):
    def failed(*args, **kwargs):
        raise OSError("simulated publication failure")

    monkeypatch.setattr(archive.os, "link", failed)
    with pytest.raises(ValueError):
        archive.write_archive(tmp_path, "archive.zip", b"data")
    assert list(tmp_path.iterdir()) == []


def test_concurrent_publication_never_overwrites_winner(tmp_path, monkeypatch):
    original = archive.os.link

    def concurrent(source, destination, **kwargs):
        (tmp_path / destination).write_bytes(b"other writer")
        original(source, destination, **kwargs)

    monkeypatch.setattr(archive.os, "link", concurrent)
    with pytest.raises(ValueError, match="appeared"):
        archive.write_archive(tmp_path, "archive.zip", b"our data")
    assert (tmp_path / "archive.zip").read_bytes() == b"other writer"
    assert {path.name for path in tmp_path.iterdir()} == {"archive.zip"}


def test_project_root_is_derived_from_module_location(monkeypatch):
    location = Path("/isolated/project/src/bybitbot/validation/archive.py")
    monkeypatch.setattr(archive, "__file__", str(location))
    assert archive._project_root() == Path("/isolated/project")
