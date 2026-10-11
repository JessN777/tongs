import { EventEmitter } from "node:events";
import {
  spawn,
  type ChildProcessWithoutNullStreams,
  type SpawnOptions,
} from "node:child_process";
import { TextDecoder } from "node:util";

import {
  isColorScheme,
  PROTOCOL_MAJOR,
  REQUIRED_CAPABILITIES,
  RESPONSE_TOO_LARGE,
  type ColorScheme,
  type DesktopEvent,
  type JsonObject,
  type JsonValue,
  type ServiceState,
  type ServiceStatusDto,
} from "../shared/bridge.js";
import { REVIEW_OPERATIONS } from "./review.js";
import type { ServiceRecoveryState } from "./renderer_lifecycle.js";
import { UTILITY_PROTOCOL_METHODS } from "../shared/utilities.js";
import {
  SIDECAR_ARGUMENTS,
  type DesktopLaunchConfig,
  validateLaunchConfig,
} from "./launch.js";

export const MAX_REQUEST_BYTES = 256 * 1024;
export const MAX_RESPONSE_BYTES = 8 * 1024 * 1024;
export const MAX_EVENT_BYTES = 64 * 1024;
export const MAX_PENDING = 64;
export const MAX_QUEUED_EVENTS = 256;
export const MAX_ASSET_CHUNK_BYTES = 512 * 1024;
export const MAX_JSON_DEPTH = 24;
export const MAX_JSON_ITEMS = 20_000;
export const MIN_JSON_DEPTH = 8;
export const MIN_JSON_ITEMS = 1_024;
const MAX_CRASH_HISTORY = 16;
/** The longest service diagnostic line kept; the rest of the line is cut. */
export const MAX_STDERR_LINE_BYTES = 4 * 1024;
/** The service diagnostics one service session may write to the main log. */
export const MAX_STDERR_SESSION_BYTES = 1024 * 1024;
export const STDERR_LOG_PREFIX = "[tongs service] ";
export const STDERR_LINE_TRUNCATED = " [line truncated]";
export const STDERR_OUTPUT_DROPPED = "further output dropped";
const EDITOR_EXPORT_ROOT_ENV = "TONGS_DESKTOP_EDITOR_EXPORT_ROOT";
/**
 * How long a stop waits for the service to exit on its own before SIGTERM,
 * then SIGKILL a second later (#442). The service answers `shutdown` at once,
 * gives pending requests up to 5 s to stop and plugins up to 2 s to clean up,
 * then closes its session for good. When a review submission does not stop,
 * that close spends up to 2 s on the submission service's own two 1 s waits
 * and up to 5 s letting a running draft store write finish. With one stuck
 * submission the stores close within about 14 s, and 15 s covers that case.
 * Several slow closes at once can take longer; the signals are then the safe
 * fallback: killing the service rolls back any unfinished SQLite write, and
 * the next start recovers an interrupted submission.
 */
const SIDECAR_EXIT_TIMEOUT_MS = 15_000;
/** The service writes its `shutdown` reply before it starts to close. */
const SHUTDOWN_REPLY_TIMEOUT_MS = 5_000;

export class SidecarError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly retryable = false,
  ) {
    super(message);
    this.name = "SidecarError";
  }
}

/** The longest configuration message the desktop shows at startup. */
export const MAX_CONFIGURATION_MESSAGE = 1000;

/**
 * The message to show when the service refused to start because config.toml
 * is invalid, or null for any other failure. The service writes this text
 * itself: it names the file, section and key, never a value. Control
 * characters become spaces and the text is cut to a fixed length.
 */
export function configurationFailureMessage(error: unknown): string | null {
  if (
    !(error instanceof SidecarError) ||
    error.code !== "configuration_invalid"
  ) {
    return null;
  }
  const text = Array.from(error.message, (character) => {
    const point = character.codePointAt(0) ?? 0;
    return point < 0x20 || (point >= 0x7f && point < 0xa0) ? " " : character;
  })
    .join("")
    .trim();
  return text.length === 0 ? null : text.slice(0, MAX_CONFIGURATION_MESSAGE);
}

export interface SidecarCrash {
  readonly code: number | null;
  readonly signal: NodeJS.Signals | null;
  readonly unexpected: boolean;
}

export interface SidecarRequest<T extends JsonValue = JsonValue> {
  readonly requestId: string;
  readonly result: Promise<T>;
}

/** The JSON value-count and depth budget every protocol frame must respect. */
export interface JsonLimits {
  readonly values: number;
  readonly depth: number;
}

const DEFAULT_JSON_LIMITS: JsonLimits = Object.freeze({
  values: MAX_JSON_ITEMS,
  depth: MAX_JSON_DEPTH,
});

class JsonLimitError extends Error {}

interface PendingRequest {
  readonly method: string;
  readonly read: boolean;
  readonly resolve: (value: JsonValue) => void;
  readonly reject: (error: Error) => void;
  readonly timer: NodeJS.Timeout;
}

