import type {
  CIStatusValue,
  DesktopBridge,
  ReviewListItemDto,
} from "../../../shared/bridge.js";

/**
 * Most CI lookups one review list keeps unsettled at once, counting lookups
 * that were cancelled but whose read has not settled yet.
 *
 * GitHub CI reads that reach the service together share one GraphQL query of
 * up to 50 pull requests, so a screen of rows should be in flight at once to
 * land in one or two queries instead of one request each. Each lookup is one
 * sidecar request, and the main process refuses a read once 64 are pending,
 * so 16 leaves room for the list reads (at most 8) and every other view. The
 * service still holds every forge request to its per-host limit, so a forge
 * without batching (GitLab) sees no more parallel requests than before.
 */
export const CI_LOOKUP_CONCURRENCY = 16;

/** How long a failed lookup waits before its one automatic retry. */
export const CI_RETRY_DELAY_MS = 30_000;

/** What a row shows when its lookup failed or waits for a rate limit. */
export const CI_UNAVAILABLE = "unavailable";

/**
 * The CI cell of a row: its status, `CI_UNAVAILABLE`, or null while it loads.
 * A status is text, as the list read names it.
 */
export type CIShown = string | null;

/** Schedules `run` after `delay` ms and returns how to cancel it. */
export type CITimer = (run: () => void, delay: number) => () => void;

export interface CILookupOptions {
  readonly concurrency?: number;
  readonly retryDelayMs?: number;
  readonly now?: () => number;
  readonly timer?: CITimer;
}

const browserTimer: CITimer = (run, delay) => {
  const handle = setTimeout(run, delay);
  return () => clearTimeout(handle);
};

interface Failure {
  /** The lookup answered with a rate limit rather than failing. */
  readonly rateLimited: boolean;
  /** Failed lookups so far; the timed retry happens after the first only. */
  readonly attempts: number;
  /** Whether the row may be looked up again. */
  due: boolean;
}

/**
 * Fills in the CI status of listed reviews, one request each, for the rows
 * near the viewport only.
 *
 * A list read leaves a review's CI `unknown` when the forge would need a
 * request per row to know it. `request` names the rows near the viewport now,
 * in the order shown: their lookups are queued in that order, and queued rows
 * that are no longer named are dropped before their lookup starts. Rows never
 * named are never looked up.
 *
 * A failed lookup shows as unavailable, not unknown, and is retried once
 * after a delay, or again whenever its row comes back near the viewport. A
 * lookup that meets a rate limit pauses every lookup of this list until the
 * limit resets.
 *
 * `reset` starts a new generation: it cancels the lookups in flight and drops
 * the queue and every status found so far, so a late result of an older list
 * never lands on a newer one.
 */
export class CILookups {
  private generation = 0;
  private changes = 0;
  // Reads started and not yet settled, across generations: a cancelled read
  // still holds a pending slot in the main process until it settles.
  private unsettled = 0;
  private queue: string[] = [];
  private readonly running = new Set<string>();
  private readonly tokens = new Set<string>();
  private readonly found = new Map<string, CIStatusValue>();
  private readonly failed = new Map<string, Failure>();
  private readonly fresh = new Set<string>();
  private near: readonly ReviewListItemDto[] = [];
  private nearHandles = new Set<string>();
  private readonly retryTimers = new Set<() => void>();
  private resumeAt: number | null = null;
  private cancelPause: (() => void) | null = null;
  private readonly concurrency: number;
  private readonly retryDelayMs: number;
  private readonly now: () => number;
  private readonly timer: CITimer;

  constructor(
    private readonly bridge: DesktopBridge,
    private readonly changed: () => void,
    options: CILookupOptions = {},
  ) {
    this.concurrency = options.concurrency ?? CI_LOOKUP_CONCURRENCY;
    this.retryDelayMs = options.retryDelayMs ?? CI_RETRY_DELAY_MS;
    this.now = options.now ?? Date.now;
    this.timer = options.timer ?? browserTimer;
  }

  /** Bumped by every change a row could show. */
  get version(): number {
    return this.changes;
  }

  /** When lookups resume after a rate limit (epoch ms), or null. */
  get pausedUntil(): number | null {
    return this.resumeAt;
  }

  /** How many rows near the viewport are queued or being looked up. */
  get waiting(): number {
    return this.queue.length + this.running.size;
  }

  /** The status a lookup found for this row, if it has finished. */
  status(handle: string): CIStatusValue | undefined {
    return this.found.get(handle);
  }

  /** Whether the row has no status yet. */
  pending(item: ReviewListItemDto): boolean {
    return item.summary.ci_status === "unknown" && !this.found.has(item.handle);
  }

  /** The CI cell to show for a row. */
  shown(item: ReviewListItemDto): CIShown {
    if (item.summary.ci_status !== "unknown") return item.summary.ci_status;
    const status = this.found.get(item.handle);
    if (status !== undefined) return status;
    if (this.failed.has(item.handle) || this.resumeAt !== null)
      return CI_UNAVAILABLE;
    return null;
  }

  /** Whether the row's cell is unavailable because of a rate limit. */
  rateLimited(item: ReviewListItemDto): boolean {
    return (
      this.shown(item) === CI_UNAVAILABLE &&
      (this.resumeAt !== null ||
        this.failed.get(item.handle)?.rateLimited === true)
    );
  }

