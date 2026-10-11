import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import type {
  DesktopBridge,
  ReviewFailureDto,
  ReviewListItemDto,
  ReviewListResult,
} from "../../../shared/bridge.js";
import type { AppRoute, FeatureContribution } from "../../core/navigation.js";
import {
  formatDate,
  RendererReadError,
  safeError,
  serviceErrorOf,
} from "../../core/presentation.js";
import { StaleQueryError, type CoordinatedRead } from "../../core/query.js";
import { useRetainedRead } from "../../core/use-read.js";
import { CI_UNAVAILABLE, CILookups, type CIShown } from "./ci.js";
import {
  appendPage,
  feedFailures,
  firstPageArrived,
  hasMorePages,
  loadingFeedCount,
  mergedReviewItems,
  nextRepositoryToLoad,
  orderedReviewItems,
  pendingFeed,
  type RepositoryFeed,
} from "./paging.js";

export {
  CI_LOOKUP_CONCURRENCY,
  CI_RETRY_DELAY_MS,
  CI_UNAVAILABLE,
  CILookups,
  type CIShown,
} from "./ci.js";
export {
  mergedReviewItems,
  nextRepositoryToLoad,
  orderedReviewItems,
  orderWatermark,
  type RepositoryFeed,
} from "./paging.js";

export type ReviewScope = "my_reviews" | "my_mrs" | "all_open";
export type ReviewSort = "updated" | "title" | "ci" | "author";

const REVIEW_SCOPES: readonly {
  readonly value: ReviewScope;
  readonly label: string;
}[] = Object.freeze([
  { value: "my_reviews", label: "My Reviews" },
  { value: "my_mrs", label: "My MRs" },
  { value: "all_open", label: "All Open" },
]);

const REVIEW_SORTS: readonly {
  readonly value: ReviewSort;
  readonly label: string;
}[] = Object.freeze([
  { value: "updated", label: "Updated" },
  { value: "title", label: "Title" },
  { value: "ci", label: "CI status" },
  { value: "author", label: "Author" },
]);

export interface InboxListSelection {
  readonly scope: ReviewScope;
  readonly state: "open" | "closed";
  readonly sort: ReviewSort;
  readonly selected: string | null;
}

const DEFAULT_LIST_SELECTION: InboxListSelection = Object.freeze({
  scope: "all_open",
  state: "open",
  sort: "updated",
  selected: null,
});

/**
 * Session memory of each review list, keyed by repository scope. Opening a
 * review unmounts the list, so without this the chosen scope, state, sort and
 * the review the user was on are all lost on the most repeated navigation in
 * the app. It records only presentation choices; the scope values handed to
 * `listReviews` are unchanged.
 */
const listSelections = new Map<string, InboxListSelection>();

/**
 * Review the list should hand focus back to, recorded only when a card
 * navigates away and consumed by the first restore that finds it. Keeping this
 * apart from `selected` is what makes the restore one-shot: `selected` outlives
 * it for the current-row marking, so a filter change, which remounts the
 * result list, must not be able to pull focus out of the control just used.
 */
const pendingSelectionFocus = new Map<string, string>();

/**
 * How many pages each repository had loaded in each review list, keyed by the
 * list's read key and then by repository. Opening a review unmounts the list
 * and Refresh replaces its feeds; both read each repository again down to this
 * depth, so a review opened from a later page is still listed on return.
 */
const loadedDepths = new Map<string, Map<string, number>>();

/**
 * Deepest a reload goes per repository. Each page is one forge request, so a
 * long scroll is not replayed in full on every refresh; a review past this
 * depth is found again by scrolling.
 */
export const MAX_RESTORED_PAGES = 10;

export function inboxListSelection(key: string): InboxListSelection {
  return listSelections.get(key) ?? DEFAULT_LIST_SELECTION;
}

export function inboxPendingSelectionFocus(key: string): string | null {
  return pendingSelectionFocus.get(key) ?? null;
}

export function inboxLoadedDepth(readKey: string, repository: string): number {
  return loadedDepths.get(readKey)?.get(repository) ?? 0;
}

function rememberLoadedDepth(
  readKey: string,
  feeds: readonly RepositoryFeed[],
): void {
  let depths = loadedDepths.get(readKey);
  for (const feed of feeds) {
    if (feed.pages <= (depths?.get(feed.repository) ?? 0)) continue;
    if (!depths) {
      depths = new Map();
      loadedDepths.set(readKey, depths);
    }
    depths.set(feed.repository, feed.pages);
  }
}

export function resetInboxListSelections(): void {
  listSelections.clear();
  pendingSelectionFocus.clear();
  loadedDepths.clear();
}

const CI_PRIORITY: Readonly<Record<string, number>> = Object.freeze({
  failed: 0,
  running: 1,
  pending: 2,
  success: 3,
  canceled: 4,
  skipped: 5,
  unknown: 6,
});

/** The CI cell of a row whose lookup has not finished yet. */
export const CI_LOADING_TEXT = "…";

/** The CI cell of a row whose lookup failed or waits for a rate limit. */
export const CI_UNAVAILABLE_TEXT = "n/a";

/**
 * Rows within about one screen above or below the list's scroll area count
 * as near the viewport, and only those are looked up.
 */
export const CI_NEAR_MARGIN = "100% 0px 100% 0px";

/** Without an IntersectionObserver, rows near the top that count as near. */
export const CI_UNOBSERVED_ROWS = 30;

/**
 * In CI order, how long the arriving statuses must stay quiet (and no
 * lookup of a row near the viewport be waiting) before the list re-sorts.
 */
