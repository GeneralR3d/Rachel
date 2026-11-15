const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const html = fs.readFileSync(path.join(__dirname, "../app/static/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1].split("// --- tabs")[0];

function dashboard(fetch) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      disabled: false, textContent: "", innerHTML: "", style: {}, appendChild() {},
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    fetch, confirm: () => true, setTimeout() {},
    document: { getElementById: element, querySelectorAll: () => [], createElement: element },
  });
  vm.runInContext(script, context);
  const run = code => vm.runInContext(code, context);
  const click = id => {
    const button = html.match(new RegExp(`<button\\b[^>]*\\bid="${id}"[^>]*>`))?.[0];
    assert.ok(button, `Missing button ${id}`);
    return run(button.match(/onclick="([^"]+)"/)[1]);
  };
  return { run, click, element };
}

function historyResponse(content, hasMore) {
  const items = content ? [{ sender: "Jamie", content }] : [];
  return Response.json(items, { headers: { "X-Has-More": String(hasMore) } });
}

test("Older and Newer follow the page header and disable navigation while loading", async () => {
  const requests = [];
  let resolveOlder;
  const { run, click, element } = dashboard(url => {
    requests.push(url);
    if (url.endsWith("page=2")) return new Promise(resolve => { resolveOlder = resolve; });
    return historyResponse("Recent message", true);
  });
  await run("selectedChat = '-123'; loadHistory()");
  assert.equal(requests.at(-1), "/history/-123?page=1");
  assert.equal(element("historyNewer").disabled, true);
  assert.equal(element("historyOlder").disabled, false);

  const pending = click("historyOlder");
  assert.equal(requests.at(-1), "/history/-123?page=2");
  assert.equal(element("historyNewer").disabled, true);
  assert.equal(element("historyOlder").disabled, true);
  resolveOlder(historyResponse("Older message", false));
  await pending;
  assert.match(element("historyOut").innerHTML, /Older message/);
  assert.equal(element("historyNewer").disabled, false);
  assert.equal(element("historyOlder").disabled, true);

  await click("historyNewer");
  assert.equal(requests.at(-1), "/history/-123?page=1");
  assert.match(element("historyOut").innerHTML, /Recent message/);
});

test("switching chats starts at page one and ignores an old chat's pending page", async () => {
  let resolveOldChat;
  const requests = [];
  const { run, click, element } = dashboard(url => {
    requests.push(url);
    if (url.startsWith("/summary/")) return Response.json({ summary: null });
    if (url.endsWith("page=2")) return new Promise(resolve => { resolveOldChat = resolve; });
    if (url.startsWith("/history/-456")) return historyResponse("New chat message", false);
    return historyResponse("Old chat message", true);
  });
  await run("selectChat(-123)");
  const pending = click("historyOlder");
  await run("selectChat(-456)");
  assert.equal(requests.at(-1), "/history/-456?page=1");
  resolveOldChat(historyResponse("Stale history response", true));
  await pending;
  assert.match(element("historyOut").innerHTML, /New chat message/);
  assert.equal(element("historyNewer").disabled, true);
  assert.equal(element("historyOlder").disabled, true);
});

test("a failed page request leaves the current page visible and can be retried", async () => {
  let failOnce = true;
  const { run, click, element } = dashboard(url => {
    if (url.endsWith("page=3")) {
      if (failOnce) {
        failOnce = false;
        return new Response(null, { status: 503, statusText: "Service Unavailable" });
      }
      return historyResponse("Oldest message", false);
    }
    return historyResponse("Current page message", true);
  });
  await run("selectedChat = '-123'; loadHistory()");
  await click("historyOlder");
  await click("historyOlder");
  assert.equal(element("historyOut").className, "");
  assert.match(element("historyOut").innerHTML, /Current page message/);
  assert.equal(element("historyPageLabel").textContent, "Page 2");
  assert.equal(element("historyNewer").disabled, false);
  assert.equal(element("historyOlder").disabled, false);

  await click("historyOlder");
  assert.match(element("historyOut").innerHTML, /Oldest message/);
  assert.equal(element("historyPageLabel").textContent, "Page 3");
});

test("Reload and Clear history both reset the selected chat to page one", async () => {
  const requests = [];
  let cleared = false;
  const { run, click, element } = dashboard((url, { method }) => {
    requests.push([method, url]);
    if (method === "DELETE") {
      cleared = true;
      return new Response(null, { status: 204 });
    }
    return historyResponse(cleared ? null : "A message", !cleared);
  });
  await run("selectedChat = '-123'; loadHistory()");
  await click("historyOlder");
  await run("loadHistory()");
  assert.deepEqual(requests.at(-1), ["GET", "/history/-123?page=1"]);

  await click("historyOlder");
  await run("clearHistory()");
  assert.deepEqual(requests.slice(-2), [
    ["DELETE", "/history/-123"],
    ["GET", "/history/-123?page=1"],
  ]);
  assert.equal(element("historyOut").textContent, "(empty)");
  assert.equal(element("historyNewer").disabled, true);
  assert.equal(element("historyOlder").disabled, true);
});
