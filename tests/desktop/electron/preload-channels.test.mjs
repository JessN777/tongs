import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { IPC_CHANNELS } from "../../../desktop/dist/src/shared/bridge.js";
import { CI_IPC_CHANNELS } from "../../../desktop/dist/src/shared/ci.js";
import { REVIEW_IPC_CHANNELS } from "../../../desktop/dist/src/shared/review.js";
import { UTILITY_IPC_CHANNELS } from "../../../desktop/dist/src/shared/utilities.js";

// The sandboxed preload cannot import the shared modules, so it redeclares
// every channel name. This reads its source as text and checks each copy.
const preload = readFileSync(
  new URL("../../../desktop/src/preload/index.cts", import.meta.url),
  "utf8",
);

const SHARED = Object.freeze({
  IPC_CHANNELS,
  CI_IPC_CHANNELS,
  REVIEW_IPC_CHANNELS,
  UTILITY_IPC_CHANNELS,
});

function preloadGroup(name) {
  const match = new RegExp(
    `const ${name} = Object\\.freeze\\(\\{([\\s\\S]*?)\\} as const\\);`,
  ).exec(preload);
  assert.ok(match, `${name} is declared in the preload`);
  const entries = [...match[1].matchAll(/([A-Za-z_$][\w$]*)\s*:\s*"([^"]*)"/g)];
  return Object.fromEntries(entries.map(([, key, value]) => [key, value]));
}

// Compares code without its layout, so a formatter's line breaks and
// trailing commas never decide a contract check.
function squash(source) {
  return source.replace(/\s+/g, "").replace(/,([)\]}])/g, "$1");
}

function assertExposes(expected) {
  assert.equal(squash(preload).includes(squash(expected)), true, expected);
}

function channelLiterals(source) {
  return [...source.matchAll(/["'`](tongs:[^"'`]*)["'`]/g)].map((match) => match[1]);
}

test("each preload channel group equals the shared constants it copies", () => {
  for (const [name, shared] of Object.entries(SHARED)) {
    assert.deepEqual(preloadGroup(name), { ...shared }, `${name} has drifted`);
  }
});

test("the preload's tongs: literals are exactly the shared channel names", () => {
  const literals = channelLiterals(preload);
  const shared = Object.values(SHARED).flatMap((group) => Object.values(group));

  assert.equal(new Set(literals).size, literals.length);
  assert.equal(new Set(shared).size, shared.length);
  assert.deepEqual([...literals].sort(), [...shared].sort());
});

test("the preload exposes the diff context and generation channels", () => {
  assert.equal(preloadGroup("IPC_CHANNELS").getDiffContext, "tongs:diff.context");
  assert.equal(preloadGroup("IPC_CHANNELS").generateDiff, "tongs:diff.generate");
  assertExposes(
    "getDiffContext: (params: DiffContextParams): DesktopRead<DiffContextResult> => read(IPC_CHANNELS.getDiffContext, params)",
  );
  assertExposes(
    "generateDiff: (params: GenerateDiffParams): DesktopRead<GenerateDiffResult> => read(IPC_CHANNELS.generateDiff, params)",
  );
});

test("the preload exposes the colour scheme channels and nothing broader", () => {
  const channels = preloadGroup("IPC_CHANNELS");
  assert.equal(channels.getAppearance, "tongs:appearance.get");
  assert.equal(channels.setColorScheme, "tongs:appearance.set-color-scheme");
  assert.equal(channels.appearance, "tongs:appearance");
  assertExposes(
    "getAppearance: async (): Promise<AppearanceDto> => ipcRenderer.invoke(IPC_CHANNELS.getAppearance) as Promise<AppearanceDto>",
  );
  assertExposes(
    "setColorScheme: async (choice: ColorScheme): Promise<AppearanceDto> => ipcRenderer.invoke(IPC_CHANNELS.setColorScheme, choice) as Promise<AppearanceDto>",
  );
  assertExposes("ipcRenderer.on(IPC_CHANNELS.appearance, wrapped)");
  assertExposes("ipcRenderer.removeListener(IPC_CHANNELS.appearance, wrapped)");
});
