"""Private Bybit backtest dashboard, intended only behind Tailscale Serve."""

import concurrent.futures
import csv
import datetime as dt
import fcntl
import io
import json
import math
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path

from backtest_config import BACKTEST_TIMEOUT_SECONDS, MAX_BACKTEST_MINUTES, validate_backtest_window
from correlation_data import DOWNLOADS, load_analysis, public_analysis
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
)
from market_config import INSTRUMENTS, SUPPORTED_INSTRUMENTS, TOP20_SNAPSHOT_DATE
from strategy_catalog import MULTI_STRATEGIES, STRATEGIES, strategy_params
from turtle_strategy import turtle_params

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(
    os.environ.get("BYBIT_WEB_DB", "/home/nicholas/data/bybit-backtest/bybit_spot_1m.sqlite3")
)
JOB_DIR = Path(os.environ.get("BYBIT_WEB_JOBS", str(ROOT / "jobs")))
OWNER = os.environ.get("BYBIT_WEB_OWNER", "").strip().lower()
if not OWNER:
    raise RuntimeError("BYBIT_WEB_OWNER is required")
STEPS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1H": 3_600_000,
    "4H": 14_400_000,
    "1D": 86_400_000,
}
MAX_CHART_MINUTES = 31 * 24 * 60
MAX_EXPORT_MINUTES = 7 * 24 * 60
MAX_SAVED_JOBS = 200
JOB_STALE_MS = (BACKTEST_TIMEOUT_SECONDS + 30) * 1000
RUN_LOCK = JOB_DIR / ".backtest.lock"
COLLECTOR = ROOT.parent / "collector.py"
SYNC_LOCK = ROOT.parent / "sync.lock"
RESEARCH_DIR = Path(
    os.environ.get("BYBIT_WEB_RESEARCH", str(ROOT.parent / "research" / "top20-current"))
)

os.umask(0o077)
JOB_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
app = Flask(__name__, static_folder="static", template_folder="templates")

# Serve under a Tailscale Serve path prefix when deployed as a platform module.
# Local/root deployments leave BYBIT_WEB_PREFIX unset and keep the original URLs.
WEB_PREFIX = os.environ.get("BYBIT_WEB_PREFIX", "").strip().rstrip("/")
if WEB_PREFIX:
    if not WEB_PREFIX.startswith("/"):
        raise RuntimeError("BYBIT_WEB_PREFIX must start with /")
    app.config["APPLICATION_ROOT"] = WEB_PREFIX

    class _PrefixMiddleware:
        """Strip the proxy path so Flask's existing routes and JS URLs keep working."""

        def __init__(self, wrapped, prefix):
            self.wrapped = wrapped
            self.prefix = prefix

        def __call__(self, environ, start_response):
            path = environ.get("PATH_INFO", "")
            if path == self.prefix:
                environ["PATH_INFO"] = "/"
            elif path.startswith(self.prefix + "/"):
                environ["PATH_INFO"] = path[len(self.prefix):]
            return self.wrapped(environ, start_response)

    app.wsgi_app = _PrefixMiddleware(app.wsgi_app, WEB_PREFIX)

app.config["MAX_CONTENT_LENGTH"] = 8 * 1024
app.config["TRUSTED_HOSTS"] = ["nicholas-4.tail068b64.ts.net", "127.0.0.1", "localhost"]
csrf_token = secrets.token_urlsafe(32)
executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="backtest")


def connection():
    if not DB_PATH.is_file():
        raise RuntimeError("数据库尚未就绪")
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA busy_timeout=5000")
    return db


