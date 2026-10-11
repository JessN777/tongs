import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import {
  configurationFailureMessage,
  MAX_CONFIGURATION_MESSAGE,
  MAX_JSON_DEPTH,
  MAX_JSON_ITEMS,
  MAX_RESPONSE_BYTES,
  MIN_JSON_ITEMS,
  SidecarError,
  SidecarTransport,
} from "../../../desktop/dist/src/main/sidecar.js";
import {
  ServiceRecoveryPolicy,
  watchServiceLoss,
} from "../../../desktop/dist/src/main/renderer_lifecycle.js";
import { harness, launch, transportFor } from "./fake-sidecar.mjs";

test("trusted launch binds the sidecar to the exact editor export root", async () => {
  const fake = harness();
  const variable = "TONGS_DESKTOP_EDITOR_EXPORT_ROOT";
  const original = process.env[variable];
  process.env[variable] = "/tmp/renderer-selected-root";
  const trustedRoot = path.join(process.cwd(), ".trusted-editor-exports");
  const transport = new SidecarTransport(
    { ...launch, utilityExportRoot: trustedRoot },
    1_000,
    1_000,
    1_000,
    fake.spawn,
  );
  try {
    await transport.start();
    assert.equal(fake.children[0].spawnOptions.env[variable], trustedRoot);
  } finally {
    await transport.stop();
    if (original === undefined) delete process.env[variable];
    else process.env[variable] = original;
  }
});

test("launch without utility authority strips an inherited export root", async () => {
  const fake = harness();
  const variable = "TONGS_DESKTOP_EDITOR_EXPORT_ROOT";
  const original = process.env[variable];
  process.env[variable] = "/tmp/untrusted-editor-exports";
  const transport = transportFor(fake);
  try {
    await transport.start();
    assert.equal(fake.children[0].spawnOptions.env[variable], undefined);
  } finally {
    await transport.stop();
    if (original === undefined) delete process.env[variable];
    else process.env[variable] = original;
  }
});

test("unexpected EOF fails every pending read and records one crash", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const pending = transport.requestRead("assets.list", {}).result;
  fake.children[0].finish(7, null);
  await assert.rejects(pending, { code: "unexpected_eof" });
  assert.equal(transport.crashHistory.length, 1);
});

test("oversized unterminated output terminates the connection", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const pending = transport.requestRead("assets.list", {}).result;
  fake.children[0].stdout.write(Buffer.alloc(MAX_RESPONSE_BYTES + 1, 0x20));
  await assert.rejects(pending, { code: "frame_too_large" });
});

test("bounded shutdown rejects pending reads and exits cleanly", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const pending = transport.requestRead("assets.list", {}).result;
  const stopped = transport.stop();
  await assert.rejects(pending, { code: "shutting_down" });
  await stopped;
  assert.equal(transport.crashHistory.at(-1).unexpected, false);
});