interface HandshakeResult extends JsonObject {
  protocol_major: number;
  core_version: string;
  session_id: string;
  capabilities: JsonValue;
  accepted_capabilities: JsonValue;
  methods: JsonValue;
  limits: JsonValue;
}

type SpawnChild = (
  executable: string,
  args: readonly string[],
  options: SpawnOptions,
) => ChildProcessWithoutNullStreams;

const spawnSidecar: SpawnChild = (executable, args, options) =>
  spawn(executable, [...args], options) as ChildProcessWithoutNullStreams;

/**
 * Characters a diagnostic line must not carry into the log: C0 controls other
 * than tab, DEL and the C1 controls (U+0080 to U+009F, which include the
 * single-byte CSI), the line and paragraph separators, and the bidirectional
 * embeddings, overrides and isolates.
 */
const UNSAFE_LOG_CHARACTERS =
  /[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u2028\u2029\u202a-\u202e\u2066-\u2069]/g;

/** Receives one line of the service's diagnostics, already bounded and prefixed. */
export type DiagnosticSink = (line: string) => void;

const consoleSink: DiagnosticSink = (line) => console.error(line);

/**
 * Forwards one service session's stderr to the main-process log, a line at a
 * time. The service redacts what it writes, so the text is kept as written,
 * apart from characters that could act on a terminal or reorder the line:
 * C0 and C1 controls except tab, DEL, line and paragraph separators, and
 * bidirectional overrides and isolates. A line over the line bound is cut with a
 * marker, and once the session's total reaches its bound one last line says
 * further output was dropped and the rest is ignored, so a chatty or stuck
 * service cannot grow the log or this buffer without limit.
 */
class DiagnosticForwarder {
  readonly #decoder = new TextDecoder("utf-8");
  #line: Buffer[] = [];
  #lineBytes = 0;
  #lineCut = false;
  #total = 0;
  #dropped = false;

  constructor(private readonly sink: DiagnosticSink) {}

  write(chunk: Buffer): void {
    let start = 0;
    while (!this.#dropped && start < chunk.length) {
      const newline = chunk.indexOf(0x0a, start);
      const end = newline < 0 ? chunk.length : newline;
      this.#append(chunk.subarray(start, end));
      if (newline < 0) return;
      this.#emitLine();
      start = newline + 1;
    }
  }

  /** Forwards a last line the service wrote without a newline. */
  end(): void {
    if (!this.#dropped && (this.#lineBytes > 0 || this.#lineCut))
      this.#emitLine();
  }

  #append(part: Buffer): void {
    const room = MAX_STDERR_LINE_BYTES - this.#lineBytes;
    if (part.length > room) this.#lineCut = true;
    const kept = part.subarray(0, Math.max(0, room));
    if (kept.length === 0) return;
    this.#line.push(Buffer.from(kept));
    this.#lineBytes += kept.length;
  }

  #emitLine(): void {
    const raw = Buffer.concat(this.#line, this.#lineBytes);
    const cut = this.#lineCut;
    this.#line = [];
    this.#lineBytes = 0;
    this.#lineCut = false;
    const text = this.#decoder
      .decode(raw)
      .replace(/\r$/, "")
      .replace(UNSAFE_LOG_CHARACTERS, "\ufffd");
    if (text.trim().length === 0 && !cut) return;
    this.#total += raw.length + 1;
    if (this.#total > MAX_STDERR_SESSION_BYTES) {
      this.#dropped = true;
      this.#line = [];
      this.#send(STDERR_OUTPUT_DROPPED);
      return;
    }
    this.#send(`${text}${cut ? STDERR_LINE_TRUNCATED : ""}`);
  }

  #send(text: string): void {
    try {
      this.sink(`${STDERR_LOG_PREFIX}${text}`);
    } catch {
      // A failing log must not take the service connection down with it.
    }
  }
}

export class SidecarTransport extends EventEmitter {
  readonly launch: DesktopLaunchConfig;
  readonly crashHistory: SidecarCrash[] = [];
  private child: ChildProcessWithoutNullStreams | null = null;
  private pending = new Map<string, PendingRequest>();
  // The unterminated tail of the current output frame, kept as the chunks it
  // arrived in so each byte is scanned and copied once per frame.
  private partialChunks: Buffer[] = [];
  private partialLength = 0;
  private nextId = 1;
  private generation = 0;
  private lastEventSequence = 0;
  private jsonLimits: JsonLimits = DEFAULT_JSON_LIMITS;
  private starting: Promise<void> | null = null;
  private stopping: Promise<void> | null = null;
  private expectedExit = false;
  private failureReported = false;
  private closeRequested = false;
  private state: ServiceState = "stopped";
  private recovery: ServiceRecoveryState = "idle";
  private notRunningReported = false;
  private notices: readonly string[] = NO_NOTICES;
  private colorScheme: ColorScheme = "system";

