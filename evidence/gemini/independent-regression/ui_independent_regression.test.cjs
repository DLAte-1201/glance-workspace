const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// Minimal DOM harness to run static/app.js in Node and exercise actual UI event handlers and fetch calls
function createDomHarness({ onFetch }) {
  const elements = {};
  const listeners = { document: {}, window: {} };

  function makeEl(id, tag = "div") {
    const el = {
      id,
      tagName: tag.toUpperCase(),
      value: "",
      textContent: "",
      innerHTML: "",
      hidden: false,
      disabled: false,
      open: false,
      checked: true,
      scrollTop: 0,
      scrollHeight: 100,
      dataset: {},
      attributes: {},
      classList: {
        _set: new Set(),
        add(c) { this._set.add(c); },
        remove(c) { this._set.delete(c); },
        toggle(c, force) {
          if (force === undefined) {
            if (this._set.has(c)) this._set.delete(c); else this._set.add(c);
          } else if (force) this._set.add(c); else this._set.delete(c);
        },
        contains(c) { return this._set.has(c); }
      },
      setAttribute(k, v) { this.attributes[k] = String(v); },
      getAttribute(k) { return this.attributes[k] ?? null; },
      removeAttribute(k) { delete this.attributes[k]; },
      querySelector() { return null; },
      querySelectorAll() { return []; },
      closest() { return null; },
      contains() { return false; },
      focus() {},
      click() {},
      showModal() { this.open = true; },
      close() { this.open = false; },
      requestSubmit() {
        if (typeof this.onsubmit === "function") {
          this.onsubmit({ preventDefault() {}, target: this });
        }
      }
    };
    elements[id] = el;
    return el;
  }

  const requiredIds = [
    "toast", "state-connection", "project-title", "breadcrumb-project", "project-description",
    "project-status", "project-count", "task-count", "project-list", "task-list", "agent-strip",
    "mode-notice", "storage-notice", "message-count", "messages", "reply-context",
    "inspector-heading", "inspector-content", "send-button", "refresh-button", "upload-button",
    "world-renderer-diagnostics", "world-stage", "world", "world-renderer-mode", "verified-count",
    "world-status", "world-accessible-controls", "chat-view", "world-view", "chat-toggle",
    "world-toggle", "refresh-renderer-info", "evidence-button", "message-form", "message-input",
    "chat-mode", "connections-dialog", "connections-content", "connections-button",
    "close-connections", "plan-provider", "plan-phase", "plan-prompt", "plan-model",
    "history-source", "upload-status", "file-input"
  ];
  for (const id of requiredIds) makeEl(id);
  elements["chat-mode"].value = "synthetic";

  const checkboxes = [
    { name: "chat-provider", value: "codex", checked: true },
    { name: "chat-provider", value: "claude", checked: true },
    { name: "chat-provider", value: "antigravity", checked: true }
  ];

  const document = {
    hidden: false,
    activeElement: null,
    getElementById(id) { return elements[id] || makeEl(id); },
    querySelector(sel) {
      if (sel.startsWith("select[data-chat-reviewer=")) {
        return { value: "claude" };
      }
      return null;
    },
    querySelectorAll(sel) {
      if (sel === 'input[name="chat-provider"]:checked') {
        return checkboxes.filter(c => c.checked);
      }
      return [];
    },
    addEventListener(type, fn) {
      (listeners.document[type] ||= []).push(fn);
    }
  };

  const storageMap = new Map();
  const localStorage = {
    getItem(k) { return storageMap.has(k) ? storageMap.get(k) : null; },
    setItem(k, v) { storageMap.set(k, String(v)); },
    removeItem(k) { storageMap.delete(k); }
  };
  const sessionStorage = {
    getItem(k) { return storageMap.has("sess:" + k) ? storageMap.get("sess:" + k) : null; },
    setItem(k, v) { storageMap.set("sess:" + k, String(v)); },
    removeItem(k) { storageMap.delete("sess:" + k); }
  };

  let uuidCounter = 0;
  const window = {
    document,
    localStorage,
    sessionStorage,
    CSS: { escape: s => String(s) },
    crypto: {
      randomUUID() {
        uuidCounter += 1;
        return `uuid-fixed-${String(uuidCounter).padStart(8, "0")}`;
      }
    },
    matchMedia() { return { matches: true, addEventListener() {} }; },
    addEventListener(type, fn) { (listeners.window[type] ||= []).push(fn); }
  };

  const context = vm.createContext({
    window,
    document,
    localStorage,
    sessionStorage,
    CSS: window.CSS,
    crypto: window.crypto,
    fetch: (...args) => onFetch(...args),
    setInterval() { return 1; },
    setTimeout() { return 1; },
    clearTimeout() {},
    MouseEvent: class {},
    Uint8Array,
    TextDecoder,
    TextEncoder,
    Error,
    TypeError,
    Promise,
    JSON,
    Math,
    Date,
    Number,
    String,
    Array,
    Object,
    Set,
    Map,
    RegExp
  });

  const appSource = fs.readFileSync(path.join(__dirname, "static", "app.js"), "utf8");
  vm.runInContext(appSource, context, { filename: "static/app.js" });

  return { elements, listeners, storageMap, window, document };
}