test("concurrent starts share the incomplete handshake", async () => {
  const fake = harness({ autoHandshake: false });
  const transport = transportFor(fake);
  let secondReady = false;
  const first = transport.start();
  const second = transport.start().then(() => {
    secondReady = true;
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(fake.children.length, 1);
  assert.equal(secondReady, false);
  fake.respond(fake.children[0]);
  await Promise.all([first, second]);
  await transport.stop();
});

test("stop during startup settles before any replacement", async () => {
  const fake = harness({ autoHandshake: false });
  const transport = transportFor(fake);
  const starting = transport.start();
  await new Promise((resolve) => setImmediate(resolve));
  const stopping = transport.stop();
  await assert.rejects(starting, { code: "shutting_down" });
  await stopping;
  assert.equal(fake.children.length, 1);
  assert.equal(transport.processId, undefined);
});

test("stale child events cannot corrupt a replacement generation", async () => {
  const fake = harness({ delayedKill: true });
  const transport = transportFor(fake, 20);
  await transport.start();
  const old = fake.children[0];
  const pending = transport.requestRead("assets.list", {}).result;
  old.stdout.write("not-json\n");
  await assert.rejects(pending, { code: "invalid_frame" });
  const replacement = transport.start();
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(fake.children.length, 1);
  old.finish(9, "SIGTERM");
  await replacement;
  const current = fake.children[1];
  assert.equal(transport.processId, current.pid);
  old.stdout.write("garbage\n");
  old.emit("exit", 9, "SIGTERM");
  assert.equal(transport.processId, current.pid);
  assert.equal(transport.sessionGeneration, 2);
  current.finish(0, null);
});

// Node can deliver a child's pipe data after its exit event, so the old
// child's stdout can still speak once a restart replaced it.
test("a replaced child's late response never answers the new session's request", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  const crashes = watchCrashes(transport);
  await transport.start();
  await transport.restart();
  const [old, current] = fake.children;
  assert.equal(old.exitCode, 0);
  const read = transport.requestRead("assets.list", {});
  let settled = false;
  read.result.then(
    () => {
      settled = true;
    },
    () => {
      settled = true;
    },
  );

  respondTo(old, read.requestId, { result: { from: "replaced child" } });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(settled, false);
  respondTo(current, read.requestId, { result: { from: "current child" } });

  assert.deepEqual(await read.result, { from: "current child" });
  assert.equal(transport.serviceState, "connected");
  assert.deepEqual(crashes, []);
  await transport.stop();
});

test("a replaced child's late partial frame stays out of the new frame buffer", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  const crashes = watchCrashes(transport);
  await transport.start();
  await transport.restart();
  const [old, current] = fake.children;
  const read = transport.requestRead("assets.list", {});

  // Unterminated, so a transport that kept it would join it to the front of
  // the current child's next frame and fail that frame as invalid.
  old.stdout.write('{"v":1,"type":"response","id":"stale-');
  await new Promise((resolve) => setImmediate(resolve));
  respondTo(current, read.requestId, { result: { assets: [] } });

  assert.deepEqual(await read.result, { assets: [] });
  assert.equal(transport.serviceState, "connected");
  assert.equal(transport.sessionGeneration, 2);
  assert.deepEqual(crashes, []);
  await transport.stop();
});

test("terminal stop rejects a concurrent start without replacement", async () => {
  const fake = harness({ delayedKill: true, delayedShutdown: true });
  const transport = transportFor(fake, 20);
  await transport.start();
  const old = fake.children[0];
  const stopping = transport.stop();
  const starting = assert.rejects(transport.start(), { code: "shutting_down" });
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(fake.children.length, 1);
  old.finish(0, null);
  await stopping;
  await starting;
  assert.equal(fake.children.length, 1);
  assert.equal(transport.processId, undefined);
});

test("terminal stop prevents an in-progress restart from resurrecting", async () => {
  const fake = harness({ delayedKill: true, delayedShutdown: true });
  const transport = transportFor(fake, 20);
  await transport.start();
  const old = fake.children[0];
  const restarting = transport.restart();
  const stopping = transport.stop();
  old.finish(0, null);
  await stopping;
  await assert.rejects(restarting, { code: "shutting_down" });
  assert.equal(fake.children.length, 1);
  assert.equal(transport.processId, undefined);
});

test("handshake accepts server capability supersets", async () => {
  const fake = harness({ extraCapabilities: ["split_diffs"] });
  const transport = transportFor(fake);
  await transport.start();
  assert.equal(transport.sessionGeneration, 1);
  await transport.stop();
});

test("a handshake timeout says the service did not start, not that a CI action is unknown", async () => {
  const fake = harness({ autoHandshake: false });
  // The request timeout is long, so only the startup deadline can expire.
  const transport = new SidecarTransport(launch, 60_000, 20, 1_000, fake.spawn);
  const states = [];
  transport.on("status", (state) => states.push(state));
  let failure = null;
  try {
    await transport.start();
  } catch (error) {
    failure = {
      code: error.code,
      message: error.message,
      retryable: error.retryable,
    };
  }
  assert.deepEqual(failure, {
    code: "startup_timeout",
    message: "The desktop service did not start in time.",
    retryable: true,
  });
  assert.equal(/\bCI\b/.test(failure.message), false);
  assert.equal(failure.message.includes("outcome"), false);
  assert.equal(transport.serviceState, "stopped");
  assert.deepEqual(states, []);
  assert.equal(
    fake.children[0].frames.filter((frame) => frame.method === "handshake").length,
    1,
  );
  assert.equal(
    fake.children[0].frames.filter((frame) => frame.type === "cancel").length,
    0,
  );
});