  constructor(
    launch: DesktopLaunchConfig,
    private readonly requestTimeoutMs = 30_000,
    private readonly startupTimeoutMs = 10_000,
    private readonly shutdownTimeoutMs = SIDECAR_EXIT_TIMEOUT_MS,
    private readonly spawnChild: SpawnChild = spawnSidecar,
    private readonly diagnostics: DiagnosticSink = consoleSink,
  ) {
    super();
    this.launch = validateLaunchConfig(launch);
    if (
      [requestTimeoutMs, startupTimeoutMs, shutdownTimeoutMs].some(
        (v) => v <= 0,
      )
    ) {
      throw new Error("Sidecar timeouts must be positive");
    }
  }

  get sessionGeneration(): number {
    return this.generation;
  }

  /** The JSON budget negotiated in the current session's handshake. */
  get negotiatedJsonLimits(): JsonLimits {
    return this.jsonLimits;
  }

  /** Whether the service completed its handshake and has not stopped since. */
  get serviceState(): ServiceState {
    return this.state;
  }

  /**
   * Startup warnings every handshake of this app run reported, such as a
   * review draft whose interrupted submission could not be read. The store
   * warns about such a draft only once, so a later session that reports
   * nothing must not erase the warning; the renderer hides each one when the
   * user dismisses it.
   */
  get serviceNotices(): readonly string[] {
    return this.notices;
  }

  /**
   * The `[ui] color_scheme` the latest handshake reported (#226). A service
   * that restarts reads config.toml again, so it can change; each change
   * emits `color-scheme`.
   */
  get configuredColorScheme(): ColorScheme {
    return this.colorScheme;
  }

  /** Whether the app's crash recovery is restarting the service or gave up. */
  get serviceRecovery(): ServiceRecoveryState {
    return this.recovery;
  }

  /** Set by the app's crash recovery policy; emits `recovery` once per change. */
  setRecovery(state: ServiceRecoveryState): void {
    if (this.recovery === state) return;
    this.recovery = state;
    this.emit("recovery", state);
  }

  get processId(): number | undefined {
    return this.child?.pid;
  }

  async start(): Promise<void> {
    if (this.closeRequested) {
      throw new SidecarError(
        "shutting_down",
        "The desktop service is stopping.",
      );
    }
    if (this.stopping) {
      await this.stopping;
      return this.start();
    }
    if (this.starting) return this.starting;
    if (this.child && !this.child.killed && !this.failureReported) return;
    this.starting = this.startOnce();
    try {
      await this.starting;
    } finally {
      this.starting = null;
    }
  }

  requestRead<T extends JsonValue = JsonValue>(
    method: string,
    params: JsonObject,
  ): SidecarRequest<T> {
    return this.request<T>(method, params, true, this.requestTimeoutMs);
  }

  requestMutation<T extends JsonValue = JsonValue>(
    method: string,
    params: JsonObject,
  ): SidecarRequest<T> {
    return this.request<T>(method, params, false, this.requestTimeoutMs);
  }

  cancelRead(requestId: string): boolean {
    const pending = this.pending.get(requestId);
    if (!pending?.read || !this.child?.stdin.writable) return false;
    this.writeFrame({ v: PROTOCOL_MAJOR, type: "cancel", id: requestId });
    return true;
  }

  async restart(): Promise<void> {
    if (this.closeRequested) {
      throw new SidecarError(
        "shutting_down",
        "The desktop service is stopping.",
      );
    }
    await this.stopSession();
    if (this.closeRequested) {
      throw new SidecarError(
        "shutting_down",
        "The desktop service is stopping.",
      );
    }
    await this.start();
  }

  async stop(): Promise<void> {
    this.closeRequested = true;
    await this.stopSession();
  }

  private async stopSession(): Promise<void> {
    if (this.stopping) return this.stopping;
    this.stopping = this.stopOnce();
    try {
      await this.stopping;
    } finally {
      this.stopping = null;
    }
  }

