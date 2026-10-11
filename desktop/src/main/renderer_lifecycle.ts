export interface RendererRecoverySteps {
  resetBindings(): void;
  restartSidecar(): Promise<void>;
  refreshAssets(): Promise<unknown>;
  loadDocument(): Promise<unknown>;
  showFailure(): Promise<unknown>;
}

export class RendererRecoveryCoordinator {
  readonly #steps: RendererRecoverySteps;
  #requested = 0;
  #active: Promise<void> | null = null;

  constructor(steps: RendererRecoverySteps) {
    this.#steps = steps;
  }

  request(): Promise<void> {
    this.#requested += 1;
    if (!this.#active) {
      const task = this.#drain();
      let owned: Promise<void>;
      owned = task.finally(() => {
        if (this.#active === owned) this.#active = null;
      });
      this.#active = owned;
    }
    return this.#active;
  }

  async #drain(): Promise<void> {
    let completed = 0;
    while (completed !== this.#requested) {
      completed = this.#requested;
      await recoverRendererSession(this.#steps);
    }
  }
}

export async function recoverRendererSession(
  steps: RendererRecoverySteps,
): Promise<boolean> {
  steps.resetBindings();
  try {
    await steps.restartSidecar();
    await steps.refreshAssets();
    await steps.loadDocument();
    return true;
  } catch {
    try {
      await steps.loadDocument();
      await steps.showFailure();
    } catch {
      // The owner may already be gone during application shutdown.
    }
    return false;
  }
}

/** At most this many automatic service restarts run within the window below. */
export const MAX_AUTOMATIC_RESTARTS = 3;
export const AUTOMATIC_RESTART_WINDOW_MS = 60_000;

/**
 * `restarting` while the app restarts a lost service, `failed` once it gave
 * up after too many restarts, and `idle` otherwise.
 */
export type ServiceRecoveryState = "idle" | "restarting" | "failed";

export interface ServiceRecoveryOptions {
  /**
   * Restarts the service and reloads the renderer, since handles from the old
   * session are no longer valid. Resolves true once the service is connected.
   */
  readonly restart: () => Promise<boolean>;
  /** True while the app quits or has no window left to recover. */
  readonly shuttingDown: () => boolean;
  readonly onState?: (state: ServiceRecoveryState) => void;
  readonly now?: () => number;
  readonly maxRestarts?: number;
  readonly windowMs?: number;
}

/**
 * Restarts the local service after it stops unexpectedly. Reports that arrive
 * while a restart runs join it. A restart that fails is retried at once while
 * the budget allows; once more than the allowed restarts would fall within the
 * window the policy gives up and leaves the service stopped, so a service that
 * crashes on startup cannot loop forever. Only a connection made some other
 * way, such as the renderer crash recovery, clears that state.
 */
export class ServiceRecoveryPolicy {
  readonly #options: ServiceRecoveryOptions;
  readonly #now: () => number;
  readonly #maxRestarts: number;
  readonly #windowMs: number;
  #restarts: number[] = [];
  #restartCount = 0;
  #state: ServiceRecoveryState = "idle";
  #active: Promise<void> | null = null;

  constructor(options: ServiceRecoveryOptions) {
    this.#options = options;
    this.#now = options.now ?? Date.now;
    this.#maxRestarts = options.maxRestarts ?? MAX_AUTOMATIC_RESTARTS;
    this.#windowMs = options.windowMs ?? AUTOMATIC_RESTART_WINDOW_MS;
    if (
      !Number.isSafeInteger(this.#maxRestarts) ||
      this.#maxRestarts < 0 ||
      !(this.#windowMs > 0)
    ) {
      throw new Error("Service recovery limits must be positive");
    }
  }

  get state(): ServiceRecoveryState {
    return this.#state;
  }

  /** The number of automatic restarts this policy has started. */
  get restartCount(): number {
    return this.#restartCount;
  }

  /** Reports that the service stopped unexpectedly or a read found it gone. */
  serviceLost(): Promise<void> {
    if (this.#active) return this.#active;
    if (this.#state === "failed" || this.#options.shuttingDown()) {
      return Promise.resolve();
    }
    let owned: Promise<void>;
    owned = this.#run().finally(() => {
      if (this.#active === owned) this.#active = null;
    });
    this.#active = owned;
    return owned;
  }

  /** Reports a connection; one made outside this policy clears a give-up. */
  serviceConnected(): void {
    if (!this.#active && this.#state === "failed") this.#setState("idle");
  }

  async #run(): Promise<void> {
    while (!this.#options.shuttingDown()) {
      if (!this.#takeRestart()) {
        this.#setState("failed");
        return;
      }
      // Say so at once, so the status line never asks for a manual restart
      // while one is about to run.
      this.#setState("restarting");
      // Let the transport finish its own exit bookkeeping before restarting it.
      await Promise.resolve();
      let connected = false;
      try {
        connected = await this.#options.restart();
      } catch {
        connected = false;
      }
      if (connected) {
        this.#setState("idle");
        return;
      }
    }
    this.#setState("idle");
  }

  #takeRestart(): boolean {
    const now = this.#now();
    this.#restarts = this.#restarts.filter((at) => now - at < this.#windowMs);
    if (this.#restarts.length >= this.#maxRestarts) return false;
    this.#restarts.push(now);
    this.#restartCount += 1;
    return true;
  }

  #setState(state: ServiceRecoveryState): void {
    if (this.#state === state) return;
    this.#state = state;
    try {
      this.#options.onState?.(state);
    } catch {
      // A listener failure must not stop the recovery itself.
    }
  }
}

/** The transport events the recovery policy listens to. */
export interface ServiceLossSource {
  on(event: string, listener: (...args: unknown[]) => void): unknown;
  off(event: string, listener: (...args: unknown[]) => void): unknown;
}

/**
 * Sends a transport's unexpected stops (`crash`) and its reads that found no
 * service (`not_running`) to the policy, and its reconnections (`status`
 * connected) too. Returns a function that detaches the listeners.
 */
export function watchServiceLoss(
  source: ServiceLossSource,
  policy: ServiceRecoveryPolicy,
): () => void {
  const lost = (): void => {
    void policy.serviceLost();
  };
  const status = (state: unknown): void => {
    if (state === "connected") policy.serviceConnected();
  };
  source.on("crash", lost);
  source.on("not_running", lost);
  source.on("status", status);
  return () => {
    source.off("crash", lost);
    source.off("not_running", lost);
    source.off("status", status);
  };
}
