import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  AppearanceController,
  COLOR_SCHEME_SAVE_FAILURE,
  DESKTOP_SETTINGS_FILE,
  DesktopSettingsStore,
  MAX_DESKTOP_SETTINGS_BYTES,
  TransientSettingsStore,
  WINDOW_BACKGROUND,
  assertColorSchemeChoice,
  colorSchemeStoreFor,
  parseSavedColorScheme,
  windowBackground,
} from "../../../desktop/dist/src/main/appearance.js";
import {
  IPC_CHANNELS,
  isAppearance,
} from "../../../desktop/dist/src/shared/bridge.js";
import { harness, transportFor } from "./fake-sidecar.mjs";

/** Records what the controller sets on Electron's native theme. */
function fakeTheme(systemDark = true) {
  const theme = {
    sources: [],
    current: "system",
    get themeSource() {
      return this.current;
    },
    set themeSource(value) {
      this.current = value;
      this.sources.push(value);
    },
    get shouldUseDarkColors() {
      return this.current === "system" ? systemDark : this.current === "dark";
    },
  };
  return theme;
}

async function scratch(context) {
  const root = await mkdtemp(path.join(os.tmpdir(), "tongs-appearance-"));
  context.after(() => rm(root, { recursive: true, force: true }));
  return root;
}

function settingsPath(root) {
  return path.join(root, "user-data", DESKTOP_SETTINGS_FILE);
}

test("the settings file is named for the desktop app and lives where it is told", () => {
  assert.equal(DESKTOP_SETTINGS_FILE, "desktop-settings.json");
  assert.throws(() => new DesktopSettingsStore("relative/settings.json"), {
    message: "The desktop settings path must be absolute",
  });
});

test("a missing settings file means nothing is saved", async (context) => {
  const root = await scratch(context);
  const store = new DesktopSettingsStore(settingsPath(root));
  assert.equal(await store.readColorScheme(), null);
});

test("a saved choice round-trips through a private settings file", async (context) => {
  const root = await scratch(context);
  const file = settingsPath(root);
  const store = new DesktopSettingsStore(file);
  await store.writeColorScheme("dark");
  assert.equal(await readFile(file, "utf8"), '{"color_scheme":"dark"}\n');
  assert.equal((await stat(file)).mode & 0o777, 0o600);
  assert.equal(await store.readColorScheme(), "dark");
  await store.writeColorScheme(null);
  assert.equal(await readFile(file, "utf8"), "{}\n");
  assert.equal(await store.readColorScheme(), null);
});

test("concurrent saves keep the last choice", async (context) => {
  const root = await scratch(context);
  const store = new DesktopSettingsStore(settingsPath(root));
  await Promise.all([
    store.writeColorScheme("light"),
    store.writeColorScheme("dark"),
    store.writeColorScheme("light"),
  ]);
  assert.equal(await store.readColorScheme(), "light");
});

for (const [label, text] of [
  ["not JSON", "{color_scheme: dark"],
  ["an array", '["dark"]'],
  ["null", "null"],
  ["an unknown scheme", '{"color_scheme":"blue"}'],
  ["a capitalised scheme", '{"color_scheme":"Dark"}'],
  ["system", '{"color_scheme":"system"}'],
  ["a number", '{"color_scheme":1}'],
]) {
  test(`a settings file holding ${label} means nothing is saved`, async (context) => {
    assert.equal(parseSavedColorScheme(text), null);
    const root = await scratch(context);
    const file = settingsPath(root);
    await mkdir(path.dirname(file), { recursive: true });
    await writeFile(file, text);
    assert.equal(await new DesktopSettingsStore(file).readColorScheme(), null);
  });
}

test("an oversized settings file is not read", async (context) => {
  const root = await scratch(context);
  const file = settingsPath(root);
  await mkdir(path.dirname(file), { recursive: true });
  const padding = " ".repeat(MAX_DESKTOP_SETTINGS_BYTES);
  await writeFile(file, `{"color_scheme":"dark"}${padding}`);
  assert.equal(await new DesktopSettingsStore(file).readColorScheme(), null);
});

test("a directory in place of the settings file means nothing is saved", async (context) => {
  const root = await scratch(context);
  const file = settingsPath(root);
  await mkdir(file, { recursive: true });
  assert.equal(await new DesktopSettingsStore(file).readColorScheme(), null);
});