  private async startOnce(): Promise<void> {
    if (this.child) await this.reapBeforeReplacement(this.child);
    this.expectedExit = false;
    this.failureReported = false;
    this.partialChunks = [];
    this.partialLength = 0;
    this.lastEventSequence = 0;
    this.jsonLimits = DEFAULT_JSON_LIMITS;
    this.generation += 1;
    const environment = { ...process.env };
    delete environment.PYTHONHOME;
    delete environment.PYTHONPATH;
    if (this.launch.utilityExportRoot === undefined) {
      delete environment[EDITOR_EXPORT_ROOT_ENV];
    } else {
      environment[EDITOR_EXPORT_ROOT_ENV] = this.launch.utilityExportRoot;
    }
    let child: ChildProcessWithoutNullStreams;
    try {
      child = this.spawnChild(this.launch.pythonExecutable, SIDECAR_ARGUMENTS, {
        cwd: this.launch.safeCwd,
        env: environment,
        stdio: ["pipe", "pipe", "pipe"],
        windowsHide: true,
      });
    } catch {
      throw new SidecarError(
        "start_failed",
        "The desktop service could not start.",
      );
    }
    this.child = child;
    const generation = this.generation;
    child.stdout.on("data", (chunk: Buffer) =>
      this.consume(child, generation, chunk),
    );
    const diagnostics = new DiagnosticForwarder(this.diagnostics);
    child.stderr.on("data", (chunk: Buffer) => diagnostics.write(chunk));
    child.stderr.once("end", () => diagnostics.end());
    child.once("error", () =>
      this.failConnection(child, generation, "start_failed"),
    );
    child.once("exit", (code, signal) =>
      this.onExit(child, generation, code, signal),
    );

    const handshake = this.request<HandshakeResult>(
      "handshake",
      {
        protocol_major: PROTOCOL_MAJOR,
        core_version: this.launch.coreVersion,
        capabilities: [...REQUIRED_CAPABILITIES],
        client: "tongs-electron-44.2.0",
        limits: { json_values: MAX_JSON_ITEMS, json_depth: MAX_JSON_DEPTH },
      },
      false,
      this.startupTimeoutMs,
      startupTimeout,
    );
    let colorScheme: ColorScheme;
    try {
      const result = await handshake.result;
      this.jsonLimits = validateHandshake(result, this.launch.coreVersion);
      colorScheme = handshakeColorScheme(result);
      this.notices = mergeNotices(this.notices, handshakeNotices(result));
    } catch (error) {
      // Only this session failed: a later start or restart may still succeed.
      // `stop()` is for app shutdown and would refuse every later start.
      await this.stopSession();
      throw error;
    }
    if (
      this.child === child &&
      this.generation === generation &&
      !this.failureReported
    ) {
      this.notRunningReported = false;
      if (colorScheme !== this.colorScheme) {
        this.colorScheme = colorScheme;
        this.emit("color-scheme", colorScheme);
      }
      this.setState("connected");
    }
  }

  /** Emits `status` once per change, so listeners see each stop and reconnect. */
  private setState(state: ServiceState): void {
    if (this.state === state) return;
    this.state = state;
    this.emit("status", state);
  }

  private request<T extends JsonValue>(
    method: string,
    params: JsonObject,
    read: boolean,
    timeoutMs: number,
    timedOut: () => SidecarError = read ? readTimeout : mutationTimeout,
  ): SidecarRequest<T> {
    if (!this.child?.stdin.writable || this.failureReported) {
      // A request that finds no service outside a shutdown or a deliberate
      // stop tells the app once per lost session, so it can restart it.
      if (
        !this.closeRequested &&
        !this.expectedExit &&
        !this.notRunningReported
      ) {
        this.notRunningReported = true;
        queueMicrotask(() => this.emit("not_running"));
      }
      throw new SidecarError(
        "not_running",
        "The desktop service is not running.",
      );
    }
    if (this.pending.size >= MAX_PENDING) {
      throw new SidecarError(
        "too_many_requests",
        "Too many desktop requests are pending.",
      );
    }
    validateJson(params, this.jsonLimits);
    const requestId = `electron-${this.generation}-${this.nextId}`;
    this.nextId = this.nextId === Number.MAX_SAFE_INTEGER ? 1 : this.nextId + 1;
    if (this.pending.has(requestId)) {
      throw new SidecarError(
        "request_id_exhausted",
        "Desktop request IDs are exhausted.",
      );
    }
    let resolvePromise!: (value: T) => void;
    let rejectPromise!: (error: Error) => void;
    const result = new Promise<T>((resolve, reject) => {
      resolvePromise = resolve;
      rejectPromise = reject;
    });
    const timer = setTimeout(() => {
      const pending = this.pending.get(requestId);
      if (!pending) return;
      if (pending.read) this.cancelRead(requestId);
      this.pending.delete(requestId);
      pending.reject(timedOut());
    }, timeoutMs);
    this.pending.set(requestId, {
      method,
      read,
      resolve: resolvePromise as (value: JsonValue) => void,
      reject: rejectPromise,
      timer,
    });
    try {
      this.writeFrame({
        v: PROTOCOL_MAJOR,
        type: "request",
        id: requestId,
        method,
        params,
      });
    } catch (error) {
      clearTimeout(timer);
      this.pending.delete(requestId);
      throw error;
    }
    return { requestId, result };
  }

  private writeFrame(frame: JsonObject): void {
    const bytes = Buffer.from(`${JSON.stringify(frame)}\n`, "utf8");
    if (bytes.length > MAX_REQUEST_BYTES) {
      throw new SidecarError(
        "request_too_large",
        "The desktop request is too large.",
      );
    }
    const child = this.child;
    const generation = this.generation;
    if (!child?.stdin.writable)
      throw new SidecarError(
        "not_running",
        "The desktop service is not running.",
      );
    child.stdin.write(bytes, (error) => {
      if (error) this.failConnection(child, generation, "write_failed");
    });
  }