def parse_int(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须为整数")
    if isinstance(value, int):
        return value
    if not isinstance(value, str) or not re.fullmatch(r"-?[0-9]{1,16}", value):
        raise ValueError(f"{name} 必须为整数")
    return int(value)


def parse_decimal(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} 必须为数值")
    try:
        raw = str(value)
        if len(raw) > 32:
            raise ValueError(f"{name} 过长")
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{name} 必须为数值") from exc
    if not number.is_finite():
        raise ValueError(f"{name} 必须为有限数值")
    return number


def portfolio_params(data):
    allocations = data.get("allocations")
    if not isinstance(allocations, list) or not 2 <= len(allocations) <= len(INSTRUMENTS):
        raise ValueError(f"屯币宝请选择 2 至 {len(INSTRUMENTS)} 个现货币种")
    seen, out, total = set(), [], Decimal(0)
    for allocation in allocations:
        if not isinstance(allocation, dict):
            raise ValueError("币种权重格式无效")
        inst = allocation.get("inst_id")
        if inst not in SUPPORTED_INSTRUMENTS or inst in seen:
            raise ValueError("币种无效或重复")
        weight = parse_decimal(allocation.get("weight_pct"), "币种权重")
        if not Decimal("0.01") <= weight < 100 or weight != weight.quantize(Decimal("0.0001")):
            raise ValueError("各币种权重须大于等于 0.01%、小于 100%，最多四位小数")
        seen.add(inst)
        total += weight
        out.append({"inst_id": inst, "weight_pct": float(weight)})
    if total != 100:
        raise ValueError(f"币种权重合计须为 100%，当前为 {total}%")
    mode = data.get("rebalance_mode", "threshold")
    if mode not in ("threshold", "interval"):
        raise ValueError("调仓模式无效")
    if mode == "threshold":
        threshold = parse_decimal(data.get("threshold_pct", 5), "偏离阈值")
        if not Decimal("0.1") <= threshold <= 50:
            raise ValueError("偏离阈值须为 0.1 至 50 个百分点")
        interval = 5
    else:
        threshold = Decimal(3)
        interval = parse_int(data.get("interval_minutes", 240), "调仓检查周期")
        if not 5 <= interval <= 43_200:
            raise ValueError("定时检查周期须为 5 至 43200 分钟")
    minimum = parse_decimal(data.get("min_trade_usdt", 5), "最小模拟成交额")
    if not 0 <= minimum <= 100_000:
        raise ValueError("最小模拟成交额须为 0 至 100000 USDT")
    return {
        "allocations": out,
        "rebalance_mode": mode,
        "threshold_pct": float(threshold),
        "interval_minutes": interval,
        "min_trade_usdt": float(minimum),
    }


def validate_initial_allocations(params):
    """Initial execution notional is known without looking up market prices."""
    fee_factor = Decimal(1) + Decimal(params["fee_bps"]) / 10_000
    capital = Decimal(params["capital"])
    minimum = Decimal(str(params["min_trade_usdt"]))
    tolerance = max(Decimal("0.000000001"), minimum * Decimal("0.000000000001"))
    for allocation in params["allocations"]:
        weight = Decimal(str(allocation["weight_pct"])) / 100
        notional = capital * weight / fee_factor
        if notional + tolerance < minimum:
            required = (minimum * fee_factor / weight).to_integral_value(rounding=ROUND_CEILING)
            raise ValueError(
                f"{allocation['inst_id']} 预计初始成交额 {notional:.2f} USDT，"
                f"低于单笔最低成交额 {minimum:.2f} USDT。"
                f"请降低该门槛（默认 5 USDT），或将组合初始资金提高至至少 {required:.2f} USDT。"
                "初始成交额已预留买入手续费。"
            )


def require_mutation():
    if request.headers.get("X-CSRF-Token") != csrf_token:
        abort(403)
    origin = request.headers.get("Origin")
    if origin and origin != "https://nicholas-4.tail068b64.ts.net":
        abort(403)


def validated_market_query(max_minutes):
    inst_id = request.args.get("inst")
    if inst_id not in SUPPORTED_INSTRUMENTS:
        raise ValueError("交易对无效")
    start, end = valid_window(request.args, max_minutes)
    return inst_id, start, end


def acquire_run_lock():
    fd = os.open(RUN_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def valid_window(data, max_minutes):
    now = int(time.time() * 1000)
    start = parse_int(data.get("start"), "start")
    end = parse_int(data.get("end"), "end")
    if start < 1_500_000_000_000 or end > now + 60_000 or end <= start:
        raise ValueError("时间区间无效")
    if start % 60_000 or end % 60_000:
        raise ValueError("时间必须按整分钟对齐")
    if max_minutes == MAX_BACKTEST_MINUTES:
        validate_backtest_window(start, end)
        return start, end
    if (end - start) // 60_000 > max_minutes:
        raise ValueError(f"时间范围最多 {max_minutes // 1440} 天")
    return start, end


def save_job(job):
    job_id = job["id"]
    if not re.fullmatch(r"[0-9a-f]{24}", job_id):
        raise ValueError("Invalid job ID")
    path = JOB_DIR / f"{job_id}.json"
    tmp = JOB_DIR / f".{job_id}.tmp"
    tmp.write_text(json.dumps(job, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def load_job(job_id):
    if not re.fullmatch(r"[0-9a-f]{24}", job_id):
        abort(404)
    path = JOB_DIR / f"{job_id}.json"
    if not path.is_file():
        abort(404)
    job = json.loads(path.read_text(encoding="utf-8"))
    if (
        job["state"] in ("queued", "running")
        and int(time.time() * 1000) - job["updated_at_ms"] > JOB_STALE_MS
    ):
        job["state"] = "error"
        job["error"] = "服务重启或任务中断，请重新运行回测"
        job["updated_at_ms"] = int(time.time() * 1000)
        save_job(job)
    return job


def run_job(job, lock_fd):
    try:
        job["state"] = "running"
        job["updated_at_ms"] = int(time.time() * 1000)
        save_job(job)
        result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            [sys.executable, "-I", str(ROOT / "backtest_worker.py")],
            input=json.dumps(job["params"]),
            text=True,
            capture_output=True,
            timeout=BACKTEST_TIMEOUT_SECONDS,
            cwd=str(ROOT),
            env={"LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1", "BYBIT_WEB_DB": str(DB_PATH)},
            check=False,
        )
        if result.returncode:
            raise RuntimeError((result.stderr or "回测进程失败")[-600:])
        job["result"] = json.loads(result.stdout)
        job["state"] = "done"
    except subprocess.TimeoutExpired:
        job["state"] = "error"
        job["error"] = "回测超过 10 分钟计算时限，请缩小区间或降低调仓频率"
    except Exception as exc:
        job["state"] = "error"
        job["error"] = str(exc)[:600]
    finally:
        job["updated_at_ms"] = int(time.time() * 1000)
        try:
            save_job(job)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


@app.before_request
def require_tailnet_owner():
    if request.path == "/healthz":
        return
    login = request.headers.get("Tailscale-User-Login", "").lower()
    if login != OWNER:
        abort(403)


@app.after_request
def security_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    )
    return response


@app.get("/healthz")
def health():
    return {"ok": True}


@app.get("/")
def index():
    return render_template(
        "index.html",
        csrf=csrf_token,
        instruments=INSTRUMENTS,
        universe_date=TOP20_SNAPSHOT_DATE,
        research_strategies=STRATEGIES,
        prefix=WEB_PREFIX,
    )


@app.get("/correlation")
def correlation_page():
    return render_template("correlation.html", prefix=WEB_PREFIX)


@app.get("/api/correlation")
def correlation_analysis():
    try:
        return jsonify(public_analysis(load_analysis(RESEARCH_DIR)))
    except (OSError, ValueError, KeyError, TypeError):
        app.logger.warning("correlation snapshot unavailable or invalid")
        return jsonify({"error": "相关性数据尚未就绪或校验失败，请稍后重试"}), 503


@app.get("/api/correlation/download/<name>")
def correlation_download(name):
    # Only named exports are served, with the same owner check as the page/API.
    if name not in DOWNLOADS:
        abort(404)
    path = RESEARCH_DIR / name
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype=DOWNLOADS[name], as_attachment=True, download_name=name)


@app.get("/api/status")
def status():
    db = connection()
    try:
        pairs = []
        for inst_id in INSTRUMENTS:
            row = db.execute(
                "SELECT COUNT(*) n, MIN(ts_ms) first, MAX(ts_ms) last "
                "FROM candles_1m WHERE symbol=?",
                (inst_id,),
            ).fetchone()
            state = db.execute(
                "SELECT backfill_complete, backfill_cursor_ms, latest_sync_ms, last_error "
                "FROM ingest_state WHERE symbol=?",
                (inst_id,),
            ).fetchone()
            n, first, last = row["n"], row["first"], row["last"]
            expected = ((last - first) // 60_000 + 1) if n else 0
            lag_ms = max(0, int(time.time() * 1000) - (last + 60_000)) if last else None
            pairs.append(
                {
                    "inst_id": inst_id,
                    "rows": n,
                    "first_ms": first,
                    "last_ms": last,
                    "missing_minutes": expected - n,
                    "lag_ms": lag_ms,
                    "fresh": lag_ms is not None and lag_ms <= 10 * 60_000,
                    "backfill_complete": bool(state["backfill_complete"]) if state else False,
                    "backfill_cursor_ms": state["backfill_cursor_ms"] if state else None,
                    "latest_sync_ms": state["latest_sync_ms"] if state else None,
                    "last_error": state["last_error"] if state else None,
                }
            )
        return jsonify({"pairs": pairs, "server_time_ms": int(time.time() * 1000)})
    finally:
        db.close()


@app.get("/api/candles")
def candles():
    try:
        inst_id, start, end = validated_market_query(MAX_CHART_MINUTES)
        step_name = request.args.get("step", "15m")
        if step_name not in STEPS:
            raise ValueError("K 线周期无效")
        step = STEPS[step_name]
        if (end - start) // step > 2000:
            raise ValueError("图表最多显示 2000 根 K 线，请缩小时间区间或增大周期")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    db = connection()
    try:
        rows = db.execute(
            """SELECT ts_ms,o,h,l,c,turnover FROM candles_1m
            WHERE symbol=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms""",
            (inst_id, start, end),
        )
        out = []
        current = None
        for row in rows:
            bucket = row["ts_ms"] // step * step
            if current is None or current[0] != bucket:
                current = [
                    bucket,
                    float(row["o"]),
                    float(row["h"]),
                    float(row["l"]),
                    float(row["c"]),
                    float(row["turnover"] or 0),
                ]
                out.append(current)
            else:
                current[2] = max(current[2], float(row["h"]))
                current[3] = min(current[3], float(row["l"]))
                current[4] = float(row["c"])
                current[5] += float(row["turnover"] or 0)
        return jsonify({"inst_id": inst_id, "step": step_name, "candles": out})
    finally:
        db.close()


@app.get("/api/quality")
def quality():
    try:
        inst_id, start, end = validated_market_query(MAX_CHART_MINUTES)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    expected = (end - start) // 60_000
    present = invalid = 0
    gaps = []
    previous = start - 60_000
    db = connection()
    try:
        rows = db.execute(
            """SELECT ts_ms,o,h,l,c FROM candles_1m
            WHERE symbol=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms""",
            (inst_id, start, end),
        )
        for ts, o, h, low_price, c in rows:
            if ts > previous + 60_000 and len(gaps) < 20:
                gaps.append(
                    {
                        "start_ms": previous + 60_000,
                        "end_ms": ts,
                        "minutes": (ts - previous) // 60_000 - 1,
                    }
                )
            try:
                opened, high, low, closed = map(float, (o, h, low_price, c))
                if (
                    not all(math.isfinite(v) and v > 0 for v in (opened, high, low, closed))
                    or high < max(opened, closed)
                    or low > min(opened, closed)
                    or high < low
                ):
                    invalid += 1
            except (TypeError, ValueError):
                invalid += 1
            present += 1
            previous = ts
        if end > previous + 60_000 and len(gaps) < 20:
            gaps.append(
                {
                    "start_ms": previous + 60_000,
                    "end_ms": end,
                    "minutes": (end - previous) // 60_000 - 1,
                }
            )
    finally:
        db.close()
    return jsonify(
        {
            "inst_id": inst_id,
            "start_ms": start,
            "end_ms": end,
            "expected": expected,
            "present": present,
            "missing": expected - present,
            "invalid": invalid,
            "coverage_pct": round(present / expected * 100, 3),
            "gaps": gaps,
            "gaps_truncated": expected - present > sum(g["minutes"] for g in gaps),
        }
    )


def csv_number(value):
    if value is None:
        return ""
    raw = str(value)
    if len(raw) > 64:
        raise ValueError("数据值过长")
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("数据中存在无效数值") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("数据中存在无效数值")
    if abs(number.adjusted()) > 100:
        raise ValueError("数据值超出导出范围")
    return format(number, "f")


@app.get("/api/candles.csv")
def export_candles():
    try:
        inst_id, start, end = validated_market_query(MAX_EXPORT_MINUTES)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    db = connection()
    try:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("ts_utc", "symbol", "open", "high", "low", "close", "vol", "turnover"))
        rows = db.execute(
            """SELECT ts_ms,o,h,l,c,vol,turnover FROM candles_1m
            WHERE symbol=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms""",
            (inst_id, start, end),
        )
        for ts, o, h, low_price, c, vol, turnover in rows:
            writer.writerow(
                (
                    dt.datetime.fromtimestamp(ts / 1000, dt.UTC).isoformat(),
                    inst_id,
                    *(csv_number(v) for v in (o, h, low_price, c, vol, turnover)),
                )
            )
        response = Response(buffer.getvalue(), content_type="text/csv; charset=utf-8")
        response.headers["Content-Disposition"] = (
            f'attachment; filename="{inst_id}-{start}-{end}.csv"'
        )
        return response
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 422
    finally:
        db.close()


@app.post("/api/backtests")
def create_backtest():
    require_mutation()
    if not request.is_json:
        abort(403)
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            raise ValueError("请求格式无效")
        inst_id = data.get("inst_id")
        strategy = data.get("strategy")
        if strategy not in ("sma_cross", "rsi_reversion", "smart_portfolio", "turtle", *STRATEGIES):
            raise ValueError("交易对或策略无效")
        if (
            strategy not in ("smart_portfolio", *MULTI_STRATEGIES)
            and inst_id not in SUPPORTED_INSTRUMENTS
        ):
            raise ValueError("交易对无效")
        if (
            strategy in STRATEGIES
            and strategy not in MULTI_STRATEGIES
            and inst_id not in INSTRUMENTS
        ):
            raise ValueError("新增策略请选择当前 Top20 交易对")
        start, end = valid_window(data, MAX_BACKTEST_MINUTES)
        fee = parse_int(data.get("fee_bps", 10), "手续费")
        slippage = parse_int(data.get("slippage_bps", 5), "滑点")
        capital = parse_int(data.get("capital", 10_000), "初始资金")
        if not 0 <= fee <= 100 or not 0 <= slippage <= 100 or not 100 <= capital <= 10_000_000:
            raise ValueError("费用、滑点或资金超出允许范围")
        params = {
            "strategy": strategy,
            "start": start,
            "end": end,
            "fee_bps": fee,
            "slippage_bps": slippage,
            "capital": capital,
        }
        if strategy == "smart_portfolio":
            params.update(portfolio_params(data))
            validate_initial_allocations(params)
        elif strategy == "sma_cross":
            fast = parse_int(data.get("fast"), "快线")
            slow = parse_int(data.get("slow"), "慢线")
            if not 2 <= fast < slow <= 1000:
                raise ValueError("均线周期需满足 2 ≤ 快线 < 慢线 ≤ 1000")
            params.update(fast=fast, slow=slow)
        elif strategy == "turtle":
            params.update(turtle_params({**data, "start": start, "end": end}))
        elif strategy in STRATEGIES:
            params.update(strategy_params({**data, "start": start, "end": end}))
        else:
            period = parse_int(data.get("period"), "RSI 周期")
            buy = parse_int(data.get("buy_below"), "买入阈值")
            sell = parse_int(data.get("sell_above"), "卖出阈值")
            if not 5 <= period <= 100 or not 5 <= buy <= 45 or not 55 <= sell <= 95:
                raise ValueError("RSI 参数超出允许范围")
            params.update(period=period, buy_below=buy, sell_above=sell)
        if strategy not in ("smart_portfolio", *MULTI_STRATEGIES):
            params["inst_id"] = inst_id
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if sum(1 for _ in JOB_DIR.glob("[0-9a-f]" * 24 + ".json")) >= MAX_SAVED_JOBS:
        return jsonify({"error": "已保存 200 个回测，请先在历史记录中删除不需要的结果"}), 429
    lock_fd = acquire_run_lock()
    if lock_fd is None:
        return jsonify({"error": "已有回测在运行，请稍后重试"}), 429
    now = int(time.time() * 1000)
    job = {
        "id": secrets.token_hex(12),
        "state": "queued",
        "params": params,
        "created_at_ms": now,
        "updated_at_ms": now,
    }
    try:
        save_job(job)
        executor.submit(run_job, job, lock_fd)
    except Exception:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        raise
    app.logger.info("backtest started id=%s inst=%s strategy=%s", job["id"], inst_id, strategy)
    return jsonify({"id": job["id"], "state": job["state"]}), 202


@app.get("/api/backtests/<job_id>")
def get_backtest(job_id):
    return jsonify(load_job(job_id))


@app.get("/api/backtests/<job_id>/benchmark")
def compare_buy_hold(job_id):
    """Read-only comparison for completed jobs, including older saved results."""
    job = load_job(job_id)
    if job["state"] != "done":
        return jsonify({"error": "请先完成回测再选择持有对照"}), 409
    inst = request.args.get("inst")
    if inst not in SUPPORTED_INSTRUMENTS:
        return jsonify({"error": "对照交易对无效"}), 400
    saved, strategy_result = job["params"], job["result"]
    start, end = valid_window(saved, MAX_BACKTEST_MINUTES)
    params = {
        "strategy": "buy_hold_benchmark",
        "inst_id": inst,
        "start": start,
        "end": end,
        "capital": saved["capital"],
        "fee_bps": saved["fee_bps"],
        "slippage_bps": saved["slippage_bps"],
        "valuation_version": strategy_result.get("engine_version", 2),
    }
    lock_fd = acquire_run_lock()
    if lock_fd is None:
        return jsonify({"error": "已有回测或对照计算在运行，请稍后重试"}), 429
    try:
        completed = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            [sys.executable, "-I", str(ROOT / "backtest_worker.py")],
            input=json.dumps(params),
            capture_output=True,
            text=True,
            timeout=65,
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "PYTHONNOUSERSITE": "1",
                "BYBIT_WEB_DB": str(DB_PATH),
            },
            check=False,
        )
        if completed.returncode:
            return jsonify(
                {
                    "error": f"{inst} 持有对照未完成："
                    + (completed.stderr.strip()[-300:] or "计算进程异常退出")
                }
            ), 422
        benchmark = json.loads(completed.stdout)
        return jsonify(
            {
                "job_id": job_id,
                "benchmark": benchmark,
                "computed_at_ms": int(time.time() * 1000),
                "strategy_return_pct": strategy_result["return_pct"],
                "excess_return_pp": round(
                    strategy_result["return_pct"] - benchmark["return_pct"], 6
                ),
                "equity_difference_usdt": round(
                    strategy_result["final_equity"] - benchmark["final_equity"], 6
                ),
            }
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "持有对照计算超过 65 秒限制"}), 504
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


