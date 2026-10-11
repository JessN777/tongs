// Read handlers resolve with a { result, error } envelope instead of
// rejecting, because Electron logs every rejected handler as an error and a
// cancelled read is expected. The preload turns the envelope back into the
// encoded failure the renderer already decodes. These tests run the compiled
// main-process controller and the compiled preload against a fake Electron and
// the scripted sidecar, so a read crosses both boundaries as in the app.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { register } from "node:module";
import test from "node:test";

import {
  decodeReadFailure,
  encodeReadFailure,
} from "../../../desktop/dist/src/shared/bridge.js";
import { serviceErrorOf } from "../../../desktop/dist/src/renderer/core/presentation.js";
import { harness, transportFor } from "./fake-sidecar.mjs";

// The main-process controller imports `ipcMain` and `shell` from Electron,
// which only exist inside Electron. Resolve that one specifier to a module
// that hands out the fakes below; every other import resolves as usual.
const fakeElectron = {
  handlers: new Map(),
  ipcMain: {
    handle(channel, handler) {
      assert.equal(fakeElectron.handlers.has(channel), false, `${channel} registered twice`);
      fakeElectron.handlers.set(channel, handler);
    },
    removeHandler(channel) {
      fakeElectron.handlers.delete(channel);
    },
  },
  shell: { openExternal: async () => undefined },
};
globalThis.__tongsReadEnvelopeElectron = fakeElectron;
const electronModule = `data:text/javascript,${encodeURIComponent(
  "const fake = globalThis.__tongsReadEnvelopeElectron;" +
    "export const ipcMain = fake.ipcMain; export const shell = fake.shell;",
)}`;
register(
  `data:text/javascript,${encodeURIComponent(
    `export async function resolve(specifier, context, next) {
      return specifier === "electron"
        ? { url: ${JSON.stringify(electronModule)}, shortCircuit: true }
        : next(specifier, context);
    }`,
  )}`,
);
const { DesktopIpcController } = await import("../../../desktop/dist/src/main/ipc.js");
const { APP_DOCUMENT } = await import("../../../desktop/dist/src/main/security.js");

const preloadSource = readFileSync(
  new URL("../../../desktop/dist/src/preload/index.cjs", import.meta.url),
  "utf8",
);

const CANCELLED = Object.freeze({
  code: "request_cancelled",
  message: "The request was cancelled.",
  retryable: false,
});
const INVALID_ENVELOPE = Object.freeze({
  code: "invalid_response",
  message: "The desktop read returned an invalid response.",
  retryable: false,
});

function appWindow() {
  const mainFrame = { url: APP_DOCUMENT };
  const webContents = { id: 7, mainFrame, send: () => undefined };
  return {
    window: { webContents, isDestroyed: () => false },
    event: { sender: webContents, senderFrame: mainFrame },
  };
}

/** Evaluate the compiled preload with a fake Electron and return its bridge. */
function loadPreload(ipcRenderer) {
  let exposed = null;
  const electron = {
    contextBridge: {
      exposeInMainWorld(name, api) {
        assert.equal(name, "tongs");
        exposed = api;
      },
    },
    ipcRenderer,
  };
  const module = { exports: {} };
  const requireElectron = (specifier) => {
    assert.equal(specifier, "electron");
    return electron;
  };
  new Function("require", "exports", "module", preloadSource)(
    requireElectron,
    module.exports,
    module,
  );
  assert.ok(exposed, "the preload exposes its bridge");
  return exposed;
}

/**
 * Start the controller on a scripted sidecar and a preload whose invoke calls
 * the registered handler the way Electron does: arguments and results are
 * cloned, and a rejected handler reaches the caller as a fresh Error whose
 * message names the channel. `settled` records how each handler finished.
 */
async function startApp({ assets = {} } = {}) {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const { window, event } = appWindow();
  const controller = new DesktopIpcController(window, transport, assets, {}, null);
  controller.register();
  const settled = [];
  const bridge = loadPreload({
    async invoke(channel, ...args) {
      const handler = fakeElectron.handlers.get(channel);
      assert.ok(handler, `${channel} has a handler`);
      const outcome = Promise.resolve().then(() =>
        handler(event, ...structuredClone(args)),
      );
      try {
        const value = structuredClone(await outcome);
        settled.push({ channel, outcome: "resolved", value });
        return value;
      } catch (error) {
        settled.push({ channel, outcome: "rejected" });
        throw new Error(`Error invoking remote method '${channel}': ${error}`);
      }
    },
  });
  return {
    bridge,
    child: fake.children[0],
    settled,
    async stop() {
      controller.dispose();
      await transport.stop();
    },
  };
}

