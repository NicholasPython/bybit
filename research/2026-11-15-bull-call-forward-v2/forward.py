#!/usr/bin/env python3
"""Bounded conditional paper ledger. Public market data; never sends orders."""

from __future__ import annotations

import argparse
import ast
import copy
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import tempfile
import time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

import bybitbot
from bybitbot.money import common_step, quantize_price
from bybitbot.terms import ValuationPolicy, option_fee, option_value

D = Decimal
HOUR = 3_600_000
DAY = 24 * HOUR
ROOT = Path(__file__).resolve().parent
PACKAGE_ROOT = Path(bybitbot.__file__).resolve().parent
PROTECTED_STUDIES = tuple(
    ROOT.parent / name for name in ("2026-11-15-bull-call-forward", "2026-11-15-options-forward")
)
DONE = {"closed", "aborted", "cancelled", "expired_unresolved", "study_ended_unresolved"}
OPENING = {"open_long", "open_short"}


class IntegrityError(ValueError):
    """Frozen inputs or ledger evidence changed."""


def iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).isoformat()


def stamp(text):
    return int(dt.datetime.fromisoformat(text).timestamp() * 1000)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path, value):
    validate_output(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        temp = Path(handle.name)
    temp.replace(path)


def state_hash(state):
    data = {k: v for k, v in state.items() if k != "checkpointHash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def adapter():
    spec = importlib.util.spec_from_file_location("bull_call_market", ROOT / "market.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_output(output):
    """Reject old study trees and symlink aliases before creating or locking anything."""
    output = Path(output).resolve()
    for original in PROTECTED_STUDIES:
        original = original.resolve()
        if output.is_relative_to(original) or original.is_relative_to(output):
            raise IntegrityError("Original study directory is protected; use a separate v2 run")
    # In particular runner.lock must never follow an existing link on open('w').
    if output.exists() and any(p.is_symlink() for p in output.rglob("*")):
        raise IntegrityError("Run directory must not contain symlinks")
    return output


def source_files():
    """Freeze the transitive local imports, including each package initializer.

    Resolve from the installed package, not cwd. Fail closed for unresolved local
    imports rather than leaving a new dependency outside the experiment identity.
    """
    files = {
        name: ROOT / name for name in ("protocol.json", "PROTOCOL.md", "forward.py", "market.py")
    }
    pending = []
    for path in files.values():
        if path.suffix != ".py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                pending.extend(a.name for a in node.names if a.name.split(".")[0] == "bybitbot")
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and (node.module == "bybitbot" or node.module.startswith("bybitbot."))
            ):
                pending.append(node.module)
                if node.module == "bybitbot":
                    pending.extend("bybitbot." + alias.name for alias in node.names)
    seen = set()
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        parts = module.split(".")
        for length in range(1, len(parts)):
            pending.append(".".join(parts[:length]))
        relative = Path(*parts[1:]) if len(parts) > 1 else Path()
        candidate = PACKAGE_ROOT / relative
        path = candidate / "__init__.py" if candidate.is_dir() else candidate.with_suffix(".py")
        if not path.is_file() or not path.resolve().is_relative_to(PACKAGE_ROOT.resolve()):
            raise IntegrityError(f"Unresolved shared source: {module}")
        files[str(Path("shared/bybitbot") / path.relative_to(PACKAGE_ROOT))] = path
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                pending.extend(a.name for a in node.names if a.name.split(".")[0] == "bybitbot")
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
                    name = importlib.util.resolve_name(
                        "." * node.level + (node.module or ""), package
                    )
                else:
                    name = node.module or ""
                if name == "bybitbot" or name.startswith("bybitbot."):
                    pending.append(name)
                    if node.module is None or name == "bybitbot":
                        pending.extend(name + "." + a.name for a in node.names)
    return dict(sorted(files.items()))


def prepare_run(output, protocol):
    output = validate_output(output)
    try:
        sources = source_files()
        hashes = {name: digest(path) for name, path in sources.items()}
    except (OSError, SyntaxError, UnicodeError) as error:
        raise IntegrityError("Frozen source unavailable or unparsable") from error
    file = output / "manifest.json"
    if file.exists():
        try:
            manifest = json.loads(file.read_text())
        except (OSError, ValueError) as error:
            raise IntegrityError("Frozen manifest unavailable or malformed") from error
        if not isinstance(manifest, dict) or (
            manifest.get("files") != hashes or manifest.get("version") != protocol["version"]
        ):
            raise IntegrityError("Frozen source/protocol changed; create a new experiment")
        try:
            for name, sha in hashes.items():
                if digest(output / "frozen" / name) != sha:
                    raise IntegrityError("Frozen source copy changed")
        except OSError as error:
            raise IntegrityError("Frozen source copy missing") from error
        return manifest
    if output.exists() and any(output.iterdir()):
        # main creates only these two empty infrastructure entries before freezing.
        unexpected = {p.name for p in output.iterdir()} - {"runner.lock", "evidence"}
        if unexpected or ((output / "evidence").exists() and any((output / "evidence").iterdir())):
            raise IntegrityError("Manifest missing from existing ledger; use a new v2 run")
    manifest = {
        "version": protocol["version"],
        "frozenUtc": iso(int(time.time() * 1000)),
        "files": hashes,
    }
    for name, source in sources.items():
        target = output / "frozen" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    atomic_json(file, manifest)
    return manifest


def new_state(protocol, manifest):
    return {
        "protocolHash": manifest["files"]["protocol.json"],
        "protocolVersion": protocol["version"],
        "valuationPolicy": protocol["valuation_policy"],
        "simulation": True,
        "marginValidated": False,
        "frozenMs": stamp(manifest["frozenUtc"]),
        "endMs": stamp(manifest["frozenUtc"]) + protocol["study_days"] * DAY,
        "lastMs": None,
        "processed": {},
        "events": [],
        "samples": [],
        "positions": {
            base: {
                "base": base,
                "phase": "waiting",
                "cash": "0",
                "fees": "0",
                "reservedRisk": "0",
                "longHeld": False,
                "shortHeld": False,
                "pending": None,
            }
            for base in protocol["bases"]
        },
    }


def event(state, ms, kind, **fields):
    state["events"].append({"ms": ms, "utc": iso(ms), "kind": kind, **fields})


def opposite_evidence(protocol, now_ms):
    """Read a checksummed local ledger; uncertain or live opposite exposure blocks entry."""
    run = (ROOT / protocol["opposing_run"]).resolve()
    try:
        raw = (run / "state.json").read_bytes()
        state = json.loads(raw)
        manifest = json.loads((run / "manifest.json").read_text())
        if state.get("checkpointHash") != state_hash(state):
            raise IntegrityError("Opposing ledger checksum mismatch")
        if (
            state["protocolHash"] != protocol["opposing_protocol_hash"]
            or manifest["protocolHash"] != protocol["opposing_protocol_hash"]
            or manifest["engineHash"] != protocol["opposing_engine_hash"]
            or digest(run.parent / "protocol.json") != protocol["opposing_protocol_hash"]
            or digest(run.parent / "forward.py") != protocol["opposing_engine_hash"]
        ):
            raise IntegrityError("Opposing study identity changed")
        blocked = {}
        for base in protocol["bases"]:
            p = state["positions"][base]
            terminal_flat = (
                p["phase"] in {"closed", "aborted", "cancelled"}
                and not p["longHeld"]
                and not p["shortHeld"]
                and p["pending"] is None
            )
            blocked[base] = not terminal_flat
        return {
            "checkedMs": now_ms,
            "blocked": blocked,
            "state": state,
            "stateSha256": hashlib.sha256(raw).hexdigest(),
            "reason": "opposite exposure or unfinished cohort blocks entry",
        }
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {
            "checkedMs": now_ms,
            "blocked": dict.fromkeys(protocol["bases"], True),
            "error": str(error),
        }


def fee(option, px, quantity, protocol):
    return option_fee(
        premium=px,
        qty=quantity,
        index_price=option["index"],
        underlying_rate=D(protocol["taker_rate"]),
        premium_cap_rate=D(protocol["premium_fee_cap"]),
    )


def executable(option, side, qty, protocol, *, entry=False, ticks=None):
    if option is None or option["status"] != "Trading":
        return None
    keys = (
        "bid",
        "ask",
        "bidSize",
        "askSize",
        "index",
        "tick",
        "minQty",
        "step",
        "maxQty",
        "minPrice",
        "maxPrice",
    )
    if any(not isinstance(option.get(k), D) or not option[k].is_finite() for k in keys):
        return None
    if not (
        0 < option["bid"] <= option["ask"]
        and option["index"] > 0
        and option["tick"] > 0
        and option["step"] > 0
    ):
        return None
    if not option["minQty"] <= qty <= option["maxQty"] or qty % option["step"]:
        return None
    if min(option["bidSize"], option["askSize"]) < qty:
        return None
    if option["bid"] % option["tick"] or option["ask"] % option["tick"]:
        return None
    if entry and (option["ask"] - option["bid"]) / ((option["ask"] + option["bid"]) / 2) > D(
        protocol["max_spread_fraction"]
    ):
        return None
    count = protocol["adverse_slippage_ticks"] if ticks is None else ticks
    px = (
        option["ask"] + count * option["tick"]
        if side == "Buy"
        else option["bid"] - count * option["tick"]
    )
    return px if px > 0 and option["minPrice"] <= px <= option["maxPrice"] else None


def structure(long, short, ms, protocol, *, deltas=True):
    if long is None or short is None:
        return False
    if (
        long["base"] != short["base"]
        or long["expiryMs"] != short["expiryMs"]
        or long["strike"] >= short["strike"]
    ):
        return False
    if (
        not protocol["min_entry_days"] * DAY
        <= long["expiryMs"] - ms
        <= protocol["max_entry_days"] * DAY
    ):
        return False
    if any(
        not isinstance(o.get(k), D) or not o[k].is_finite()
        for o in (long, short)
        for k in ("index", "strike")
    ):
        return False
    index = (long["index"] + short["index"]) / 2
    if index <= 0 or not D(protocol["width_index_min"]) <= (
        short["strike"] - long["strike"]
    ) / index <= D(protocol["width_index_max"]):
        return False
    if deltas and any(
        not isinstance(o.get("delta"), D) or not o["delta"].is_finite() for o in (long, short)
    ):
        return False
    return not deltas or (
        D(protocol["long_delta_min"]) <= long["delta"] <= D(protocol["long_delta_max"])
        and D(protocol["short_delta_min"]) <= short["delta"] <= D(protocol["short_delta_max"])
    )


def economics(long, short, qty, protocol, *, paid_long=None):
    """Cost gates with current exit quotes; use actual paid long cash once held."""
    buy = executable(long, "Buy", qty, protocol, entry=True)
    sell = executable(short, "Sell", qty, protocol, entry=True)
    close_long = executable(long, "Sell", qty, protocol)
    close_short = executable(short, "Buy", qty, protocol)
    stress_buy = executable(
        long, "Buy", qty, protocol, entry=True, ticks=protocol["stress_adverse_ticks"]
    )
    stress_sell = executable(
        short, "Sell", qty, protocol, entry=True, ticks=protocol["stress_adverse_ticks"]
    )
    required = (sell, close_long, close_short, stress_sell)
    if paid_long is None:
        required += (buy, stress_buy)
    if any(px is None for px in required):
        return None
    long_cost = paid_long if paid_long is not None else buy * qty + fee(long, buy, qty, protocol)
    debit = long_cost - sell * qty + fee(short, sell, qty, protocol)
    width = (short["strike"] - long["strike"]) * qty
    profit = width - debit
    stress_cost = (
        paid_long
        if paid_long is not None
        else stress_buy * qty + fee(long, stress_buy, qty, protocol)
    )
    stress_debit = stress_cost - stress_sell * qty + fee(short, stress_sell, qty, protocol)
    stress_profit = width - stress_debit
    if min(debit, profit, stress_debit, stress_profit) <= 0:
        return None
    closing_cash = (
        (close_long - close_short) * qty
        - fee(long, close_long, qty, protocol)
        - fee(short, close_short, qty, protocol)
    )
    roundtrip = debit - closing_cash
    mid = (long["ask"] + long["bid"] - short["ask"] - short["bid"]) / 2 * qty
    gates = {
        "rewardRisk": profit / debit >= D(protocol["min_reward_risk"]),
        "takeProfitReachable": profit >= debit * D(protocol["take_profit_debit_fraction"]),
        "entryFriction": debit - mid <= profit * D(protocol["max_entry_friction_profit"]),
        "roundtripRisk": roundtrip <= debit * D(protocol["max_roundtrip_loss_risk"]),
        "roundtripProfit": roundtrip <= profit * D(protocol["max_roundtrip_loss_profit"]),
        "stressRewardRisk": stress_profit / stress_debit >= D(protocol["min_reward_risk"]),
        "firstLegOutlay": long_cost <= D(protocol["max_first_leg_outlay_usdt"]),
    }
    return {
        "debit": debit,
        "profit": profit,
        "stressDebit": stress_debit,
        "longCost": long_cost,
        "gates": gates,
        "eligible": all(gates.values()),
    }


def remaining_budget(state, protocol, excluding=None):
    used = sum(D(p["reservedRisk"]) for b, p in state["positions"].items() if b != excluding)
    return max(D(0), D(protocol["aggregate_risk_budget_usdt"]) - used)


def choose(base, market, available, protocol):
    options = [
        o for o in market["options"].values() if o["base"] == base and o["status"] == "Trading"
    ]
    candidates = []
    for long in options:
        if (
            not isinstance(long.get("delta"), D)
            or not long["delta"].is_finite()
            or not D(protocol["long_delta_min"]) <= long["delta"] <= D(protocol["long_delta_max"])
        ):
            continue
        for short in options:
            if not structure(long, short, market["ms"], protocol):
                continue
            step = (
                common_step(long["step"], short["step"])
                if min(long["step"], short["step"]) > 0
                else D(0)
            )
            if step <= 0:
                continue
            minimum = (max(long["minQty"], short["minQty"]) / step).to_integral_value(
                rounding=ROUND_CEILING
            ) * step
            cost = economics(long, short, minimum, protocol)
            if cost is None or not cost["eligible"]:
                continue
            cap = min(
                available / (cost["stressDebit"] / minimum),
                D(protocol["max_first_leg_outlay_usdt"]) / (cost["longCost"] / minimum),
                long["bidSize"],
                long["askSize"],
                short["bidSize"],
                short["askSize"],
                long["maxQty"],
                short["maxQty"],
            )
            qty = (cap / step).to_integral_value(rounding=ROUND_FLOOR) * step
            if qty < minimum:
                continue
            cost = economics(long, short, qty, protocol)
            if cost is None or not cost["eligible"] or cost["stressDebit"] > available:
                continue
            rank = (
                abs(long["expiryMs"] - market["ms"] - protocol["target_entry_days"] * DAY),
                long["expiryMs"],
                abs(short["delta"] - D(protocol["target_short_delta"])),
                abs(long["delta"] - D(protocol["target_long_delta"])),
                long["strike"],
                short["strike"],
                long["symbol"],
                short["symbol"],
            )
            candidates.append((rank, long, short, qty, cost))
    return min(candidates, key=lambda c: c[0]) if candidates else None


def limit_price(option, side, protocol):
    f = D(protocol["entry_limit_fraction"])
    raw = option["ask"] * (1 + f) if side == "Buy" else option["bid"] * (1 - f)
    # Research limits are aggressive adverse bounds: Buy rounds up, Sell down.
    return quantize_price(raw, option["tick"], up=side == "Buy")


def intent(state, p, market, purpose, symbol, side, protocol, limit=None):
    quote = market["options"].get(symbol)
    p["pending"] = {
        "purpose": purpose,
        "symbol": symbol,
        "side": side,
        "createdMs": market["ms"],
        "decisionSourceMs": quote["sourceMs"] if quote else market["ms"],
        "deadlineMs": market["ms"] + protocol["pending_ttl_seconds"] * 1000
        if purpose in OPENING
        else None,
        "limit": str(limit) if limit is not None else None,
    }
    event(state, market["ms"], "intent", base=p["base"], **p["pending"])


def abort(state, p, market, protocol, reason):
    event(state, market["ms"], "entry_aborted", base=p["base"], reason=reason)
    p["pending"] = None
    if p["longHeld"]:
        p["phase"] = "aborting"
        intent(state, p, market, "close_long", p["long_symbol"], "Sell", protocol)
    else:
        p["phase"] = "cancelled"
        p["reservedRisk"] = "0"


def value(p, market, protocol):
    policy = ValuationPolicy(**protocol["valuation_policy"])
    if (
        policy.currency != "USDT"
        or policy.option_price_field != "bid_ask_adverse_ticks"
        or policy.fx_source != "identity_USDT"
    ):
        raise IntegrityError("Unsupported research liquidation valuation policy")
    if p["phase"] in {"expired_unresolved", "study_ended_unresolved"} and (
        p["longHeld"] or p["shortHeld"]
    ):
        return None
    amount = D(p["cash"])
    for held, key, side, sign in (
        ("longHeld", "long_symbol", "Sell", 1),
        ("shortHeld", "short_symbol", "Buy", -1),
    ):
        if not p[held]:
            continue
        option = market["options"].get(p[key])
        if option is None or market["ms"] >= p["expiryMs"]:
            return None
        qty = D(p["quantity"])
        px = executable(option, side, qty, protocol)
        if px is None:
            return None
        amount += sign * option_value(price=px, qty=qty) - fee(option, px, qty, protocol)
    return str(amount)


def fill(state, p, market, pending, px, protocol):
    option = market["options"][pending["symbol"]]
    qty = D(p["quantity"])
    charge = fee(option, px, qty, protocol)
    flow = (-1 if pending["side"] == "Buy" else 1) * px * qty - charge
    p["cash"] = str(D(p["cash"]) + flow)
    p["fees"] = str(D(p["fees"]) + charge)
    purpose = pending["purpose"]
    p["longHeld" if pending["symbol"] == p["long_symbol"] else "shortHeld"] = purpose in OPENING
    p["pending"] = None
    event(
        state,
        market["ms"],
        "simulated_fill",
        base=p["base"],
        **pending,
        quantity=str(qty),
        price=str(px),
        fee=str(charge),
        cashFlow=str(flow),
        quoteSourceMs=option["sourceMs"],
    )
    if purpose == "open_long":
        p["phase"] = "opening_short"
        intent(
            state, p, market, "open_short", p["short_symbol"], "Sell", protocol, D(p["shortLimit"])
        )
    elif purpose == "open_short":
        p["phase"] = "open"
        p["openedMs"] = market["ms"]
        p["entryDebit"] = str(-D(p["cash"]))
        p["reservedRisk"] = p["entryDebit"]
        event(state, market["ms"], "spread_opened", base=p["base"], entryDebit=p["entryDebit"])
    elif purpose == "close_short":
        p["phase"] = "closing_long"
        intent(state, p, market, "close_long", p["long_symbol"], "Sell", protocol)
    else:
        p["phase"] = "aborted" if p["phase"] == "aborting" else "closed"
        p["closedMs"] = market["ms"]
        event(
            state, market["ms"], "position_ended", base=p["base"], phase=p["phase"], pnl=p["cash"]
        )


def pending_step(state, p, market, protocol):
    pending = p["pending"]
    purpose, ms, base = pending["purpose"], market["ms"], p["base"]
    if purpose in OPENING:
        if ms >= pending["deadlineMs"]:
            abort(state, p, market, protocol, "entry_timeout")
            return
        if not market["signals"][base]["eligible"] or market["conflicts"][base]:
            abort(state, p, market, protocol, "signal_or_opposing_exposure")
            return
    option = market["options"].get(pending["symbol"])
    if option is None or option["sourceMs"] <= pending["decisionSourceMs"]:
        return
    qty = D(p["quantity"])
    px = executable(option, pending["side"], qty, protocol, entry=purpose in OPENING)
    if px is None:
        return
    if pending["limit"] is not None and (
        (pending["side"] == "Buy" and px > D(pending["limit"]))
        or (pending["side"] == "Sell" and px < D(pending["limit"]))
    ):
        return
    if purpose in OPENING:
        long = market["options"].get(p["long_symbol"])
        short = market["options"].get(p["short_symbol"])
        if not structure(long, short, ms, protocol, deltas=False):
            abort(state, p, market, protocol, "structure_or_entry_window")
            return
        paid = -D(p["cash"]) if purpose == "open_short" else None
        costs = economics(long, short, qty, protocol, paid_long=paid)
        cap = min(D(p["reservedRisk"]), remaining_budget(state, protocol, excluding=base))
        if costs is None or not costs["eligible"] or costs["stressDebit"] > cap:
            abort(state, p, market, protocol, "cost_or_aggregate_budget")
            return
    fill(state, p, market, pending, px, protocol)


def step_position(state, p, market, protocol):
    ms, base = market["ms"], p["base"]
    if p["phase"] in DONE:
        return
    if p.get("expiryMs") is not None and ms >= p["expiryMs"]:
        p["phase"] = "expired_unresolved" if p["longHeld"] or p["shortHeld"] else "cancelled"
        p["pending"] = None
        event(state, ms, p["phase"], base=base)
        return
    if p["phase"] == "waiting":
        entry_buffer = (
            protocol["end_exit_buffer_seconds"] + 2 * protocol["pending_ttl_seconds"]
        ) * 1000
        if ms >= state["endMs"] - entry_buffer:
            p["phase"] = "cancelled"
            event(state, ms, "entry_window_ended", base=base)
            return
        if not market["signals"][base]["eligible"] or market["conflicts"][base]:
            return
        selected = choose(base, market, remaining_budget(state, protocol), protocol)
        if selected is None:
            event(state, ms, "no_cost_eligible_candidate", base=base)
            return
        _, long, short, qty, costs = selected
        p.update(
            {
                "long_symbol": long["symbol"],
                "short_symbol": short["symbol"],
                "quantity": str(qty),
                "expiryMs": long["expiryMs"],
                "widthValue": str((short["strike"] - long["strike"]) * qty),
                "reservedRisk": str(costs["stressDebit"]),
                "longLimit": str(limit_price(long, "Buy", protocol)),
                "shortLimit": str(limit_price(short, "Sell", protocol)),
                "phase": "opening_long",
                "entrySignal": market["signals"][base],
                "decisionMs": ms,
            }
        )
        event(
            state,
            ms,
            "candidate_selected",
            base=base,
            long=p["long_symbol"],
            short=p["short_symbol"],
            quantity=p["quantity"],
            reservedRisk=p["reservedRisk"],
        )
        intent(state, p, market, "open_long", p["long_symbol"], "Buy", protocol, D(p["longLimit"]))
        return
    # Stop incomplete entries before the same reserved window used for exit.
    if (
        p["pending"] is not None
        and p["pending"]["purpose"] in OPENING
        and ms >= state["endMs"] - protocol["end_exit_buffer_seconds"] * 1000
    ):
        abort(state, p, market, protocol, "study_end_entry_cutoff")
        return
    # Keep exit intents until filled/expiry. Openings abort on signal loss.
    if p["pending"] is not None:
        pending_step(state, p, market, protocol)
        return
    if p["phase"] == "open":
        pnl = value(p, market, protocol)
        reason = None
        if ms >= state["endMs"] - protocol["end_exit_buffer_seconds"] * 1000:
            reason = "study_end_exit"
        elif ms - p["openedMs"] >= protocol["max_hold_hours"] * HOUR:
            reason = "max_hold"
        elif p["expiryMs"] - ms <= protocol["close_before_expiry_hours"] * HOUR:
            reason = "before_expiry"
        elif not market["signals"][base]["eligible"]:
            reason = "strength_signal_lost"
        elif pnl is not None:
            if D(pnl) >= D(p["entryDebit"]) * D(protocol["take_profit_debit_fraction"]):
                reason = "take_profit"
            elif D(pnl) <= -D(p["entryDebit"]) * D(protocol["stop_loss_debit_fraction"]):
                reason = "stop_loss"
        if reason:
            p["phase"] = "closing_short"
            p["exitReason"] = reason
            event(state, ms, "exit_trigger", base=base, reason=reason, netPnl=pnl)
            intent(state, p, market, "close_short", p["short_symbol"], "Buy", protocol)


def apply(state, market, protocol, snapshot_hash, record):
    if snapshot_hash in state["processed"]:
        return state
    if state["lastMs"] is not None and market["ms"] <= state["lastMs"]:
        raise ValueError("Non-increasing market source time")
    state = copy.deepcopy(state)
    ms = market["ms"]
    if (
        state["lastMs"] is not None
        and ms - state["lastMs"] > protocol["max_observation_gap_seconds"] * 1000
    ):
        event(
            state,
            ms,
            "observation_gap",
            previousMs=state["lastMs"],
            gapSeconds=(ms - state["lastMs"]) / 1000,
        )
    for base in protocol["bases"]:
        step_position(state, state["positions"][base], market, protocol)
    if sum(D(p["reservedRisk"]) for p in state["positions"].values()) > D(
        protocol["aggregate_risk_budget_usdt"]
    ):
        raise IntegrityError("Aggregate risk reservation exceeded")
    state["samples"].append(
        {
            "ms": ms,
            "utc": iso(ms),
            "signals": market["signals"],
            "conflicts": market["conflicts"],
            "netPnl": {b: value(p, market, protocol) for b, p in state["positions"].items()},
            "phases": {b: p["phase"] for b, p in state["positions"].items()},
        }
    )
    state["lastMs"] = ms
    state["processed"][snapshot_hash] = record
    return state


def verify_ledger(state, output, market_adapter):
    if state.get("checkpointHash") != state_hash(state):
        raise IntegrityError("Checkpoint checksum mismatch")
    for sha, record in state["processed"].items():
        path = Path(record["path"]).resolve()
        if not path.is_relative_to(ROOT / "snapshots") or digest(path) != sha:
            raise IntegrityError("Historical manifest changed or escaped data directory")
        try:
            market_adapter.verify_snapshot(path)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise IntegrityError(f"Historical raw evidence invalid: {error}") from error
        evidence = Path(record["oppositePath"]).resolve()
        if (
            not evidence.is_relative_to(output.resolve() / "evidence")
            or digest(evidence) != record["oppositeHash"]
        ):
            raise IntegrityError("Historical opposing-exposure evidence changed")
    for base, p in state["positions"].items():
        fills = [e for e in state["events"] if e["kind"] == "simulated_fill" and e["base"] == base]
        if sum((D(e["cashFlow"]) for e in fills), D(0)) != D(p["cash"]) or sum(
            (D(e["fee"]) for e in fills), D(0)
        ) != D(p["fees"]):
            raise IntegrityError("Fill accounting mismatch")


def report(state, path, *, final=False):
    validate_output(path.parent)
    latest = state["samples"][-1] if state["samples"] else None
    lines = [
        "# BTC/ETH 转强后看涨价差纸面实验",
        "",
        "**真实公开行情；逐腿模拟成交。未验证真实成交、账户保证金或清算。**",
        "",
        f"协议：{state['protocolVersion']}；冻结：{iso(state['frozenMs'])}；观察截止：{iso(state['endMs'])}。",
        f"估值政策：{state['valuationPolicy']['version']}（不利买卖盘口、含假设平仓费）。",
        f"最近行情：{iso(state['lastMs']) if state['lastMs'] else '尚无'}；"
        f"快照数：{len(state['processed'])}。",
        "",
        "| 标的 | 阶段 | 转强 | 原看跌阻挡 | 数量 | 净借记 | 净现金 | 费用 | 净损益估值 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for base, p in state["positions"].items():
        signal = latest["signals"][base]["eligible"] if latest else None
        conflict = latest["conflicts"][base] if latest else None
        pnl = latest["netPnl"][base] if latest else None
        lines.append(
            f"| {base} | {p['phase']} | {signal} | {conflict} | "
            f"{p.get('quantity', '—')} | {p.get('entryDebit', '—')} | "
            f"{p['cash']} | {p['fees']} | {pnl if pnl is not None else '未知'} |"
        )
        if p.get("long_symbol"):
            lines.extend(
                [
                    "",
                    f"{base}：买入 {p['long_symbol']} / 卖出 {p['short_symbol']}；"
                    f"长腿持有 {p['longHeld']}；短腿持有 {p['shortHeld']}。",
                ]
            )
    if latest:
        lines.extend(
            [
                "",
                "## 最近完成K线信号",
                "",
                "| 标的 | 日收盘 | SMA20 | 前一日SMA20 | SMA50 | 最近3小时收盘 | 24小时收益 |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for base, s in latest["signals"].items():
            lines.append(
                f"| {base} | {s['dailyClose']} | {s['sma20']} | {s['previousSma20']} | "
                f"{s['sma50']} | {s['last3HourlyCloses']} | {s['return24h']} |"
            )
    lines.extend(
        [
            "",
            "## 逐腿模拟成交",
            "",
            "| 时间UTC | 标的 | 动作 | 合约 | 数量 | 价格 | 费用 | 净现金流 | 距决策盘口秒 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for e in state["events"]:
        if e["kind"] == "simulated_fill":
            lag = (e["quoteSourceMs"] - e["decisionSourceMs"]) / 1000
            lines.append(
                f"| {e['utc']} | {e['base']} | {e['purpose']} | {e['symbol']} | "
                f"{e['quantity']} | {e['price']} | {e['fee']} | {e['cashFlow']} | {lag} |"
            )
    gaps = [e for e in state["events"] if e["kind"] == "observation_gap"]
    lines.extend(["", f"超过30分钟的观察缺口：{len(gaps)}；缺口期间不推测信号、触发或成交。", ""])
    for base, p in state["positions"].items():
        vals = [D(s["netPnl"][base]) for s in state["samples"] if s["netPnl"][base] is not None]
        unknown = sum(s["netPnl"][base] is None for s in state["samples"])
        flat = not p["longHeld"] and not p["shortHeld"]
        realized = p["cash"] if flat else "未平仓，不能列为已实现"
        lines.append(
            f"- {base}：已实现净损益 {realized}；"
            f"已观察最大净亏损 {max(D(0), -min(vals)) if vals else '未知'}；"
            f"未知估值 {unknown} 次。"
        )
    if final:
        lines.extend(
            [
                "",
                "实验已停止观察。任何 unresolved 或持腿状态均为未解决敞口，不能视为平仓；"
                "零成交表示没有完成合格入场，并非盈利证据。",
            ]
        )
    lines.extend(
        [
            "",
            "BTC优先，BTC/ETH合计理论风险预算25 USDT；"
            "未配对长腿单独限制含费用支出100 USDT。"
            "理论风险预算不包含未来费用、异常执行及保证金。",
            "每标的最多一次入场尝试，不加仓、不重复入场。止盈止损、信号失效和72小时规则只能在采样时触发，随后新盘口才可能模拟成交。",
            "响应源时间不是单个期权报价最后更新时间；顶层盘口与可见数量不保证真实成交。现金流不等于持仓盈亏；缺价/未核对结算记未知。",
            "这些数据可用于模拟定价回测，但不能替代历史期权真实报价。",
            "",
        ]
    )
    path.write_text("\n".join(lines))


def process(path, protocol, manifest, output, market_adapter):
    output = validate_output(output)
    if prepare_run(output, protocol) != manifest:
        raise IntegrityError("Run manifest changed")
    state_file = output / "state.json"
    if state_file.exists():
        state = json.loads(state_file.read_text())
        verify_ledger(state, output, market_adapter)
        if state["protocolHash"] != manifest["files"]["protocol.json"] or state[
            "frozenMs"
        ] != stamp(manifest["frozenUtc"]):
            raise IntegrityError("Ledger identity mismatch")
    else:
        state = new_state(protocol, manifest)
    sha = digest(path)
    if sha in state["processed"]:
        return state
    raw_manifest = json.loads(path.read_text())
    if stamp(raw_manifest["startedUtc"]) < state["frozenMs"]:
        raise IntegrityError("Snapshot predates protocol freeze")
    if not path.resolve().is_relative_to(ROOT / "snapshots"):
        raise IntegrityError("Snapshot outside isolated data directory")
    market = market_adapter.load_market(path, max_age_seconds=protocol["max_response_age_seconds"])
    if (
        not 0
        <= int(time.time() * 1000) - market["ms"]
        <= protocol["max_response_age_seconds"] * 1000
    ):
        raise ValueError("New capture is stale or future relative to local clock")
    if market["ms"] < state["frozenMs"] or market["ms"] >= state["endMs"]:
        raise ValueError("Snapshot outside observation window")
    opposing = opposite_evidence(protocol, market["ms"])
    market["conflicts"] = opposing["blocked"]
    # JSON evidence uses decimal strings; arithmetic market quotes remain Decimal.
    market["signals"] = json.loads(json.dumps(market["signals"], default=str))
    directory = Path(tempfile.mkdtemp(prefix="opposite-", dir=output / "evidence"))
    evidence = directory / "ledger.json"
    atomic_json(evidence, opposing)
    record = {
        "path": str(path.resolve()),
        "oppositePath": str(evidence.resolve()),
        "oppositeHash": digest(evidence),
        "ms": market["ms"],
    }
    state = apply(state, market, protocol, sha, record)
    state["checkpointHash"] = state_hash(state)
    atomic_json(state_file, state)
    report(state, output / "STATUS.md")
    print(
        json.dumps(
            {
                "utc": iso(market["ms"]),
                "samples": len(state["processed"]),
                "signals": {b: s["eligible"] for b, s in market["signals"].items()},
                "oppositeBlocked": market["conflicts"],
                "phases": {b: p["phase"] for b, p in state["positions"].items()},
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return state


def finish(state, output, reason):
    output = validate_output(output)
    for p in state["positions"].values():
        if p["phase"] not in DONE:
            p["phase"] = (
                "study_ended_unresolved" if p["longHeld"] or p["shortHeld"] else "cancelled"
            )
            p["pending"] = None
    state["endedReason"] = reason
    state["checkpointHash"] = state_hash(state)
    atomic_json(output / "state.json", state)
    report(state, output / "STATUS.md", final=True)
    report(state, output / "FINAL_REPORT.md", final=True)
    atomic_json(
        output / "heartbeat.json",
        {"status": "ended", "utc": iso(int(time.time() * 1000)), "reason": reason},
    )
    print(f"STUDY_ENDED: {reason}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--run-dir", type=Path, default=ROOT / "run-v2")
    args = parser.parse_args()
    protocol = json.loads((ROOT / "protocol.json").read_text())
    output = validate_output(args.run_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "evidence").mkdir(exist_ok=True)
    with (output / "runner.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = prepare_run(output, protocol)
        market_adapter = adapter()
        state = new_state(protocol, manifest)
        if (output / "state.json").exists():
            state = json.loads((output / "state.json").read_text())
            verify_ledger(state, output, market_adapter)
        while int(time.time() * 1000) < state["endMs"]:
            try:
                prepare_run(output, protocol)
                if all(p["phase"] in DONE for p in state["positions"].values()):
                    break
                path = market_adapter.collect(ROOT / "snapshots")
                state = process(path, protocol, manifest, output, market_adapter)
                atomic_json(
                    output / "heartbeat.json", {"status": "ok", "utc": iso(state["lastMs"])}
                )
                if all(p["phase"] in DONE for p in state["positions"].values()):
                    break
            except Exception as error:
                atomic_json(
                    output / "heartbeat.json",
                    {
                        "status": "error",
                        "utc": iso(int(time.time() * 1000)),
                        "error": f"{type(error).__name__}: {error}",
                    },
                )
                print(f"COLLECTION_OR_LEDGER_ERROR: {type(error).__name__}: {error}", flush=True)
                if isinstance(error, IntegrityError) or not args.watch:
                    raise
            if not args.watch:
                return
            remaining = max(0, (state["endMs"] - int(time.time() * 1000)) / 1000)
            time.sleep(min(protocol["sampling_seconds"], remaining))
        prepare_run(output, protocol)
        if (output / "state.json").exists():
            state = json.loads((output / "state.json").read_text())
            verify_ledger(state, output, market_adapter)
        finish(
            state,
            output,
            "all cohorts terminal"
            if all(p["phase"] in DONE for p in state["positions"].values())
            else "five-day wall-clock bound",
        )


if __name__ == "__main__":
    main()
