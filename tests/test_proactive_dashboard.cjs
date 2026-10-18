const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { test } = require("node:test");

// Exercise the actual zero-build dashboard functions without contacting Telegram.
const html = fs.readFileSync(require("node:path").join(__dirname, "../app/static/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const proactiveScript = script.slice(script.indexOf("// --- proactive outreach"), script.indexOf("// --- tabs"));

function dashboard(api) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: "", textContent: "", innerHTML: "", disabled: false,
      dataset: {}, style: {}, classList: { toggle() {}, remove() {} },
      appendChild() {}, setAttribute() {},
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    api, CHATS: [], toast() {}, esc: String,
    document: {
      getElementById: element, querySelector: element,
      querySelectorAll: () => [], createElement: element,
    },
  });
  vm.runInContext(proactiveScript, context);
  vm.runInContext("selectedProactiveSchedule = 1", context);
  return { context, element };
}

const exhausted = {
  id: 1, name: "Check-in", chat_id: 42, cron_expression: "0 9 * * *",
  timezone: "Asia/Singapore", intent: "Check in", enabled: true,
  repeat_times: 1, repeat_completed: 1, last_status: "succeeded",
  next_run_at: null, last_run_at: null, last_error: null,
};

test("final Run now refresh keeps exhausted job disabled and shows Done", async () => {
  const { context, element } = dashboard(async path => path.endsWith("/run") ? exhausted : [exhausted]);
  await vm.runInContext("runProactiveSchedule()", context);
  assert.equal(element("proactiveRunBtn").disabled, true);
  assert.match(element("proactiveMeta").textContent, /Done/);
  assert.equal(element("proactiveRepeatCompleted").value, 1);
});

test("Run now conflict refreshes exhausted state", async () => {
  const { context, element } = dashboard(async path => {
    if (path.endsWith("/run")) throw new Error("repeat limit reached");
    return [exhausted];
  });
  await vm.runInContext("runProactiveSchedule()", context);
  assert.equal(element("proactiveRunBtn").disabled, true);
});

test("clearing limit sends null and never submits the consumed counter", () => {
  const { context, element } = dashboard();
  element("proactiveRepeatCompleted").value = 7;
  element("proactiveRepeatTimes").value = "";
  assert.equal(vm.runInContext("proactiveFormBody().repeat_times", context), null);
  assert.equal(vm.runInContext("'repeat_completed' in proactiveFormBody()", context), false);
  element("proactiveRepeatTimes").value = "3";
  assert.equal(vm.runInContext("proactiveFormBody().repeat_times", context), 3);
});

test("unlimited schedules remain runnable after many attempts", () => {
  const { context, element } = dashboard();
  context.row = { ...exhausted, repeat_times: null, repeat_completed: 50 };
  vm.runInContext("renderProactiveSchedule(row)", context);
  assert.equal(element("proactiveRunBtn").disabled, false);
  assert.equal(element("proactiveRepeatTimes").value, "");
  assert.match(element("proactiveMeta").textContent, /50 \/ unlimited/);
});
