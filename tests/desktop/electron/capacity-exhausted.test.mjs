// A full session write ledger reaches the desktop as capacity_exhausted. Every
// allowlist keeps the code, every message map shows the restart text, and the
// failure is never offered as retryable.
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import {
  CAPACITY_EXHAUSTED_MESSAGE,
  reviewMutationMessage,
} from "../../../desktop/dist/src/shared/review.js";
import {
  classifyMutationFailure,
  mutationFailureText,
} from "../../../desktop/dist/src/renderer/features/pipelines/index.js";
import { harness, transportFor } from "./fake-sidecar.mjs";

const RESTART_TEXT =
  "Too many write actions in this session. Restart tongs to continue.";
const SOURCE_ROOT = fileURLToPath(new URL("../../../desktop/src", import.meta.url));

function sourceFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const entryPath = path.join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(entryPath);
    return /\.(ts|tsx)$/.test(entry.name) ? [entryPath] : [];
  });
}

function respondWithServiceError(child, requestId, serviceCode) {
  child.stdout.write(
    `${JSON.stringify({
      v: 1,
      type: "response",
      id: requestId,
      error: {
        code: "service_error",
        message: RESTART_TEXT,
        retryable: false,
        details: { service_code: serviceCode },
      },
    })}\n`,
  );
}

test("the review message map shows the restart text", () => {
  assert.equal(CAPACITY_EXHAUSTED_MESSAGE, RESTART_TEXT);
  assert.equal(reviewMutationMessage("capacity_exhausted"), RESTART_TEXT);
});

test("the sidecar keeps capacity_exhausted and does not mark it retryable", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  try {
    for (const method of ["pipelines.retry", "review_mutations.comment", "review_actions.merge"]) {
      const mutation = transport.requestMutation(method, {
        operation_id: `full-ledger-${method}`,
      });
      respondWithServiceError(fake.children[0], mutation.requestId, "capacity_exhausted");
      const failure = await mutation.result.then(
        () => null,
        (error) => ({
          code: error.code,
          message: error.message,
          retryable: error.retryable,
        }),
      );
      assert.deepEqual(failure, {
        code: "capacity_exhausted",
        message: RESTART_TEXT,
        retryable: false,
      });
    }
  } finally {
    await transport.stop();
  }
});

test("the pipelines view treats a full ledger as a known rejection with the restart text", () => {
  const failure = { code: "capacity_exhausted", message: "x", retryable: false };
  assert.equal(classifyMutationFailure(failure), "known");
  assert.equal(mutationFailureText(failure, false), RESTART_TEXT);
});

test("every desktop error code map that lists shutdown_failed also lists capacity_exhausted", () => {
  const maps = sourceFiles(SOURCE_ROOT)
    .map((file) => ({
      file: path.relative(SOURCE_ROOT, file),
      text: readFileSync(file, "utf8"),
    }))
    .filter(({ text }) => text.includes("shutdown_failed"));
  const files = maps.map(({ file }) => file);
  for (const expected of ["main/ipc.ts", "main/sidecar.ts", "shared/review.ts"]) {
    assert.equal(files.includes(expected), true, expected);
  }
  for (const { file, text } of maps) {
    assert.equal(
      text.includes('"capacity_exhausted"') || text.includes("capacity_exhausted:"),
      true,
      file,
    );
  }
  const ipc = maps.find(({ file }) => file === "main/ipc.ts").text;
  const start = ipc.indexOf("const SAFE_REVIEW_ERROR_CODES");
  const end = ipc.indexOf("function safeReviewMutationError");
  assert.equal(start > 0 && end > start, true);
  assert.equal(ipc.slice(start, end).includes('"capacity_exhausted"'), true);
});
