import { mkdir, open, rename, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import {
  isColorScheme,
  type AppearanceDto,
  type ColorScheme,
  type SavedColorScheme,
} from "../shared/bridge.js";

/**
 * The desktop colour scheme (#226). `[ui] color_scheme` in config.toml sets
 * the default, which the service reports in its handshake. The toggle's
 * choice is saved in the desktop app's own settings file, never in
 * config.toml, and wins over the configured value; choosing System clears it.
 * The scheme in effect drives Electron's native theme, so the stylesheet's
 * `prefers-color-scheme` rules, native menus and dialogs all follow it.
 */

/** The desktop app's own settings, in Electron's user data folder. */
export const DESKTOP_SETTINGS_FILE = "desktop-settings.json";
/** A larger settings file is not read, so a damaged one cannot stall startup. */
export const MAX_DESKTOP_SETTINGS_BYTES = 16 * 1024;
/** The window colour behind the first paint, matching the stylesheet's `--bg`. */
export const WINDOW_BACKGROUND = Object.freeze({
  dark: "#0c1119",
  light: "#f6f8fa",
} as const);
export const COLOR_SCHEME_SAVE_FAILURE =
  "The colour scheme could not be saved.";

/** The part of Electron's `nativeTheme` the appearance drives. */
export interface NativeThemeTarget {
  themeSource: ColorScheme;
  readonly shouldUseDarkColors: boolean;
}

/** Reads the saved colour scheme from settings text; anything else is none. */
export function parseSavedColorScheme(text: string): SavedColorScheme | null {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return null;
  }
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const saved = (value as Record<string, unknown>).color_scheme;
  return saved === "light" || saved === "dark" ? saved : null;
}

/** The toggle's choice, exactly one of the three scheme names. */
export function assertColorSchemeChoice(value: unknown): ColorScheme {
  if (!isColorScheme(value)) throw new Error("Invalid desktop colour scheme");
  return value;
}

export function windowBackground(dark: boolean): string {
  return dark ? WINDOW_BACKGROUND.dark : WINDOW_BACKGROUND.light;
}

/** Where the toggle's choice is read from and saved to. */
export interface ColorSchemeStore {
  readColorScheme(): Promise<SavedColorScheme | null>;
  writeColorScheme(choice: SavedColorScheme | null): Promise<void>;
}

/**
 * The desktop settings file. A missing, unreadable, oversized or malformed
 * file means nothing is saved. Writes replace the file atomically with mode
 * 0600 and run one at a time, so the last choice is the one kept.
 */
export class DesktopSettingsStore implements ColorSchemeStore {
  #writes: Promise<void> = Promise.resolve();

  constructor(readonly file: string) {
    if (!path.isAbsolute(file) || file.includes("\0")) {
      throw new Error("The desktop settings path must be absolute");
    }
  }

  async readColorScheme(): Promise<SavedColorScheme | null> {
    let text: string;
    try {
      const handle = await open(this.file, "r");
      try {
        const status = await handle.stat();
        if (!status.isFile() || status.size > MAX_DESKTOP_SETTINGS_BYTES) {
          return null;
        }
        text = await handle.readFile("utf8");
      } finally {
        await handle.close();
      }
    } catch {
      return null;
    }
    return text.length > MAX_DESKTOP_SETTINGS_BYTES
      ? null
      : parseSavedColorScheme(text);
  }

  writeColorScheme(choice: SavedColorScheme | null): Promise<void> {
    const write = this.#writes.then(() => this.#write(choice));
    this.#writes = write.catch(() => undefined);
    return write;
  }

  async #write(choice: SavedColorScheme | null): Promise<void> {
    await mkdir(path.dirname(this.file), { recursive: true, mode: 0o700 });
    const document = choice === null ? {} : { color_scheme: choice };
    const temporary = `${this.file}.${process.pid}.tmp`;
    try {
      await writeFile(temporary, `${JSON.stringify(document)}\n`, {
        mode: 0o600,
      });
      await rename(temporary, this.file);
    } catch (error) {
      await rm(temporary, { force: true });
      throw error;
    }
  }
}

/**
 * A store that starts empty and keeps the toggle's choice in memory only, so
 * a run that uses it never reads or changes the saved settings file.
 */
export class TransientSettingsStore implements ColorSchemeStore {
  #choice: SavedColorScheme | null = null;

  async readColorScheme(): Promise<SavedColorScheme | null> {
    return this.#choice;
  }

  async writeColorScheme(choice: SavedColorScheme | null): Promise<void> {
    this.#choice = choice;
  }
}

/**
 * The store for this run (#438). A production smoke run ignores the saved
 * choice and follows the configured scheme, so its screenshots and report do
 * not depend on what a developer last chose in the app bar, and it leaves
 * that choice untouched. Every other run uses the settings file in the user
 * data folder.
 */
export function colorSchemeStoreFor(
  userDataPath: string,
  smoke: boolean,
): ColorSchemeStore {
  return smoke
    ? new TransientSettingsStore()
    : new DesktopSettingsStore(path.join(userDataPath, DESKTOP_SETTINGS_FILE));
}

/**
 * Owns the scheme in effect: the saved choice when there is one, else the
 * configured value. Every change bumps the revision, applies the scheme to
 * the native theme and tells each listener.
 */
export class AppearanceController {
  #configured: ColorScheme = "system";
  #saved: SavedColorScheme | null = null;
  #revision = 0;
  readonly #listeners = new Set<(appearance: AppearanceDto) => void>();

  constructor(
    private readonly store: ColorSchemeStore,
    private readonly theme: NativeThemeTarget,
  ) {
    this.#apply();
  }

  get current(): AppearanceDto {
    return Object.freeze({
      configured: this.#configured,
      saved: this.#saved,
      effective: this.#saved ?? this.#configured,
      revision: this.#revision,
    });
  }

  /** Whether the window is dark now, after the OS scheme when it follows it. */
  get dark(): boolean {
    return this.theme.shouldUseDarkColors;
  }

  /** Reads the saved choice and applies it. */
  async load(): Promise<AppearanceDto> {
    const saved = await this.store.readColorScheme();
    if (saved !== this.#saved) {
      this.#saved = saved;
      this.#changed();
    }
    return this.current;
  }

  /** Takes the configured value a service handshake reported. */
  setConfigured(value: ColorScheme): void {
    const configured = isColorScheme(value) ? value : "system";
    if (configured === this.#configured) return;
    this.#configured = configured;
    this.#changed();
  }

  /**
   * Saves the toggle's choice, then applies it. A failed save changes
   * nothing and rejects with a message that names no path.
   */
  async choose(value: unknown): Promise<AppearanceDto> {
    const choice = assertColorSchemeChoice(value);
    const saved = choice === "system" ? null : choice;
    try {
      await this.store.writeColorScheme(saved);
    } catch {
      throw new Error(COLOR_SCHEME_SAVE_FAILURE);
    }
    if (saved !== this.#saved) {
      this.#saved = saved;
      this.#changed();
    }
    return this.current;
  }

  onChange(listener: (appearance: AppearanceDto) => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  #changed(): void {
    this.#revision += 1;
    this.#apply();
    const appearance = this.current;
    for (const listener of [...this.#listeners]) {
      try {
        listener(appearance);
      } catch {
        // One failing listener must not stop the others or the change.
      }
    }
  }

  #apply(): void {
    const effective = this.#saved ?? this.#configured;
    if (this.theme.themeSource !== effective)
      this.theme.themeSource = effective;
  }
}