test("[UI 결함 재현 1] VERSION_CONFLICT 외에 서버 접수 후 HTTP 응답 유실(네트워크 오류) 시 같은 입력 재시도는 동일 submission_id를 재사용해야 함", async () => {
  let serverState = {
    version: 1,
    session_token: "tok-1",
    storage_writable: true,
    project: { id: "local-workroom", title: "Glance", status: "active" },
    chat_turns: [],
    runs: [],
    events: []
  };
  const receivedSubmissions = [];
  let dropNextPostResponse = true;

  const harness = createDomHarness({
    onFetch: async (url, opts = {}) => {
      if (url === "/api/state") {
        return { ok: true, status: 200, json: async () => serverState };
      }
      if (url === "/api/commands" && opts.method === "POST") {
        const body = JSON.parse(opts.body);
        if (body.command === "chat_send") {
          receivedSubmissions.push(body.payload.submission_id);
          // Server accepts and commits the turn even though the HTTP response is lost in transit!
          if (!serverState.chat_turns.some(t => t.submission_id === body.payload.submission_id)) {
            serverState.version += 1;
            serverState.chat_turns.push({
              id: "turn-" + serverState.version,
              kind: "ask",
              submission_id: body.payload.submission_id,
              text: body.payload.text,
              providers: body.payload.providers,
              run_ids: ["run-" + serverState.version],
              runs: []
            });
          }
          if (dropNextPostResponse) {
            dropNextPostResponse = false;
            throw new Error("Network connection reset after server accepted request");
          }
          return {
            ok: true,
            status: 200,
            json: async () => ({
              ok: true,
              version: serverState.version,
              state: serverState,
              duplicate: true,
              unchanged: true
            })
          };
        }
      }
      throw new Error("Unexpected fetch: " + url);
    }
  });

  // Wait for initial refresh() to complete
  await new Promise(r => setTimeout(r, 30));

  // User types a question and submits
  harness.elements["message-input"].value = "응답 유실 테스트 질문";
  await harness.elements["message-form"].onsubmit({ preventDefault() {} });
  await new Promise(r => setTimeout(r, 30));

  // Because the first HTTP response was lost, message-input still has the text ("응답 유실 테스트 질문").
  // User presses Send again to retry the exact same pending input.
  assert.equal(harness.elements["message-input"].value, "응답 유실 테스트 질문");
  await harness.elements["message-form"].onsubmit({ preventDefault() {} });
  await new Promise(r => setTimeout(r, 30));

  // Both attempts represent the same user intent after a lost HTTP response, so submission_id MUST match,
  // and serverState.chat_turns must have only 1 turn (no duplicate execution!).
  assert.equal(receivedSubmissions.length, 2);
  assert.equal(
    receivedSubmissions[0],
    receivedSubmissions[1],
    `Expected retry after lost HTTP response to reuse submission_id ${receivedSubmissions[0]}, but UI generated a new submission_id ${receivedSubmissions[1]}`
  );
  assert.equal(serverState.chat_turns.length, 1);
});

test("[UI 결함 재현 2] 응답 유실 뒤 새로고침 시 클라이언트가 pending submission_id를 보존·대조하여 이미 저장된 요청인지 확인해야 함", async () => {
  const serverState = {
    version: 2,
    session_token: "tok-1",
    storage_writable: true,
    project: { id: "local-workroom", title: "Glance", status: "active" },
    chat_turns: [],
    runs: [],
    events: []
  };

  let postCount = 0;
  const harness = createDomHarness({
    onFetch: async (url, opts = {}) => {
      if (url === "/api/state") {
        return { ok: true, status: 200, json: async () => serverState };
      }
      if (url === "/api/commands" && opts.method === "POST") {
        postCount += 1;
        const body = JSON.parse(opts.body);
        serverState.chat_turns.push({
          id: "turn-saved-1",
          kind: "ask",
          submission_id: body.payload.submission_id,
          text: body.payload.text,
          providers: body.payload.providers,
          run_ids: ["run-1"],
          runs: []
        });
        throw new Error("HTTP response lost before reaching browser");
      }
      throw new Error("Unexpected fetch");
    }
  });

  await new Promise(r => setTimeout(r, 30));
  harness.elements["message-input"].value = "새로고침 전 응답 유실 질문";
  await harness.elements["message-form"].onsubmit({ preventDefault() {} });
  await new Promise(r => setTimeout(r, 30));

  assert.equal(postCount, 1);
  // Check whether UI persisted the pending submission_id in storage before/during flight so reload can reconcile it
  assert.ok(
    harness.storageMap.size > 0,
    "Expected UI to persist pending submission_id before receiving HTTP response so a page reload after response loss can look up the saved submission_id in state.chat_turns"
  );
});