test("mutation timeout is uncertain and never sends cancel or replays a late result", async () => {
  const fake = harness();
  const transport = new SidecarTransport(launch, 20, 1_000, 1_000, fake.spawn);
  await transport.start();
  const request = transport.requestMutation("pipelines.retry", {
    operation_id: "timeout-operation",
    pipeline: "pipeline-handle",
  });

  await assert.rejects(request.result, {
    code: "mutation_timeout",
    message: "The CI mutation response timed out. Its outcome is unknown.",
    retryable: true,
  });
  assert.equal(
    fake.children[0].frames.filter((frame) => frame.type === "cancel").length,
    0,
  );
  assert.equal(
    fake.children[0].frames.filter((frame) => frame.method === "pipelines.retry").length,
    1,
  );

  fake.children[0].stdout.write(
    `${JSON.stringify({
      v: 1,
      type: "response",
      id: request.requestId,
      result: {
        operation_id: "timeout-operation",
        action: "retry_pipeline",
        outcome: "known",
        error: null,
        resync_required: false,
      },
    })}\n`,
  );
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(transport.crashHistory.length, 0);
  assert.equal(
    fake.children[0].frames.filter((frame) => frame.method === "pipelines.retry").length,
    1,
  );
  await transport.stop();
});

test("mutation cannot be cancelled through the read cancellation channel", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const request = transport.requestMutation("jobs.cancel", {
    operation_id: "cancel-channel-operation",
    pipeline: "pipeline-handle",
    job: "job-handle",
  });

  assert.equal(transport.cancelRead(request.requestId), false);
  assert.equal(
    fake.children[0].frames.filter((frame) => frame.type === "cancel").length,
    0,
  );
  fake.children[0].finish(8, null);
  await assert.rejects(request.result, { code: "unexpected_eof" });
});

test("mutation responses preserve validated service error classifications", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const mutation = transport.requestMutation("pipelines.retry", {
    operation_id: "known-rejection-operation",
    pipeline: "pipeline-handle",
  });
  fake.children[0].stdout.write(
    `${JSON.stringify({
      v: 1,
      type: "response",
      id: mutation.requestId,
      error: {
        code: "service_error",
        message: "safe server message",
        retryable: false,
        details: { service_code: "not_found" },
      },
    })}\n`,
  );
  await assert.rejects(mutation.result, { code: "not_found" });

  const read = transport.requestRead("pipelines.list", {});
  fake.children[0].stdout.write(
    `${JSON.stringify({
      v: 1,
      type: "response",
      id: read.requestId,
      error: {
        code: "service_error",
        message: "safe server message",
        retryable: false,
        details: { service_code: "not_found" },
      },
    })}\n`,
  );
  await assert.rejects(read.result, { code: "service_error" });
  await transport.stop();
});

test("restart rejects an in-flight mutation and never replays it in the new session", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const result = transport.requestMutation("pipelines.cancel", {
    operation_id: "restart-operation",
    pipeline: "pipeline-handle",
  }).result;

  await transport.restart();
  await assert.rejects(result, { code: "shutting_down" });
  assert.equal(
    fake.children[0].frames.filter((frame) => frame.method === "pipelines.cancel").length,
    1,
  );
  assert.equal(
    fake.children[1].frames.filter((frame) => frame.method === "pipelines.cancel").length,
    0,
  );
  assert.equal(transport.sessionGeneration, 2);
  await transport.stop();
});

function respondTo(child, id, body) {
  child.stdout.write(`${JSON.stringify({ v: 1, type: "response", id, ...body })}\n`);
}