  private consume(
    child: ChildProcessWithoutNullStreams,
    generation: number,
    chunk: Buffer,
  ): void {
    // A failed connection reads nothing more: the child is being stopped, and
    // a frame after the bad one must not reach a listener.
    if (
      this.child !== child ||
      this.generation !== generation ||
      this.failureReported
    )
      return;
    let start = 0;
    while (true) {
      // Only the new chunk is scanned: the held tail is known to hold no newline.
      const newline = chunk.indexOf(0x0a, start);
      if (newline < 0) {
        this.holdPartial(chunk.subarray(start));
        if (this.partialLength > MAX_RESPONSE_BYTES)
          this.failConnection(child, generation, "frame_too_large");
        return;
      }
      const raw = this.takeFrame(chunk.subarray(start, newline));
      start = newline + 1;
      if (raw.length === 0 || raw.length > MAX_RESPONSE_BYTES) {
        this.holdPartial(chunk.subarray(start));
        this.failConnection(child, generation, "invalid_frame");
        return;
      }
      this.consumeFrame(child, generation, raw);
      if (
        this.child !== child ||
        this.generation !== generation ||
        this.failureReported
      )
        return;
    }
  }

  private holdPartial(piece: Buffer): void {
    if (piece.length === 0) return;
    this.partialChunks.push(piece);
    this.partialLength += piece.length;
  }

  /** Joins the held tail with the frame's last piece, copying at most once. */
  private takeFrame(last: Buffer): Buffer {
    if (this.partialChunks.length === 0) return last;
    this.partialChunks.push(last);
    const raw = Buffer.concat(
      this.partialChunks,
      this.partialLength + last.length,
    );
    this.partialChunks = [];
    this.partialLength = 0;
    return raw;
  }

  private consumeFrame(
    child: ChildProcessWithoutNullStreams,
    generation: number,
    raw: Buffer,
  ): void {
    let value: unknown;
    try {
      value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw));
    } catch {
      this.failConnection(child, generation, "invalid_frame");
      return;
    }
    try {
      validateJson(value, this.jsonLimits);
    } catch (error) {
      if (
        !(error instanceof JsonLimitError) ||
        !this.failOversizedResponse(value)
      ) {
        this.failConnection(child, generation, "invalid_frame");
      }
      return;
    }
    if (
      !isRecord(value) ||
      value.v !== PROTOCOL_MAJOR ||
      typeof value.type !== "string"
    ) {
      this.failConnection(child, generation, "invalid_frame");
      return;
    }
    if (value.type === "event") {
      if (raw.length > MAX_EVENT_BYTES || !validEvent(value)) {
        this.failConnection(child, generation, "invalid_event");
        return;
      }
      const event: DesktopEvent = {
        sequence: value.sequence,
        name: value.event,
        data: value.data as JsonValue,
      };
      if (
        this.lastEventSequence &&
        event.sequence !== this.lastEventSequence + 1
      ) {
        this.emit("event", {
          sequence: event.sequence,
          name: "protocol.resync_required",
          data: { reason: "sequence_gap" },
        } satisfies DesktopEvent);
      }
      this.lastEventSequence = event.sequence;
      this.emit("event", event);
      return;
    }
    if (value.type !== "response" || !validResponse(value)) {
      this.failConnection(child, generation, "invalid_response");
      return;
    }
    const pending = this.pending.get(value.id);
    if (!pending) return;
    clearTimeout(pending.timer);
    this.pending.delete(value.id);
    if (Object.hasOwn(value, "result")) {
      pending.resolve(value.result as JsonValue);
      return;
    }
    const error = value.error as Record<string, unknown>;
    const code = mutationErrorCode(error, pending.read);
    pending.reject(
      new SidecarError(
        code,
        typeof error.message === "string"
          ? error.message
          : "The desktop request failed.",
        error.retryable === true,
      ),
    );
  }

  /**
   * Fail only the request an over-budget response answers. A frame that parses
   * but exceeds the JSON value or depth budget still names its request, so the
   * other pending requests and the service itself survive. Returns false when
   * the frame is not a response with a request id, which fails the connection.
   */
  private failOversizedResponse(value: unknown): boolean {
    if (
      !isRecord(value) ||
      value.v !== PROTOCOL_MAJOR ||
      value.type !== "response" ||
      typeof value.id !== "string"
    ) {
      return false;
    }
    const pending = this.pending.get(value.id);
    if (!pending) return true;
    clearTimeout(pending.timer);
    this.pending.delete(value.id);
    if (!pending.read) {
      // The mutation may have run before its answer overflowed, so its
      // outcome is unknown and a blind retry could repeat the write.
      pending.reject(
        new SidecarError(
          "invalid_response",
          "The action's result was too large to read, so its outcome is unknown. Refresh before retrying.",
          false,
        ),
      );
      return true;
    }
    pending.reject(
      new SidecarError(
        RESPONSE_TOO_LARGE,
        "The desktop response is too large to show.",
        false,
      ),
    );
    return true;
  }

  private failConnection(
    child: ChildProcessWithoutNullStreams,
    generation: number,
    code: string,
  ): void {
    if (
      this.child !== child ||
      this.generation !== generation ||
      this.failureReported
    )
      return;
    this.failureReported = true;
    const error = new SidecarError(
      code,
      "The desktop service connection failed.",
      true,
    );
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pending.clear();
    if (!child.killed) child.kill("SIGTERM");
    this.setState("stopped");
    // A failure while a deliberate stop or restart runs is not a crash: the
    // stop already ends this session, and reporting it would spend one of the
    // recovery policy's automatic restarts.
    if (!this.expectedExit) this.emit("crash", error);
  }

  private onExit(
    child: ChildProcessWithoutNullStreams,
    generation: number,
    code: number | null,
    signal: NodeJS.Signals | null,
  ): void {
    if (this.child !== child || this.generation !== generation) return;
    const unexpected = !this.expectedExit;
    this.crashHistory.push({ code, signal, unexpected });
    if (this.crashHistory.length > MAX_CRASH_HISTORY) this.crashHistory.shift();
    if (unexpected && !this.failureReported)
      this.failConnection(child, generation, "unexpected_eof");
    this.child = null;
  }

  private async reapBeforeReplacement(
    child: ChildProcessWithoutNullStreams,
  ): Promise<void> {
    if (await waitForExit(child, this.shutdownTimeoutMs)) return;
    if (!child.killed) child.kill("SIGTERM");
    if (await waitForExit(child, 1_000)) return;
    child.kill("SIGKILL");
    if (!(await waitForExit(child, 1_000))) {
      throw new SidecarError(
        "reap_failed",
        "The prior desktop service did not stop.",
      );
    }
  }

  private async stopOnce(): Promise<void> {
    const child = this.child;
    this.expectedExit = true;
    this.setState("stopped");
    const stopped = new SidecarError(
      "shutting_down",
      "The desktop service is stopping.",
    );
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(stopped);
    }
    this.pending.clear();
    if (!child) return;
    try {
      const shutdown = this.request<JsonObject>(
        "shutdown",
        {},
        false,
        Math.min(this.shutdownTimeoutMs, SHUTDOWN_REPLY_TIMEOUT_MS),
      );
      await shutdown.result;
    } catch {
      // The bounded process shutdown below remains authoritative.
    }
    child.stdin.end();
    if (await waitForExit(child, this.shutdownTimeoutMs)) return;
    child.kill("SIGTERM");
    if (await waitForExit(child, 1_000)) return;
    child.kill("SIGKILL");
    await waitForExit(child, 1_000);
  }
}