test("the default follows the OS scheme", async (context) => {
  const root = await scratch(context);
  const theme = fakeTheme();
  const controller = new AppearanceController(
    new DesktopSettingsStore(settingsPath(root)),
    theme,
  );
  const appearance = await controller.load();
  assert.deepEqual({ ...appearance }, {
    configured: "system",
    saved: null,
    effective: "system",
    revision: 0,
  });
  assert.equal(theme.current, "system");
  assert.equal(isAppearance(appearance), true);
});

test("the configured scheme applies until a choice is saved", async (context) => {
  const root = await scratch(context);
  const theme = fakeTheme();
  const controller = new AppearanceController(
    new DesktopSettingsStore(settingsPath(root)),
    theme,
  );
  await controller.load();
  controller.setConfigured("light");
  assert.equal(controller.current.effective, "light");
  assert.equal(theme.current, "light");
  assert.equal(controller.dark, false);
});

test("a saved choice wins over the configured scheme at once", async (context) => {
  const root = await scratch(context);
  const theme = fakeTheme(false);
  const controller = new AppearanceController(
    new DesktopSettingsStore(settingsPath(root)),
    theme,
  );
  await controller.load();
  controller.setConfigured("light");
  const seen = [];
  controller.onChange((appearance) => seen.push(appearance.effective));

  const chosen = await controller.choose("dark");
  assert.equal(chosen.saved, "dark");
  assert.equal(chosen.effective, "dark");
  assert.equal(theme.current, "dark");
  assert.equal(controller.dark, true);
  assert.deepEqual(seen, ["dark"]);

  // A service restart that reads another configured value keeps the choice.
  controller.setConfigured("system");
  assert.equal(controller.current.effective, "dark");
  assert.equal(theme.current, "dark");
});

test("choosing System clears the saved choice and falls back to the config", async (context) => {
  const root = await scratch(context);
  const file = settingsPath(root);
  const theme = fakeTheme();
  const controller = new AppearanceController(new DesktopSettingsStore(file), theme);
  await controller.load();
  controller.setConfigured("dark");
  await controller.choose("light");
  assert.equal(theme.current, "light");

  const cleared = await controller.choose("system");
  assert.equal(cleared.saved, null);
  assert.equal(cleared.configured, "dark");
  assert.equal(cleared.effective, "dark");
  assert.equal(theme.current, "dark");
  assert.equal(await readFile(file, "utf8"), "{}\n");
});

test("a saved choice applies again after a restart", async (context) => {
  const root = await scratch(context);
  const file = settingsPath(root);
  const first = new AppearanceController(new DesktopSettingsStore(file), fakeTheme());
  await first.load();
  await first.choose("light");

  const theme = fakeTheme();
  const restarted = new AppearanceController(new DesktopSettingsStore(file), theme);
  const appearance = await restarted.load();
  assert.equal(appearance.saved, "light");
  assert.equal(appearance.effective, "light");
  assert.equal(theme.current, "light");
  restarted.setConfigured("dark");
  assert.equal(theme.current, "light");
});

test("every change bumps the revision and an unchanged value does not", async (context) => {
  const root = await scratch(context);
  const controller = new AppearanceController(
    new DesktopSettingsStore(settingsPath(root)),
    fakeTheme(),
  );
  await controller.load();
  const revisions = [];
  controller.onChange((appearance) => revisions.push(appearance.revision));
  controller.setConfigured("dark");
  controller.setConfigured("dark");
  await controller.choose("light");
  await controller.choose("light");
  await controller.choose("system");
  assert.deepEqual(revisions, [1, 2, 3]);
  assert.equal(controller.current.revision, 3);
});

test("a failing listener does not stop the change or the others", async (context) => {
  const root = await scratch(context);
  const theme = fakeTheme();
  const controller = new AppearanceController(
    new DesktopSettingsStore(settingsPath(root)),
    theme,
  );
  const seen = [];
  controller.onChange(() => {
    throw new Error("listener failed");
  });
  const unsubscribe = controller.onChange((appearance) => seen.push(appearance.effective));
  controller.setConfigured("light");
  unsubscribe();
  controller.setConfigured("dark");
  assert.deepEqual(seen, ["light"]);
  assert.equal(theme.current, "dark");
});

for (const value of ["Dark", "auto", "", " light", 1, null, undefined, {}, ["dark"], true]) {
  test(`the toggle rejects ${JSON.stringify(value) ?? "undefined"} without saving or applying`, async (context) => {
    assert.throws(() => assertColorSchemeChoice(value), {
      message: "Invalid desktop colour scheme",
    });
    const root = await scratch(context);
    const file = settingsPath(root);
    const theme = fakeTheme();
    const controller = new AppearanceController(new DesktopSettingsStore(file), theme);
    await controller.load();
    await assert.rejects(controller.choose(value), {
      message: "Invalid desktop colour scheme",
    });
    assert.equal(controller.current.revision, 0);
    assert.deepEqual(theme.sources, []);
    await assert.rejects(stat(file), { code: "ENOENT" });
  });
}