function nested(depth) {
  let value = "leaf";
  for (let level = 0; level < depth; level += 1) value = [value];
  return value;
}

function watchCrashes(transport) {
  const crashes = [];
  transport.on("crash", (error) => crashes.push(error.code));
  return crashes;
}

test("handshake declares the JSON value and depth limits", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  assert.deepEqual(fake.children[0].handshake.params.limits, {
    json_values: MAX_JSON_ITEMS,
    json_depth: MAX_JSON_DEPTH,
  });
  assert.deepEqual(transport.negotiatedJsonLimits, {
    values: MAX_JSON_ITEMS,
    depth: MAX_JSON_DEPTH,
  });
  await transport.stop();
});

test("over-budget response fails only its request and other reads survive", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const crashes = watchCrashes(transport);
  const threads = transport.requestRead("discussions.list", { review: "review-handle" });
  const commits = transport.requestRead("commits.list", { review: "review-handle" });
  const deep = transport.requestRead("jobs.list", { pipeline: "pipeline-handle" });
  const child = fake.children[0];

  respondTo(child, threads.requestId, {
    result: { discussions: new Array(MAX_JSON_ITEMS).fill(0) },
  });
  await assert.rejects(threads.result, {
    code: "response_too_large",
    message: /too large to show/,
    retryable: false,
  });
  respondTo(child, deep.requestId, { result: nested(MAX_JSON_DEPTH) });
  await assert.rejects(deep.result, { code: "response_too_large" });
  respondTo(child, commits.requestId, { result: { commits: [] } });
  assert.deepEqual(await commits.result, { commits: [] });

  const later = transport.requestRead("reviews.list", {});
  respondTo(child, later.requestId, { result: { items: [], failures: [] } });
  assert.deepEqual(await later.result, { items: [], failures: [] });
  assert.equal(crashes.length, 0);
  assert.equal(transport.crashHistory.length, 0);
  assert.equal(fake.children.length, 1);
  assert.equal(child.killed, false);
  await transport.stop();
});

test("over-budget mutation response rejects as outcome unknown", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const crashes = watchCrashes(transport);
  const mutation = transport.requestMutation("pipelines.retry", {
    operation_id: "oversized-operation",
    pipeline: "pipeline-handle",
  });
  respondTo(fake.children[0], mutation.requestId, {
    result: { receipts: new Array(MAX_JSON_ITEMS).fill(0) },
  });
  const failure = await mutation.result.then(
    () => null,
    (error) => ({ code: error.code, message: error.message, retryable: error.retryable }),
  );
  assert.equal(failure?.code, "invalid_response");
  assert.equal(failure?.retryable, false);
  assert.match(String(failure?.message), /outcome is unknown/);
  assert.match(String(failure?.message), /Refresh before retrying/);
  assert.equal(crashes.length, 0);
  await transport.stop();
});

test("response exactly at the value budget is delivered", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const read = transport.requestRead("commits.list", { review: "review-handle" });
  // The envelope holds the object, v, type, id, and the result list itself.
  const items = new Array(MAX_JSON_ITEMS - 5).fill(0);
  respondTo(fake.children[0], read.requestId, { result: items });
  assert.equal((await read.result).length, items.length);
  await transport.stop();
});

test("over-budget response for an unknown request is dropped without a crash", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const crashes = watchCrashes(transport);
  respondTo(fake.children[0], "electron-1-999", {
    result: new Array(MAX_JSON_ITEMS).fill(0),
  });
  const read = transport.requestRead("reviews.list", {});
  respondTo(fake.children[0], read.requestId, { result: { items: [] } });
  assert.deepEqual(await read.result, { items: [] });
  assert.equal(crashes.length, 0);
  await transport.stop();
});

test("over-budget frame without a request id still fails the connection", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const crashes = watchCrashes(transport);
  const pending = transport.requestRead("reviews.list", {});
  fake.children[0].stdout.write(
    `${JSON.stringify({ v: 1, type: "event", sequence: 1, event: "x", data: nested(MAX_JSON_DEPTH) })}\n`,
  );
  await assert.rejects(pending.result, { code: "invalid_frame" });
  assert.deepEqual(crashes, ["invalid_frame"]);
});

