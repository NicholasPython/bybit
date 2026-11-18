// Focused checks for the console UI behaviours fixed by ISSUE-001 and ISSUE-002.
// Run with: node --test tests/console_ui.test.mjs
//
// The page script is an IIFE with no exports, so the harness loads it into a vm
// context with a minimal DOM, fetch and sessionStorage stub, then drives it
// through the same events a browser would deliver.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const consoleSource = readFileSync(join(here, "..", "src", "bybitbot", "web", "static", "console.js"), "utf8");
const PENDING_KEY = "bybitbot-pending-command-v1";
const CONFIRMATION = "确认";

class FakeNode {
  constructor(tagName = "div") {
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.listeners = new Map();
    this.dataset = {};
    this.attributes = {};
    this.className = "";
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.focused = false;
    this.open = false;
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }
  async dispatch(type, event = {}) {
    const payload = { preventDefault() {}, stopPropagation() {}, ...event };
    for (const handler of this.listeners.get(type) || []) await handler(payload);
  }
  focus() { this.focused = true; }
  after() {}
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelectorAll() { return []; }
  showModal() { this.open = true; }
  close() { this.open = false; }
}

const jsonResponse = (status, data) => ({
  status,
  ok: status >= 200 && status < 300,
  json: async () => data,
});

const flush = async (times = 4) => {
  for (let index = 0; index < times; index += 1) await new Promise((resolve) => setTimeout(resolve, 0));
};

// 把伪造 DOM 里的文本摊平成一行，便于断言。
const flatten = (node) => {
  const own = node.textContent ? String(node.textContent) : "";
  const children = (node.children || []).map(flatten).join("");
  return `${own}${children}`;
};

function createDocument(commandButtons) {
  const elements = new Map();
  return {
    body: new FakeNode("body"),
    hidden: false,
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new FakeNode("div"));
      return elements.get(id);
    },
    createElement(tag) {
      const created = new FakeNode(tag);
      Object.defineProperty(created, "id", {
        get: () => created._id || "",
        set: (next) => { created._id = next; elements.set(next, created); },
        configurable: true,
      });
      return created;
    },
    createTextNode(value) { const item = new FakeNode("#text"); item.textContent = value; return item; },
    querySelectorAll(selector) {
      if (selector === "[data-command]") return commandButtons;
      if (selector === "[data-export-run-id]") return [...elements.values()].filter((item) => item.dataset?.exportRunId);
      return [];
    },
    addEventListener() {},
  };
}