test("a failed save changes nothing and names no path", async (context) => {
  const root = await scratch(context);
  // A regular file where the folder should be fails even for root.
  const blocker = path.join(root, "user-data");
  await writeFile(blocker, "not a folder");
  const file = path.join(blocker, DESKTOP_SETTINGS_FILE);
  const theme = fakeTheme();
  const controller = new AppearanceController(new DesktopSettingsStore(file), theme);
  await controller.load();
  await assert.rejects(controller.choose("dark"), (error) => {
    assert.equal(error.message, COLOR_SCHEME_SAVE_FAILURE);
    assert.equal(error.message.includes(root), false);
    return true;
  });
  assert.equal(controller.current.effective, "system");
  assert.equal(controller.current.revision, 0);
  assert.deepEqual(theme.sources, []);
});

/** A user data folder whose settings file already holds a saved choice. */
async function userDataWithSavedChoice(context, choice) {
  const root = await scratch(context);
  const userData = path.join(root, "user-data");
  const file = path.join(userData, DESKTOP_SETTINGS_FILE);
  await mkdir(userData, { recursive: true });
  const text = `${JSON.stringify({ color_scheme: choice })}\n`;
  await writeFile(file, text);
  return { userData, file, text };
}

test("a normal run applies the saved choice from the user data folder", async (context) => {
  const { userData, file } = await userDataWithSavedChoice(context, "dark");
  const store = colorSchemeStoreFor(userData, false);
  assert.equal(store instanceof DesktopSettingsStore, true);
  assert.equal(store.file, file);
  const theme = fakeTheme(false);
  const controller = new AppearanceController(store, theme);
  const appearance = await controller.load();
  controller.setConfigured("light");
  assert.equal(appearance.saved, "dark");
  assert.equal(controller.current.effective, "dark");
  assert.equal(theme.current, "dark");
});

for (const [saved, configured] of [
  ["dark", "light"],
  ["light", "dark"],
  ["dark", "system"],
]) {
  test(`a smoke run ignores a saved ${saved} choice and follows the configured ${configured}`, async (context) => {
    const { userData, file, text } = await userDataWithSavedChoice(context, saved);
    const store = colorSchemeStoreFor(userData, true);
    assert.equal(store instanceof TransientSettingsStore, true);
    const theme = fakeTheme();
    const controller = new AppearanceController(store, theme);
    const loaded = await controller.load();
    assert.deepEqual({ ...loaded }, {
      configured: "system",
      saved: null,
      effective: "system",
      revision: 0,
    });
    assert.equal(theme.current, "system");
    assert.deepEqual(theme.sources, []);

    controller.setConfigured(configured);
    assert.equal(controller.current.saved, null);
    assert.equal(controller.current.effective, configured);
    assert.equal(theme.current, configured);
    assert.equal(await readFile(file, "utf8"), text);
  });
}

test("a smoke run keeps a toggle choice in memory and leaves the saved file alone", async (context) => {
  const { userData, file, text } = await userDataWithSavedChoice(context, "dark");
  const theme = fakeTheme();
  const controller = new AppearanceController(colorSchemeStoreFor(userData, true), theme);
  await controller.load();
  controller.setConfigured("dark");

  const chosen = await controller.choose("light");
  assert.equal(chosen.saved, "light");
  assert.equal(chosen.effective, "light");
  assert.equal(theme.current, "light");
  const cleared = await controller.choose("system");
  assert.equal(cleared.saved, null);
  assert.equal(cleared.effective, "dark");
  assert.equal(await readFile(file, "utf8"), text);
});

test("a smoke run with no user data folder writes nothing there", async (context) => {
  const root = await scratch(context);
  const userData = path.join(root, "user-data");
  const controller = new AppearanceController(colorSchemeStoreFor(userData, true), fakeTheme());
  await controller.load();
  await controller.choose("dark");
  await assert.rejects(stat(userData), { code: "ENOENT" });
});

test("the transient store starts empty and keeps only the last choice", async () => {
  const store = new TransientSettingsStore();
  assert.equal(await store.readColorScheme(), null);
  await store.writeColorScheme("light");
  assert.equal(await store.readColorScheme(), "light");
  await store.writeColorScheme(null);
  assert.equal(await store.readColorScheme(), null);
});