test("unparseable frame still fails every pending request", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const crashes = watchCrashes(transport);
  const first = transport.requestRead("reviews.list", {});
  const second = transport.requestRead("commits.list", { review: "review-handle" });
  fake.children[0].stdout.write(`{"v":1,"type":"response","id":"${first.requestId}"\n`);
  await assert.rejects(first.result, { code: "invalid_frame" });
  await assert.rejects(second.result, { code: "invalid_frame" });
  assert.deepEqual(crashes, ["invalid_frame"]);
});

test("negotiated tighter limits apply to responses and requests", async () => {
  const fake = harness({ limitOverrides: { json_values: MIN_JSON_ITEMS, json_depth: 12 } });
  const transport = transportFor(fake);
  await transport.start();
  assert.deepEqual(transport.negotiatedJsonLimits, { values: MIN_JSON_ITEMS, depth: 12 });
  const crashes = watchCrashes(transport);
  const read = transport.requestRead("commits.list", { review: "review-handle" });
  respondTo(fake.children[0], read.requestId, { result: new Array(MIN_JSON_ITEMS).fill(0) });
  await assert.rejects(read.result, { code: "response_too_large" });
  assert.throws(() => transport.requestRead("reviews.list", { deep: nested(12) }));
  assert.equal(crashes.length, 0);
  await transport.stop();
});

for (const [name, overrides] of [
  ["missing value limit", { json_values: undefined }],
  ["missing depth limit", { json_depth: undefined }],
  ["looser value limit", { json_values: MAX_JSON_ITEMS + 1 }],
  ["looser depth limit", { json_depth: MAX_JSON_DEPTH + 1 }],
  ["value limit below the floor", { json_values: MIN_JSON_ITEMS - 1 }],
  ["fractional depth limit", { json_depth: 12.5 }],
  ["textual value limit", { json_values: "20000" }],
]) {
  test(`handshake with a ${name} fails closed`, async () => {
    const fake = harness({ limitOverrides: overrides });
    const transport = transportFor(fake);
    await assert.rejects(transport.start(), {
      code: "incompatible_handshake",
      message: /JSON limits are incompatible/,
    });
  });
}

/** Waits a bounded number of turns for a condition the test drives. */
async function until(condition, label) {
  for (let turn = 0; turn < 200; turn += 1) {
    if (condition()) return;
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
  assert.fail(`timed out waiting for ${label}`);
}

test("a failed first handshake does not stop a later restart", async () => {
  const fake = harness({ autoHandshake: false });
  const transport = new SidecarTransport(launch, 60_000, 20, 1_000, fake.spawn);
  await assert.rejects(transport.start(), { code: "startup_timeout" });
  assert.equal(transport.serviceState, "stopped");
  const restarting = transport.restart();
  await until(() => fake.children[1]?.handshake !== undefined, "second handshake");
  fake.respond(fake.children[1]);
  await restarting;
  assert.equal(transport.serviceState, "connected");
  assert.equal(transport.sessionGeneration, 2);
  assert.equal(transport.processId, fake.children[1].pid);
  await transport.stop();
  await assert.rejects(transport.start(), { code: "shutting_down" });
});

test("a rejected first handshake does not stop a later start", async () => {
  const extras = { recovery_warnings: "not a list" };
  const fake = harness({ handshakeExtras: extras });
  const transport = transportFor(fake);
  await assert.rejects(transport.start(), { code: "incompatible_handshake" });
  delete extras.recovery_warnings;
  await transport.start();
  assert.equal(transport.serviceState, "connected");
  assert.equal(fake.children.length, 2);
  await transport.stop();
});

/** Wires a transport to the recovery policy the way the desktop app does. */
function recovering(transport, { now = () => 0 } = {}) {
  const states = [];
  const policy = new ServiceRecoveryPolicy({
    restart: async () => {
      try {
        await transport.restart();
      } catch {
        // The renderer recovery path reports a failed restart as a result.
      }
      return transport.serviceState === "connected";
    },
    shuttingDown: () => false,
    onState: (state) => {
      states.push(state);
      transport.setRecovery(state);
    },
    now,
  });
  const detach = watchServiceLoss(transport, policy);
  return { policy, states, detach };
}

test("a crashed service is restarted and a retried read succeeds", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const { policy, states, detach } = recovering(transport);
  const first = transport.requestRead("repositories.discover", {}).result;
  fake.children[0].finish(9, null);
  await assert.rejects(first, { code: "unexpected_eof" });
  await until(() => transport.serviceState === "connected", "restart");
  await until(() => policy.state === "idle", "recovery to finish");
  assert.deepEqual(states, ["restarting", "idle"]);
  assert.equal(transport.serviceRecovery, "idle");
  const retry = transport.requestRead("repositories.discover", {});
  respondTo(fake.children[1], retry.requestId, { result: { repositories: [] } });
  assert.deepEqual(await retry.result, { repositories: [] });
  detach();
  await transport.stop();
});