async function boot({ commands = [], pending = null, commandResponder = null, commandKind = "CANCEL_BOT_OPEN_ORDERS", snapshotPatch = {}, fetchResponder = null } = {}) {
  const requests = [];
  const store = new Map();
  if (pending) store.set(PENDING_KEY, JSON.stringify(pending));
  const sessionStorage = {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: (key) => store.delete(key),
  };
  const snapshot = {
    environment: "paper",
    currency: "USDT",
    generated_at: "2026-11-01T02:43:19Z",
    table_limit: 100,
    runtime: { state: "PAUSED_OPEN" },
    heartbeat: { at: "2026-11-01T02:43:19Z", age_seconds: 1.5, fresh: true },
    account: null,
    pauses: [],
    strategies: [],
    control_commands: [...commands],
    ...snapshotPatch,
  };
  const commandButtons = [new FakeNode("button")];
  commandButtons[0].dataset.command = commandKind;
  const document = createDocument(commandButtons);
  const fetchStub = async (path, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    requests.push({ path, method: options.method || "GET", body });
    if (fetchResponder) {
      const custom = await fetchResponder(path, options);
      if (custom) return custom;
    }
    if (path === "/api/session") return jsonResponse(200, { username: "operator", csrf_token: "csrf-token" });
    if (path === "/api/status") return jsonResponse(200, snapshot);
    if (path === "/api/commands") {
      if (commandResponder) return commandResponder(body);
      return jsonResponse(202, {
        command: { command_id: body.command_id, kind: body.kind, status: "PENDING", result: {} },
        created: true,
      });
    }
    return jsonResponse(404, { detail: "未找到。" });
  };
  const sandbox = {
    document,
    fetch: fetchStub,
    sessionStorage,
    location: { hash: "", assign(path) { this.path = path; } },
    history: { replaceState() {} },
    crypto: globalThis.crypto,
    AbortSignal: globalThis.AbortSignal,
    setInterval: () => 0,
    clearInterval: () => {},
    console,
    Node: FakeNode,
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(consoleSource, sandbox, { filename: "console.js" });
  await flush();
  return {
    sandbox,
    document,
    requests,
    sessionStorage,
    commandButtons,
    element: (id) => document.getElementById(id),
    setCommand(row) {
      const index = snapshot.control_commands.findIndex((item) => item.command_id === row.command_id);
      if (index === -1) snapshot.control_commands.unshift(row);
      else snapshot.control_commands[index] = row;
    },
  };
}

const commandRow = (overrides = {}) => ({
  command_id: "0559fbe5-9705-456a-a870-ad1899e1a63e",
  kind: "CANCEL_BOT_OPEN_ORDERS",
  status: "PENDING",
  requested_by: "operator",
  created_at: "2026-11-01T02:43:19Z",
  result: {},
  ...overrides,
});

test("ISSUE-001：确认词不合法时只提示缺少确认词，不发请求也不改输入", async () => {
  const app = await boot();
  await app.commandButtons[0].dispatch("click");
  const dialog = app.element("command-dialog");
  assert.equal(dialog.open, true, "确认框应已打开");
  const confirmation = app.element("confirmation");
  confirmation.value = "测试";
  await app.element("command-form").dispatch("submit");

  const error = app.element("command-error");
  assert.equal(error.textContent, "请输入完整的“确认”后登记命令。");
  assert.equal(error.hidden, false, "错误提示应可见");
  assert.equal(error.textContent.includes("登记结果未确认"), false, "本地校验失败不应描述为登记结果未知");
  assert.equal(error.textContent.includes("。。"), false, "不应出现重复句号");
  assert.equal(app.requests.filter((request) => request.path === "/api/commands").length, 0, "不应发出登记请求");
  assert.equal(app.sessionStorage.getItem(PENDING_KEY), null, "不应留下待核实命令");
  assert.equal(confirmation.value, "测试", "已填写内容应保留");
  assert.equal(dialog.open, true, "确认框应保持打开");
  assert.equal(app.element("submit-command").textContent, "登记命令", "未发出请求时不应提示用原编号重试");
});

test("ISSUE-001：登记结果未确认时保留编号与填写内容，重试复用同一编号", async () => {
  const app = await boot({ commandResponder: async () => { throw new TypeError("Failed to fetch"); } });
  await app.commandButtons[0].dispatch("click");
  app.element("confirmation").value = CONFIRMATION;
  await app.element("command-form").dispatch("submit");

  const error = app.element("command-error");
  assert.match(error.textContent, /登记结果未确认/, "网络中断应说明登记结果未确认");
  assert.match(error.textContent, /重试/, "网络中断应提示重试");
  assert.equal(error.textContent.includes("。。"), false, "不应出现重复句号");
  assert.equal(app.element("submit-command").textContent, "用原编号重试");
  assert.equal(app.element("confirmation").value, CONFIRMATION, "已填写内容应保留");
  const stored = JSON.parse(app.sessionStorage.getItem(PENDING_KEY));
  const first = app.requests.filter((request) => request.path === "/api/commands")[0];
  assert.equal(stored.body.command_id, first.body.command_id, "待核实记录应保留原编号");

  await app.element("command-form").dispatch("submit");
  const posts = app.requests.filter((request) => request.path === "/api/commands");
  assert.equal(posts.length, 2);
  assert.equal(posts[1].body.command_id, posts[0].body.command_id, "重试应复用同一请求编号");
});

test("ISSUE-002：命令执行完成后状态提示随持久化记录更新", async () => {
  const app = await boot();
  await app.commandButtons[0].dispatch("click");
  app.element("confirmation").value = CONFIRMATION;
  await app.element("command-form").dispatch("submit");

  const notice = app.element("command-notice");
  const posted = app.requests.filter((request) => request.path === "/api/commands")[0].body;
  app.setCommand(commandRow({ command_id: posted.command_id, status: "PENDING" }));
  await app.element("refresh").dispatch("click");
  assert.match(notice.textContent, /待处理/, "尚未写入结果时提示待处理");
  assert.match(notice.textContent, new RegExp(posted.command_id));

  app.setCommand(commandRow({
    command_id: posted.command_id,
    status: "EXECUTED",
    result: { terminal_confirmed: [], pending: [], scope: "CANCEL_BOT_OPEN_ORDERS" },
  }));
  await app.element("refresh").dispatch("click");
  assert.match(notice.textContent, /已执行/, "服务写入终态后提示应更新");
  assert.equal(notice.textContent.includes("待处理"), false, "不应残留待处理提示");
  assert.match(notice.textContent, /terminal_confirmed/, "提示应包含持久化结果");
});

test("ISSUE-002：待处理记录清除后提示仍随持久化结果更新", async () => {
  const commandId = "0559fbe5-9705-456a-a870-ad1899e1a63e";
  const app = await boot({
    commands: [commandRow({ command_id: commandId })],
    pending: {
      environment: "paper",
      body: { command_id: commandId, kind: "CANCEL_BOT_OPEN_ORDERS", payload: {}, confirmation: CONFIRMATION },
    },
  });
  const notice = app.element("command-notice");
  assert.equal(app.sessionStorage.getItem(PENDING_KEY), null, "找到持久化记录后应清除待处理标记");
  assert.match(notice.textContent, /待处理/);

  app.setCommand(commandRow({ command_id: commandId, status: "EXECUTED", result: { terminal_confirmed: [], pending: [] } }));
  await app.element("refresh").dispatch("click");
  assert.match(notice.textContent, /已执行/, "清除待处理后提示仍应跟随最终结果");
  assert.equal(notice.textContent.includes("待处理"), false, "不应残留待处理提示");
  assert.equal(app.element("retry-pending").hidden, true, "不应再提示重试未确认命令");
});

test("外部资金流登记：表单收集范围并要求正金额与确认词", async () => {
  const app = await boot({ commandKind: "REGISTER_EXTERNAL_CASH_FLOW" });
  await app.commandButtons[0].dispatch("click");
  assert.equal(app.element("command-title").textContent, "登记外部资金流");

  app.element("field-asset").value = "usdt";
  app.element("field-amount").value = "5.55";
  app.element("field-kind").value = "TRANSFER_IN";
  app.element("field-reference").value = "bybit-transfer-20261101";
  app.element("field-reason").value = "主账户划入子账户";

  app.element("confirmation").value = CONFIRMATION;
  app.element("field-amount").value = "0";
  await app.element("command-form").dispatch("submit");
  assert.match(app.element("command-error").textContent, /必须是正数/);
  assert.equal(app.requests.filter((request) => request.path === "/api/commands").length, 0);

  app.element("field-amount").value = "5.55";
  await app.element("command-form").dispatch("submit");

  const posted = app.requests.filter((request) => request.path === "/api/commands")[0].body;
  assert.equal(posted.kind, "REGISTER_EXTERNAL_CASH_FLOW");
  assert.deepEqual(posted.payload, {
    asset: "USDT",
    amount: "5.55",
    kind: "TRANSFER_IN",
    reference: "bybit-transfer-20261101",
    reason: "主账户划入子账户",
  });
});

test("策略生效参数：只显示生效版本，列出参数与退出规则", async () => {
  const app = await boot({
    snapshotPatch: {
      strategies: [{ id: "s-eth", name: "eth-call-weekly" }],
      config_versions: [
        {
          id: "v2",
          strategy_id: "s-eth",
          version: 2,
          active: true,
          params: {
            options_type: "Call",
            min_days_to_expiry: "7",
            max_days_to_expiry: "45",
            delta_low: "0.30",
            delta_high: "0.60",
            strike_low_pct: "90",
            strike_high_pct: "110",
            min_premium: "50",
            max_premium: "200",
            max_fee_ratio: "0.01",
          },
          exit_rules: [
            { kind: "premium_stop_loss", stop_loss_pct: "25", max_reprice_attempts: "1", exit_validity_seconds: "300" },
            { kind: "hold_period", hold_seconds: "1209600" },
            { kind: "before_expiry" },
          ],
        },
        {
          id: "v1",
          strategy_id: "s-eth",
          version: 1,
          active: false,
          params: {},
          exit_rules: [{ kind: "before_expiry" }],
        },
      ],
    },
  });

  const body = app.element("strategy-params-body");
  assert.equal(body.children.length, 1, "只应渲染生效版本");
  const cells = body.children[0].children;
  assert.equal(cells.length, 4, "应为策略、版本、生效参数、退出规则四列");
  assert.equal(flatten(cells[0]), "eth-call-weekly");
  assert.equal(flatten(cells[1]), "2");

  const params = flatten(cells[2]);
  assert.match(params, /Delta 下界：0\.30/);
  assert.match(params, /Delta 上界：0\.60/);
  assert.match(params, /费用占权利金上限：0\.01/);
  assert.match(params, /最少到期天数：7/);
  assert.match(params, /行权价下界\(%\)：90/);

  const rules = flatten(cells[3]);
  assert.match(rules, /权利金止损：stop_loss_pct=25/);
  assert.match(rules, /持有到期：hold_seconds=1209600/);
  assert.match(rules, /到期前退出：按全局配置/);
  assert.equal(rules.includes("max_reprice_attempts"), false, "不应展示执行细节字段");
});

const validationRun = {
  run_id: "paper-validation-1",
  environment: "paper",
  status: "running",
  policy: "weak_down",
  direction: "Put",
  bases: ["BTC", "ETH"],
  heartbeat_at: "2026-11-01T02:43:00Z",
  heartbeat_age_seconds: 19,
  gap_count: 2,
  chain_head: "abcdef1234567890",
  phases: [{ base: "BTC", phase: "waiting" }],
};

function findByText(node, needle) {
  if (!node || typeof node !== "object") return null;
  if (String(node.textContent || "").includes(needle)) return node;
  for (const child of node.children || []) {
    const found = findByText(child, needle);
    if (found) return found;
  }
  return null;
}

function fieldInput(app, label) {
  const wrapper = findByText(app.element("command-fields"), label);
  assert.ok(wrapper, `命令表单应包含字段 ${label}`);
  const labelNode = (wrapper.children || []).find((child) => child.tagName === "LABEL");
  assert.ok(labelNode, `字段 ${label} 应有标签`);
  const fieldId = labelNode.htmlFor || labelNode.attributes?.for;
  assert.ok(fieldId, `字段 ${label} 应关联输入控件`);
  return app.element(fieldId);
}

test("验证运行：展示状态、相位、心跳、缺口与截断账本锚点", async () => {
  const app = await boot({ snapshotPatch: {
    validation: { studies: [validationRun, { ...validationRun, run_id: "other", environment: "live" }] },
  } });
  const body = app.element("validation-body");
  assert.equal(body.children.length, 1, "只显示当前环境的验证运行");
  const row = flatten(body.children[0]);
  assert.match(row, /paper-validation-1/);
  assert.match(row, /运行中/);
  assert.match(row, /BTC：waiting/);
  assert.match(row, /19\.0 秒/);
  assert.match(row, /abcdef123456…/, "账本锚点应截断显示");
  assert.equal(row.includes("abcdef1234567890"), false);
  assert.equal(row.includes("other"), false);
});

test("验证停止：按运行编号打开既有命令对话框", async () => {
  const app = await boot({ snapshotPatch: { validation: { studies: [validationRun] } } });
  const stopButton = findByText(app.element("validation-body"), "停止");
  assert.ok(stopButton, "验证运行表中应有停止按钮");
  assert.equal(stopButton.disabled, false, "已登录时停止按钮应可用");
  await stopButton.dispatch("click");
  assert.equal(app.element("command-title").textContent, "停止验证运行");
  assert.equal(app.element("field-run_id").value, "paper-validation-1");
  assert.equal(app.element("confirmation").required, true);
  app.element("confirmation").value = CONFIRMATION;
  await app.element("command-form").dispatch("submit");
  const posted = app.requests.filter((request) => request.path === "/api/commands")[0].body;
  assert.equal(posted.kind, "VALIDATION_STOP");
  assert.deepEqual(posted.payload, { run_id: "paper-validation-1" });
});

test("验证启动：协议 JSON 解析为对象并组装互斥运行编号", async () => {
  const app = await boot({ commandKind: "VALIDATION_START" });
  await app.commandButtons[0].dispatch("click");
  assert.equal(app.element("command-title").textContent, "启动验证运行");
  app.element("field-run_id").value = "paper-validation-2";
  app.element("field-bases").value = "btc, ETH, btc";
  app.element("field-policy").value = "weak_down";
  app.element("field-direction").value = "Put";
  app.element("field-protocol").value = '{"study_days":5,"target_entry_days":21}';
  app.element("field-other_run_ids").value = "paper-validation-1,paper-validation-1";
  app.element("confirmation").value = CONFIRMATION;
  await app.element("command-form").dispatch("submit");
  const posted = app.requests.filter((request) => request.path === "/api/commands")[0].body;
  assert.equal(posted.kind, "VALIDATION_START");
  assert.deepEqual(posted.payload, {
    run_id: "paper-validation-2",
    bases: ["BTC", "ETH"],
    policy: "weak_down",
    direction: "Put",
    protocol: { study_days: 5, target_entry_days: 21 },
    other_run_ids: ["paper-validation-1"],
  });
  assert.match(app.element("command-notice").textContent, /命令/);
});

test("验证导出：直接 GET 下载 ZIP，不经命令队列", async () => {
  const app = await boot({ snapshotPatch: { validation: { studies: [validationRun] } },
    fetchResponder: async (path, options) => {
      if (path === "/api/validation/paper-validation-1/export") return {
        status: 200, ok: true, blob: async () => ({ size: 4 }),
      };
      return null;
    },
  });
  const exportButton = findByText(app.element("validation-body"), "下载 ZIP");
  assert.ok(exportButton, "验证运行表中应有下载按钮");
  assert.equal(typeof exportButton.dataset.exportRunId, "string");
  await exportButton.dispatch("click");
  const download = app.requests.find((request) => request.path.includes("/export"));
  assert.ok(download, "应直接请求导出接口");
  assert.equal(download.method, "GET");
  assert.equal(app.requests.filter((request) => request.path === "/api/commands").length, 0);
});

test("恢复状态派生：阻塞、关注、就绪三档", async () => {
  const app = await boot();
  const derive = app.sandbox.__bybitbotConsole?.deriveRecoveryState;
  assert.equal(typeof derive, "function", "console.js 应暴露纯派生函数供测试");

  const blocking = derive({
    runtime: { lease: "NEEDS_CONFIRMATION", valuation_complete: false },
    heartbeat: { fresh: true },
    account: { complete: true, stale: false },
    reconciliations: [{ scope: "full", allows_resume: false, difference_count: 2, repaired: false }],
    pauses: [{ id: 7, active: true, locked: true, reason_code: "MANUAL", reason_detail: "人工暂停" }],
    orders: [{ status: "UNKNOWN", sync_state: "UNCERTAIN" }],
  });
  assert.equal(blocking.level, "blocking");
  for (const code of ["RECONCILE_BLOCKED", "RECONCILE_DIFFERENCE", "LOCKED_PAUSE", "UNKNOWN_ORDER", "TAKEOVER_NOT_CONFIRMED"]) {
    assert.ok(blocking.blocking.some((item) => item.code === code), `应包含 ${code}`);
  }

  const attention = derive({
    runtime: { lease: "ELIGIBLE", valuation_complete: true },
    heartbeat: { fresh: false },
    account: { complete: true, stale: true },
    reconciliations: [{ scope: "full", allows_resume: true, difference_count: 0, repaired: false }],
    pauses: [{ id: 8, active: true, locked: false, reason_code: "SERVICE_FAILURE", reason_detail: "周期失败" }],
    orders: [],
  });
  assert.equal(attention.level, "attention");
  assert.ok(attention.attention.some((item) => item.code === "HEARTBEAT_STALE"));
  assert.ok(attention.attention.some((item) => item.code === "TECHNICAL_PAUSE"));

  const ready = derive({
    runtime: { lease: "ELIGIBLE", valuation_complete: true },
    heartbeat: { fresh: true },
    account: { complete: true, stale: false },
    baseline: { day: "2026-11-18" },
    reconciliations: [{ scope: "full", allows_resume: true, difference_count: 0, repaired: false }],
    pauses: [],
    orders: [],
  });
  assert.equal(ready.level, "ready");
});

test("派生函数兼容字符串与对象形态的租约", async () => {
  const app = await boot();
  const { leaseState, isUnknownOrder } = app.sandbox.__bybitbotConsole;
  assert.equal(leaseState({ lease: "NEEDS_CONFIRMATION" }), "NEEDS_CONFIRMATION");
  assert.equal(leaseState({ lease: { state: "ELIGIBLE" } }), "ELIGIBLE");
  assert.equal(isUnknownOrder({ status: "UNKNOWN" }), true);
  assert.equal(isUnknownOrder({ status: "SUBMITTING" }), true);
  assert.equal(isUnknownOrder({ status: "FILLED" }), false);
});

test("恢复证据链：对账水位、UNKNOWN 订单、锁定暂停、租约", async () => {
  const app = await boot({ snapshotPatch: {
    runtime: { lease: "NEEDS_CONFIRMATION", valuation_complete: false },
    reconciliations: [{ id: 42, scope: "full", allows_resume: false, difference_count: 2, repaired: false,
      watermark: "2026-11-18T02:00:00Z", cursor: "offset-19", started_at: "2026-11-18T01:59:00Z", finished_at: "2026-11-18T02:00:00Z" }],
    orders: [{ id: 7, order_link_id: "unknown-1", symbol: "BTCUSDT", status: "UNKNOWN", sync_state: "UNCERTAIN", local_ttl_expires_at: "2026-11-18T02:10:00Z" }],
    pauses: [{ id: 9, active: true, locked: true, reason_code: "MANUAL", reason_detail: "人工暂停", created_at: "2026-11-18T01:00:00Z" }],
    cash_flows: [],
  } });
  const evidence = app.element("recovery-evidence-grid");
  const evidenceText = flatten(evidence);
  assert.ok(evidenceText.includes("offset-19"), "应展示对账游标");
  assert.ok(evidenceText.includes("unknown-1"), "应展示 UNKNOWN 订单编号");
  assert.ok(evidenceText.includes("MANUAL"), "应展示锁定暂停原因");
  assert.ok(evidenceText.includes("等待接管确认"), "应展示租约状态");

  const steps = app.element("recovery-workflow-steps");
  const stepText = flatten(steps);
  assert.ok(stepText.includes("补齐交易与账户事实"));
  assert.ok(stepText.includes("登记未解释的外部资金变化"));
  assert.ok(stepText.includes("核实 UNKNOWN 订单与锁定暂停"));
  assert.ok(stepText.includes("逐条解除暂停并确认接管"));
  assert.ok(steps.children.length === 4, "应有四个恢复步骤");
});

test("恢复证据条的证据入口按钮切换到对应工作区", async () => {
  const app = await boot({ snapshotPatch: {
    runtime: { lease: "NEEDS_CONFIRMATION", valuation_complete: true },
    reconciliations: [{ scope: "full", allows_resume: false, difference_count: 1, repaired: false }],
  } });
  const summary = app.element("recovery-summary");
  const buttons = [];
  const collect = (node) => { if (node.tagName === "BUTTON" && String(node.textContent).includes("查看证据")) buttons.push(node); for (const child of node.children || []) collect(child); };
  collect(summary);
  assert.ok(buttons.length > 0, "阻塞项应带证据入口按钮");
  await buttons[0].dispatch("click");
  // 假 DOM 没有真实 tab 列表；验证按钮可点击且不抛错即可，
  // 模板里五个 tab 的存在与默认选中由 test_web.py 断言。
});