test("the production entry point picks the smoke store from the smoke report flag", () => {
  const entry = readFileSync(
    new URL("../../../desktop/src/main/index.ts", import.meta.url),
    "utf8",
  );
  assert.equal(entry.split("colorSchemeStoreFor(").length - 1, 1);
  assert.equal(
    entry.includes('colorSchemeStoreFor(app.getPath("userData"), Boolean(smokePath))'),
    true,
  );
  assert.match(entry, /const smokePath = argumentValue\(process\.argv, "--tongs-smoke-report"\);/);
  assert.match(entry, /if \(smokePath\) await captureSmokeReport\(smokePath\);/);
  assert.equal(entry.includes("new DesktopSettingsStore("), false);
});

test("the window background matches the stylesheet in each scheme", () => {
  assert.equal(windowBackground(true), WINDOW_BACKGROUND.dark);
  assert.equal(windowBackground(false), WINDOW_BACKGROUND.light);
  assert.equal(WINDOW_BACKGROUND.dark, "#0c1119");
  assert.equal(WINDOW_BACKGROUND.light, "#f6f8fa");
});

test("the appearance channels are distinct tongs: names", () => {
  assert.equal(IPC_CHANNELS.getAppearance, "tongs:appearance.get");
  assert.equal(IPC_CHANNELS.setColorScheme, "tongs:appearance.set-color-scheme");
  assert.equal(IPC_CHANNELS.appearance, "tongs:appearance");
});

for (const [label, value] of [
  ["a non-object", "dark"],
  ["extra fields", { configured: "system", saved: null, effective: "system", revision: 0, path: "/x" }],
  ["an effective scheme that disagrees", { configured: "dark", saved: null, effective: "light", revision: 1 }],
  ["a saved System", { configured: "dark", saved: "system", effective: "system", revision: 1 }],
  ["an unknown configured scheme", { configured: "blue", saved: "dark", effective: "dark", revision: 1 }],
  ["a negative revision", { configured: "system", saved: null, effective: "system", revision: -1 }],
  ["a fractional revision", { configured: "system", saved: null, effective: "system", revision: 1.5 }],
]) {
  test(`an appearance with ${label} is rejected`, () => {
    assert.equal(isAppearance(value), false);
  });
}

test("the transport takes the configured scheme from the handshake", async () => {
  const fake = harness({ handshakeExtras: { color_scheme: "dark" } });
  const transport = transportFor(fake);
  const changes = [];
  transport.on("color-scheme", (scheme) => changes.push(scheme));
  assert.equal(transport.configuredColorScheme, "system");
  await transport.start();
  assert.equal(transport.configuredColorScheme, "dark");
  assert.deepEqual(changes, ["dark"]);
  await transport.restart();
  assert.deepEqual(changes, ["dark"]);
  await transport.stop();
});

test("a handshake without a colour scheme follows the OS", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  const changes = [];
  transport.on("color-scheme", (scheme) => changes.push(scheme));
  await transport.start();
  assert.equal(transport.configuredColorScheme, "system");
  assert.deepEqual(changes, []);
  await transport.stop();
});

test("a service restart that reads another scheme reports the change", async () => {
  // Each handshake reads the next value, as a restarted service reads
  // config.toml again.
  const schemes = ["light", "light", "dark"];
  let handshakes = 0;
  const fake = harness({
    handshakeExtras: {
      get color_scheme() {
        const scheme = schemes[handshakes];
        handshakes += 1;
        return scheme;
      },
    },
  });
  const transport = transportFor(fake);
  const changes = [];
  transport.on("color-scheme", (scheme) => changes.push(scheme));
  await transport.start();
  await transport.restart();
  assert.deepEqual(changes, ["light"]);
  await transport.restart();
  assert.equal(handshakes, 3);
  assert.deepEqual(changes, ["light", "dark"]);
  assert.equal(transport.configuredColorScheme, "dark");
  await transport.stop();
});

for (const [label, value] of [
  ["capitalised", "Dark"],
  ["unknown", "auto"],
  ["empty", ""],
  ["a number", 1],
  ["null", null],
  ["a list", ["dark"]],
]) {
  test(`a handshake whose colour scheme is ${label} is incompatible`, async () => {
    const fake = harness({ handshakeExtras: { color_scheme: value } });
    const transport = transportFor(fake);
    await assert.rejects(transport.start(), { code: "incompatible_handshake" });
    assert.equal(transport.configuredColorScheme, "system");
    await transport.stop();
  });
}