async function requestFrame(child, method) {
  for (let attempt = 0; attempt < 400; attempt += 1) {
    const frame = child.frames.find(
      (item) => item.type === "request" && item.method === method,
    );
    if (frame) return frame;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error(`the controller never sent ${method}`);
}

async function cancelFrame(child, id) {
  for (let attempt = 0; attempt < 400; attempt += 1) {
    if (child.frames.some((item) => item.type === "cancel" && item.id === id)) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error(`the controller never cancelled ${id}`);
}

function respond(child, id, body) {
  child.stdout.write(`${JSON.stringify({ v: 1, type: "response", id, ...body })}\n`);
}

async function rejectionOf(promise) {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  assert.fail("the read resolved");
}

test("a cancelled read resolves its handler and reaches the renderer as before", async () => {
  const app = await startApp();
  try {
    const read = app.bridge.getReviewCIStatus({ review: "review-handle-1" });
    const frame = await requestFrame(app.child, "reviews.ci_status");
    assert.equal(await app.bridge.cancelRead(read.requestToken), true);
    await cancelFrame(app.child, frame.id);
    respond(app.child, frame.id, { error: { ...CANCELLED } });

    const error = await rejectionOf(read.result);
    assert.equal(error instanceof Error, true);
    assert.deepEqual({ ...serviceErrorOf(error) }, { ...CANCELLED });
    // The renderer sees the same encoded failure the handler used to throw.
    assert.equal(error.message, encodeReadFailure(CANCELLED).message);

    const reads = app.settled.filter((item) => item.channel === "tongs:reviews.ci-status");
    assert.equal(reads.length, 1);
    assert.equal(reads[0].outcome, "resolved");
    assert.deepEqual(reads[0].value, { result: null, error: { ...CANCELLED } });
    assert.equal(app.settled.filter((item) => item.outcome === "rejected").length, 0);
    assert.equal(await app.bridge.cancelRead(read.requestToken), false);
  } finally {
    await app.stop();
  }
});

test("a successful read resolves with its result alone", async () => {
  const app = await startApp();
  try {
    const read = app.bridge.getReviewCIStatus({ review: "review-handle-2" });
    const frame = await requestFrame(app.child, "reviews.ci_status");
    respond(app.child, frame.id, { result: { ci_status: "success", retry_after_seconds: null } });
    assert.deepEqual(await read.result, { ci_status: "success", retry_after_seconds: null });
    assert.deepEqual(app.settled.map((item) => item.outcome), ["resolved"]);
    assert.deepEqual(app.settled[0].value, {
      result: { ci_status: "success", retry_after_seconds: null },
      error: null,
    });
  } finally {
    await app.stop();
  }
});

test("real failures keep their code through each read path", async () => {
  const app = await startApp();
  try {
    const cases = [
      {
        start: () => app.bridge.getReviewCIStatus({ review: "review-handle-3" }),
        method: "reviews.ci_status",
        error: { code: "not_found", message: "The review no longer exists.", retryable: false },
      },
      {
        start: () => app.bridge.getCICapabilities("repository-handle-1"),
        method: "ci.capabilities",
        error: { code: "rate_limited", message: "The forge is rate limiting requests.", retryable: true },
      },
      {
        start: () => app.bridge.getReviewActionCapabilities("review-handle-4"),
        method: "review_actions.capabilities",
        error: { code: "authentication_failed", message: "The forge rejected the token.", retryable: false },
      },
    ];
    for (const { start, method, error } of cases) {
      const read = start();
      const frame = await requestFrame(app.child, method);
      respond(app.child, frame.id, { error });
      const failure = await rejectionOf(read.result);
      assert.deepEqual({ ...serviceErrorOf(failure) }, error, method);
      assert.equal(failure.message, encodeReadFailure(error).message, method);
    }
    assert.equal(app.settled.length, cases.length);
    assert.equal(app.settled.every((item) => item.outcome === "resolved"), true);
  } finally {
    await app.stop();
  }
});

test("a result that breaks the contract still names the broken field", async () => {
  const app = await startApp();
  try {
    const read = app.bridge.getReviewCIStatus({ review: "review-handle-5" });
    const frame = await requestFrame(app.child, "reviews.ci_status");
    respond(app.child, frame.id, { result: { ci_status: "bogus", retry_after_seconds: null } });
    const failure = serviceErrorOf(await rejectionOf(read.result));
    assert.equal(failure.code, "invalid_response");
    assert.equal(
      failure.message,
      'Invalid reviews.ci_status result: field "ci_status" must be a known CI status',
    );
    assert.deepEqual(app.settled.map((item) => item.outcome), ["resolved"]);
  } finally {
    await app.stop();
  }
});

test("invalid read parameters resolve with an invalid_response envelope", async () => {
  const app = await startApp();
  try {
    const read = app.bridge.getReviewCIStatus({ review: "" });
    const failure = serviceErrorOf(await rejectionOf(read.result));
    assert.equal(failure.code, "invalid_response");
    assert.equal(failure.message, "Invalid desktop text parameter");
    assert.deepEqual(app.settled.map((item) => item.outcome), ["resolved"]);
  } finally {
    await app.stop();
  }
});

test("a cancelled asset listing resolves its handler too", async () => {
  const assets = {
    beginRefresh: () => ({
      requestId: "assets-1",
      result: Promise.reject(Object.assign(new Error(CANCELLED.message), CANCELLED)),
    }),
  };
  const app = await startApp({ assets });
  try {
    const failure = serviceErrorOf(await rejectionOf(app.bridge.listAssets().result));
    assert.deepEqual({ ...failure }, { ...CANCELLED });
    assert.deepEqual(app.settled.map((item) => item.outcome), ["resolved"]);
  } finally {
    await app.stop();
  }
});

test("a read from an unauthorized sender still rejects", async () => {
  const app = await startApp();
  try {
    const handler = fakeElectron.handlers.get("tongs:reviews.ci-status");
    const foreignFrame = { url: "https://example.invalid/" };
    const foreign = { sender: { id: 9, mainFrame: foreignFrame }, senderFrame: foreignFrame };
    await assert.rejects(
      handler(foreign, { requestToken: "0123456789abcdef", params: { review: "review-handle-6" } }),
      /Unauthorized desktop IPC sender/,
    );
    const assetHandler = fakeElectron.handlers.get("tongs:assets.list");
    await assert.rejects(
      assetHandler(foreign, { requestToken: "0123456789abcdef", params: {} }),
      /Unauthorized desktop IPC sender/,
    );
    assert.equal(app.child.frames.some((item) => item.type === "request" && item.method === "reviews.ci_status"), false);
  } finally {
    await app.stop();
  }
});

/** A preload whose every invoke resolves with `value`, as a broken main would. */
function preloadAnswering(value) {
  return loadPreload({ invoke: async () => structuredClone(value) });
}

test("the preload accepts exactly the two envelope shapes", async () => {
  const longest = "x".repeat(240);
  const bridge = preloadAnswering({
    result: null,
    error: { code: "snapshot_expired", message: longest, retryable: true },
  });
  const failure = await rejectionOf(bridge.pageDiff({ snapshot: "s", resource: "r", cursor: 0 }).result);
  assert.deepEqual(
    { ...decodeReadFailure(failure) },
    { code: "snapshot_expired", message: longest, retryable: true },
  );
  assert.deepEqual(
    await preloadAnswering({ result: { commits: [] }, error: null }).listCommits("review-1").result,
    { commits: [] },
  );
  assert.equal(await preloadAnswering({ result: null, error: null }).listCommits("review-1").result, null);
});

test("the preload rejects a malformed envelope as invalid_response", async () => {
  const valid = { code: "not_found", message: "Missing.", retryable: false };
  const malformed = [
    ["no envelope", undefined],
    ["null", null],
    ["text", "tongs-read-failure:%7B%7D"],
    ["array", [null, null]],
    ["raw result", { commits: [] }],
    ["missing error", { result: { commits: [] } }],
    ["missing result", { error: null }],
    ["extra key", { result: 1, error: null, extra: true }],
    ["result beside error", { result: { commits: [] }, error: valid }],
    ["error text", { result: null, error: "not_found" }],
    ["error array", { result: null, error: ["not_found", "Missing.", false] }],
    ["error extra key", { result: null, error: { ...valid, details: {} } }],
    ["error missing key", { result: null, error: { code: "not_found", message: "Missing." } }],
    ["uppercase code", { result: null, error: { ...valid, code: "Not_Found" } }],
    ["empty code", { result: null, error: { ...valid, code: "" } }],
    ["long code", { result: null, error: { ...valid, code: `a${"b".repeat(64)}` } }],
    ["code number", { result: null, error: { ...valid, code: 404 } }],
    ["empty message", { result: null, error: { ...valid, message: "" } }],
    ["long message", { result: null, error: { ...valid, message: "x".repeat(241) } }],
    ["message number", { result: null, error: { ...valid, message: 1 } }],
    ["retryable text", { result: null, error: { ...valid, retryable: "false" } }],
  ];
  for (const [label, value] of malformed) {
    const failure = await rejectionOf(preloadAnswering(value).listCommits("review-1").result);
    assert.deepEqual({ ...decodeReadFailure(failure) }, { ...INVALID_ENVELOPE }, label);
  }
});

test("the preload releases the request token once a read settles", async () => {
  const cancels = [];
  const bridge = loadPreload({
    async invoke(channel, value) {
      if (channel !== "tongs:read.cancel") return { result: null, error: { ...CANCELLED } };
      cancels.push(value);
      return true;
    },
  });
  const read = bridge.listCommits("review-1");
  assert.equal(await bridge.cancelRead(read.requestToken), true);
  await rejectionOf(read.result);
  assert.equal(await bridge.cancelRead(read.requestToken), false);
  assert.deepEqual(cancels, [read.requestToken]);
});