test("a read that finds no service restarts it once, and the retried read succeeds", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const pending = transport.requestRead("repositories.discover", {}).result;
  fake.children[0].finish(9, null);
  await assert.rejects(pending, { code: "unexpected_eof" });
  // Nothing watched the crash, so only the failed read can report it.
  const { policy, detach } = recovering(transport);
  assert.throws(() => transport.requestRead("repositories.discover", {}), {
    code: "not_running",
  });
  assert.throws(() => transport.requestRead("repositories.discover", {}), {
    code: "not_running",
  });
  await until(() => transport.serviceState === "connected", "restart");
  await until(() => policy.state === "idle", "recovery to finish");
  assert.equal(policy.restartCount, 1);
  assert.equal(fake.children.length, 2);
  const retry = transport.requestRead("repositories.discover", {});
  respondTo(fake.children[1], retry.requestId, { result: { repositories: [] } });
  assert.deepEqual(await retry.result, { repositories: [] });
  detach();
  await transport.stop();
});

test("a read during shutdown or a deliberate stop does not ask for a restart", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  const lost = [];
  transport.on("not_running", () => lost.push("not_running"));
  await transport.start();
  await transport.restart();
  await transport.stop();
  assert.throws(() => transport.requestRead("repositories.discover", {}), {
    code: "not_running",
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(lost, []);
});

test("a read while a restart stops the old service does not ask for a restart", async () => {
  const fake = harness({ delayedShutdown: true });
  const transport = transportFor(fake, 1_000);
  const lost = [];
  transport.on("not_running", () => lost.push("not_running"));
  await transport.start();
  const old = fake.children[0];
  const restarting = transport.restart();
  await until(() => old.frames.some((frame) => frame.method === "shutdown"), "shutdown request");
  const shutdown = old.frames.find((frame) => frame.method === "shutdown");
  // The old service accepts the shutdown but has not exited yet.
  respondTo(old, shutdown.id, { result: {} });
  await until(() => old.stdin.writableEnded, "the old service's input to close");
  assert.equal(fake.children.length, 1);
  assert.throws(() => transport.requestRead("repositories.discover", {}), {
    code: "not_running",
  });
  old.finish(0, null);
  await restarting;
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(lost, []);
  assert.equal(transport.serviceState, "connected");
  const current = fake.children[1];
  const stopping = transport.stop();
  await until(() => current.frames.some((frame) => frame.method === "shutdown"), "final shutdown");
  const last = current.frames.find((frame) => frame.method === "shutdown");
  respondTo(current, last.id, { result: {} });
  current.finish(0, null);
  await stopping;
});

test("a read after a failed handshake does not ask for a restart", async () => {
  const fake = harness({ autoHandshake: false });
  const transport = new SidecarTransport(launch, 60_000, 20, 1_000, fake.spawn);
  const lost = [];
  transport.on("not_running", () => lost.push("not_running"));
  await assert.rejects(transport.start(), { code: "startup_timeout" });
  assert.throws(() => transport.requestRead("repositories.discover", {}), {
    code: "not_running",
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(lost, []);
  assert.equal(fake.children.length, 1);
  await transport.stop();
});

test("an invalid frame while a restart stops the old service does not spend a restart", async () => {
  const fake = harness({ delayedShutdown: true });
  const transport = transportFor(fake, 50);
  await transport.start();
  const { policy, detach } = recovering(transport);
  const crashes = watchCrashes(transport);
  const old = fake.children[0];
  // The old service answers the restart's shutdown request with garbage.
  old.stdin.on("data", (chunk) => {
    if (String(chunk).includes('"method":"shutdown"')) old.stdout.write("garbage\n");
  });
  await transport.restart();
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.deepEqual(crashes, []);
  assert.equal(policy.restartCount, 0);
  assert.equal(policy.state, "idle");
  assert.equal(fake.children.length, 2);
  assert.equal(transport.serviceState, "connected");
  detach();
  await transport.stop();
});

test("a service that keeps crashing is restarted three times, then left stopped", async () => {
  const fake = harness();
  // Every service after the first exits before its handshake completes.
  const spawn = (...args) => {
    const child = fake.spawn(...args);
    if (fake.children.length > 1) queueMicrotask(() => child.finish(1, null));
    return child;
  };
  const transport = new SidecarTransport(launch, 1_000, 1_000, 1_000, spawn);
  await transport.start();
  const { policy, states, detach } = recovering(transport);
  fake.children[0].finish(9, null);
  await until(() => policy.state === "failed", "the recovery to give up");
  assert.equal(policy.restartCount, 3);
  assert.equal(fake.children.length, 4);
  assert.equal(transport.serviceState, "stopped");
  assert.equal(transport.serviceRecovery, "failed");
  assert.deepEqual(states, ["restarting", "failed"]);
  // Later reads fail at once and never start another service.
  assert.throws(() => transport.requestRead("repositories.discover", {}), {
    code: "not_running",
  });
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(fake.children.length, 4);
  detach();
  await transport.stop();
});

test("an invalid config.toml fails startup with the service's own message", async () => {
  const fake = harness({ autoHandshake: false });
  const transport = transportFor(fake);
  const starting = transport.start();
  await until(() => fake.children[0]?.handshake !== undefined, "handshake");
  const message =
    "Invalid config file /home/me/.config/tongs/config.toml: [cache] mr_list_ttl must be a whole number, not a string.";
  fake.children[0].stdout.write(
    `${JSON.stringify({
      v: 1,
      type: "response",
      id: fake.children[0].handshake.id,
      error: {
        code: "service_error",
        message,
        retryable: false,
        details: { service_code: "configuration_invalid", operation: "start_session" },
      },
    })}\n`,
  );
  const failure = await starting.then(
    () => null,
    (error) => error,
  );
  assert.equal(failure?.code, "configuration_invalid");
  assert.equal(failure?.message, message);
  assert.equal(configurationFailureMessage(failure), message);
  assert.equal(transport.serviceState, "stopped");
});

test("only a configuration failure has a startup message, cleaned and bounded", () => {
  assert.equal(
    configurationFailureMessage(new SidecarError("start_failed", "Could not start.")),
    null,
  );
  assert.equal(configurationFailureMessage(new Error("configuration_invalid")), null);
  assert.equal(configurationFailureMessage("configuration_invalid"), null);
  assert.equal(
    configurationFailureMessage(new SidecarError("configuration_invalid", " \n\t ")),
    null,
  );
  assert.equal(
    configurationFailureMessage(
      new SidecarError("configuration_invalid", "Line one\nline two\u0007."),
    ),
    "Line one line two .",
  );
  const long = configurationFailureMessage(
    new SidecarError("configuration_invalid", "x".repeat(MAX_CONFIGURATION_MESSAGE + 50)),
  );
  assert.equal(long?.length, MAX_CONFIGURATION_MESSAGE);
});
