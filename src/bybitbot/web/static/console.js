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

  // 纯派生函数：只汇总页面可见事实，不替代交易服务的风控/恢复判定。
  const isActivePause = (pause) => !!(pause && pause.active);
  const isUnknownOrder = (order) => !!(order && ["UNKNOWN", "SUBMITTING"].includes(order.status));
  const leaseState = (runtime) => {
    const lease = runtime?.lease;
    return typeof lease === "object" && lease ? text(lease.state) : text(lease);
  };
  const deriveRecoveryState = (data = {}) => {
    const runtime = data.runtime || {};
    const pauses = (data.pauses || []).filter(isActivePause);
    const recon = (data.reconciliations || []).find((row) => row.scope === "full");
    const blocking = [];
    const attention = [];
    const add = (target, code, detail, action) => target.push({ code, detail, action });
    if (recon && recon.allows_resume === false) add(blocking, "RECONCILE_BLOCKED", "最近全量对账不允许恢复", "review-reconciliations");
    if (recon && Number(recon.difference_count) > 0 && recon.repaired === false) add(blocking, "RECONCILE_DIFFERENCE", "存在未修复对账差异", "review-reconciliations");
    for (const pause of pauses.filter((row) => row.locked)) add(blocking, "LOCKED_PAUSE", `#${text(pause.id)} ${text(pause.reason_code)}：${text(pause.reason_detail)}`, "review-pauses");
    const unknown = (data.orders || []).filter(isUnknownOrder);
    if (unknown.length) add(blocking, "UNKNOWN_ORDER", `${unknown.length} 笔订单结果未确定`, "review-orders");
    if (data.account && data.account.complete === false) add(blocking, "ACCOUNT_INCOMPLETE", "账户快照不完整", "review-account");
    if (runtime.valuation_complete === false) add(blocking, "VALUATION_UNAVAILABLE", "风险估值不可用", "review-account");
    if (leaseState(runtime) === "NEEDS_CONFIRMATION") add(blocking, "TAKEOVER_NOT_CONFIRMED", "执行租约等待人工接管确认", "review-takeover");
    if (data.heartbeat && data.heartbeat.fresh === false) add(attention, "HEARTBEAT_STALE", "交易服务心跳失效", "review-health");
    if (data.account && data.account.stale) add(attention, "ACCOUNT_STALE", "账户快照已过期", "review-account");
    if ((data.quotes || []).some((quote) => quote.stale)) add(attention, "QUOTE_STALE", "存在过期行情快照", "review-quotes");
    if (!data.baseline) add(attention, "BASELINE_MISSING", "风险基线尚未建立", "review-baseline");
    for (const pause of pauses.filter((row) => !row.locked)) add(attention, "TECHNICAL_PAUSE", `#${text(pause.id)} ${text(pause.reason_code)}`, "review-pauses");
    if ((data.alerts || []).some((alert) => alert.status === "ACTIVE" && Number(alert.delivery_attempts) > 0)) add(attention, "ALERT_DELIVERY", "告警尚未确认送达", "review-alerts");
    return {
      level: blocking.length ? "blocking" : attention.length ? "attention" : "ready",
      blocking,
      attention,
      recon: recon || null,
      baseline: data.baseline || null,
      lease: leaseState(runtime),
      accountComplete: data.account ? data.account.complete === true : false,
      accountUid: data.account?.account_uid || runtime.account_uid || null,
    };
  };
  const deriveHealth = (data = {}) => {
    const runtime = data.runtime || {};
    return {
      heartbeatFresh: data.heartbeat?.fresh === true,
      heartbeat: data.heartbeat || null,
      tickAt: runtime.tick_at || null,
      reconciledAt: runtime.reconciled_at || null,
      configVersion: runtime.config_version || null,
      lastError: runtime.last_error || null,
      account: data.account || null,
      staleQuotes: (data.quotes || []).filter((quote) => quote.stale).length,
      activeAlerts: (data.alerts || []).filter((alert) => alert.status === "ACTIVE").length,
      latency: runtime.latency || [],
      retention: runtime.retention || null,
    };
  };
  const deriveRiskPosture = (data = {}) => {
    const config = data.configuration || {};
    const active = (data.pauses || []).filter(isActivePause);
    return {
      currency: data.currency || null,
      equity: data.runtime?.equity || null,
      dailyPnl: data.runtime?.daily_pnl || null,
      drawdown: data.runtime?.drawdown || null,
      limits: config.budget || {},
      risk: config.risk || {},
      inventoryCount: (data.inventory || []).length,
      positionCount: (data.positions || []).length,
      openOrderCount: (data.orders || []).filter((order) => !["FILLED", "CANCELED", "REJECTED", "EXPIRED"].includes(order.status)).length,
      lockedPauses: active.filter((pause) => pause.locked).length,
      rejects: (data.risk_rejects || []).length,
      caveat: "预留额度与逐标的 Delta 明细未由只读 API 提供；此页不臆测当前占用。",
    };
  };
  const deriveNextStep = (data = {}) => {
    const recovery = deriveRecoveryState(data);
    const pauses = (data.pauses || []).filter(isActivePause);
    const unknown = (data.orders || []).filter(isUnknownOrder);
    const locked = pauses.filter((pause) => pause.locked);
    const lease = typeof data.runtime?.lease === "object" && data.runtime.lease ? data.runtime.lease : null;
    if (!data.heartbeat?.fresh) return { title: "先确认交易服务是否在线", detail: "页面可以读取历史记录，但不能证明 worker 正在运行。", command: null, tab: "health" };
    if (locked.length) return { title: "先核查锁定暂停", detail: `有 ${locked.length} 条锁定暂停；按证据核实后再逐条解除。`, command: "RESUME", tab: "recovery", payload: { pause_id: locked[0].id } };
    if (unknown.length) return { title: "先核实结果未知的订单", detail: `有 ${unknown.length} 笔订单结果未确定；不要重复发单。`, command: null, tab: "trades" };
    if (!data.baseline) return { title: "建立首次风险基线", detail: "身份、估值与对账通过后，才能设置风险起点。", command: "INITIALIZE_BASELINE", tab: "ops" };
    if (recovery.lease === "NEEDS_CONFIRMATION" || lease?.takeover_required) return { title: "确认执行者接管", detail: "记录旧进程停止依据；确认后仍需通过恢复检查。", command: "CONFIRM_TAKEOVER", tab: "ops" };
    if (pauses.length) return { title: "核验后解除剩余暂停", detail: `还有 ${pauses.length} 条活动暂停，每次只处理一条。`, command: "RESUME", tab: "ops", payload: { pause_id: pauses[0].id } };
    if (recovery.level === "attention") return { title: "核对关注项后继续", detail: "页面发现过期快照或告警等待处理。", command: null, tab: "recovery" };
    return { title: "当前无需人工处置", detail: "没有发现阻塞项。开仓资格仍由交易服务判定。", command: null, tab: "recovery" };
  };
  const __test = { deriveRecoveryState, deriveHealth, deriveRiskPosture, deriveNextStep, leaseState, isUnknownOrder };
  if (typeof window !== "undefined") window.__bybitbotConsole = __test;

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
    VALIDATION_START: "启动验证运行", VALIDATION_STOP: "停止验证运行",
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
  const tableSignatures = new Map();
  const table = (id, rows, columns, empty) => {
    const body = $(id);
    if (!body) return;
    let signature;
    try { signature = JSON.stringify(rows || []); } catch { signature = null; }
    if (signature !== null && tableSignatures.get(id) === signature) return;
    tableSignatures.set(id, signature);
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

  function renderRecoveryEvidence(data) {
    const panel = $("recovery-evidence");
    if (!panel) return;
    const grid = $("recovery-evidence-grid") || node("div", null, "evidence-grid");
    grid.replaceChildren();
    const full = (data.reconciliations || []).find((row) => row.scope === "full");
    const cell = (label, value) => {
      const item = node("div", null, "evidence-item");
      item.append(node("strong", label));
      const content = node("div", undefined, "muted");
      content.append(value instanceof Node ? value : document.createTextNode(text(value)));
      item.append(content);
      return item;
    };
    grid.append(cell("最近全量对账", full
      ? stacked(`#${full.id} · ${stamp(full.finished_at || full.started_at)}`, `${full.difference_count || 0} 项差异 · ${full.allows_resume ? "允许恢复" : "不允许恢复"}`)
      : "尚无全量对账记录"));
    grid.append(cell("对账水位与游标", full
      ? stacked(text(full.watermark, "无水位"), text(full.cursor, "无游标"), true)
      : "无可展示水位"));
    const unknown = (data.orders || []).filter(isUnknownOrder);
    const unknownBox = node("div");
    if (!unknown.length) unknownBox.append(node("p", "无 UNKNOWN / SUBMITTING 订单。", "muted"));
    for (const order of unknown) {
      unknownBox.append(node("p", `${order.order_link_id || `#${order.id}`} · ${order.symbol || "标的未知"} · 状态 ${state(order.status)} · 同步 ${state(order.sync_state)} · 本地 TTL ${stamp(order.local_ttl_expires_at)}`));
    }
    grid.append(cell("订单结果证据", unknownBox));
    const lease = typeof data.runtime?.lease === "object" && data.runtime.lease ? data.runtime.lease : null;
    grid.append(cell("执行租约", lease
      ? stacked(state(lease.state), `持有：${lease.held ? "是" : "否"} · 持有者：${text(lease.holder_id)} · 心跳：${stamp(lease.heartbeat_at)} · 需确认接管：${lease.takeover_required ? "是" : "否"}`)
      : state(data.runtime?.lease)));
    const locked = (data.pauses || []).filter((pause) => pause.active && pause.locked);
    const lockedBox = node("div");
    if (!locked.length) lockedBox.append(node("p", "无活动锁定暂停。", "muted"));
    for (const pause of locked.slice(0, 8)) lockedBox.append(node("p", `#${pause.id} ${pause.reason_code}：${pause.reason_detail}（${stamp(pause.created_at)}）`));
    grid.append(cell("活动锁定暂停", lockedBox));
    grid.append(cell("账本锚点", stacked(text(data.validation?.studies?.[0]?.chain_head, "无验证运行锚点"), `${data.cash_flows?.length || 0} 条外部资金流登记`, true)));
    if (!grid.parentNode) panel.append(grid);
  }

  function renderRecoveryWorkflow(data) {
    const panel = $("recovery-workflow");
    if (!panel) return;
    const list = $("recovery-workflow-steps") || node("ol", null, "workflow-steps");
    list.replaceChildren();
    const full = (data.reconciliations || []).find((row) => row.scope === "full");
    const cashKinds = (data.cash_flows || []).map((row) => String(row.kind || "").toUpperCase());
    const hasCashFlow = cashKinds.some((kind) => ["DEPOSIT", "WITHDRAWAL", "TRANSFER_IN", "TRANSFER_OUT"].includes(kind));
    const activeLocked = (data.pauses || []).filter((pause) => pause.active && pause.locked);
    const unknown = (data.orders || []).filter(isUnknownOrder);
    const steps = [
      {
        title: "1. 补齐交易与账户事实",
        done: !!full && full.allows_resume === true,
        detail: full ? `最近全量对账 ${stamp(full.finished_at || full.started_at)}：${full.allows_resume ? "允许恢复" : "仍不允许恢复"}。` : "等待交易服务完成一轮全量对账。",
      },
      {
        title: "2. 登记未解释的外部资金变化",
        done: hasCashFlow,
        detail: hasCashFlow ? `已登记 ${data.cash_flows.length} 条外部资金流。` : "若现金变化无法解释，先登记外部资金流；没有外部变化时跳过。",
        command: "REGISTER_EXTERNAL_CASH_FLOW",
      },
      {
        title: "3. 核实 UNKNOWN 订单与锁定暂停",
        done: !unknown.length && !activeLocked.length,
        detail: unknown.length || activeLocked.length
          ? `${unknown.length} 笔 UNKNOWN 订单、${activeLocked.length} 条锁定暂停；人工确认不能替代交易事实。`
          : "无 UNKNOWN 订单与活动锁定暂停。",
      },
      {
        title: "4. 逐条解除暂停并确认接管",
        done: !activeLocked.length && leaseState(data.runtime || {}) === "ELIGIBLE",
        detail: `租约 ${state(leaseState(data.runtime || {}))}；每次只申请解除一条暂停，并重新核验对账。`,
        command: "RESUME",
      },
    ];
    for (const step of steps) {
      const item = node("li", null, step.done ? "workflow-step done" : "workflow-step");
      item.append(node("strong", step.title));
      item.append(node("span", step.detail, "muted"));
      if (step.command && !step.done) {
        const button = action("发起命令", step.command, step.command === "RESUME" && activeLocked.length ? { pause_id: activeLocked[0].id } : {});
        item.append(button);
      }
      list.append(item);
    }
    if (!list.parentNode) panel.append(list);
  }

  function commandPrerequisites(data) {
    const recovery = deriveRecoveryState(data);
    const hints = {
      PAUSE_NEW_RISK: "高影响操作：暂停后需要按证据逐条恢复；仍需输入确认原因。",
      RESUME: recovery.blocking.length ? `当前有 ${recovery.blocking.length} 项阻塞证据；服务会重新核验对账与订单事实。` : "服务仍会核验对账、估值与订单事实。",
      CONFIRM_TAKEOVER: recovery.lease === "NEEDS_CONFIRMATION" ? "请填写旧进程停止的可核查证据；确认后仍需干净对账。" : "当前租约不需要接管确认；服务会校验。",
      REGISTER_EXTERNAL_CASH_FLOW: "只允许估值币种与正数金额；交易所流水标识用于去重。",
      INITIALIZE_BASELINE: data.baseline ? "已有基线；服务不会覆盖。" : "必须先通过身份、估值与对账检查；首日仍标记为不完整。",
    };
    return hints;
  }

  function renderGoalSummaries(data) {
    const recovery = deriveRecoveryState(data);
    const health = deriveHealth(data);
    const risk = deriveRiskPosture(data);
    const levelText = { blocking: "存在阻塞证据", attention: "需要关注", ready: "页面可见证据无明显阻塞" };
    const recoveryPanel = $("recovery-summary");
    if (recoveryPanel) {
      recoveryPanel.replaceChildren();
      const heading = node("div", null, "goal-verdict");
      heading.append(node("strong", levelText[recovery.level], `goal-level ${recovery.level}`));
      heading.append(node("span", `最近全量对账：${recovery.recon ? stamp(recovery.recon.finished_at || recovery.recon.started_at) : "尚无记录"} · 允许恢复：${recovery.recon ? (recovery.recon.allows_resume ? "是" : "否") : "未知"}`, "muted"));
      recoveryPanel.append(heading);
      const list = node("ul", null, "goal-checklist");
      const items = [...recovery.blocking, ...recovery.attention];
      if (!items.length) list.append(node("li", "未发现阻塞或关注项；这不代表允许开仓，执行资格仍由交易服务判定。", "muted"));
      for (const item of items) {
        const row = node("li", null, `goal-item ${item.code === "RECONCILE_BLOCKED" || item.code === "LOCKED_PAUSE" || item.code === "UNKNOWN_ORDER" ? "blocking" : "attention"}`);
        row.append(node("strong", item.code));
        row.append(node("span", item.detail, "muted"));
        const target = {
          "review-reconciliations": ["recovery", "recovery-evidence"],
          "review-pauses": ["ops", null], "review-orders": ["trades", null],
          "review-account": ["trades", null], "review-takeover": ["ops", null],
          "review-health": ["health", null], "review-quotes": ["health", null],
          "review-baseline": ["ops", null], "review-alerts": ["recovery", null],
        }[item.action];
        if (target) {
          const link = node("button", "查看证据 →", "text-button");
          link.type = "button";
          link.addEventListener("click", () => {
            selectTab(target[0]);
            if (target[1]) { const section = $(target[1]); if (section?.scrollIntoView) section.scrollIntoView({ block: "start" }); }
          });
          row.append(link);
        }
        list.append(row);
      }
      if (items.length) recoveryPanel.append(list);
      recoveryPanel.append(node("p", `账户：${recovery.accountUid || "未确认"} · 估值完整性：${data.runtime?.valuation_complete ? "完整" : "不可用"} · 租约：${state(recovery.lease)} · 基线：${recovery.baseline ? "已建立" : "未建立"}`, "section-caption"));
      renderRecoveryEvidence(data);
      renderRecoveryWorkflow(data);
    }
    const healthPanel = $("health-summary");
    if (healthPanel) {
      healthPanel.replaceChildren();
      const grid = node("dl", null, "goal-facts");
      for (const [label, value] of [
        ["交易服务心跳", health.heartbeatFresh ? `有效 · ${age(health.heartbeat?.age_seconds)}前` : "失效或未读取"],
        ["最近 tick", stamp(health.tickAt)],
        ["最近对账", stamp(health.reconciledAt)],
        ["配置版本", text(health.configVersion)],
        ["账户快照", health.account ? `${age(health.account.age_seconds)}前 · ${health.account.stale ? "已过期" : "有效"}` : "尚未采集"],
        ["过期行情", `${health.staleQuotes} 条`],
        ["活动告警", `${health.activeAlerts} 条`],
        ["延迟 P95", health.latency?.length ? `${health.latency[0].p95_ms ?? "—"} ms` : "无记录"],
        ["留存清理", health.retention ? stamp(health.retention.ran_at) : "无记录"],
      ]) { const cell = node("div"); cell.append(node("dt", label)); cell.append(node("dd", value)); grid.append(cell); }
      healthPanel.append(grid);
      if (health.lastError) healthPanel.append(node("p", health.lastError, "notice warning"));
    }
    const riskPanel = $("risk-summary");
    if (riskPanel) {
      riskPanel.replaceChildren();
      const grid = node("dl", null, "goal-facts");
      for (const [label, value] of [
        ["风险权益", health.account ? amount(data.runtime?.equity, data.currency) : "不可用"],
        ["当日盈亏", data.runtime?.baseline_available ? amount(data.runtime?.daily_pnl, data.currency) : "基线未就绪"],
        ["回撤", data.runtime?.baseline_available ? amount(data.runtime?.drawdown, data.currency) : "基线未就绪"],
        ["bot 库存行", risk.inventoryCount],
        ["账户持仓行", risk.positionCount],
        ["非终态订单", risk.openOrderCount],
        ["锁定暂停", risk.lockedPauses],
        ["风控拒绝", risk.rejects],
      ]) { const cell = node("div"); cell.append(node("dt", label)); cell.append(node("dd", value)); grid.append(cell); }
      riskPanel.append(grid);
      riskPanel.append(node("p", risk.caveat, "section-caption"));
    }
  }

  function render(data) {
    snapshot = data;
    const next = deriveNextStep(data);
    const hero = $("next-step");
    if (hero) {
      hero.replaceChildren();
      hero.append(node("span", "现在先做这件事", "eyebrow"));
      hero.append(node("strong", next.title, "next-title"));
      hero.append(node("span", next.detail, "muted"));
      if (next.command) {
        const button = action("打开操作窗口", next.command, next.payload || {});
        button.className = "button primary";
        hero.append(button);
      }
      const details = node("button", "查看依据", "button secondary");
      details.type = "button";
      details.addEventListener("click", () => selectTab(next.tab));
      hero.append(details);
    }
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
      { value: (r) => { const details = node("div"); for (const d of (r.differences || [])) details.append(node("p", `${d.kind}：${d.detail}`)); return (details.children || details.childNodes || []).length ? details : "无差异"; }, className: "wrap" },
    ], "暂无对账记录。恢复开仓前必须完成对账。");
    table("cash-flows-body", data.cash_flows, [
      (r) => `#${r.id}`, (r) => r.asset,
      { value: (r) => amount(r.amount, r.asset), className: "numeric" }, (r) => text(r.kind),
      (r) => text(r.reference, "无引用"), (r) => stamp(r.occurred_at),
    ], "暂无已登记的外部资金流。未解释的现金变化会触发严重暂停。");
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
    renderValidation(data.validation, data.environment);
    renderLimits(data.configuration);
    renderGoalSummaries(data);
    void loadShadow();
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
    document.querySelectorAll("[data-export-run-id]").forEach((button) => {
      button.disabled = !session;
    });
    $("refresh-at").textContent = `读取于 ${stamp(data.generated_at)}`;
    $("row-limit").textContent = `每表显示最近 ${data.table_limit} 条记录`;
    const prerequisites = commandPrerequisites(data);
    document.querySelectorAll("[data-command]").forEach((button) => { button.disabled = !session; const hint = prerequisites[button.dataset.command]; if (hint) button.title = hint; });
    const commandRows = data.control_commands || [];
    if (pending && pending.environment === data.environment && commandRows.some((row) => row.command_id === pending.body?.command_id)) {
      noticeCommandId = pending.body.command_id;
      savePending(null);
    }
    const tracked = noticeCommandId ? commandRows.find((row) => row.command_id === noticeCommandId) : null;
    if (tracked) $("command-notice").textContent = commandNotice(tracked);
  }

  // Configuration is loaded once, independently of the 15-second status refresh.
  const shadowFields = [
    ["shadow", "observation_days", "观察天数", "sampling", "1–30 天", 1, 30, 1],
    ["auto", "window", "滚动窗口（样本数）", "sampling", "5–500 个样本", 5, 500, 1],
    ["auto", "sample_interval_seconds", "采样间隔（秒）", "sampling", "2–3600 秒，实际频率受 worker 轮询影响", 2, 3600, 1],
    ["shadow", "max_signal_age_ms", "行情最大年龄（毫秒）", "sampling", "至少 1000 毫秒", 1000, null, 1],
    ["auto", "max_signal_age_ms", "信号最大年龄（毫秒）", "sampling", "至少 1000 毫秒", 1000, null, 1],
    ["auto", "stop_cooldown_seconds", "止损冷却（秒）", "sampling", "至少 60 秒", 60, null, 1],
    ["auto", "target_notional", "每腿目标名义金额（USDT）", "execution", "必须大于 0", 0, null, "any"],
    ["shadow", "min_depth_notional", "最小盘口深度（USDT）", "execution", "必须大于 0；深度不足时不模拟开仓", 0, null, "any"],
    ["auto", "fee_rate", "单腿 taker 费率", "execution", "小数比例：0.00055 = 0.055%", 0, 1, "any"],
    ["auto", "slippage_bps", "单边滑点估计（基点）", "execution", "1 基点 = 0.01%", 0, null, "any"],
    ["shadow", "impact_bps_per_depth", "每腿深度冲击（基点）", "execution", "按可见盘口深度估算", 0, null, "any"],
    ["auto", "funding_buffer", "资金费率缓冲（USDT）", "execution", "非负金额", 0, null, "any"],
    ["shadow", "min_sharpe", "最低模拟 Sharpe", "review", "超过该值才具备候选资格", 0, null, "any"],
    ["shadow", "max_drawdown", "最大模拟回撤", "review", "小数比例：0.03 = 3%", 0, 1, "any"],
  ];
  const pairFields = [
    ["symbol_a", "合约 A", "例如 BTCUSDT", "text"],
    ["symbol_b", "合约 B", "例如 ETHUSDT", "text"],
    ["hedge_ratio", "对冲系数 β", "必须大于 0", "number"],
    ["z_entry", "入场阈值 |Z|", "大于退出阈值", "number"],
    ["z_exit", "退出阈值 |Z|", "非负，且小于入场阈值", "number"],
    ["z_stop", "止损阈值 |Z|", "大于入场阈值", "number"],
    ["max_notional", "单组名义上限（USDT）", "必须大于裸敞口上限", "number"],
    ["max_unhedged_notional", "裸敞口上限（USDT）", "必须小于单组名义上限", "number"],
  ];
  let shadowConfig = null;
  let shadowPairs = [];
  let shadowBusy = false;
  let shadowDirty = false;
  const configField = (container, id, label, help, type = "number", min = 0, max = null, step = "any") => {
    const wrapper = node("div", undefined, "shadow-field");
    const caption = node("label", label);
    caption.htmlFor = id;
    const input = node("input");
    input.id = id; input.type = type; input.required = true;
    input.autocomplete = "off";
    if (type === "number") {
      input.min = String(min); input.step = String(step);
      if (max !== null) input.max = String(max);
    } else { input.maxLength = 32; input.spellcheck = false; }
    const hint = node("small", help); hint.id = `${id}-help`;
    input.setAttribute("aria-describedby", hint.id);
    wrapper.append(caption, input, hint); container.append(wrapper);
    return input;
  };
  const updateShadowControls = () => {
    const editable = !!shadowConfig?.writable;
    $("shadow-config-fields").disabled = shadowBusy || !editable;
    $("shadow-config-save").disabled = shadowBusy || !editable || !shadowDirty;
    $("shadow-config-save").textContent = shadowBusy ? "正在处理…" : "保存配置";
    $("shadow-config-reload").disabled = shadowBusy;
    $("shadow-config-reload").textContent = shadowDirty ? "放弃修改并重新读取" : "重新读取配置";
    $("shadow-config-dirty").textContent = shadowDirty ? "有未保存的修改" : shadowConfig ? "已读取服务器配置" : "尚未读取";
    $("shadow-add-pair").disabled = shadowPairs.length >= 20;
    $("shadow-pairs-empty").hidden = shadowPairs.length > 0;
  };
  const markShadowDirty = () => {
    if (!shadowConfig?.writable || shadowBusy) return;
    shadowDirty = true;
    showError($("shadow-config-error"), "");
    updateShadowControls();
  };
  const addShadowPair = (pair) => {
    shadowPairs.push({
      values: Object.fromEntries(pairFields.map(([key]) => [key, text(pair[key]?.value ?? pair[key], "")])),
      inputs: {},
      currency: pair.max_notional?.currency || "USDT",
      unhedgedCurrency: pair.max_unhedged_notional?.currency || "USDT",
    });
    renderShadowPairs();
  };
  // 配对按数据重建 DOM：字段 id 跟随下标，重新读取后仍然稳定。
  const renderShadowPairs = () => {
    const container = $("shadow-pair-fields");
    container.replaceChildren();
    shadowPairs.forEach((entry, index) => {
      const card = node("div", undefined, "shadow-pair");
      const heading = node("div", undefined, "section-heading");
      const title = node("strong", `${entry.values.symbol_a.toUpperCase() || "合约 A"}:${entry.values.symbol_b.toUpperCase() || "合约 B"}`);
      const remove = node("button", "移除配对", "button secondary small"); remove.type = "button";
      heading.append(title, remove);
      const grid = node("div", undefined, "shadow-field-grid");
      card.append(heading, grid);
      for (const [key, label, help, type] of pairFields) {
        const currency = key === "max_notional" ? entry.currency : entry.unhedgedCurrency;
        const input = configField(grid, `shadow-pair-${index}-${key}`, label.replace("USDT", currency), help, type);
        input.value = entry.values[key];
        entry.inputs[key] = input;
        input.addEventListener("input", () => {
          entry.values[key] = input.value;
          if (key === "symbol_a" || key === "symbol_b") {
            title.textContent = `${entry.values.symbol_a.toUpperCase() || "合约 A"}:${entry.values.symbol_b.toUpperCase() || "合约 B"}`;
          }
        });
      }
      remove.addEventListener("click", () => {
        if (shadowBusy || !shadowConfig?.writable) return;
        shadowPairs.splice(index, 1);
        renderShadowPairs();
        markShadowDirty();
      });
      container.append(card);
    });
    $("shadow-pairs-empty").hidden = shadowPairs.length > 0;
  };
  async function loadShadowConfig() {
    if (shadowBusy) return;
    shadowBusy = true; updateShadowControls();
    showError($("shadow-config-error"), "");
    try {
      const data = await request("/api/shadow/config");
      shadowConfig = data;
      $("shadow-setting-enabled").checked = data.shadow.enabled;
      for (const [group, key] of shadowFields) $("shadow-setting-" + group + "-" + key).value = String(data[group][key]);
      shadowPairs = []; renderShadowPairs();
      for (const pair of data.pairs) addShadowPair(pair);
      shadowDirty = false;
      $("shadow-config-status").textContent = data.writable
        ? "已读取服务器上的已保存配置。修改后点击保存，再手动重启 worker；这里不确认运行中参数。"
        : data.read_only_reason || "当前部署仅允许查看配置，请在服务器侧维护。";
    } catch (error) {
      // Retain draft values on failed reload; retrying is an explicit user action.
      showError($("shadow-config-error"), error.status ? error.message : "无法读取配置；已保留输入，请重试读取。");
      $("shadow-config-status").textContent = "尚未取得最新配置。";
    } finally { shadowBusy = false; updateShadowControls(); }
  }
  const shadowForm = $("shadow-config-form");
  if (shadowForm) {
    for (const [group, key, label, section, help, min, max, step] of shadowFields) {
      configField($("shadow-" + section + "-fields"), "shadow-setting-" + group + "-" + key, label, help, "number", min, max, step);
    }
    shadowForm.addEventListener("input", markShadowDirty);
    shadowForm.addEventListener("change", markShadowDirty);
    $("shadow-add-pair").addEventListener("click", () => {
      if (shadowBusy || !shadowConfig?.writable || shadowPairs.length >= 20) return;
      addShadowPair({ symbol_a: "", symbol_b: "", hedge_ratio: "1", z_entry: "2", z_exit: "0.5", z_stop: "3", max_notional: { value: "1000", currency: "USDT" }, max_unhedged_notional: { value: "25", currency: "USDT" } });
      markShadowDirty();
      document.getElementById(`shadow-pair-${shadowPairs.length - 1}-symbol_a`)?.focus();
    });
    $("shadow-config-reload").addEventListener("click", loadShadowConfig);
    shadowForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (shadowBusy || !shadowConfig?.writable || !shadowDirty || !session) return;
      if (shadowForm.reportValidity && !shadowForm.reportValidity()) return;
      if ($("shadow-setting-enabled").checked && !shadowPairs.length) {
        showError($("shadow-config-error"), "请添加至少一个观察配对后再启用影子盘。"); return;
      }
      const body = { revision: shadowConfig.revision, shadow: { ...shadowConfig.shadow, enabled: $("shadow-setting-enabled").checked }, auto: {}, pairs: [] };
      for (const [group, key, , , , , , step] of shadowFields) {
        const value = $("shadow-setting-" + group + "-" + key).value.trim();
        body[group][key] = step === 1 ? Number(value) : value;
      }
      for (const entry of shadowPairs) {
        const pair = {};
        // 读取输入框当前值：模型只是缓存，页面上的值才是操作者看到的。
        for (const [key] of pairFields) pair[key] = (entry.inputs[key]?.value ?? entry.values[key]).trim();
        pair.symbol_a = pair.symbol_a.toUpperCase(); pair.symbol_b = pair.symbol_b.toUpperCase();
        pair.pair_key = `${pair.symbol_a}:${pair.symbol_b}`;
        pair.max_notional = { value: pair.max_notional, currency: entry.currency };
        pair.max_unhedged_notional = { value: pair.max_unhedged_notional, currency: entry.unhedgedCurrency };
        body.pairs.push(pair);
      }
      shadowBusy = true; updateShadowControls(); showError($("shadow-config-error"), "");
      try {
        const result = await request("/api/shadow/config", {
          method: "PUT", headers: { "Content-Type": "application/json", "X-CSRF-Token": session.csrf_token }, body: JSON.stringify(body),
        });
        shadowConfig = { ...shadowConfig, ...body, revision: result.revision };
        shadowDirty = false;
        $("shadow-config-status").textContent = `配置已保存，旧文件已备份${result.backup ? `（${result.backup}）` : ""}。请手动重启 worker 与 web 后生效；当前运行参数尚未确认。`;
      } catch (error) {
        showError($("shadow-config-error"), error.status
          ? `${error.message} 输入已保留。`
          : "保存结果未确认，输入已保留。请重新读取服务器配置核对后再修改。");
      } finally { shadowBusy = false; updateShadowControls(); }
    });
  }

  // Shadow results never submit an order or a promotion command.
  async function loadShadow() {
    try {
      const shadow = await request("/api/shadow");
      const summary = $("shadow-summary");
      if (summary) {
        summary.replaceChildren();
        for (const [label, value] of [
          ["Web 启动时配置", shadow.enabled ? "已启用（运行状态未核实）" : "关闭"],
          ["统计观察窗", `${shadow.observation_days} 天`],
          ["模拟决策", shadow.decisions],
          ["完整闭环", shadow.closed_trades],
          ["模拟 Sharpe", amount(shadow.sharpe)],
          ["最大回撤", amount(shadow.max_drawdown)],
          ["累计模拟盈亏 (USDT)", amount(shadow.cumulative_pnl)],
          ["晋升候选", shadow.promotion_candidate ? "待人工审阅" : "未达标"],
        ]) {
          const cell = node("div");
          cell.append(node("dt", label));
          cell.append(node("dd", value));
          summary.append(cell);
        }
      }
      const configNote = shadow.pending_restart ? "已保存参数已变更；统计仍按 Web 启动时口径显示。请重启 worker 与 web 后核对采样。" : "";
      showError($("shadow-note"), [shadow.promotion_note, configNote].filter(Boolean).join(" "));
      table("shadow-decisions-body", shadow.last_decisions, [
        (r) => r.pair_key, (r) => state(r.action), (r) => r.reason,
        { value: (r) => amount(r.net_pnl, "USDT"), className: "numeric" },
        (r) => r.depth_ok ? "深度足够" : "深度不足",
        (r) => stamp(new Date(r.at_ms).toISOString()),
      ], "暂无影子盘模拟决策。启用 spread.shadow.enabled 后才会采样。");
      const daily = Object.entries(shadow.daily_pnl || {}).map(([day, value]) => ({ day, value }));
      table("shadow-daily-body", daily, [
        (r) => r.day,
        { value: (r) => amount(r.value, "USDT"), className: "numeric" },
      ], "暂无逐日模拟盈亏。");
    } catch (error) {
      showError($("shadow-note"), error.status ? error.message : "无法读取影子盘状态。");
    }
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

  const VALIDATION_STATES = {
    running: "运行中", ending: "正在收尾", ended: "已结束",
  };
  const validationChain = (value) => {
    const head = text(value, "");
    return head && head !== "0".repeat(head.length) ? `${head.slice(0, 12)}…` : "尚未锚定";
  };
  const validationAction = (kind, runId, label) => {
    const button = node("button", label, "button secondary small");
    button.type = "button";
    button.dataset.command = kind;
    button.dataset.runId = runId;
    button.disabled = !session;
    button.addEventListener("click", () => openCommand(kind, { run_id: runId }));
    return button;
  };
  const downloadValidation = async (runId, button) => {
    if (button.disabled) return;
    button.disabled = true;
    showError($("validation-error"), "");
    try {
      const response = await fetch(`/api/validation/${encodeURIComponent(runId)}/export`, {
        credentials: "same-origin",
        signal: AbortSignal.timeout(12000),
      });
      if (!response.ok) {
        let detail = `导出未完成（${response.status}）。`;
        try { detail = (await response.json()).detail || detail; } catch { /* 保留状态码提示。 */ }
        if (response.status === 401) location.assign("/login");
        throw new Error(detail);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `bybitbot-validation-${runId.replace(/[^A-Za-z0-9_-]+/g, "-")}.zip`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      showError($("validation-error"), error.message || "导出请求未完成，请稍后重试。");
    } finally { button.disabled = false; }
  };
  const exportAction = (runId) => {
    const button = node("button", "下载 ZIP", "button secondary small");
    button.type = "button";
    button.disabled = !session;
    button.addEventListener("click", () => downloadValidation(runId, button));
    return button;
  };
  function renderValidation(validation, environment) {
    const runs = (validation?.studies || []).filter((run) => !environment || run.environment === environment);
    $("validation-scope").textContent = runs.length
      ? `当前环境 ${environment || "未知"}，账户范围由服务校验`
      : "当前环境暂无验证运行；启动入口位于人工控制。";
    table("validation-body", runs, [
      (r) => stacked(r.run_id, `${text(r.policy)} · ${text(r.direction)}${r.bases?.length ? ` · ${r.bases.join("/")}` : ""}`, true),
      (r) => badge(r.status === "ended" ? "CONFIRMED" : r.status === "running" ? "EXECUTING" : "PENDING", VALIDATION_STATES[r.status] || text(r.status)),
      (r) => {
        const box = node("div");
        for (const item of r.phases || []) box.append(node("p", `${item.base}：${item.phase}`));
        return (box.children || []).length ? box : "尚无阶段记录";
      },
      (r) => stacked(stamp(r.heartbeat_at), r.heartbeat_age_seconds === null || r.heartbeat_age_seconds === undefined ? "心跳时间未知" : age(r.heartbeat_age_seconds)),
      { value: (r) => r.gap_count ?? 0, className: "numeric" },
      { value: (r) => validationChain(r.chain_head), className: "number" },
      (r) => {
        const box = node("div");
        box.append(validationAction("VALIDATION_STOP", r.run_id, "停止"));
        const exportButton = exportAction(r.run_id);
        exportButton.dataset.exportRunId = r.run_id;
        box.append(exportButton);
        return box;
      },
    ], "当前环境暂无验证运行。启动前请确认研究口径与 paper-only 范围。");
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
    VALIDATION_START: [["run_id", "运行编号", "text", true], ["bases", "验证标的（逗号分隔，BTC/ETH）", "text", true], ["policy", "信号口径", "policy", true], ["direction", "价差方向", "direction", true], ["protocol", "研究协议 JSON（对象）", "textarea", true], ["other_run_ids", "互斥运行编号（可选，逗号分隔）", "text"]],
    VALIDATION_STOP: [["run_id", "运行编号", "text", true]],
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
    VALIDATION_START: "以冻结的研究协议启动 paper-only 验证运行；不会加载交易凭据或直接发送交易所请求。服务会校验口径、互斥登记与源码冻结，命令入队不等于运行已启动。",
    VALIDATION_STOP: "申请结束指定验证运行并封存证据。服务会保留未解决敞口；停止命令入队不等于运行已结束。",
  };
  function scopeText(kind, payload = {}, environment) {
    let summary = `环境 ${environment || snapshot?.environment || "未知"}`;
    if (kind === "CANCEL_ORDER") summary += `；订单 ${(payload.order_link_ids || []).join(", ") || "未填写"}`;
    else if (kind === "REDUCE") summary += `；策略 ${payload.strategy_id || "未填写"}；${payload.category || "未选择"} ${payload.symbol || "未填写"}；数量 ${payload.qty || "未填写"}；价格不低于 ${payload.price_floor || "未填写"}`;
    else if (kind === "RESUME") summary += `；暂停 #${payload.pause_id || "未填写"}`;
    else if (kind === "CANCEL_BOT_OPEN_ORDERS") summary += `；${payload.strategy_id ? `策略 ${payload.strategy_id}` : "全部策略"}；${payload.symbol || "全部标的"}；仅 bot 开仓单`;
    else if (kind === "REGISTER_EXTERNAL_CASH_FLOW") summary += `；${payload.asset || "未填写"} ${payload.amount || "未填写"}；${payload.kind || "未选择"}；流水 ${payload.reference || "未填写"}`;
    else if (kind === "VALIDATION_START") summary += `；运行 ${payload.run_id || "未填写"}；${(payload.bases || []).join("/") || "未选择标的"}；${payload.policy || "未选择口径"} / ${payload.direction || "未选择方向"}；协议字段 ${Object.keys(payload.protocol || {}).length}`;
    else if (kind === "VALIDATION_STOP") summary += `；运行 ${payload.run_id || "未填写"}；只结束当前 paper 环境的指定运行`;
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
      else if (key === "bases") value = [...new Set(value.split(/[\s,]+/).filter(Boolean).map((item) => item.toUpperCase()))];
      else if (key === "other_run_ids") value = [...new Set(value.split(/[\s,]+/).filter(Boolean))];
      else if (key === "protocol") {
        try { value = JSON.parse(value); } catch { throw new Error(`${label}必须是合法 JSON 对象。`); }
        if (!value || typeof value !== "object" || Array.isArray(value) || !Object.keys(value).length) throw new Error(`${label}必须是非空 JSON 对象。`);
      }
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
      const input = node(type === "textarea" ? "textarea" : ["category", "cashflow", "policy", "direction"].includes(type) ? "select" : "input");
      input.id = id; input.name = key; input.required = !!required; input.autocomplete = "off";
      if (type === "category") for (const [value, title] of [["spot", "现货 spot"], ["option", "期权 option"]]) {
        const option = node("option", title); option.value = value; input.append(option);
      }
      if (type === "policy") for (const [value, title] of [["strong_up", "转强看涨 strong_up"], ["weak_down", "弱势看跌 weak_down"]]) {
        const option = node("option", title); option.value = value; input.append(option);
      }
      if (type === "direction") for (const [value, title] of [["Call", "看涨价差 Call"], ["Put", "看跌价差 Put"]]) {
        const option = node("option", title); option.value = value; input.append(option);
      }
      if (type === "cashflow") for (const [value, title] of [["DEPOSIT", "充值 deposit"], ["TRANSFER_IN", "划入 transfer in"], ["WITHDRAWAL", "提现 withdrawal"], ["TRANSFER_OUT", "划出 transfer out"]]) {
        const option = node("option", title); option.value = value; input.append(option);
      }
      if (["id", "decimal"].includes(type)) input.inputMode = type === "id" ? "numeric" : "decimal";
      if (input.tagName !== "SELECT") {
        input.maxLength = key === "order_link_ids" ? 3200 : key === "protocol" ? 16384 : 500;
      }
      input.value = Array.isArray(values[key]) ? values[key].join(", ") : values[key] && typeof values[key] === "object" ? JSON.stringify(values[key]) : text(values[key], "");
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
    if (!tabs.some((tab) => tab.dataset.tab === name)) name = "recovery";
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
  document.querySelectorAll("[data-goto]").forEach((link) => {
    link.addEventListener("click", (event) => { event.preventDefault(); selectTab(link.dataset.goto); });
  });
  selectTab(location.hash.slice(1));
  async function refresh() {
    if (refreshing) return;
    refreshing = true; $("refresh").disabled = true;
    try { render(await request("/api/status")); showError($("connection-error"), ""); }
    catch (error) { showError($("connection-error"), error.status ? error.message : "无法读取管理服务；表中保留的是上次读取的记录，请勿据此判断实时状态。"); }
    finally { refreshing = false; $("refresh").disabled = false; }
  }
  $("export-audit")?.addEventListener("click", async () => {
    const button = $("export-audit");
    button.disabled = true;
    showError($("audit-export-note"), "");
    try {
      const response = await fetch("/api/audit.csv", { credentials: "same-origin", signal: AbortSignal.timeout(30000) });
      if (!response.ok) {
        if (response.status === 401) location.assign("/login");
        throw new Error(`导出未完成（${response.status}）。`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "bybitbot-audit.csv";
      anchor.click();
      URL.revokeObjectURL(url);
      if (response.headers.get("X-Export-Truncated") === "true") {
        showError($("audit-export-note"), "导出结果已被截断，文件内容不是完整历史；请按时间范围重新核对。");
      }
    } catch (error) {
      showError($("audit-export-note"), error.message || "导出失败，请稍后重试。");
    } finally { button.disabled = false; }
  });
  $("refresh").addEventListener("click", refresh);
  $("logout").addEventListener("click", async () => {
    try { await post("/api/logout", null, session?.csrf_token); location.assign("/login"); }
    catch (error) { showError($("connection-error"), error.message || "退出登录未完成，请重试。"); }
  });
  (async () => {
    try {
      session = await request("/api/session");
      $("operator").textContent = `管理员 ${session.username}`;
      if (session.expires_at) {
        const expires = new Date(session.expires_at);
        if (Number.isFinite(expires.getTime()) && expires.getTime() - Date.now() < 5 * 60 * 1000) {
          showError($("connection-error"), "管理会话即将到期；请保存核对结果并重新登录。");
        }
      }
      if (shadowForm) void loadShadowConfig();
      await refresh();
      setInterval(() => { if (!document.hidden) refresh(); }, 15000);
      document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
    } catch (error) { showError($("connection-error"), error.message || "无法验证会话，请重新登录。"); }
  })();
})();