  /**
   * Mark rows of an explicit refresh, whose lookups read past the service's
   * CI cache. Rows loaded later, as by load more, are not marked.
   */
  markFresh(handles: Iterable<string>): void {
    for (const handle of handles) this.fresh.add(handle);
  }

  /**
   * Name the rows near the viewport now, in the order shown. Their lookups
   * are queued in that order; queued rows not named any more are dropped.
   */
  request(items: readonly ReviewListItemDto[]): void {
    const handles = new Set<string>();
    for (const item of items) {
      handles.add(item.handle);
      // A failed row coming back near the viewport is tried again.
      const failure = this.failed.get(item.handle);
      if (failure && !failure.rateLimited && !this.nearHandles.has(item.handle))
        failure.due = true;
    }
    this.near = items;
    this.nearHandles = handles;
    this.rebuild();
  }

  /** Start over for a new list. */
  reset(): void {
    const hadResults = this.found.size > 0 || this.failed.size > 0;
    this.cancel();
    this.found.clear();
    this.failed.clear();
    this.fresh.clear();
    this.near = [];
    this.nearHandles = new Set();
    if (hadResults) this.notify();
  }

  /** Cancel every lookup and timer, as when the list goes away. */
  stop(): void {
    this.reset();
    this.cancelPause?.();
    this.cancelPause = null;
    this.resumeAt = null;
  }

  private cancel(): void {
    this.generation += 1;
    this.queue = [];
    this.running.clear();
    for (const token of this.tokens)
      void this.bridge.cancelRead(token).catch(() => false);
    this.tokens.clear();
    for (const cancel of this.retryTimers) cancel();
    this.retryTimers.clear();
  }

  private wants(item: ReviewListItemDto): boolean {
    if (!this.pending(item) || this.running.has(item.handle)) return false;
    const failure = this.failed.get(item.handle);
    return failure === undefined || failure.due;
  }

  private rebuild(): void {
    this.queue =
      this.resumeAt === null
        ? this.near
            .filter((item) => this.wants(item))
            .map((item) => item.handle)
        : [];
    this.pump();
  }

  private pump(): void {
    while (this.unsettled < this.concurrency && this.queue.length > 0) {
      const handle = this.queue.shift() as string;
      void this.lookup(handle, this.generation);
    }
  }

  private notify(): void {
    this.changes += 1;
    this.changed();
  }

  private async lookup(handle: string, generation: number): Promise<void> {
    this.unsettled += 1;
    this.running.add(handle);
    const failure = this.failed.get(handle);
    if (failure) failure.due = false;
    let token: string | null = null;
    let outcome: {
      readonly status: CIStatusValue;
      readonly wait: number | null;
    } | null;
    try {
      const read = this.bridge.getReviewCIStatus(
        this.fresh.has(handle)
          ? { review: handle, fresh: true }
          : { review: handle },
      );
      token = read.requestToken;
      if (generation === this.generation) this.tokens.add(token);
      const result = await read.result;
      const wait = result.retry_after_seconds;
      outcome = {
        status: result.ci_status,
        wait: typeof wait === "number" && wait > 0 ? wait : null,
      };
    } catch {
      // A rate limit is a result; anything else is a failure to show.
      outcome = null;
    } finally {
      this.unsettled -= 1;
      if (token !== null) this.tokens.delete(token);
    }
    if (generation !== this.generation) {
      // The slot this cancelled read held is free for the current list.
      this.pump();
      return;
    }
    this.running.delete(handle);
    if (outcome !== null && outcome.wait === null) {
      this.found.set(handle, outcome.status);
      this.failed.delete(handle);
      this.fresh.delete(handle);
    } else if (outcome !== null) {
      this.failed.set(handle, {
        rateLimited: true,
        attempts: failure?.attempts ?? 0,
        due: false,
      });
      this.pause(outcome.wait as number);
    } else {
      const attempts = (failure?.attempts ?? 0) + 1;
      this.failed.set(handle, { rateLimited: false, attempts, due: false });
      if (attempts === 1) this.retryLater(handle, generation);
    }
    this.notify();
    this.rebuild();
  }

  private retryLater(handle: string, generation: number): void {
    const cancel = this.timer(() => {
      this.retryTimers.delete(cancel);
      if (generation !== this.generation) return;
      const failure = this.failed.get(handle);
      if (!failure || failure.rateLimited) return;
      failure.due = true;
      this.rebuild();
    }, this.retryDelayMs);
    this.retryTimers.add(cancel);
  }

  /** Hold every lookup of this list until a rate limit resets. */
  private pause(seconds: number): void {
    const until = this.now() + Math.max(1, seconds) * 1000;
    if (this.resumeAt !== null && this.resumeAt >= until) return;
    this.resumeAt = until;
    this.queue = [];
    this.cancelPause?.();
    this.cancelPause = this.timer(
      () => {
        this.cancelPause = null;
        this.resumeAt = null;
        for (const failure of this.failed.values())
          if (failure.rateLimited) failure.due = true;
        this.notify();
        this.rebuild();
      },
      Math.max(0, until - this.now()),
    );
  }
}