@app.get("/api/backtests")
def list_backtests():
    paths = sorted(
        JOB_DIR.glob("[0-9a-f]" * 24 + ".json"), key=lambda p: p.stat().st_mtime, reverse=True
    )[:20]
    return jsonify(
        {
            "jobs": [
                {k: v for k, v in load_job(path.stem).items() if k != "result"} for path in paths
            ],
            "max_saved": MAX_SAVED_JOBS,
        }
    )


@app.get("/api/backtests/<job_id>/download")
def download_backtest(job_id):
    job = load_job(job_id)
    response = Response(
        json.dumps(job, ensure_ascii=False, indent=2) + "\n",
        content_type="application/json; charset=utf-8",
    )
    response.headers["Content-Disposition"] = f'attachment; filename="backtest-{job_id}.json"'
    return response


@app.delete("/api/backtests/<job_id>")
def delete_backtest(job_id):
    require_mutation()
    job = load_job(job_id)
    if job["state"] in ("queued", "running"):
        return jsonify({"error": "运行中的回测不能删除"}), 409
    (JOB_DIR / f"{job_id}.json").unlink()
    app.logger.info("backtest deleted id=%s", job_id)
    return jsonify({"deleted": job_id})


@app.post("/api/ingest/sync")
def sync_now():
    require_mutation()
    if not COLLECTOR.is_file():
        return jsonify({"error": "采集脚本不存在"}), 503
    try:
        result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            [
                "/usr/bin/flock",
                "-E",
                "75",
                "-n",
                str(SYNC_LOCK),
                sys.executable,
                str(COLLECTOR),
                "sync",
            ],
            capture_output=True,
            text=True,
            timeout=65,
            cwd=str(ROOT.parent),
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1"},
            check=False,
        )
    except subprocess.TimeoutExpired:
        app.logger.warning("manual sync timed out")
        return jsonify({"error": "同步超过 65 秒限制"}), 504
    if result.returncode == 75:
        return jsonify({"error": "定时同步正在运行，请稍后再试"}), 409
    if result.returncode:
        app.logger.warning("manual sync failed exit=%s", result.returncode)
        return jsonify(
            {"error": "同步失败，请查看服务器 sync.log", "detail": result.stderr[-300:]}
        ), 502
    app.logger.info("manual sync completed")
    return jsonify({"ok": True, "message": "同步完成", "summary": result.stdout[-1200:]})


@app.errorhandler(403)
def forbidden(_):
    return "Forbidden", 403


@app.errorhandler(404)
def missing(_):
    return "Not found", 404
