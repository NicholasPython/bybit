"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const text = (value, fallback = "—") => value === null || value === undefined || value === "" ? fallback : String(value);
  const node = (tag, value, className) => {
    const item = document.createElement(tag);
    if (value !== undefined) item.textContent = text(value);
    if (className) item.className = className;
    return item;
  };
  const showError = (target, message) => {
    target.textContent = message;
    target.hidden = !message;
  };
  const amount = (value, unit = "") => {
    if (value === null || value === undefined) return "不可用";
    const exact = String(value).replace(/(\.\d*?[1-9])0+$|\.0+$/, "$1");
    return `${exact}${unit ? ` ${unit}` : ""}`;
  };
  const stamp = (value) => {
    if (!value) return "尚无记录";
    const parsed = new Date(value);
    return Number.isFinite(parsed.getTime()) ? parsed.toISOString().replace("T", " ").replace(/\.\d+Z$/, " UTC") : "时间无效";
  };
  const age = (value) => value === null || value === undefined ? "未知" : `${Number(value).toFixed(1)} 秒`;
  const stateNames = {
    STARTING: "正在启动", RECONCILING: "正在对账", RUNNING: "运行中", PAUSED_OPEN: "暂停新增风险",
    EXITING: "正在退出", STOPPED: "已停止", PREPARED: "已准备", SUBMITTING: "提交中",
    ACCEPTED: "已受理", PARTIALLY_FILLED: "部分成交", FILLED: "已成交", CANCELED: "已撤销",
    REJECTED: "已拒绝", EXPIRED: "交易所已失效", UNKNOWN: "结果未知", NONE: "未发送",
    PENDING: "待处理", CONFIRMED: "已核实", UNCERTAIN: "待核实", EXECUTING: "执行中",
    EXECUTED: "已执行", FAILED: "失败 / 待核实", ACTIVE: "活动中", DELIVERED: "已送达",
    RESOLVED: "已恢复", CRITICAL: "严重", WARNING: "警告", INFO: "提示",
    ELIGIBLE: "具备执行资格", NEEDS_CONFIRMATION: "等待接管确认", LOST: "执行权丢失",
    NOT_HELD: "未持有", CONTENDED: "其他执行者持有", RELEASED: "已释放",
  };
  const kindNames = {
    PAUSE_NEW_RISK: "暂停新增风险", CANCEL_BOT_OPEN_ORDERS: "撤销 bot 开仓单",
    CANCEL_ORDER: "撤销指定 bot 订单", REDUCE: "受限减仓", RESUME: "恢复指定暂停",
    CONFIRM_TAKEOVER: "确认执行者接管", INITIALIZE_BASELINE: "建立首次风险基线",
    REGISTER_EXTERNAL_CASH_FLOW: "登记外部资金流",
  };
  const state = (value) => text(stateNames[value], text(value, "未知"));
  const badge = (value, label) => {
    const severity = ["UNKNOWN", "UNCERTAIN", "CRITICAL", "LOST", "FAILED"].includes(value) ? "danger"
      : ["PENDING", "EXECUTING", "PAUSED_OPEN", "WARNING", "NEEDS_CONFIRMATION"].includes(value) ? "warn" : "";
    return node("span", label || state(value), `badge ${severity}`);
  };
  const commandNotice = (row) => {
    if (["PENDING", "EXECUTING"].includes(row.status)) {
      return `命令 ${row.command_id}：${state(row.status)}。请跟踪服务写入的执行结果；待处理不等于交易完成。`;
    }
    const result = row.result && Object.keys(row.result).length ? `服务结果：${JSON.stringify(row.result)}。` : "服务未写入结果字段。";
    return `命令 ${row.command_id}：${state(row.status)}。${result}`;
  };
  const stacked = (first, second, numeric = false) => {
    const box = node("div", first, numeric ? "number" : "");
    if (second) box.append(node("span", second, "muted"));
    return box;
  };
  const table = (id, rows, columns, empty) => {
    const body = $(id);
    body.replaceChildren();
    if (!rows || rows.length === 0) {
      const cell = node("td", empty || "当前环境暂无记录。", "empty-state");
      cell.colSpan = columns.length;
      const row = node("tr"); row.append(cell); body.append(row);
      return;
    }
    for (const item of rows) {
      const row = node("tr");
      for (const column of columns) {
        const definition = typeof column === "function" ? { value: column } : column;
        const cell = node("td", undefined, definition.className);
        const content = definition.value(item);
        cell.append(content instanceof Node ? content : document.createTextNode(text(content)));
        row.append(cell);
      }
      body.append(row);
    }
  };
  const request = async (path, options = {}) => {
    const response = await fetch(path, { credentials: "same-origin", ...options, signal: AbortSignal.timeout(12000) });
    let data = {};
    if (response.status !== 204) data = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401 && path !== "/api/login") location.assign("/login");
      const error = new Error(data.detail || `请求未完成（${response.status}）。`);
      error.status = response.status;
      throw error;
    }
    return data;
  };
  const post = (path, body, csrf) => request(path, {
    method: "POST", headers: { "Content-Type": "application/json", ...(csrf ? { "X-CSRF-Token": csrf } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });

  if (document.body.dataset.view === "login") {
    $("login-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = event.submitter;
      button.disabled = true;
      showError($("login-error"), "");
      try {
        await post("/api/login", { username: $("username").value, password: $("password").value });
        $("password").value = "";
        location.assign("/");
      } catch (error) {
        showError($("login-error"), error.status ? error.message : "无法连接管理服务，请检查网络后重试。");
        $("password").value = "";
        $("password").focus();
      } finally { button.disabled = false; }
    });
    return;
  }

  let session = null;
  let snapshot = null;
  let refreshing = false;
  let current = null;
  let submitted = null;
  let sending = false;
  let pending = null;
  let noticeCommandId = null;
  const pendingKey = "bybitbot-pending-command-v1";
  try { pending = JSON.parse(sessionStorage.getItem(pendingKey) || "null"); } catch { /* Private browsing may disable storage. */ }
  const savePending = (value) => {
    pending = value;
    try { if (value) sessionStorage.setItem(pendingKey, JSON.stringify(value)); else sessionStorage.removeItem(pendingKey); }
    catch { /* The dialog still retains its original ID during this page visit. */ }
    $("retry-pending").hidden = !value;
  };
  const action = (title, kind, payload) => {
    const button = node("button", title, "button secondary small");
    button.type = "button";
    button.addEventListener("click", () => openCommand(kind, payload));
    return button;
  };
  const specsFor = (symbol) => snapshot?.instruments?.find((spec) => spec.symbol === symbol);
  const moneyUnit = (row) => {
    const spec = specsFor(row.symbol);
    return (row.category === "option" ? spec?.settle_coin : spec?.quote_coin) || "币种未核实";
  };
  const qtyUnit = (row) => row.category === "option" ? "合约" : (specsFor(row.symbol)?.base_coin || "基础币");
  const activePauses = () => (snapshot?.pauses || []).filter((pause) => pause.active);

  function render(data) {
    snapshot = data;
    const runtime = data.runtime || {};
    const account = data.account;
    const heartbeat = data.heartbeat || {};
    const currency = data.currency;
    const pauses = activePauses();
    $("environment").textContent = data.environment === "paper" ? "paper 模拟" : data.environment;
    $("status-heading").textContent = heartbeat.fresh ? state(runtime.state) : "交易服务心跳失效";
    $("status-description").textContent = heartbeat.fresh
      ? (data.environment === "paper" ? "模拟环境使用配置资金与保守可见深度撮合；模拟结果不能证明实际成交能力。" : "以持久化交易记录为准；执行资格、风控与对账检查仍由交易服务判定。")
      : "页面可以读取历史记录，但尚未确认交易服务在线。已有交易所挂单与持仓可能仍存在。";
    $("heartbeat").textContent = heartbeat.at ? `${age(heartbeat.age_seconds)}前 · ${heartbeat.fresh ? "有效" : "失效"}` : "尚无心跳";
    $("heartbeat").title = stamp(heartbeat.at);
    $("account-age").textContent = account ? `${age(account.age_seconds)}前 · ${account.stale ? "已过期" : account.complete ? "完整" : "不完整"}` : "尚未采集";
    $("account-age").title = stamp(account?.captured_at);
    $("lease").textContent = typeof runtime.lease === "object" && runtime.lease ? state(runtime.lease.state) : state(runtime.lease);
    $("source").textContent = text(runtime.source, "尚未记录");
    $("pause-summary").replaceChildren();
    if (pauses.length) {
      $("pause-summary").append(node("strong", `${pauses.length} 条活动暂停`));
      const list = node("ul");
      for (const pause of pauses.slice(0, 6)) list.append(node("li", `#${pause.id} ${pause.reason_code}：${pause.reason_detail}（${pause.locked ? "锁定，需按规则解除" : "重新同步后校验"}）`));
      if (pauses.length > 6) list.append(node("li", "其余暂停请在人工控制中查看。"));
      $("pause-summary").append(list);
    } else $("pause-summary").append(node("p", "没有活动暂停记录；仍须具备执行资格、可信行情及完整风险基线。", "muted"));
    $("account-identity").textContent = account ? `账户 ${account.account_uid} · ${account.account_mode} · ${stamp(account.captured_at)}` : "尚无账户快照";
    $("valuation-version").textContent = `口径 ${text(data.configuration?.valuation?.version, text(data.configuration?.valuation?.policy_version))}`;
    $("baseline-note").textContent = data.baseline
      ? `${data.baseline.day}：${data.baseline.complete ? "完整交易日基线" : "首次接入，不完整交易日"}，建立于 ${stamp(data.baseline.established_at)}；历史回撤高点不会因重启或换日重置。`
      : "尚无风险基线。完整估值及对账通过后，可在策略与风控中明确建立首次风险起点；首日仍为不完整交易日。";
    $("baseline-action").hidden = !!data.baseline;
    $("config-version").textContent = runtime.config_version ? `当前配置 ${runtime.config_version}` : "尚无生效版本";
    table("coins-body", account?.coins, [
      (r) => r.coin, ...["equity", "available", "locked", "borrowed", "accrued_interest", "usd_value"].map((key) => ({ value: (r) => amount(r[key], key === "usd_value" ? "USD" : r.coin), className: "numeric" })),
    ], "尚未采集账户快照。请检查交易服务与账户连接。");
    table("valuation-body", [runtime], [
      () => currency,
      { value: (r) => r.valuation_complete ? amount(r.equity, currency) : "估值不可用", className: "numeric" },
      ...["daily_pnl", "drawdown"].map((key) => ({ value: (r) => r.baseline_available ? amount(r[key], currency) : "基线未就绪", className: "numeric" })),
      (r) => badge(r.valuation_complete ? "CONFIRMED" : "UNCERTAIN", r.valuation_complete ? "完整估值" : "估值不可用"),
      (r) => r.baseline_available ? "已建立" : "不可用于开仓",
    ]);
    table("positions-body", data.positions, [
      (r) => stacked(r.symbol, r.category), (r) => text(r.side),
      { value: (r) => amount(r.size, qtyUnit(r)), className: "numeric" },
      { value: (r) => amount(r.avg_price, moneyUnit(r)), className: "numeric" },
      { value: (r) => amount(r.mark_price, moneyUnit(r)), className: "numeric" },
      (r) => text(r.settle_coin), (r) => badge(r.trusted ? "CONFIRMED" : "UNCERTAIN", r.trusted ? "可信" : "待对账"), (r) => stamp(r.updated_at),
    ], "暂无已同步的账户持仓。现货余额请同时查看账户余额表。");
    const strategyName = (id) => data.strategies?.find((s) => s.id === id)?.name || `策略 ${id}`;
    table("inventory-body", data.inventory, [
      (r) => strategyName(r.strategy_id), (r) => r.symbol,
      { value: (r) => amount(r.qty, specsFor(r.symbol)?.category === "option" ? "合约" : specsFor(r.symbol)?.base_coin || "基础币"), className: "numeric" },
      { value: (r) => amount(r.avg_cost, r.currency), className: "numeric" }, (r) => r.currency,
      (r) => stacked(r.trusted ? "可信" : "待对账", r.cost_source), (r) => stamp(r.updated_at),
      (r) => r.trusted && r.qty && !/^0(?:\.0*)?$/.test(r.qty) ? action("受限减仓", "REDUCE", { strategy_id: r.strategy_id, category: specsFor(r.symbol)?.category || "spot", symbol: r.symbol, qty: amount(r.qty) }) : "待核实库存",
    ], "暂无 bot 归属库存。外部账户资产不会自动成为策略库存。");
    table("orders-body", data.orders, [
      (r) => stacked(r.order_link_id, `意图 ${r.intent_id}`, true), (r) => stacked(r.symbol, r.category), (r) => r.side,
      ...["qty", "qty_filled"].map((key) => ({ value: (r) => amount(r[key], qtyUnit(r)), className: "numeric" })),
      { value: (r) => amount(r.price, moneyUnit(r)), className: "numeric" },
      (r) => stacked(state(r.status), `同步：${state(r.sync_state)}`), (r) => stamp(r.created_at),
      (r) => ["FILLED", "CANCELED", "REJECTED", "EXPIRED"].includes(r.status) ? "终态记录" : action("核对并撤单", "CANCEL_ORDER", { order_link_ids: [r.order_link_id] }),
    ], "暂无物理订单。策略默认禁用时，不会自动生成委托。");
    table("executions-body", data.executions, [
      (r) => stacked(r.exchange_exec_id, r.order_link_id, true), (r) => r.symbol, (r) => r.side,
      { value: (r) => amount(r.exec_qty, qtyUnit(r)), className: "numeric" },
      { value: (r) => amount(r.exec_price, moneyUnit(r)), className: "numeric" },
      { value: (r) => amount(r.exec_fee, r.exec_fee_asset), className: "numeric" },
      (r) => r.exec_fee_asset, (r) => stamp(r.exec_time),
    ], "暂无成交事实。委托已受理不代表已有成交。");
    table("risk-body", data.risk_rejects, [
      (r) => stacked(`意图 ${r.intent_id}`, r.signal_id), (r) => r.symbol,
      { value: (r) => stacked(r.reason_code, r.reason_detail), className: "wrap" }, (r) => r.config_version, (r) => stamp(r.created_at),
    ], "暂无风控拒绝记录。");
    table("quotes-body", data.quotes, [
      (r) => stacked(r.symbol, r.category), (r) => stamp(r.source_ts), (r) => stamp(r.received_at),
      { value: (r) => age(r.age_seconds), className: "numeric" },
      (r) => badge(r.stale ? "UNCERTAIN" : "CONFIRMED", r.anomalous ? "异常，拒绝使用" : r.stale ? "已过期" : "时效有效"),
    ], "暂无持久化决策行情快照；交易服务会在生成意图时保存引用。");
    table("reconciliations-body", data.reconciliations, [
      (r) => stacked(`#${r.id}`, r.scope), (r) => stamp(r.started_at), (r) => stamp(r.finished_at),
      { value: (r) => r.difference_count, className: "numeric" }, (r) => r.repaired ? "有修复" : "无修复记录",
      (r) => badge(r.allows_resume ? "CONFIRMED" : "UNCERTAIN", r.allows_resume ? "对账允许" : "不允许"),
      { value: (r) => { const details = node("div"); for (const d of (r.differences || [])) details.append(node("p", `${d.kind}：${d.detail}`)); return details.childNodes.length ? details : "无差异"; }, className: "wrap" },
    ], "暂无对账记录。恢复开仓前必须完成对账。");
    table("alerts-body", data.alerts, [
      (r) => badge(r.severity), { value: (r) => stacked(r.title, r.detail), className: "wrap" },
      (r) => state(r.status), (r) => stamp(r.first_seen_at), (r) => stamp(r.resolved_at),
    ], "暂无告警记录。");
    table("strategies-body", data.strategies, [
      (r) => stacked(r.name, `编号 ${r.id}`), (r) => r.kind === "spot_fixed_budget_dca" ? "定时预算现货买入" : "单腿期权买入",
      (r) => r.enabled ? "启用" : "禁用",
      (r) => data.config_versions?.find((v) => v.strategy_id === r.id && v.active)?.version || "未生效",
      (r) => { const v = data.config_versions?.find((v) => v.strategy_id === r.id && v.active); return stacked(v?.symbols?.join(", ") || "尚无配置", v?.schedule_utc?.join(", ")); },
      ...["budget_per_run", "max_spend_per_trade"].map((key) => ({ value: (r) => { const v = data.config_versions?.find((v) => v.strategy_id === r.id && v.active); return amount(v?.[key], v?.currency); }, className: "numeric" })),
      (r) => data.config_versions?.find((v) => v.strategy_id === r.id && v.active)?.currency || currency,
    ], "暂无持久化策略实例。请启动交易服务以校验并载入配置。");
    renderStrategyParams(data);
    renderLimits(data.configuration);
    table("pauses-body", data.pauses, [
      (r) => stacked(`#${r.id}`, `${r.scope}${r.scope_key ? ` ${r.scope_key}` : ""}`),
      { value: (r) => stacked(r.reason_code, r.reason_detail), className: "wrap" },
      (r) => r.locked ? "需按规则人工解除" : "技术暂停", (r) => r.active ? "活动中" : "已解除",
      (r) => stamp(r.created_at), (r) => r.resolved_at ? stacked(stamp(r.resolved_at), r.resolved_by) : "尚未解除",
      (r) => r.active ? action("核验并恢复", "RESUME", { pause_id: r.id }) : "历史记录",
    ], "暂无暂停记录。");
    table("commands-body", data.control_commands, [
      (r) => node("span", r.command_id, "number"),
      { value: (r) => stacked(kindNames[r.kind] || r.kind, scopeText(r.kind, r.payload, data.environment)), className: "wrap" },
      (r) => badge(r.status), (r) => r.requested_by, (r) => stamp(r.created_at),
      { value: (r) => ["PENDING", "EXECUTING"].includes(r.status) ? "等待交易服务写入最终结果" : JSON.stringify(r.result || {}), className: "wrap" },
    ], "暂无控制命令。所有命令均会保留编号、操作者与结果。");
    $("refresh-at").textContent = `读取于 ${stamp(data.generated_at)}`;
    $("row-limit").textContent = `每表显示最近 ${data.table_limit} 条记录`;
    document.querySelectorAll("[data-command]").forEach((button) => { button.disabled = !session; });
    const commandRows = data.control_commands || [];
    if (pending && pending.environment === data.environment && commandRows.some((row) => row.command_id === pending.body?.command_id)) {
      noticeCommandId = pending.body.command_id;
      savePending(null);
    }
    const tracked = noticeCommandId ? commandRows.find((row) => row.command_id === noticeCommandId) : null;
    if (tracked) $("command-notice").textContent = commandNotice(tracked);
  }

  const PARAM_LABELS = {
    options_type: "期权类型",
    min_days_to_expiry: "最少到期天数",
    max_days_to_expiry: "最多到期天数",
    delta_low: "Delta 下界",
    delta_high: "Delta 上界",
    strike_low_pct: "行权价下界(%)",
    strike_high_pct: "行权价上界(%)",
    min_premium: "最低权利金",
    max_premium: "最高权利金",
    max_fee_ratio: "费用占权利金上限",
    min_open_interest: "最低持仓量",
    max_price_deviation: "最大价格偏离",
    min_qty: "最小数量",
  };
  const RULE_LABELS = {
    hold_period: "持有到期",
    profit_threshold: "止盈",
    premium_stop_loss: "权利金止损",
    before_expiry: "到期前退出",
  };
  // 每种退出规则只显示有业务含义的字段，其余（重试次数、有效期等）属于
  // 执行细节，展示出来反而淹没真正的阈值。
  const RULE_FIELDS = {
    hold_period: ["hold_seconds"],
    profit_threshold: ["profit_threshold_pct"],
    premium_stop_loss: ["stop_loss_pct"],
    before_expiry: [],
  };

  function renderStrategyParams(data) {
    const versions = (data.config_versions || []).filter((v) => v.active);
    const names = new Map((data.strategies || []).map((s) => [s.id, s.name]));
    const rows = versions.map((v) => {
      const params = v.params || {};
      const paramBox = node("div");
      const entries = Object.keys(PARAM_LABELS).filter((key) => params[key] !== undefined);
      if (!entries.length) paramBox.append(node("p", "无额外参数，按全局配置执行", "muted"));
      for (const key of entries) {
        const line = node("p");
        line.append(node("span", `${PARAM_LABELS[key]}：`, "muted"));
        line.append(node("span", params[key], "number"));
        paramBox.append(line);
      }
      const ruleBox = node("div");
      const rules = v.exit_rules || [];
      if (!rules.length) ruleBox.append(node("p", "未配置退出规则", "muted"));
      for (const rule of rules) {
        const line = node("p");
        line.append(node("span", `${RULE_LABELS[rule.kind] || rule.kind}：`, "muted"));
        const fields = RULE_FIELDS[rule.kind] || [];
        const detail = fields
          .filter((key) => rule[key] !== undefined && rule[key] !== null)
          .map((key) => `${key}=${rule[key]}`).join("，");
        line.append(node("span", detail || "按全局配置", "number"));
        ruleBox.append(line);
      }
      return {
        name: names.get(v.strategy_id) || `策略 ${v.strategy_id}`,
        version: v.version,
        params: paramBox,
        rules: ruleBox,
      };
    });
    table("strategy-params-body", rows, [
      (r) => r.name,
      { value: (r) => r.version, className: "numeric" },
      { value: (r) => r.params, className: "wrap" },
      { value: (r) => r.rules, className: "wrap" },
    ], "暂无生效的策略配置版本。交易服务载入配置后会写入不可变版本。");
  }

  function renderLimits(config) {
    const rows = [];
    const add = (name, value, unit, scope) => rows.push({ name, value, unit, scope });
    const names = {
      max_spend_per_trade: ["单笔最大支出", "每笔新开仓"], max_spot_exposure_per_strategy: ["策略现货敞口", "单一策略"],
      max_spot_exposure_account: ["账户现货敞口", "全部策略及账户资产"], option_premium_budget: ["期权权利金预算", "未平仓成本及开仓预留"],
      fee_buffer: ["费用余量", "新订单资金预留"], max_daily_loss: ["当日最大亏损", "资金流调整权益"], max_drawdown: ["最大回撤", "从持久化历史高点计算"],
    };
    for (const group of [config?.budget, config?.risk]) for (const [key, value] of Object.entries(group || {})) {
      if (value?.value !== undefined) add(names[key]?.[0] || key, amount(value.value), value.currency, names[key]?.[1] || "按配置");
    }
    add("最大活动订单数", config?.budget?.max_open_orders, "张订单", "账户共享，含结果未知订单");
    for (const [base, value] of Object.entries(config?.risk?.delta_limit_per_underlying || {})) add(`${base} 双侧 Delta 绝对限额`, amount(value), base, "实际持仓及挂单可能成交区间");
    for (const [base, value] of Object.entries(config?.risk?.premium_cost_limit_per_underlying || {})) add(`${base} 权利金成本上限`, amount(value.value), value.currency, "同一标的持仓成本及预留");
    for (const [expiry, value] of Object.entries(config?.risk?.premium_cost_limit_per_expiry || {})) add(`${expiry} 到期集中度`, amount(value.value), value.currency, "配置指定 UTC 到期日");
    add("最大行情年龄", config?.market?.max_quote_age_seconds, "秒", "过期数据不得批准新开仓");
    add("账户快照有效期", config?.market?.account_state_max_age_seconds, "秒", "账户身份与状态");
    add("最大价差", amount(config?.market?.max_spread_bps), "基点", "有效买卖报价");
    add("最大价格偏离", amount(config?.market?.max_price_deviation_bps), "基点", "有边界限价执行");
    const option = config?.option || {};
    add("期权到期窗口", `${text(option.min_days_to_expiry, "—")}～${text(option.max_days_to_expiry, "—")}`, "天", "新开仓的距到期天数");
    if (option.delta_range) add("期权 Delta 区间", option.delta_range.map((v) => amount(v)).join("～"), "绝对 Delta", "筛选候选合约");
    if (option.strike_range_pct) add("期权行权价带", option.strike_range_pct.map((v) => amount(v)).join("～"), "% 现价", "筛选候选合约");
    if (option.max_fee_ratio !== null && option.max_fee_ratio !== undefined) {
      add("期权费用占权利金上限", amount(option.max_fee_ratio), "比例（每边 taker）", "超过即不选该合约");
    }
    if (config?.risk?.max_premium_to_equity_ratio !== null && config?.risk?.max_premium_to_equity_ratio !== undefined) {
      add("权利金占权益比例上限", amount(config.risk.max_premium_to_equity_ratio), "比例", "期权权利金总额对风控权益");
    }
    add("日界线时区", config?.day_boundary_tz, "时区", "日损益基线，内部时间 UTC");
    table("limits-body", rows, [(r) => r.name, { value: (r) => r.value, className: "numeric" }, (r) => r.unit, { value: (r) => r.scope, className: "wrap" }]);
  }

  const commandFields = {
    PAUSE_NEW_RISK: [["reason", "暂停原因", "textarea", true]],
    CANCEL_BOT_OPEN_ORDERS: [["strategy_id", "策略编号（留空表示全部）", "id"], ["symbol", "指定标的（可选）", "symbol"]],
    CANCEL_ORDER: [["order_link_ids", "bot 订单编号（每行一个）", "textarea", true]],
    REDUCE: [["strategy_id", "策略编号", "id", true], ["category", "产品", "category", true], ["symbol", "标的", "symbol", true], ["qty", "减仓数量（基础币 / 合约）", "decimal", true], ["price_floor", "最低委托价格（报价 / 结算币）", "decimal", true], ["reason", "减仓原因", "textarea", true]],
    RESUME: [["pause_id", "暂停编号", "id", true], ["reason", "恢复原因", "textarea", true]],
    CONFIRM_TAKEOVER: [["evidence", "旧进程已经停止的证据", "textarea", true], ["reason", "接管原因", "textarea", true]],
    INITIALIZE_BASELINE: [["reason", "建立风险起点的原因", "textarea", true]],
    REGISTER_EXTERNAL_CASH_FLOW: [["asset", "币种", "symbol", true], ["amount", "金额（正数）", "decimal", true], ["kind", "资金流类型", "cashflow", true], ["reference", "交易所流水标识", "text", true], ["occurred_at", "发生时间（ISO 8601，可留空）", "text"], ["reason", "登记原因", "textarea", true]],
  };
  const descriptions = {
    PAUSE_NEW_RISK: "暂停账户新增风险，交易服务会处理 bot 开仓挂单。已有可信库存的退出仍需按规则校验。",
    CANCEL_BOT_OPEN_ORDERS: "仅处理当前环境的 bot 开仓单，不撤销人工单或减仓单。撤单受理后仍可能有成交，须跟踪最终状态。",
    CANCEL_ORDER: "核对指定 bot 物理订单并申请撤单。命令执行完成不代表订单一定已撤销，请查看终态确认和仍待核实列表。",
    REDUCE: "只处理当前策略可信 bot 库存。数量、可用持仓、价格底线与执行权均由交易服务再次校验；不保证能够成交。",
    RESUME: "每次只申请解除指定暂停。未知订单须先取得交易事实证据；人工确认不会替代对账。其他暂停继续生效。",
    CONFIRM_TAKEOVER: "确认旧执行者已停止，并记录可核查依据。仅有数据库锁或服务页面连通不能证明旧执行者已停止。",
    INITIALIZE_BASELINE: "以完整估值建立首次风险起点，首日标记为不完整交易日。此操作不会覆盖已有起点，也不覆盖接入前损失。",
    REGISTER_EXTERNAL_CASH_FLOW: "登记账户外的充值、提现或划转。金额填正数，方向由类型决定；只登记估值币种。交易所流水标识用于日后核对，登记会记录操作者与时间。",
  };
  function scopeText(kind, payload = {}, environment) {
    let summary = `环境 ${environment || snapshot?.environment || "未知"}`;
    if (kind === "CANCEL_ORDER") summary += `；订单 ${(payload.order_link_ids || []).join(", ") || "未填写"}`;
    else if (kind === "REDUCE") summary += `；策略 ${payload.strategy_id || "未填写"}；${payload.category || "未选择"} ${payload.symbol || "未填写"}；数量 ${payload.qty || "未填写"}；价格不低于 ${payload.price_floor || "未填写"}`;
    else if (kind === "RESUME") summary += `；暂停 #${payload.pause_id || "未填写"}`;
    else if (kind === "CANCEL_BOT_OPEN_ORDERS") summary += `；${payload.strategy_id ? `策略 ${payload.strategy_id}` : "全部策略"}；${payload.symbol || "全部标的"}；仅 bot 开仓单`;
    else if (kind === "REGISTER_EXTERNAL_CASH_FLOW") summary += `；${payload.asset || "未填写"} ${payload.amount || "未填写"}；${payload.kind || "未选择"}；流水 ${payload.reference || "未填写"}`;
    else summary += "；当前账户";
    return summary;
  }
  function collectPayload() {
    const payload = {};
    for (const [key, label, type, required] of commandFields[current.kind]) {
      const field = $(`field-${key}`);
      let value = field.value.trim();
      if (!value && !required) continue;
      if (type === "id") {
        if (!/^[1-9][0-9]*$/.test(value) || !Number.isSafeInteger(Number(value))) throw new Error(`${label}必须是正整数。`);
        value = Number(value);
      } else if (type === "decimal") {
        if (!/^(?:0|[1-9][0-9]{0,19})(?:\.[0-9]{1,18})?$/.test(value) || !/[1-9]/.test(value)) throw new Error(`${label}必须是正数，请保留原始精度。`);
      } else if (type === "symbol") value = value.toUpperCase();
      if (key === "order_link_ids") value = [...new Set(value.split(/[\s,]+/).filter(Boolean))];
      payload[key] = value;
    }
    return payload;
  }
  function openCommand(kind, values = {}, restored = null) {
    if (!session || !snapshot) return;
    if (pending && !restored) {
      $("command-notice").textContent = `命令 ${pending.body.command_id} 的登记结果尚待核实。请先重试原编号或刷新命令记录。`;
      $("retry-pending").focus();
      return;
    }
    if (restored) noticeCommandId = restored.body.command_id;
    current = { kind, id: restored?.body.command_id || crypto.randomUUID() };
    submitted = restored?.body || null;
    values = submitted?.payload || values;
    $("command-title").textContent = kindNames[kind];
    $("command-description").textContent = descriptions[kind];
    $("command-uuid").textContent = current.id;
    $("command-fields").replaceChildren();
    for (const [key, label, type, required] of commandFields[kind]) {
      const wrapper = node("div");
      const id = `field-${key}`;
      const caption = node("label", label); caption.htmlFor = id;
      const input = node(type === "textarea" ? "textarea" : ["category", "cashflow"].includes(type) ? "select" : "input");
      input.id = id; input.name = key; input.required = !!required; input.autocomplete = "off";
      if (type === "category") for (const [value, title] of [["spot", "现货 spot"], ["option", "期权 option"]]) {
        const option = node("option", title); option.value = value; input.append(option);
      }
      if (type === "cashflow") for (const [value, title] of [["DEPOSIT", "充值 deposit"], ["TRANSFER_IN", "划入 transfer in"], ["WITHDRAWAL", "提现 withdrawal"], ["TRANSFER_OUT", "划出 transfer out"]]) {
        const option = node("option", title); option.value = value; input.append(option);
      }
      if (["id", "decimal"].includes(type)) input.inputMode = type === "id" ? "numeric" : "decimal";
      if (input.tagName !== "SELECT") input.maxLength = key === "order_link_ids" ? 3200 : 500;
      input.value = Array.isArray(values[key]) ? values[key].join("\n") : text(values[key], "");
      input.disabled = !!submitted;
      input.addEventListener("input", () => { try { $("scope-summary").textContent = scopeText(kind, collectPayload()); } catch { $("scope-summary").textContent = "请完整填写操作范围、数量和价格。"; } });
      wrapper.append(caption, input); $("command-fields").append(wrapper);
    }
    $("confirmation-field").hidden = kind === "PAUSE_NEW_RISK";
    $("confirmation").required = kind !== "PAUSE_NEW_RISK";
    $("confirmation").value = submitted?.confirmation || "";
    $("confirmation").disabled = !!submitted;
    $("scope-summary").textContent = scopeText(kind, values);
    showError($("command-error"), restored ? "上次登记结果未确认；使用原编号与原范围重试，可安全查询或登记同一命令。" : "");
    $("submit-command").textContent = restored ? "用原编号重试" : "登记命令";
    $("command-dialog").showModal();
  }
  const closeDialog = () => { if (!sending) $("command-dialog").close(); };
  $("close-dialog").addEventListener("click", closeDialog);
  $("cancel-dialog").addEventListener("click", closeDialog);
  $("command-dialog").addEventListener("cancel", (event) => { if (sending) event.preventDefault(); });
  document.querySelectorAll("[data-command]").forEach((button) => button.addEventListener("click", () => openCommand(button.dataset.command)));
  const retryButton = node("button", "重试未确认命令", "button secondary small");
  retryButton.id = "retry-pending"; retryButton.type = "button"; retryButton.hidden = !pending;
  $("command-notice").after(retryButton);
  retryButton.addEventListener("click", () => {
    if (pending?.environment !== snapshot?.environment) { $("command-notice").textContent = "未确认命令属于其他环境，请切回原环境核对，勿在当前环境重发。"; return; }
    openCommand(pending.body.kind, pending.body.payload, pending);
  });
  $("command-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (sending) return;
    showError($("command-error"), "");
    if (!submitted) {
      if (current.kind !== "PAUSE_NEW_RISK" && $("confirmation").value !== "确认") {
        showError($("command-error"), "请输入完整的“确认”后登记命令。");
        $("confirmation").focus();
        return;
      }
      try {
        submitted = { command_id: current.id, kind: current.kind, payload: collectPayload(), confirmation: current.kind === "PAUSE_NEW_RISK" ? null : "确认" };
      } catch (error) {
        showError($("command-error"), error.message);
        return;
      }
    }
    sending = true;
    $("submit-command").disabled = true;
    $("command-fields").querySelectorAll("input, textarea, select").forEach((field) => { field.disabled = true; });
    $("confirmation").disabled = true;
    try {
      savePending({ environment: snapshot.environment, body: submitted });
      const data = await post("/api/commands", submitted, session.csrf_token);
      savePending(null);
      noticeCommandId = data.command.command_id;
      $("command-notice").textContent = commandNotice(data.command);
      $("command-dialog").close();
      submitted = null;
      await refresh();
    } catch (error) {
      const unconfirmed = !error.status || error.status >= 500 || error.status === 409;
      if (!unconfirmed) {
        savePending(null); submitted = null;
        $("command-fields").querySelectorAll("input, textarea, select").forEach((field) => { field.disabled = false; });
        $("confirmation").disabled = false;
      }
      showError($("command-error"), error.status ? error.message : "无法连接管理服务；登记结果未确认，请保留此编号并点击重试。");
      $("submit-command").textContent = submitted ? "用原编号重试" : "登记命令";
    } finally { sending = false; $("submit-command").disabled = false; }
  });
  const tabs = [...document.querySelectorAll("[data-tab]")];
  function selectTab(name, focus = false) {
    if (!tabs.some((tab) => tab.dataset.tab === name)) name = "account";
    for (const tab of tabs) {
      const selected = tab.dataset.tab === name;
      tab.setAttribute("aria-selected", String(selected)); tab.tabIndex = selected ? 0 : -1;
      $(`panel-${tab.dataset.tab}`).hidden = !selected;
      if (selected) { $("page-context").textContent = tab.textContent; if (focus) tab.focus(); }
    }
    history.replaceState(null, "", `#${name}`);
  }
  for (const [index, tab] of tabs.entries()) {
    tab.addEventListener("click", () => selectTab(tab.dataset.tab));
    tab.addEventListener("keydown", (event) => {
      const move = ["ArrowDown", "ArrowRight"].includes(event.key) ? 1 : ["ArrowUp", "ArrowLeft"].includes(event.key) ? -1 : 0;
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + move + tabs.length) % tabs.length;
      if (move || ["Home", "End"].includes(event.key)) { event.preventDefault(); selectTab(tabs[next].dataset.tab, true); }
    });
  }
  $("open-ops").addEventListener("click", (event) => { event.preventDefault(); selectTab("ops"); });
  selectTab(location.hash.slice(1));
  async function refresh() {
    if (refreshing) return;
    refreshing = true; $("refresh").disabled = true;
    try { render(await request("/api/status")); showError($("connection-error"), ""); }
    catch (error) { showError($("connection-error"), error.status ? error.message : "无法读取管理服务；表中保留的是上次读取的记录，请勿据此判断实时状态。"); }
    finally { refreshing = false; $("refresh").disabled = false; }
  }
  $("refresh").addEventListener("click", refresh);
  $("logout").addEventListener("click", async () => {
    try { await post("/api/logout", null, session?.csrf_token); location.assign("/login"); }
    catch (error) { showError($("connection-error"), error.message || "退出登录未完成，请重试。"); }
  });
  (async () => {
    try {
      session = await request("/api/session");
      $("operator").textContent = `管理员 ${session.username}`;
      await refresh();
      setInterval(() => { if (!document.hidden) refresh(); }, 15000);
      document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
    } catch (error) { showError($("connection-error"), error.message || "无法验证会话，请重新登录。"); }
  })();
})();