/**
 * Relays a transport's connection state to the renderer. It lives beside the
 * transport, so the packaged main-process module list stays the same. Every
 * change gets a larger revision, so a renderer that asks for the current state
 * while a change is in flight can drop whichever of the two answers is older.
 */
export class ServiceStatusPublisher {
  #revision = 0;
  #state: ServiceState;
  #recovery: ServiceRecoveryState;
  #listener: ((state: ServiceState) => void) | null = null;
  #recoveryListener: ((state: ServiceRecoveryState) => void) | null = null;

  constructor(
    private readonly source: SidecarTransport,
    private readonly publish: (status: ServiceStatusDto) => void,
  ) {
    this.#state = source.serviceState;
    this.#recovery = source.serviceRecovery;
  }

  /**
   * `recovery` is present only while the app restarts a lost service or after
   * it gave up, so the status line can say which; the renderer reads it as
   * optional.
   */
  get current(): ServiceStatusDto & {
    readonly recovery?: ServiceRecoveryState;
  } {
    // Notices outlive a stopped session: they are this app run's warnings.
    const status = {
      state: this.#state,
      revision: this.#revision,
      notices: [...this.source.serviceNotices],
    };
    return this.#recovery === "idle"
      ? status
      : { ...status, recovery: this.#recovery };
  }

  start(): void {
    if (this.#listener) return;
    this.#listener = (state) => {
      if (state === this.#state) return;
      this.#state = state;
      this.#revision += 1;
      this.publish(this.current);
    };
    this.#recoveryListener = (recovery) => {
      if (recovery === this.#recovery) return;
      this.#recovery = recovery;
      this.#revision += 1;
      this.publish(this.current);
    };
    this.source.on("status", this.#listener);
    this.source.on("recovery", this.#recoveryListener);
    // A change between construction and subscription still reaches the renderer.
    this.#listener(this.source.serviceState);
    this.#recoveryListener(this.source.serviceRecovery);
  }

  dispose(): void {
    if (this.#listener) this.source.off("status", this.#listener);
    if (this.#recoveryListener)
      this.source.off("recovery", this.#recoveryListener);
    this.#listener = null;
    this.#recoveryListener = null;
  }
}

const SERVICE_ERROR_CODES = new Set([
  "authentication_failed",
  "capacity_exhausted",
  "closed",
  "configuration_invalid",
  "conflict",
  "internal",
  "invalid_input",
  "invalid_response",
  "network_unavailable",
  "not_found",
  "not_started",
  "permission_denied",
  "rate_limited",
  "resource_not_issued",
  "revision_changed",
  "revision_unavailable",
  "shutdown_failed",
  "unsupported",
]);

function mutationErrorCode(
  error: Record<string, unknown>,
  read: boolean,
): string {
  const protocolCode =
    typeof error.code === "string" ? error.code : "service_error";
  if (read || protocolCode !== "service_error" || !isRecord(error.details))
    return protocolCode;
  const serviceCode = error.details.service_code;
  return typeof serviceCode === "string" && SERVICE_ERROR_CODES.has(serviceCode)
    ? serviceCode
    : protocolCode;
}

function readTimeout(): SidecarError {
  return new SidecarError("timeout", "The desktop read timed out.", true);
}

/** A mutation may have run, so its timeout says the outcome is unknown. */
function mutationTimeout(): SidecarError {
  return new SidecarError(
    "mutation_timeout",
    "The CI mutation response timed out. Its outcome is unknown.",
    true,
  );
}

/** The handshake changes nothing, so its timeout only says startup failed. */
function startupTimeout(): SidecarError {
  return new SidecarError(
    "startup_timeout",
    "The desktop service did not start in time.",
    true,
  );
}

const NO_NOTICES: readonly string[] = Object.freeze([]);
const MAX_SERVICE_NOTICES = 20;
const MAX_SERVICE_NOTICE_LENGTH = 1000;

/**
 * Reads the optional startup warnings of a handshake. They are plain text for
 * the user. A value that is not a list of strings means an incompatible
 * service, like any other malformed handshake field; an over-long list or text
 * is only cut down, so one long notice cannot stop the service from starting.
 */
function handshakeNotices(value: JsonValue): readonly string[] {
  if (!isRecord(value) || value.recovery_warnings === undefined)
    return NO_NOTICES;
  const warnings = value.recovery_warnings;
  if (
    !Array.isArray(warnings) ||
    !warnings.every((warning) => typeof warning === "string")
  ) {
    throw new SidecarError(
      "incompatible_handshake",
      "The desktop service is incompatible.",
    );
  }
  const notices = (warnings as string[])
    .filter((warning) => warning.length > 0)
    .slice(0, MAX_SERVICE_NOTICES)
    .map(truncateNotice);
  return notices.length === 0 ? NO_NOTICES : Object.freeze(notices);
}

/**
 * Reads the optional configured colour scheme of a handshake. A service that
 * predates the setting omits it and the window follows the OS; any value but
 * one of the three scheme names means an incompatible service.
 */
function handshakeColorScheme(value: JsonValue): ColorScheme {
  if (!isRecord(value) || value.color_scheme === undefined) return "system";
  if (!isColorScheme(value.color_scheme)) {
    throw new SidecarError(
      "incompatible_handshake",
      "The desktop service is incompatible.",
    );
  }
  return value.color_scheme;
}

/** Adds the new notices after the kept ones, without repeats, up to the bound. */
function mergeNotices(
  kept: readonly string[],
  added: readonly string[],
): readonly string[] {
  if (added.length === 0) return kept;
  const merged = [...new Set([...kept, ...added])].slice(
    0,
    MAX_SERVICE_NOTICES,
  );
  return merged.length === kept.length ? kept : Object.freeze(merged);
}

/**
 * Cuts a notice to `MAX_SERVICE_NOTICE_LENGTH` UTF-16 units, ending in an
 * ellipsis, without splitting a surrogate pair. Python bounds the text the
 * same way, so a current service never needs the cut.
 */
function truncateNotice(text: string): string {
  if (text.length <= MAX_SERVICE_NOTICE_LENGTH) return text;
  let end = MAX_SERVICE_NOTICE_LENGTH - 1;
  const last = text.charCodeAt(end - 1);
  if (last >= 0xd800 && last <= 0xdbff) end -= 1;
  return `${text.slice(0, end)}…`;
}

function validateHandshake(value: JsonValue, coreVersion: string): JsonLimits {
  if (!isRecord(value))
    throw new SidecarError("invalid_handshake", "Invalid desktop handshake.");
  const required = [...REQUIRED_CAPABILITIES].sort();
  const capabilities = stringArray(value.capabilities);
  const accepted = stringArray(value.accepted_capabilities);
  const methods = stringArray(value.methods);
  if (
    value.protocol_major !== PROTOCOL_MAJOR ||
    value.core_version !== coreVersion ||
    typeof value.session_id !== "string" ||
    value.session_id.length < 16 ||
    !required.every((capability) => capabilities.includes(capability)) ||
    JSON.stringify(accepted) !== JSON.stringify(required) ||
    !REQUIRED_METHODS.every((method) => methods.includes(method)) ||
    !isRecord(value.limits) ||
    value.limits.request_frame_bytes !== MAX_REQUEST_BYTES ||
    value.limits.response_frame_bytes !== MAX_RESPONSE_BYTES ||
    value.limits.event_frame_bytes !== MAX_EVENT_BYTES ||
    value.limits.pending_requests !== MAX_PENDING ||
    value.limits.queued_events !== MAX_QUEUED_EVENTS ||
    value.limits.asset_chunk_bytes !== MAX_ASSET_CHUNK_BYTES
  ) {
    throw new SidecarError(
      "incompatible_handshake",
      "The desktop service is incompatible.",
    );
  }
  return negotiatedJsonLimits(value.limits);
}

/**
 * Accept the sidecar's negotiated JSON budget only when it is an integer pair
 * no looser than this client's own limits and no tighter than the protocol
 * floor, so the two sides can never enforce different budgets silently.
 */
function negotiatedJsonLimits(limits: Record<string, unknown>): JsonLimits {
  const values = limits.json_values;
  const depth = limits.json_depth;
  if (
    !Number.isSafeInteger(values) ||
    !Number.isSafeInteger(depth) ||
    Number(values) < MIN_JSON_ITEMS ||
    Number(values) > MAX_JSON_ITEMS ||
    Number(depth) < MIN_JSON_DEPTH ||
    Number(depth) > MAX_JSON_DEPTH
  ) {
    throw new SidecarError(
      "incompatible_handshake",
      "The desktop service JSON limits are incompatible with this app.",
    );
  }
  return Object.freeze({ values: Number(values), depth: Number(depth) });
}

const REQUIRED_METHODS = Object.freeze(
  [
    "assets.list",
    "assets.read",
    "ci.capabilities",
    "ci.receipt",
    "commits.list",
    "diff.open",
    "diff.page",
    "discussions.list",
    "discussions.page",
    "host.set_location",
    "jobs.list",
    "logs.open",
    "logs.page",
    "jobs.cancel",
    "jobs.retry",
    "pipelines.cancel",
    "pipelines.list",
    "pipelines.retry",
    "plugins.invoke",
    "plugins.list",
    "repositories.discover",
    "repositories.open",
    "review_pipelines.list",
    "reviews.ci_status",
    "reviews.get",
    "reviews.list",
    "shutdown",
    ...UTILITY_PROTOCOL_METHODS,
    ...REVIEW_OPERATIONS.map((operation) => operation.method),
  ].sort(),
);

function validResponse(
  value: Record<string, unknown>,
): value is Record<string, JsonValue> & { id: string } {
  return (
    typeof value.id === "string" &&
    Object.hasOwn(value, "result") !== Object.hasOwn(value, "error") &&
    (!Object.hasOwn(value, "error") || isRecord(value.error))
  );
}

function validEvent(value: Record<string, unknown>): value is Record<
  string,
  unknown
> & {
  sequence: number;
  event: string;
  data: JsonValue;
} {
  return (
    Number.isSafeInteger(value.sequence) &&
    Number(value.sequence) > 0 &&
    typeof value.event === "string" &&
    value.event.length <= 120 &&
    Object.hasOwn(value, "data")
  );
}

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string"))
    return [];
  return [...value].sort();
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function validateJson(
  value: unknown,
  limits: JsonLimits,
  depth = 0,
  budget = { items: 0 },
): asserts value is JsonValue {
  // The same whole-frame budget the sidecar applies: at most `limits.values`
  // values, and nothing nested deeper than `limits.depth` below the frame.
  if (depth > limits.depth)
    throw new JsonLimitError("JSON is nested too deeply");
  budget.items += 1;
  if (budget.items > limits.values)
    throw new JsonLimitError("JSON has too many values");
  if (value === null || typeof value === "string" || typeof value === "boolean")
    return;
  if (typeof value === "number" && Number.isFinite(value)) return;
  if (Array.isArray(value)) {
    for (const item of value) validateJson(item, limits, depth + 1, budget);
    return;
  }
  if (!isRecord(value)) throw new Error("invalid JSON");
  for (const [key, item] of Object.entries(value)) {
    if (key === "__proto__" || key === "constructor" || key === "prototype")
      throw new Error("invalid JSON key");
    validateJson(item, limits, depth + 1, budget);
  }
}

async function waitForExit(
  child: ChildProcessWithoutNullStreams,
  timeoutMs: number,
): Promise<boolean> {
  if (child.exitCode !== null || child.signalCode !== null) return true;
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(false), timeoutMs);
    child.once("exit", () => {
      clearTimeout(timer);
      resolve(true);
    });
  });
}