export const CI_SORT_SETTLE_MS = 250;

export function createInboxFeature(): FeatureContribution {
  return {
    id: "reviews.inbox",
    order: 10,
    matches: (route) => route.kind === "inbox",
    render: (context, route) =>
      route.kind === "inbox" ? (
        <InboxView
          key={route.repository?.handle ?? "all"}
          bridge={context.bridge}
          queries={context.queries}
          route={route}
          repositories={context.repositories}
          repositoriesReady={context.repositoriesReady}
          repositoriesError={context.repositoriesError ?? null}
          retryDiscovery={context.retryDiscovery}
          repositoryGeneration={context.repositoryGeneration}
          navigate={context.navigate}
        />
      ) : null,
  };
}

function InboxView({
  bridge,
  queries,
  route,
  repositories,
  repositoriesReady,
  repositoriesError,
  retryDiscovery,
  repositoryGeneration,
  navigate,
}: {
  readonly bridge: DesktopBridge;
  readonly queries: FeatureParameters["queries"];
  readonly route: Extract<AppRoute, { kind: "inbox" }>;
  readonly repositories: FeatureParameters["repositories"];
  readonly repositoriesReady: boolean;
  readonly repositoriesError: string | null;
  readonly retryDiscovery: (() => void) | undefined;
  readonly repositoryGeneration: number;
  readonly navigate: (route: AppRoute) => void;
}): ReactNode {
  const repository = route.repository;
  const listKey = repository?.handle ?? "all";
  const [listSelection, setListSelection] = useState<InboxListSelection>(() =>
    inboxListSelection(listKey),
  );
  const [focusTarget, setFocusTarget] = useState<string | null>(() =>
    inboxPendingSelectionFocus(listKey),
  );
  const retain = (change: Partial<InboxListSelection>): void => {
    const next = Object.freeze({ ...listSelection, ...change });
    listSelections.set(listKey, next);
    setListSelection(next);
  };
  const releaseFocusTarget = (): void => {
    pendingSelectionFocus.delete(listKey);
    setFocusTarget(null);
  };
  const changeFilter = (change: Partial<InboxListSelection>): void => {
    releaseFocusTarget();
    retain(change);
  };
  const openReview = (handle: string): void => {
    pendingSelectionFocus.set(listKey, handle);
    retain({ selected: handle });
  };
  const reviewScope = listSelection.scope;
  const reviewState = listSelection.state;
  const reviewSort = listSelection.sort;
  const selectScope = (next: ReviewScope): void => {
    changeFilter(
      next === "all_open" ? { scope: next } : { scope: next, state: "open" },
    );
  };
  const queryIdentity = `${repository?.handle ?? "all"}:${reviewScope}:${reviewState}`;
  const title = repository?.display_name ?? "All reviews";
  return (
    <>
      <header className="view-header">
        <div>
          <p className="eyebrow">{repository?.forge_type ?? "Workspace"}</p>
          <h1 className="view-title">{title}</h1>
        </div>
      </header>
      <div className="inbox-controls" aria-label="Review list controls">
        <fieldset className="control-group review-scope-control">
          <legend>Review scope</legend>
          <div className="segmented-control">
            {REVIEW_SCOPES.map((option) => (
              <button
                key={option.value}
                className={`button ${reviewScope === option.value ? "button-active" : "button-secondary"}`}
                data-review-scope={option.value}
                aria-pressed={reviewScope === option.value}
                onClick={() => selectScope(option.value)}
              >
                {option.label}
              </button>
            ))}
          </div>
        </fieldset>
        <fieldset className="control-group review-state-control">
          <legend>Review state</legend>
          <div className="segmented-control">
            <button
              className={`button ${reviewState === "open" ? "button-active" : "button-secondary"}`}
              data-review-state="open"
              aria-pressed={reviewState === "open"}
              onClick={() => changeFilter({ state: "open" })}
            >
              Open
            </button>
            <button
              className={`button ${reviewState === "closed" ? "button-active" : "button-secondary"}`}
              data-review-state="closed"
              aria-pressed={reviewState === "closed"}
              disabled={reviewScope !== "all_open"}
              title={
                reviewScope === "all_open"
                  ? undefined
                  : "Closed and merged reviews are available in All Open."
              }
              onClick={() => changeFilter({ state: "closed" })}
            >
              Closed &amp; merged
            </button>
          </div>
        </fieldset>
        <label className="control-field">
          <span>Sort reviews</span>
          <select
            value={reviewSort}
            onChange={(event) =>
              changeFilter({ sort: event.currentTarget.value as ReviewSort })
            }
          >
            {REVIEW_SORTS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <InboxResults
        key={queryIdentity}
        bridge={bridge}
        queries={queries}
        repository={repository}
        repositories={repositories}
        repositoriesReady={repositoriesReady}
        repositoriesError={repositoriesError}
        retryDiscovery={retryDiscovery}
        repositoryGeneration={repositoryGeneration}
        reviewScope={reviewScope}
        reviewState={reviewState}
        reviewSort={reviewSort}
        selected={listSelection.selected}
        select={openReview}
        focusTarget={focusTarget}
        releaseFocusTarget={releaseFocusTarget}
        navigate={navigate}
      />
    </>
  );
}

function InboxResults({
  bridge,
  queries,
  repository,
  repositories,
  repositoriesReady,
  repositoriesError,
  retryDiscovery,
  repositoryGeneration,
  reviewScope,
  reviewState,
  reviewSort,
  selected,
  select,
  focusTarget,
  releaseFocusTarget,
  navigate,
}: {
  readonly bridge: DesktopBridge;
  readonly queries: FeatureParameters["queries"];
  readonly repository: Extract<AppRoute, { kind: "inbox" }>["repository"];
  readonly repositories: FeatureParameters["repositories"];
  readonly repositoriesReady: boolean;
  readonly repositoriesError: string | null;
  readonly retryDiscovery: (() => void) | undefined;
  readonly repositoryGeneration: number;
  readonly reviewScope: ReviewScope;
  readonly reviewState: "open" | "closed";
  readonly reviewSort: ReviewSort;
  readonly selected: string | null;
  readonly select: (handle: string) => void;
  readonly focusTarget: string | null;
  readonly releaseFocusTarget: () => void;
  readonly navigate: (route: AppRoute) => void;
}): ReactNode {
  const readKey = `inbox:${repository?.handle ?? "all"}:${reviewScope}:${reviewState}`;
  // The combined read queues repositories it has not started yet, and the
  // query coordinator can only cancel reads that already have a token, so the
  // queue is stopped through this controller whenever the read is replaced,
  // disabled or unmounted.
  const discoveredRead = useRef<AbortController | null>(null);
  // Bumped by every new combined read, so a page that belongs to an older
  // read (or a later page requested before a refresh) is dropped.
  const readGeneration = useRef(0);
  // Page reads in flight past the first page, by query key to repository.
  const moreReads = useRef(new Map<string, string>());
  // Every page loaded so far, one entry per repository in discovery order.
  const [feeds, setFeeds] = useState<readonly RepositoryFeed[] | null>(null);
  const feedsRef = useRef(feeds);
  feedsRef.current = feeds;
  // The first read fills `feeds` as each repository's first page arrives. A
  // refresh keeps showing the previous pages and swaps them in when complete.
  const liveRead = useRef(false);
  const hasValue = useRef(false);
  // Set by the Refresh button and consumed by the read it starts, so only an
  // explicit refresh reads past the service's list cache.
  const freshRead = useRef(false);
  // Whether the read whose pages are swapped in when it completes was an
  // explicit refresh, so its CI lookups read past the CI cache too.
  const swapFresh = useRef(false);
  // Rows arrive without CI where the forge would need a request per row, and
  // each shown row's status is filled in here. Every new list starts a new
  // generation, so a late lookup never lands on a newer list.
  const [, setCIVersion] = useState(0);
  const ciLookups = useRef<CILookups | null>(null);
  if (ciLookups.current === null)
    ciLookups.current = new CILookups(bridge, () =>
      setCIVersion((current) => current + 1),
    );
  const ci = ciLookups.current;
  const cancelMoreReads = useCallback(() => {
    const cancelled = new Set(moreReads.current.values());
    for (const key of moreReads.current.keys()) void queries.cancel(key);
    moreReads.current.clear();
    if (cancelled.size === 0) return;
    // A cancelled page read never settles into its feed, so clear its loading
    // flag here. Otherwise a refresh that fails keeps the old feeds on screen
    // with that repository stuck loading and Load more disabled.
    setFeeds((current) =>
      current?.some((feed) => feed.loading && cancelled.has(feed.repository))
        ? Object.freeze(
            current.map((feed) =>
              feed.loading && cancelled.has(feed.repository)
                ? Object.freeze({ ...feed, loading: false })
                : feed,
            ),
          )
        : current,
    );
  }, [queries]);
  const begin = useCallback(() => {
    discoveredRead.current?.abort();
    discoveredRead.current = null;
    cancelMoreReads();
    const generation = ++readGeneration.current;
    const controller = new AbortController();
    discoveredRead.current = controller;
    const sources = repository ? [repository] : repositories;
    const fresh = freshRead.current;
    freshRead.current = false;
    const live = !hasValue.current;
    liveRead.current = live;
    swapFresh.current = fresh;
    if (live) {
      ci.reset();
      setFeeds(
        Object.freeze(sources.map((source) => pendingFeed(source.handle))),
      );
    }
    const depths = new Map(loadedDepths.get(readKey) ?? []);
    return listDiscoveredReviews(
      bridge,
      sources,
      reviewScope,
      reviewState,
      controller.signal,
      live
        ? (index, feed) => {
            if (generation !== readGeneration.current) return;
            // Rows of an explicit refresh read past the CI cache; rows that
            // load more adds later do not.
            if (fresh) ci.markFresh(feed.items.map((item) => item.handle));
            setFeeds((current) =>
              current && current[index]?.repository === feed.repository
                ? Object.freeze(
                    current.map((item, at) => (at === index ? feed : item)),
                  )
                : current,
            );
          }
        : undefined,
      depths,
      fresh,
    );
  }, [
    bridge,
    cancelMoreReads,
    ci,
    readKey,
    repositories,
    repository,
    reviewScope,
    reviewState,
  ]);
  const readDependencies = [
    repository?.handle,
    repositoryGeneration,
    reviewScope,
    reviewState,
  ];
  const state = useRetainedRead(
    queries,
    readKey,
    begin,
    readDependencies,
    repositoriesReady,
  );
  const { refresh } = state;
  const refreshFresh = useCallback(() => {
    freshRead.current = true;
    refresh();
  }, [refresh]);
  useEffect(() => {
    if (feeds) rememberLoadedDepth(readKey, feeds);
  }, [feeds, readKey]);
  useEffect(() => {
    if (!state.value) return;
    hasValue.current = true;
    // A live read already holds these pages and any loaded after them.
    if (!liveRead.current) {
      // The refreshed rows replace the shown ones, so their CI is looked up
      // again and lookups still running for the old rows land nowhere. Only
      // the rows this refresh read look CI up past the cache.
      ci.reset();
      if (swapFresh.current)
        ci.markFresh(
          state.value.feeds.flatMap((feed) =>
            feed.items.map((item) => item.handle),
          ),
        );
      setFeeds(state.value.feeds);
    }
    liveRead.current = false;
  }, [state.value]);
  // Leaving the list (a tab or state change remounts it) cancels its lookups.
  useEffect(() => () => ci.stop(), [ci]);
  useEffect(
    () => () => {
      discoveredRead.current?.abort();
      discoveredRead.current = null;
      cancelMoreReads();
    },
    [...readDependencies, repositoriesReady],
  );

  const loadMore = useCallback(
    (target: string) => {
      const key = `${readKey}:more:${target}`;
      const feed = feedsRef.current?.find((item) => item.repository === target);
      if (
        moreReads.current.has(key) ||
        !feed ||
        feed.cursor === null ||
        feed.loading
      )
        return;
      const generation = readGeneration.current;
      const cursor = feed.cursor;
      moreReads.current.set(key, target);
      const update = (
        change: (current: RepositoryFeed) => RepositoryFeed,
      ): void =>
        setFeeds((current) => {
          const found = current?.find((item) => item.repository === target);
          return current && found
            ? replaceFeed(current, change(found))
            : current;
        });
      const settle = (
        change: (current: RepositoryFeed) => RepositoryFeed,
      ): void => {
        if (generation !== readGeneration.current) return;
        moreReads.current.delete(key);
        update(change);
      };
      update((current) => ({ ...current, loading: true }));
      void queries
        .run(key, () =>
          bridge.listReviews({
            scope: reviewScope,
            state: reviewState,
            repository: target,
            cursor,
          }),
        )
        .then(
          (page) =>
            settle((current) =>
              appendPage(
                current,
                page.items,
                page.next_cursor ?? null,
                page.failures,
              ),
            ),
          (error: unknown) =>
            settle((current) =>
              error instanceof StaleQueryError
                ? { ...current, loading: false }
                : {
                    ...current,
                    loading: false,
                    failures: [repositoryFailure(target, error)],
                  },
            ),
        );
    },
    [bridge, queries, readKey, reviewScope, reviewState],
  );

  const shown = state.error && !state.value ? null : feeds;
  // A discovery that failed before any succeeded leaves nothing to read, so the
  // pane says why and offers the retry instead of waiting for it forever.
  const discoveryFailed = !repositoriesReady && repositoriesError !== null;
  // With no repositories there is no feed to settle, so the list (and its
  // empty label) is shown once the read itself has finished.
  const listReady =
    shown !== null &&
    (shown.some(firstPageArrived) ||
      (shown.length === 0 && !state.loading && Boolean(state.value)));
  return (
    <>
      <div className="view-actions">
        <button
          className="button button-secondary"
          disabled={state.loading}
          onClick={refreshFresh}
        >
          {state.loading && state.value ? "Refreshing…" : "Refresh reviews"}
        </button>
      </div>
      {discoveryFailed && (
        <>
          <Notice kind="error">{repositoriesError}</Notice>
          {retryDiscovery && (
            <button
              className="button button-secondary discovery-retry"
              onClick={retryDiscovery}
            >
              Retry
            </button>
          )}
        </>
      )}
      {!discoveryFailed && state.loading && !shown?.some(firstPageArrived) && (
        <Notice kind="loading">
          {repositoriesReady
            ? "Loading reviews from the local service…"
            : "Waiting for local repository discovery…"}
        </Notice>
      )}
      {Boolean(state.error) && (
        <Notice kind="error">
          {state.value
            ? "Refresh failed. Showing the previous review list."
            : safeError(state.error)}
        </Notice>
      )}
      {listReady && shown !== null && (
        <ReviewList
          feeds={shown}
          navigate={navigate}
          sort={reviewSort}
          selected={selected}
          select={select}
          focusTarget={focusTarget}
          releaseFocusTarget={releaseFocusTarget}
          emptyLabel={emptyReviewLabel(reviewScope, reviewState)}
          loadMore={loadMore}
          ci={ci}
        />
      )}
    </>
  );
}

function replaceFeed(
  feeds: readonly RepositoryFeed[],
  next: RepositoryFeed,
): readonly RepositoryFeed[] {
  return Object.freeze(
    feeds.map((feed) =>
      feed.repository === next.repository ? Object.freeze(next) : feed,
    ),
  );
}

/**
 * Most repository reads the combined inbox keeps in flight at once. The main
 * process refuses a read once 64 requests are pending, so fanning out one read
 * per discovered repository at once fails every tab for larger workspaces.
 */
export const DISCOVERED_REVIEW_CONCURRENCY = 8;

/**
 * Read one review list per discovered repository through a small pool and
 * merge them in repository order. `depths` asks for more than the first page
 * of a repository, up to `MAX_RESTORED_PAGES`. A repository whose read is refused or fails
 * becomes one entry in `failures` instead of rejecting the whole list.
 *
 * `requestTokens` grows as reads start, so a coordinator cancelling this read
 * reaches every read in flight at that moment. Aborting `signal` also cancels
 * the reads in flight, starts none of the queued ones, and rejects the result.
 * `fresh` marks each repository's first page read as an explicit refresh, so
 * the service reads it from the forge instead of its list cache.
 */
export function listDiscoveredReviews(
  bridge: DesktopBridge,
  repositories: readonly { readonly handle: string }[],
  scope: ReviewScope,
  state: "open" | "closed",
  signal?: AbortSignal,
  onRepository?: (index: number, feed: RepositoryFeed) => void,
  depths?: ReadonlyMap<string, number>,
  fresh = false,
): CoordinatedRead<DiscoveredReviews> {
  const handles = repositories.map((repository) => repository.handle);
  const requestTokens: string[] = [];
  const inFlight = new Set<string>();
  const outcomes: ReviewListResult[] = [];
  const feeds: RepositoryFeed[] = handles.map((handle) => pendingFeed(handle));
  // Set when a read comes back cancelled without our signal firing, for
  // example when the coordinator cancels every read on a cache clear.
  let stopped = false;
  const aborted = (): boolean => signal?.aborted === true;

  // Reads one page of a repository and appends it. A failed read becomes a
  // failure on the feed, which keeps its cursor so the page can be retried.
  const readPage = async (
    handle: string,
    feed: RepositoryFeed,
    cursor?: string,
  ): Promise<RepositoryFeed> => {
    let token: string | null = null;
    let outcome: ReviewListResult;
    try {
      const read = bridge.listReviews(
        cursor !== undefined
          ? { scope, state, repository: handle, cursor }
          : fresh
            ? { scope, state, repository: handle, fresh: true }
            : { scope, state, repository: handle },
      );
      token = read.requestToken;
      requestTokens.push(token);
      inFlight.add(token);
      outcome = await read.result;
    } catch (error) {
      if (serviceErrorOf(error)?.code === "request_cancelled") stopped = true;
      outcome = { items: [], failures: [repositoryFailure(handle, error)] };
    } finally {
      if (token !== null) inFlight.delete(token);
    }
    return appendPage(
      feed,
      outcome.items,
      outcome.next_cursor ?? null,
      outcome.failures,
    );
  };
  // Reads one page of a repository. When the list had loaded that repository
  // deeper before it was reloaded, the next page goes to the back of the queue
  // as its own task, so every repository's first page is read before any
  // deeper page and a deep repository never holds back a shallow one. The rows
  // read so far are shown meanwhile, marked as still loading.
  const readRepository = async (
    index: number,
    feed: RepositoryFeed,
    cursor?: string,
  ): Promise<void> => {
    const handle = handles[index] as string;
    const depth = Math.min(depths?.get(handle) ?? 1, MAX_RESTORED_PAGES);
    const next = await readPage(handle, feed, cursor);
    feeds[index] = next;
    const deeper = next.cursor;
    if (
      next.pages < depth &&
      deeper !== null &&
      next.failures.length === 0 &&
      !aborted() &&
      !stopped
    ) {
      onRepository?.(index, Object.freeze({ ...next, loading: true }));
      queue.push(() => readRepository(index, next, deeper));
      return;
    }
    outcomes[index] = { items: next.items, failures: next.failures };
    // Each repository is shown as soon as its last restored page arrives.
    if (!aborted() && !stopped) onRepository?.(index, next);
  };
  const queue: (() => Promise<void>)[] = handles.map(
    (_handle, index) => () =>
      readRepository(index, feeds[index] as RepositoryFeed),
  );
  let active = 0;
  let drain: () => void = () => undefined;
  const drained = new Promise<void>((resolve) => {
    drain = resolve;
  });
  // Starts queued reads up to the pool size; settles once none is left to run.
  const pump = (): void => {
    while (
      active < DISCOVERED_REVIEW_CONCURRENCY &&
      queue.length > 0 &&
      !aborted() &&
      !stopped
    ) {
      const task = queue.shift() as () => Promise<void>;
      active += 1;
      void task()
        .catch(() => undefined)
        .then(() => {
          active -= 1;
          pump();
        });
    }
    if (active === 0) drain();
  };

  const result = new Promise<DiscoveredReviews>((resolve, reject) => {
    const cancelled = (): RendererReadError =>
      new RendererReadError(
        "request_cancelled",
        "The review list read was cancelled.",
        false,
      );
    if (aborted()) {
      reject(cancelled());
      return;
    }
    const onAbort = (): void => {
      for (const token of inFlight)
        void bridge.cancelRead(token).catch(() => false);
      reject(cancelled());
    };
    signal?.addEventListener("abort", onAbort, { once: true });
    pump();
    void drained.then(() => {
      signal?.removeEventListener("abort", onAbort);
      if (aborted()) return;
      if (stopped) {
        reject(cancelled());
        return;
      }
      const items = outcomes.flatMap((outcome) => outcome.items);
      const failures = outcomes.flatMap((outcome) => outcome.failures);
      // Every repository failing usually means the service itself is down,
      // so surface the error state rather than an empty list.
      const first = failures[0];
      if (
        items.length === 0 &&
        first !== undefined &&
        failures.length === handles.length
      ) {
        reject(
          new RendererReadError(first.code, first.message, first.retryable),
        );
        return;
      }
      resolve({ items, failures, feeds: Object.freeze([...feeds]) });
    });
  });
  return { requestTokens, result };
}

/** A combined read: the flat list plus what each repository has loaded. */
export interface DiscoveredReviews extends ReviewListResult {
  readonly feeds: readonly RepositoryFeed[];
}

function repositoryFailure(
  repository: string,
  error: unknown,
): ReviewFailureDto {
  const failure = serviceErrorOf(error);
  return Object.freeze({
    repository,
    code: failure?.code ?? "read_failed",
    message:
      failure?.message ?? "The local service could not complete this read.",
    retryable: failure?.retryable ?? true,
  });
}

type FeatureParameters = Parameters<FeatureContribution["render"]>[0];

function ReviewList({
  feeds,
  navigate,
  sort,
  selected,
  select,
  focusTarget,
  releaseFocusTarget,
  emptyLabel,
  loadMore,
  ci,
}: {
  readonly feeds: readonly RepositoryFeed[];
  readonly navigate: (route: AppRoute) => void;
  readonly sort: ReviewSort;
  readonly selected: string | null;
  readonly select: (handle: string) => void;
  readonly focusTarget: string | null;
  readonly releaseFocusTarget: () => void;
  readonly emptyLabel: string;
  readonly loadMore: (repository: string) => void;
  readonly ci: CILookups;
}): ReactNode {
  // In CI order the list sorts by the statuses known when lookups last
  // settled, so rows do not jump on every arriving result.
  const ciOrder = useRef<ReadonlyMap<string, string> | null>(null);
  const [, setOrderVersion] = useState(0);
  if (sort !== "ci") ciOrder.current = null;
  else if (ciOrder.current === null)
    ciOrder.current = settledCIOrder(mergedReviewItems(feeds), ci);
  const order = ciOrder.current;
  const presentation = inboxFeedPresentation(
    feeds,
    sort,
    order === null ? ci : orderedCI(order),
  );
  const items = presentation.items;
  const itemsRef = useRef(items);
  itemsRef.current = items;
  const listRef = useRef<HTMLDivElement | null>(null);
  // Rows near the viewport, kept by an IntersectionObserver over the cards.
  // Only these are looked up, in the order shown; rows that update-time order
  // still holds back are looked up once they are shown and come near.
  const nearRows = useRef(new Set<string>());
  const observed = useRef(new Map<Element, string>());
  const rowObserver = useRef<IntersectionObserver | null>(null);
  const syncCI = useCallback(() => {
    const shown = itemsRef.current;
    ci.request(
      rowObserver.current === null
        ? shown.slice(0, CI_UNOBSERVED_ROWS)
        : shown.filter((item) => nearRows.current.has(item.handle)),
    );
  }, [ci]);
  useEffect(() => {
    const Observer = globalThis.IntersectionObserver;
    const list = listRef.current;
    if (typeof Observer !== "function" || !list) return;
    const observer = new Observer(
      (entries) => {
        for (const entry of entries) {
          const handle = observed.current.get(entry.target);
          if (handle === undefined) continue;
          if (entry.isIntersecting) nearRows.current.add(handle);
          else nearRows.current.delete(handle);
        }
        syncCI();
      },
      { root: scrollRoot(list), rootMargin: CI_NEAR_MARGIN },
    );
    rowObserver.current = observer;
    return () => {
      observer.disconnect();
      rowObserver.current = null;
      observed.current.clear();
      nearRows.current.clear();
    };
  }, [syncCI, presentation.empty]);
  // After every render, watch the cards that appeared, forget the ones that
  // went away, and name the rows near the viewport again.
  useEffect(() => {
    const observer = rowObserver.current;
    if (observer !== null) {
      const cards = [
        ...(listRef.current?.querySelectorAll<HTMLElement>(
          "[data-review-handle]",
        ) ?? []),
      ];
      const current = new Set<Element>(cards);
      for (const [card, handle] of observed.current) {
        if (current.has(card)) continue;
        observer.unobserve(card);
        observed.current.delete(card);
        nearRows.current.delete(handle);
      }
      for (const card of cards) {
        const handle = card.dataset.reviewHandle;
        if (handle === undefined || observed.current.has(card)) continue;
        observed.current.set(card, handle);
        observer.observe(card);
      }
    }
    syncCI();
  });
  // Re-sort once the statuses of the rows near the viewport have settled,
  // keeping focus on the row that had it.
  const refocus = useRef<string | null>(null);
  useEffect(() => {
    if (sort !== "ci" || ci.waiting > 0) return;
    const timer = setTimeout(() => {
      const next = settledCIOrder(itemsRef.current, ci);
      if (sameOrder(next, ciOrder.current)) return;
      const active = document.activeElement;
      refocus.current =
        active instanceof HTMLElement && listRef.current?.contains(active)
          ? (active.dataset.reviewHandle ?? null)
          : null;
      ciOrder.current = next;
      setOrderVersion((current) => current + 1);
    }, CI_SORT_SETTLE_MS);
    return () => clearTimeout(timer);
  }, [sort, ci, ci.version, ci.waiting]);
  useLayoutEffect(() => {
    const handle = refocus.current;
    if (handle === null) return;
    refocus.current = null;
    const target = [
      ...(listRef.current?.querySelectorAll<HTMLButtonElement>("button") ?? []),
    ].find((button) => button.dataset.reviewHandle === handle);
    if (target && document.activeElement !== target)
      target.focus({ preventScroll: true });
  });
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const [nearEnd, setNearEnd] = useState(false);
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;
  const selectRef = useRef(select);
  selectRef.current = select;
  const open = useCallback((item: ReviewListItemDto) => {
    selectRef.current(item.handle);
    navigateRef.current({ kind: "review", item, panel: "overview" });
  }, []);
  useEffect(() => {
    if (focusTarget === null) return;
    const target = [
      ...(listRef.current?.querySelectorAll<HTMLButtonElement>("button") ?? []),
    ].find((button) => button.dataset.reviewHandle === focusTarget);
    if (!target) {
      // Rows can arrive after the list mounts, so wait while pages are still
      // loading. Once every read has settled without the review, give the
      // target up: a page loaded later by scrolling must not pull focus.
      if (presentation.loading === 0) releaseFocusTarget();
      return;
    }
    target.focus();
    if (typeof target.scrollIntoView === "function")
      target.scrollIntoView({ block: "nearest" });
    releaseFocusTarget();
  }, [focusTarget, items.length, presentation.loading]);
  // Update-time order loads the next page by itself when the end of the list
  // comes into view. Other orders only load when asked through the button.
  const autoLoad = sort === "updated";
  useEffect(() => {
    const sentinel = sentinelRef.current;
    const Observer = globalThis.IntersectionObserver;
    if (!autoLoad || !sentinel || typeof Observer !== "function") return;
    const observer = new Observer(
      (entries) => setNearEnd(entries.some((entry) => entry.isIntersecting)),
      { rootMargin: "0px 0px 600px 0px" },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [autoLoad, presentation.next !== null]);
  // A repository whose last page read failed is retried only on request, so
  // a persistent failure cannot turn scrolling into a request loop.
  const autoNext =
    presentation.next !== null &&
    feeds.some(
      (feed) =>
        feed.repository === presentation.next && feed.failures.length === 0,
    )
      ? presentation.next
      : null;
  useEffect(() => {
    if (autoLoad && nearEnd && autoNext && presentation.loading === 0)
      loadMore(autoNext);
  }, [autoLoad, nearEnd, autoNext, presentation.loading, loadMore]);
  const moveFocus = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (
      event.key !== "ArrowDown" &&
      event.key !== "ArrowUp" &&
      event.key !== "Home" &&
      event.key !== "End"
    )
      return;
    const buttons = [
      ...event.currentTarget.querySelectorAll<HTMLButtonElement>("button"),
    ];
    const current = buttons.indexOf(
      document.activeElement as HTMLButtonElement,
    );
    const direction = event.key === "ArrowDown" ? 1 : -1;
    const target =
      event.key === "Home"
        ? buttons[0]
        : event.key === "End"
          ? buttons.at(-1)
          : buttons[
              (current < 0 ? 0 : current + direction + buttons.length) %
                buttons.length
            ];
    if (target) {
      event.preventDefault();
      target.focus();
    }
  };
  return (
    <>
      {ci.pausedUntil !== null && (
        <Notice kind="error">{rateLimitText(ci.pausedUntil)}</Notice>
      )}
      {presentation.partialFailures > 0 && (
        <Notice kind="error">
          {presentation.partialFailures} repository read
          {presentation.partialFailures === 1 ? "" : "s"} failed. Available
          reviews are shown below.
        </Notice>
      )}
      {presentation.empty ? (
        <Notice kind="empty">{emptyLabel}</Notice>
      ) : (
        <div
          className="review-list"
          role="list"
          aria-label="Review results"
          ref={listRef}
          onKeyDown={moveFocus}
        >
          {items.map((item) => (
            <ReviewCard
              key={item.handle}
              item={item}
              ci={ci.shown(item)}
              ciLimited={ci.rateLimited(item)}
              selected={item.handle === selected}
              open={open}
            />
          ))}
        </div>
      )}
      <div ref={sentinelRef} className="review-list-end" aria-hidden="true" />
      {(presentation.loading > 0 || presentation.next !== null) && (
        <div className="review-list-more">
          {presentation.loading > 0 && (
            <span role="status" className="review-list-status">
              Loading more from {presentation.loading}{" "}
              {presentation.loading === 1 ? "repository" : "repositories"}
            </span>
          )}
          {presentation.next !== null && (
            <button
              className="button button-secondary"
              data-load-more={presentation.next}
              disabled={presentation.loading > 0}
              onClick={() => {
                if (presentation.next) loadMore(presentation.next);
              }}
            >
              Load more
            </button>
          )}
        </div>
      )}
    </>
  );
}

/**
 * What the list shows for the pages loaded so far: the rows in the chosen
 * order, how many repositories are still reading, and which repository to
 * read next. In update-time order rows stop at the point where the order
 * across repositories is known.
 */
export function inboxFeedPresentation(
  feeds: readonly RepositoryFeed[],
  sort: ReviewSort,
  ci?: CIOrder,
): {
  readonly items: readonly ReviewListItemDto[];
  readonly empty: boolean;
  readonly partialFailures: number;
  readonly loading: number;
  readonly next: string | null;
} {
  const items =
    sort === "updated"
      ? orderedReviewItems(feeds)
      : sortReviewItems(mergedReviewItems(feeds), sort, ci);
  const loading = loadingFeedCount(feeds);
  return Object.freeze({
    items,
    empty: items.length === 0 && loading === 0 && !hasMorePages(feeds),
    partialFailures: feedFailures(feeds).length,
    loading,
    next: nextRepositoryToLoad(feeds)?.repository ?? null,
  });
}

/** The CI status a row sorts by, or null while it has none. */
export type CIOrder = {
  readonly shown: (item: ReviewListItemDto) => string | null;
};

/**
 * The CI statuses lookups have found so far, which the list sorts by until
 * they settle again. Rows whose status came with the list are left out: they
 * always sort by their live status, so a refresh that changes one re-sorts it.
 */
function settledCIOrder(
  items: readonly ReviewListItemDto[],
  ci: CILookups,
): ReadonlyMap<string, string> {
  const order = new Map<string, string>();
  for (const item of items) {
    if (item.summary.ci_status !== "unknown") continue;
    const shown = ci.shown(item);
    if (shown !== null && shown !== CI_UNAVAILABLE)
      order.set(item.handle, shown);
  }
  return order;
}

function sameOrder(
  next: ReadonlyMap<string, string>,
  current: ReadonlyMap<string, string> | null,
): boolean {
  if (current === null || current.size !== next.size) return false;
  for (const [handle, status] of next)
    if (current.get(handle) !== status) return false;
  return true;
}

/**
 * Sort a row the list knows by its live status, and a row it left unknown by
 * the status lookups had found when they last settled.
 */
function orderedCI(order: ReadonlyMap<string, string>): CIOrder {
  return {
    shown: (item) =>
      item.summary.ci_status !== "unknown"
        ? item.summary.ci_status
        : (order.get(item.handle) ?? null),
  };
}

/** The nearest scrolling ancestor, which the row observer measures against. */
function scrollRoot(element: HTMLElement): Element | null {
  const view = element.ownerDocument.defaultView;
  if (!view) return null;
  for (
    let parent = element.parentElement;
    parent !== null;
    parent = parent.parentElement
  ) {
    const overflow = view.getComputedStyle(parent).overflowY;
    if (overflow === "auto" || overflow === "scroll") return parent;
  }
  return null;
}

function rateLimitText(until: number): string {
  const time = new Date(until).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
  return `Forge rate limit reached. CI status lookups resume at ${time}.`;
}

/**
 * Sort loaded rows. CI order uses the status a lookup found when the list
 * left it unknown; a row still waiting or unavailable sorts with the unknown
 * ones.
 */
export function sortReviewItems(
  items: readonly ReviewListItemDto[],
  sort: ReviewSort,
  ci?: CIOrder,
): readonly ReviewListItemDto[] {
  const ciOf = (item: ReviewListItemDto): string => {
    const shown = ci ? ci.shown(item) : item.summary.ci_status;
    // A row still loading or unavailable sorts with the unknown ones.
    return shown === null || shown === CI_UNAVAILABLE ? "unknown" : shown;
  };
  const sorted = [...items];
  sorted.sort((left, right) => {
    const leftSummary = left.summary;
    const rightSummary = right.summary;
    let order: number;
    if (sort === "title")
      order = leftSummary.title.localeCompare(rightSummary.title, undefined, {
        sensitivity: "base",
      });
    else if (sort === "ci")
      order =
        (CI_PRIORITY[ciOf(left)] ?? 9) - (CI_PRIORITY[ciOf(right)] ?? 9) ||
        leftSummary.title.localeCompare(rightSummary.title, undefined, {
          sensitivity: "base",
        });
    else if (sort === "author")
      order =
        leftSummary.author.username.localeCompare(
          rightSummary.author.username,
          undefined,
          { sensitivity: "base" },
        ) ||
        leftSummary.title.localeCompare(rightSummary.title, undefined, {
          sensitivity: "base",
        });
    else
      order = (
        rightSummary.updated_at || rightSummary.created_at
      ).localeCompare(leftSummary.updated_at || leftSummary.created_at);
    return order || left.handle.localeCompare(right.handle);
  });
  return Object.freeze(sorted);
}

function emptyReviewLabel(
  scope: ReviewScope,
  state: "open" | "closed",
): string {
  if (scope === "my_reviews") return "No open reviews are waiting for you.";
  if (scope === "my_mrs") return "You have no open merge requests.";
  return state === "open"
    ? "No open reviews match this repository scope."
    : "No closed or merged reviews match this repository scope.";
}

const ReviewCard = memo(function ReviewCard({
  item,
  ci,
  ciLimited,
  selected,
  open,
}: {
  readonly item: ReviewListItemDto;
  /** The CI status to show, unavailable, or null while its lookup runs. */
  readonly ci: CIShown;
  /** Whether an unavailable status waits for a rate limit to reset. */
  readonly ciLimited: boolean;
  readonly selected: boolean;
  readonly open: (item: ReviewListItemDto) => void;
}): ReactNode {
  return (
    <button
      className={`review-card${selected ? " review-card-selected" : ""}`}
      role="listitem"
      data-review-number={item.summary.number}
      data-review-handle={item.handle}
      aria-current={selected ? "true" : undefined}
      onClick={() => open(item)}
    >
      {ci === null ? (
        <span
          className="state state-loading"
          data-ci-loading="true"
          title="Loading CI status"
        >
          {CI_LOADING_TEXT}
        </span>
      ) : ci === CI_UNAVAILABLE ? (
        <span
          className="state state-unavailable"
          data-ci-unavailable={ciLimited ? "rate_limited" : "failed"}
          title={
            ciLimited
              ? "CI status unavailable until the forge rate limit resets"
              : "CI status unavailable"
          }
        >
          {CI_UNAVAILABLE_TEXT}
        </span>
      ) : (
        <span className={`state state-${ci}`} data-ci-status={ci}>
          {ci}
        </span>
      )}
      <span className="review-number">#{item.summary.number}</span>
      <strong className="review-title">{item.summary.title}</strong>
      <span className="review-meta">
        {item.summary.author.display_name || item.summary.author.username} ·{" "}
        {item.summary.source_branch} → {item.summary.target_branch} ·{" "}
        {formatDate(item.summary.updated_at)}
      </span>
    </button>
  );
});

export function inboxPresentation(result: ReviewListResult): {
  readonly empty: boolean;
  readonly partialFailures: number;
} {
  return Object.freeze({
    empty: result.items.length === 0,
    partialFailures: result.failures.length,
  });
}

function Notice({
  kind,
  children,
}: {
  readonly kind: string;
  readonly children: ReactNode;
}): ReactNode {
  return (
    <div
      className={`notice notice-${kind}`}
      role={kind === "error" ? "alert" : "status"}
    >
      {children}
    </div>
  );
}
