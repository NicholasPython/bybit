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

function createDocument(commandButtons) {
  const elements = new Map();
  return {
    body: new FakeNode("body"),
    hidden: false,
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new FakeNode("div"));
      return elements.get(id);
    },
    createElement(tag) { return new FakeNode(tag); },
    createTextNode(value) { const item = new FakeNode("#text"); item.textContent = value; return item; },
    querySelectorAll(selector) { return selector === "[data-command]" ? commandButtons : []; },
    addEventListener() {},
  };
}

async function boot({ commands = [], pending = null, commandResponder = null, commandKind = "CANCEL_BOT_OPEN_ORDERS" } = {}) {
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
  };
  const commandButtons = [new FakeNode("button")];
  commandButtons[0].dataset.command = commandKind;
  const document = createDocument(commandButtons);
  const fetchStub = async (path, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    requests.push({ path, method: options.method || "GET", body });
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
