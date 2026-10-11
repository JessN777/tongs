import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import {
  MAX_DIFF_CONTEXT_LINES,
  type DesktopBridge,
  type DiffContextResult,
  type DiffContextRow,
  type DiffContextStatus,
  type DiffFileRow,
  type DiffGapRow,
  type DiffHunkRow,
  type DiffLayout,
  type DiffPage,
  type DiffRow,
  type DiffWireRow,
  type RepositoryDto,
  type ReviewRevisionDto,
  type SplitDiffCell,
  type SplitDiffRow,
} from "../../../shared/bridge.js";
import type {
  AppRoute,
  DiscussionDiffTarget,
  FeatureContribution,
  InlineAnchorSelection,
  InlineAnchorStore,
  InlineSelectedLine,
} from "../../core/navigation.js";
import {
  RendererReadError,
  safeError,
  serviceErrorOf,
} from "../../core/presentation.js";
import { QueryCoordinator } from "../../core/query.js";
import { useRetainedRead } from "../../core/use-read.js";
import {
  InlineComposer,
  anchorIdentity,
  useInlineReviewComposer,
  type InlineComposerController,
} from "../review/composer.js";
import {
  peekPendingEdit,
  requestDrawerOpen,
  pendingEntryTarget,
  subscribePendingEdit,
  takePendingEdit,
} from "../review/drawer.js";
import {
  REVIEW_ROW_ATTRIBUTE,
  focusedReviewRow,
  pendingRowKey,
  pressComposerPrimary,
  reviewDrawerIsOpen,
  stepReviewRow,
  threadRowDiscussion,
  threadRowKey,
  useReviewKeyMap,
} from "../review/keys.js";
import {
  PendingCard,
  PendingCardMirror,
  type PendingDraftEntry,
} from "../review/pending-card.js";
import {
  DiffThread,
  DiffThreadMirror,
  threadFileCountText,
  threadFileCounts,
  threadRowClassName,
  threadRowExpanded,
  threadsOnLine,
  threadsOnOldLine,
  useDiscussionThreads,
  type AnchoredThread,
  type DiscussionThreadSlot,
} from "../review/thread.js";
import { ReviewHeaderControls } from "../review/header.js";
import { ReviewHeader } from "../review-detail/index.js";
import {
  MAX_HIGHLIGHT_CHARACTERS,
  MAX_HIGHLIGHT_LINES,
  SyntaxContent,
  highlightFile,
  highlightLines,
  type FileSyntax,
  type SyntaxLine,
} from "./syntax.js";
import {
  setFileViewed,
  useViewedFiles,
  viewedFileKey,
  viewedFilesFor,
  viewedProgressText,
  type ViewedFiles,
} from "./viewed.js";

const MAX_DIFF_PAGES = 1000;
const MAX_DIFF_ROWS = 100_000;

/**
 * Stated above the files while the forge says it capped the changed file list
 * (a GitLab "1000+" review, or a GitHub pull request past its file limit). The
 * header's Open on forge is where the reader sees every file.
 */
export const CHANGES_OVERFLOW_NOTICE =
  "The forge returned only part of the changed files, so this list is incomplete. Use Open on forge to see every file.";

/**
 * The most rows the whole-file view draws for one file, the terminal's
 * `FULL_FILE_MAX_ROWS`. A file past it keeps its gaps, which still expand one
 * at a time.
 */
export const FULL_FILE_MAX_ROWS = 20_000;

/** Shown when a comment is attempted on an expanded, unchanged line. */
export const EXPANDED_READ_ONLY_MESSAGE =
  "Expanded lines are read-only. Comment on a line inside a hunk.";

/** The terminal's wording for a file read that disagrees with the hunks. */
export const CONTEXT_MISMATCH_MESSAGE =
  "The file at this commit does not match the diff. Refresh the review.";

/** Shown when Full file is pressed while a gap of the file is being read. */
export const CONTEXT_LOADING_MESSAGE = "Still loading this file.";

/** Shown when the gap after the last hunk turns out to be empty. */
export const NO_MORE_LINES_MESSAGE = "No more lines below.";

/** The terminal's fallback text per read status, when the service sends none. */
export const CONTEXT_STATUS_MESSAGES: Readonly<
  Partial<Record<DiffContextStatus, string>>
> = Object.freeze({
  too_large: "File too large to expand.",
  binary: "Binary file, there are no lines to expand.",
  not_found: "The file was not found at this commit.",
  mismatch: CONTEXT_MISMATCH_MESSAGE,
  unsupported: "This forge cannot read files, so lines cannot be expanded.",
});

/** Shown for a file whose diff the forge withheld in full or in part. */
export const TRUNCATED_NOTICE =
  "This file's diff is too large for the forge to send. Use Generate diff locally to build it here (downloads both versions of the file).";

/** The header badge of a diff tongs computed from the file's two versions. */
export const GENERATED_LABEL = "Generated locally";

/** Shown on a diff generated locally: the forge has no diff to anchor to. */
export const GENERATED_COMMENT_MESSAGE =
  "Comments are not available on a diff generated locally.";

/** Shown under the truncation notice after a generation is cancelled. */
export const GENERATION_CANCELLED_MESSAGE =
  "Cancelled. Use Generate diff locally to try again.";

/** Shown when a generation fails and the service gives no reason. */
export const GENERATION_FAILED_MESSAGE = "Could not generate the diff.";

/**
 * Shown after an expansion or a generation found the open diff gone: the diff
 * is read again, exactly as Refresh reads it, and the action can be repeated.
 */
export const DIFF_RELOADED_MESSAGE =
  "This diff was no longer open, so it was reloaded. Try again.";

/** The text of a collapsed gap row. */
export function gapLabel(count: number | null): string {
  if (count === null) return "More lines below";
  return `${count.toLocaleString("en-US")} unchanged ${count === 1 ? "line" : "lines"}`;
}

/** The text of the row above an expanded gap, which collapses it again. */
export function hideLabel(count: number): string {
  return `... hide ${count.toLocaleString("en-US")} ${count === 1 ? "line" : "lines"}`;
}

/** The progress line while a file's diff is generated locally. */
export function generatingNotice(file: DiffFileRow): string {
  const oneSide = file.status === "added" || file.status === "deleted";
  return `Generating diff locally (downloading ${oneSide ? "1 file" : "2 files"}).`;
}

/** Shown when the whole-file view would draw more than its cap. */
export function fullFileTooLargeMessage(rows: number): string {
  return `File too large to show in full (${rows.toLocaleString("en-US")} lines). Expand its gaps one at a time.`;
}

const LINK_MODES: ReadonlySet<string> = new Set(["120000", "160000"]);

/**
 * Why Full file cannot show this file, or null when it can. A file the sidecar
 * sent gap rows for can always be shown; the reasons follow the terminal's.
 */
export function fullFileRefusal(
  file: DiffFileRow,
  rows: readonly DiffWireRow[],
  generated: boolean,
): string | null {
  if (generated)
    return "Unchanged lines cannot be shown around a diff generated locally.";
  if (rows.some((row) => row.kind === "gap")) return null;
  if (file.status === "added")
    return "The diff already shows every line of an added file.";
  if (file.status === "deleted")
    return "The diff already shows every line of a deleted file.";
  if (file.is_binary) return "Binary file, there are no lines to show.";
  if (file.is_truncated)
    return "The forge did not send this whole diff. Use Generate diff locally to build it.";
  const hunks = rows.filter((row): row is DiffHunkRow => row.kind === "hunk");
  if (
    hunks.length === 0 ||
    file.is_empty ||
    file.is_mode_only ||
    file.is_rename_only ||
    file.is_unavailable
  )
    return "This file has no changed lines to show around.";
  if (
    LINK_MODES.has(file.old_mode ?? "") ||
    LINK_MODES.has(file.new_mode ?? "")
  )
    return "Symlinks and submodules have no lines to show.";
  const first = hunks[0]!;
  if (first.old_start <= 1 && first.new_start <= 1)
    return "The diff already shows the lines around this file's changes.";
  return "The diff hunks do not line up with the file, so it cannot be shown in full.";
}

/**
 * One in-diff composer is open at a time. It is bound to the anchor it was
 * opened with, so extending or moving the line selection never retargets the
 * composer that is already collecting text.
 */
export interface InlineComposerSlot {
  readonly controller: InlineComposerController;
  readonly anchor: InlineAnchorSelection | null;
  /**
   * The pending entry the open composer is editing, or null when it is
   * collecting a new comment. The card for that entry gives way to the
   * composer, so the entry is never shown twice.
   */
  readonly editing: PendingDraftEntry | null;
  /**
   * `keepSelection` leaves the app-owned line selection alone, which is what a
   * row inside an existing multi-line selection needs: card #188 composes on a
   * single line, and discarding the range the reader built with the shipped
   * shift-click affordance would be a regression while #189 is pending.
   */
  readonly open: (
    anchor: InlineAnchorSelection,
    keepSelection?: boolean,
    entry?: PendingDraftEntry | null,
  ) => void;
  readonly close: () => void;
}

/** The row a drag started on, or the row it is being extended to. */
export interface DragRow {
  readonly side: "old" | "new";
  readonly hunkIndex: number;
  readonly oldLine: number | null;
  readonly newLine: number | null;
}

/**
 * A press on a line number starts a drag that selects the contiguous range it
 * covers on that one side of that one hunk. The drag lives on the workspace so
 * a release anywhere ends it, and it remembers the row it last extended to so
 * that a pointer moving inside one row does not rebuild the selection on every
 * event.
 */
export interface LineRangeDrag {
  readonly begin: (row: DragRow) => void;
  readonly extendTo: (row: DragRow, extend: () => void) => void;
}

export interface LoadedDiff {
  readonly layout: DiffLayout;
  readonly snapshotId: string;
  readonly resource: string;
  readonly revision: DiffPage["revision"];
  /** Every row of the snapshot, the gap rows of a diff opened with gaps included. */
  readonly rows: readonly DiffWireRow[];
  readonly partialError: RendererReadError | null;
  /**
   * True while the rows are the pages read so far and more are on the way.
   * The diff is drawn from its first page, and anything that would give up on
   * a row the diff does not hold yet waits for the rest instead.
   */
  readonly loadingMore?: boolean;
}

export interface LoadDiffOptions {
  /**
   * A diff snapshot already open for this review. The sidecar projects that
   * same read again, so a layout switch makes no forge request.
   */
  readonly fromSnapshot?: string | undefined;
  /** Called with the rows read so far, after the first page and each later one. */
  readonly onProgress?: (loaded: LoadedDiff) => void;
  /**
   * Asks for gap rows around the hunks of every file whose unchanged lines can
   * be shown. The diff view always asks; a caller that leaves it out gets
   * exactly the rows a diff had before gap rows existed.
   */
  readonly gaps?: boolean;
}

export function createDiffFeature(): FeatureContribution {
  return {
    id: "review.diff",
    order: 22,
    reviewPanel: { id: "diff", label: "Files changed", order: 20 },
    matches: (route) => route.kind === "review" && route.panel === "diff",
    render: (context, route) =>
      route.kind === "review" && route.panel === "diff" ? (
        <DiffView
          bridge={context.bridge}
          queries={context.queries}
          route={route}
          navigate={context.navigate}
          panels={context.reviewPanels}
          repositories={context.repositories}
          anchors={context.inlineAnchors}
        />
      ) : null,
  };
}

function DiffView({
  bridge,
  queries,
  route,
  navigate,
  panels,
  repositories,
  anchors,
}: {
  readonly bridge: DesktopBridge;
  readonly queries: QueryCoordinator;
  readonly route: Extract<AppRoute, { kind: "review" }>;
  readonly navigate: (route: AppRoute) => void;
  readonly panels: FeatureContributionParameters["reviewPanels"];
  readonly repositories: readonly RepositoryDto[];
  /**
   * The line selection. This view only writes it; the workspace below reads
   * it, so a line click re-renders the workspace and not this view.
   */
  readonly anchors: InlineAnchorStore;
}): ReactNode {
  // The suggestion block syntax is the forge's, so the composer needs to know
  // which forge this review lives on.
  const forge =
    repositories.find(
      (repository) => repository.handle === route.item.repository,
    )?.forge_type ?? null;
  const [layout, setLayout] = useState<DiffLayout>("unified");
  const [loaded, setLoaded] = useState<LoadedDiff | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [jumpError, setJumpError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  // The snapshot an expansion or a generation found gone, so the diff was read
  // again for it. The next expansion or generation that succeeds on a newer
  // snapshot clears it, as does any later Refresh or layout switch.
  const [reloadedFrom, setReloadedFrom] = useState<string | null>(null);
  // The toolbar's slot for the selected file's Full file toggle, which the
  // workspace below owns because it owns the file selection.
  const [fileTools, setFileTools] = useState<HTMLElement | null>(null);
  const reloadExpired = useCallback((snapshotId: string) => {
    setReloadedFrom(snapshotId);
    setReload((value) => value + 1);
  }, []);
  const contextSucceeded = useCallback((snapshotId: string) => {
    setReloadedFrom((current) =>
      current === null || current === snapshotId ? current : null,
    );
  }, []);
  // The diff on screen and the Refresh it was read for. A layout switch
  // re-projects that same read in the sidecar; a Refresh reads the forge.
  const shown = useRef<{
    readonly loaded: LoadedDiff;
    readonly reload: number;
  } | null>(null);
  // The review detail carries the forge's truncation flag. It is read beside
  // the diff, and a failed or older read simply shows no banner: the flag is
  // optional on the wire and absent means a complete list.
  const beginDetail = useCallback(
    () => bridge.getReview(route.item.handle),
    [bridge, route.item.handle],
  );
  const detail = useRetainedRead(
    queries,
    `diff-detail:${route.item.handle}`,
    beginDetail,
    [route.item.handle, reload],
  );
  const changesOverflow = detail.value?.detail.changes_overflow === true;
  useEffect(() => {
    let current = true;
    let partial: LoadedDiff | null = null;
    const previous = shown.current;
    const fromSnapshot =
      previous !== null &&
      previous.reload === reload &&
      previous.loaded.resource === route.item.handle &&
      previous.loaded.layout !== layout
        ? previous.loaded.snapshotId
        : undefined;
    const show = (value: LoadedDiff): void => {
      shown.current = { loaded: value, reload };
      setLoaded(value);
    };
    setLoading(true);
    setError(null);
    void loadAllPages(route.item.handle, layout, bridge, queries, {
      fromSnapshot,
      gaps: true,
      onProgress: (value) => {
        if (!current) return;
        partial = value;
        show(value);
      },
    })
      .then((value) => {
        if (current) {
          show(value);
          setError(value.partialError);
          setLoading(false);
        }
      })
      .catch((reason: unknown) => {
        if (current) {
          if (invalidatesSelection(reason)) anchors.select(null);
          // A layout switch can stop the previous layout mid-paging; its
          // drawn pages are then still on screen waiting for the rest.
          const drawn =
            partial ??
            (shown.current?.loaded.loadingMore ? shown.current.loaded : null);
          if (drawn === null) {
            setError(reason);
          } else {
            // The pages already drawn stay, as a bounded partial snapshot
            // that names why the rest is missing.
            const stopped = partialFailure(reason);
            show(
              Object.freeze({
                ...drawn,
                loadingMore: false,
                partialError: stopped,
              }),
            );
            setError(stopped);
          }
          setLoading(false);
        }
      });
    return () => {
      current = false;
      void queries.cancel(`diff:${route.item.handle}`);
    };
  }, [bridge, layout, queries, reload, route.item.handle]);
  useEffect(() => {
    if (!loaded || !route.diffTarget) return;
    const target = resolveDiscussionTarget(
      route.item.handle,
      loaded,
      route.diffTarget,
    );
    if (target) {
      setJumpError(null);
      // The route keeps its jump across a Refresh, a layout switch and a tab
      // switch. Once the jump has opened its file, a reader who marked that
      // file viewed again has moved on, so a later read leaves the line alone
      // rather than selecting a row the collapsed file does not draw.
      if (
        revealedJumps.has(route.diffTarget) &&
        fileIsViewed(route.item.handle, loaded, target.fileIndex)
      )
        return;
      revealJumpTarget(route.item.handle, loaded, route.diffTarget, target);
      anchors.select(target);
    } else if (!loaded.loadingMore) {
      setJumpError(
        "This discussion location is unavailable in the displayed revision. Refresh the diff or open the discussion on the forge.",
      );
    }
  }, [anchors, loaded, route.diffTarget, route.item.handle]);
  const files = useMemo(
    () =>
      loaded === null
        ? EMPTY_FILES
        : loaded.rows.filter((row): row is DiffFileRow => row.kind === "file"),
    [loaded?.rows],
  );
  // The marks belong to the head commit on screen, so a Refresh that brings a
  // new revision shows a review with nothing viewed yet.
  const viewed = useViewedFiles(
    route.item.handle,
    loaded?.revision.head_sha ?? "",
  );
  return (
    <>
      <ReviewHeader
        route={route}
        navigate={navigate}
        panels={panels}
        progress={
          files.length > 0 ? viewedProgressText(files, viewed.files) : null
        }
        controls={
          loaded ? (
            <ReviewHeaderControls
              bridge={bridge}
              route={route}
              navigate={navigate}
              forge={forge}
              revision={loaded.revision}
            />
          ) : null
        }
      />
      <div className="diff-toolbar">
        <strong className="toolbar-title">Layout</strong>
        <button
          className={`button ${layout === "unified" ? "button-active" : "button-secondary"}`}
          onClick={() => {
            setReloadedFrom(null);
            setLayout("unified");
          }}
        >
          Unified
        </button>
        <button
          className={`button ${layout === "split" ? "button-active" : "button-secondary"}`}
          onClick={() => {
            setReloadedFrom(null);
            setLayout("split");
          }}
        >
          Split
        </button>
        <button
          className="button button-secondary"
          disabled={loading}
          onClick={() => {
            setReloadedFrom(null);
            setReload((value) => value + 1);
          }}
        >
          Refresh
        </button>
        <span className="diff-file-tools" ref={setFileTools} />
        {loaded && (
          <code
            className="revision"
            data-head-sha={loaded.revision.head_sha}
            data-base-sha={loaded.revision.base_sha}
            data-start-sha={loaded.revision.start_sha ?? ""}
          >
            {loaded.revision.head_sha.slice(0, 12)}
          </code>
        )}
      </div>
      {loading && (
        <Notice kind="loading">
          {loaded?.loadingMore
            ? "Loading the rest of the diff…"
            : loaded
              ? "Refreshing diff…"
              : "Loading source diff…"}
        </Notice>
      )}
      {Boolean(error) && (
        <Notice kind="error">
          {safeError(error)}
          {loaded && loaded.partialError !== error
            ? " Showing the previous diff snapshot."
            : ""}
        </Notice>
      )}
      {jumpError && <Notice kind="warning">{jumpError}</Notice>}
      {reloadedFrom !== null && (
        <Notice kind="warning">{DIFF_RELOADED_MESSAGE}</Notice>
      )}
      {loaded && changesOverflow && (
        <Notice kind="warning">{CHANGES_OVERFLOW_NOTICE}</Notice>
      )}
      {loaded && (
        <DiffWorkspace
          bridge={bridge}
          review={route.item.handle}
          forge={forge}
          loaded={loaded}
          files={files}
          viewed={viewed}
          reloadToken={reload}
          anchors={anchors}
          reloadExpired={reloadExpired}
          contextSucceeded={contextSucceeded}
          fileTools={fileTools}
        />
      )}
    </>
  );
}

/**
 * The jumps that have already opened their file. A jump from a discussion is a
 * request to read that line, so the file it lands in stops being viewed rather
 * than hiding the line the reader asked for. That happens once per jump: the
 * route keeps its target across a tab switch, a layout switch and a Refresh,
 * and none of those asks to read the line again.
 */
const revealedJumps = new WeakSet<DiscussionDiffTarget>();

function revealJumpTarget(
  review: string,
  loaded: LoadedDiff,
  jump: DiscussionDiffTarget,
  target: InlineAnchorSelection,
): void {
  if (revealedJumps.has(jump)) return;
  revealedJumps.add(jump);
  revealFile(review, loaded, target.fileIndex);
}

function fileAt(
  loaded: LoadedDiff,
  fileIndex: number,
): DiffFileRow | undefined {
  return loaded.rows.find(
    (row): row is DiffFileRow =>
      row.kind === "file" && row.file_index === fileIndex,
  );
}

/** True when the file at `fileIndex` is marked as viewed at this revision. */
function fileIsViewed(
  review: string,
  loaded: LoadedDiff,
  fileIndex: number,
): boolean {
  const file = fileAt(loaded, fileIndex);
  return (
    file !== undefined &&
    viewedFilesFor(review, loaded.revision.head_sha).has(viewedFileKey(file))
  );
}

/** Clears the viewed mark of the file at `fileIndex`, if it has one. */
function revealFile(
  review: string,
  loaded: LoadedDiff,
  fileIndex: number,
): void {
  const file = fileAt(loaded, fileIndex);
  if (file)
    setFileViewed(review, loaded.revision.head_sha, viewedFileKey(file), false);
}

export async function loadAllPages(
  review: string,
  layout: DiffLayout,
  bridge: DesktopBridge,
  queries: QueryCoordinator,
  options: LoadDiffOptions = {},
): Promise<LoadedDiff> {
  const key = `diff:${review}`;
  const { fromSnapshot, onProgress, gaps = false } = options;
  const first: DiffPage<DiffWireRow> = await queries.run(key, () =>
    bridge.openDiff({
      review,
      layout,
      max_items: 1000,
      ...(fromSnapshot === undefined ? {} : { from_snapshot: fromSnapshot }),
      ...(gaps ? { gaps: true } : {}),
    }),
  );
  if (first.cursor !== 0)
    throw invalidPage("The first diff page did not start at cursor zero.");
  assertNextCursor(first);
  const rows = [...first.entries];
  if (rows.length > MAX_DIFF_ROWS)
    return loadedDiff(
      layout,
      first,
      rows.slice(0, MAX_DIFF_ROWS),
      paginationLimit("The first diff page exceeded the renderer row limit."),
    );
  let page = first;
  let pageCount = 1;
  while (page.next_cursor !== null) {
    // Draw what is read so far before asking for the next page.
    onProgress?.(loadedDiff(layout, first, [...rows], null, true));
    if (pageCount >= MAX_DIFF_PAGES || rows.length >= MAX_DIFF_ROWS)
      return loadedDiff(
        layout,
        first,
        rows.slice(0, MAX_DIFF_ROWS),
        paginationLimit("The diff exceeded the bounded renderer paging limit."),
      );
    const cursor = page.next_cursor;
    page = await queries.run(key, () =>
      bridge.pageDiff<DiffWireRow>({
        snapshot: first.snapshot_id,
        resource: first.resource,
        cursor,
        max_items: 1000,
      }),
    );
    if (
      page.snapshot_id !== first.snapshot_id ||
      page.resource !== first.resource ||
      !sameRevision(page, first)
    )
      throw new RendererReadError(
        "revision_changed",
        "The review revision changed during diff pagination.",
        false,
      );
    if (page.cursor !== cursor)
      throw invalidPage("The diff page cursor did not match the request.");
    assertNextCursor(page);
    if (rows.length + page.entries.length > MAX_DIFF_ROWS)
      return loadedDiff(
        layout,
        first,
        [...rows, ...page.entries].slice(0, MAX_DIFF_ROWS),
        paginationLimit("The diff exceeded the bounded renderer row limit."),
      );
    rows.push(...page.entries);
    pageCount += 1;
  }
  return loadedDiff(layout, first, rows, null);
}

function loadedDiff(
  layout: DiffLayout,
  first: DiffPage<DiffWireRow>,
  rows: readonly DiffWireRow[],
  partialError: RendererReadError | null,
  loadingMore = false,
): LoadedDiff {
  return Object.freeze({
    layout,
    snapshotId: first.snapshot_id,
    resource: first.resource,
    revision: first.revision,
    rows: Object.freeze(rows),
    partialError,
    loadingMore,
  });
}

/** The failure that stopped a diff after some of its pages were drawn. */
function partialFailure(reason: unknown): RendererReadError {
  if (reason instanceof RendererReadError) return reason;
  const failure = serviceErrorOf(reason);
  return failure === null
    ? new RendererReadError("unreported", "", false)
    : new RendererReadError(failure.code, failure.message, failure.retryable);
}

function assertNextCursor(page: DiffPage<DiffWireRow>): void {
  if (page.next_cursor !== null && page.next_cursor <= page.cursor)
    throw invalidPage("The diff page cursor did not advance.");
}

function invalidPage(message: string): RendererReadError {
  // A diff-owned code, so the diff reload advice stays on the diff surface.
  return new RendererReadError("invalid_diff_page", message, false);
}

function paginationLimit(message: string): RendererReadError {
  return new RendererReadError("pagination_limit", message, false);
}

function invalidatesSelection(error: unknown): boolean {
  const failure = serviceErrorOf(error);
  return (
    failure !== null &&
    (failure.code === "snapshot_expired" || failure.code === "revision_changed")
  );
}

type FeatureContributionParameters = Parameters<
  FeatureContribution["render"]
>[0];

/**
 * The row above an expanded gap's lines. It exists only in the renderer and
 * collapses the gap back to its gap row without asking the sidecar.
 */
interface DiffHideRow {
  readonly kind: "hide";
  readonly file_index: number;
  readonly gap_index: number;
  readonly count: number;
}

/**
 * A row the diff view draws: a snapshot row, an expanded unchanged line, or
 * the row that hides an expanded gap again.
 */
type DiffViewRow = DiffWireRow | DiffContextRow | DiffHideRow;

/** One gap's unchanged lines, read once per open diff. */
type GapLines = readonly DiffContextRow[];

/** Where a truncated file's local generation stands. */
type Generation =
  | { readonly state: "running"; readonly notice: string }
  | { readonly state: "done"; readonly rows: readonly DiffWireRow[] }
  | { readonly state: "failed"; readonly message: string };

/** What the view draws of one file's unchanged lines and local generation. */
interface FileContextView {
  /** The new file's length, once any read of it has said. */
  readonly lineCount: number | null;
  /** The gaps read so far, by gap index. */
  readonly lines: ReadonlyMap<number, GapLines>;
  /** The gaps shown expanded. */
  readonly shown: ReadonlySet<number>;
  readonly loading: ReadonlySet<number>;
  /** While Full file is on: the gaps that were shown before it. */
  readonly full: ReadonlySet<number> | null;
  readonly fullLoading: boolean;
  /** The last refusal or failure for this file, until dismissed. */
  readonly message: string | null;
  readonly generation: Generation | null;
}

const EMPTY_FILE_CONTEXT: FileContextView = Object.freeze({
  lineCount: null,
  lines: new Map<number, GapLines>(),
  shown: new Set<number>(),
  loading: new Set<number>(),
  full: null,
  fullLoading: false,
  message: null,
  generation: null,
});

type GapRead =
  | {
      readonly status: "ok";
      readonly rows: GapLines;
      readonly lineCount: number | null;
    }
  | { readonly status: "failed"; readonly message: string }
  | { readonly status: "expired" }
  | { readonly status: "stale" };

/** The outcome of reading one gap into the cache. */
type GapLoad = "ok" | "empty" | "failed";

/**
 * Requests one gap may take. A gap holds at most the 20,000 lines the service
 * reads for expansion, 1,000 per request, so this only stops a misbehaving
 * reply from looping.
 */
const MAX_CONTEXT_REQUESTS = 200;

function withItem(set: ReadonlySet<number>, item: number): ReadonlySet<number> {
  return new Set([...set, item]);
}

function withoutItem(
  set: ReadonlySet<number>,
  item: number,
): ReadonlySet<number> {
  const next = new Set(set);
  next.delete(item);
  return next;
}

/** A gap's length, filling in the gap after the last hunk once it is known. */
function gapCount(gap: DiffGapRow, lineCount: number | null): number | null {
  if (gap.count !== null) return gap.count;
  if (lineCount === null) return null;
  return Math.max(0, lineCount - gap.new_start + 1);
}

function readFailure(reason: unknown, prefix: string): string {
  const cause = serviceErrorOf(reason)?.message?.trim() ?? "";
  return cause === "" ? prefix : `${prefix} (${cause})`;
}

function isExpired(reason: unknown): boolean {
  return serviceErrorOf(reason)?.code === "snapshot_expired";
}

/**
 * The unchanged lines and local generations of one open diff snapshot. The
 * sidecar is only ever asked by review handle, snapshot and file index, never
 * by path or commit. Everything here is dropped with the snapshot: a Refresh
 * or a layout switch starts again with every gap collapsed.
 */
class DiffContextStore {
  private readonly files = new Map<number, FileContextView>();
  private readonly reads = new Set<string>();
  private readonly generations = new Map<
    number,
    { readonly run: number; readonly tokens: string[] }
  >();
  private runs = 0;
  private closed = false;

  constructor(
    private readonly bridge: DesktopBridge,
    private readonly review: string,
    readonly snapshotId: string,
    private readonly revision: ReviewRevisionDto,
    private readonly changed: () => void,
    private readonly expired: () => void,
    /** Called when an expansion or a generation on this snapshot succeeds. */
    private readonly succeeded: () => void,
  ) {}

  file(fileIndex: number): FileContextView {
    return this.files.get(fileIndex) ?? EMPTY_FILE_CONTEXT;
  }

  open(): void {
    this.closed = false;
  }

  /** Stops every read in flight; their replies are dropped. */
  close(): void {
    this.closed = true;
    const tokens = [...this.reads];
    this.reads.clear();
    for (const running of this.generations.values())
      tokens.push(...running.tokens);
    this.generations.clear();
    for (const token of tokens)
      void this.bridge.cancelRead(token).catch(() => false);
  }

  notify(fileIndex: number, message: string | null): void {
    this.update(fileIndex, { message });
  }

  /** Expands one gap, reading its lines first when this diff has not yet. */
  async expand(gap: DiffGapRow): Promise<void> {
    const current = this.file(gap.file_index);
    if (
      current.shown.has(gap.gap_index) ||
      current.loading.has(gap.gap_index) ||
      current.fullLoading
    )
      return;
    if (!current.lines.has(gap.gap_index)) {
      const outcome = await this.load(gap, false);
      if (outcome === "empty") this.succeed();
      if (outcome !== "ok") return;
    }
    const now = this.file(gap.file_index);
    this.update(gap.file_index, {
      shown: withItem(now.shown, gap.gap_index),
      message: null,
    });
    this.succeed();
  }

  /**
   * Shows every gap of one file, or restores the gaps shown before when Full
   * file is already on. A file past `FULL_FILE_MAX_ROWS` is refused before any
   * gap opens; the gap after the last hunk is read first when its length is
   * not known yet, because that read also says how long the file is.
   */
  async toggleFull(
    fileIndex: number,
    gaps: readonly DiffGapRow[],
    sourceRows: number,
  ): Promise<void> {
    const current = this.file(fileIndex);
    if (current.fullLoading) return;
    if (current.loading.size > 0) {
      this.update(fileIndex, { message: CONTEXT_LOADING_MESSAGE });
      return;
    }
    if (current.full !== null) {
      this.update(fileIndex, {
        shown: current.full,
        full: null,
        message: null,
      });
      return;
    }
    const previous = current.shown;
    this.update(fileIndex, {
      full: previous,
      fullLoading: true,
      message: null,
    });
    const active = (): boolean =>
      !this.closed && this.file(fileIndex).full === previous;
    const stop = (message?: string): void => {
      if (!active()) return;
      this.update(fileIndex, {
        shown: previous,
        full: null,
        fullLoading: false,
        ...(message === undefined ? {} : { message }),
      });
    };
    let rows = sourceRows;
    for (const gap of gaps) rows += gap.count ?? 0;
    if (rows > FULL_FILE_MAX_ROWS) return stop(fullFileTooLargeMessage(rows));
    const after = gaps.find((gap) => gap.count === null);
    if (
      after !== undefined &&
      this.file(fileIndex).lineCount === null &&
      !this.file(fileIndex).lines.has(after.gap_index)
    ) {
      // One line is enough: every reply says how long the file is.
      const learned = await this.learnLineCount(after);
      if (!active()) return;
      if (!learned) return stop();
    }
    const lineCount = this.file(fileIndex).lineCount;
    if (after !== undefined) rows += gapCount(after, lineCount) ?? 0;
    if (rows > FULL_FILE_MAX_ROWS) return stop(fullFileTooLargeMessage(rows));
    for (const gap of gaps) {
      if (this.file(fileIndex).lines.has(gap.gap_index)) continue;
      if (gapCount(gap, lineCount) === 0) continue;
      const outcome = await this.load(gap, true);
      if (!active()) return;
      if (outcome === "failed") return stop();
    }
    const now = this.file(fileIndex);
    this.update(fileIndex, {
      shown: new Set(
        gaps
          .map((gap) => gap.gap_index)
          .filter((index) => now.lines.has(index)),
      ),
      fullLoading: false,
    });
    this.succeed();
  }

  /**
   * Collapses one expanded gap back to its gap row. Its lines stay cached, so
   * expanding it again reads nothing. With Full file on, collapsing a gap
   * turns Full file off and keeps the gaps that were expanded before it.
   */
  collapse(fileIndex: number, gapIndex: number): boolean {
    const current = this.file(fileIndex);
    if (current.fullLoading) return false;
    if (current.full !== null) {
      this.update(fileIndex, {
        shown: withoutItem(current.full, gapIndex),
        full: null,
        message: null,
      });
      return true;
    }
    if (!current.shown.has(gapIndex)) return false;
    this.update(fileIndex, {
      shown: withoutItem(current.shown, gapIndex),
      message: null,
    });
    return true;
  }

  /** Turns Full file off for a file the reader has left. */
  leaveFull(fileIndex: number): void {
    const current = this.file(fileIndex);
    if (current.full === null) return;
    this.update(fileIndex, {
      shown: current.full,
      full: null,
      fullLoading: false,
    });
  }

  /** Reads one line after the last hunk to learn the file's length. */
  private async learnLineCount(gap: DiffGapRow): Promise<boolean> {
    const fileIndex = gap.file_index;
    this.update(fileIndex, {
      loading: withItem(this.file(fileIndex).loading, gap.gap_index),
    });
    const reply = await this.request(gap.file_index, gap.new_start, 1);
    if (reply.status === "stale" || this.closed) return false;
    const loading = withoutItem(this.file(fileIndex).loading, gap.gap_index);
    if (reply.status === "expired") {
      this.update(fileIndex, { loading });
      this.expired();
      return false;
    }
    if (reply.status === "failed") {
      this.update(fileIndex, { loading, message: reply.message });
      return false;
    }
    this.update(fileIndex, {
      loading,
      lineCount: reply.result.line_count ?? this.file(fileIndex).lineCount,
    });
    return true;
  }

  /** Reads one gap's lines into the cache and reports how that went. */
  private async load(gap: DiffGapRow, quiet: boolean): Promise<GapLoad> {
    const fileIndex = gap.file_index;
    const before = this.file(fileIndex);
    this.update(fileIndex, {
      loading: withItem(before.loading, gap.gap_index),
      message: null,
    });
    const read = await this.readGap(gap, before.lineCount);
    if (read.status === "stale" || this.closed) return "failed";
    const now = this.file(fileIndex);
    const loading = withoutItem(now.loading, gap.gap_index);
    if (read.status === "expired") {
      this.update(fileIndex, { loading });
      this.expired();
      return "failed";
    }
    if (read.status === "failed") {
      this.update(fileIndex, { loading, message: read.message });
      return "failed";
    }
    const lineCount = read.lineCount ?? now.lineCount;
    if (read.rows.length === 0) {
      this.update(fileIndex, {
        loading,
        lineCount,
        ...(quiet ? {} : { message: NO_MORE_LINES_MESSAGE }),
      });
      return "empty";
    }
    const lines = new Map(now.lines);
    lines.set(gap.gap_index, read.rows);
    this.update(fileIndex, { loading, lineCount, lines });
    return "ok";
  }

  /**
   * Reads every line of one gap, at most `MAX_DIFF_CONTEXT_LINES` per request.
   * A reply may stop early at the sidecar's size bound; the next request picks
   * up from the line after the last one returned.
   */
  private async readGap(
    gap: DiffGapRow,
    knownLineCount: number | null,
  ): Promise<GapRead> {
    let next = gap.new_start;
    let end: number | null =
      gap.count !== null
        ? gap.new_start + gap.count
        : knownLineCount !== null
          ? Math.max(next, knownLineCount + 1)
          : null;
    let lineCount = knownLineCount;
    const rows: DiffContextRow[] = [];
    for (let requests = 0; end === null || next < end; requests += 1) {
      if (requests >= MAX_CONTEXT_REQUESTS)
        return {
          status: "failed",
          message: CONTEXT_STATUS_MESSAGES.too_large!,
        };
      const count = Math.min(
        MAX_DIFF_CONTEXT_LINES,
        end === null ? MAX_DIFF_CONTEXT_LINES : end - next,
      );
      const reply = await this.request(gap.file_index, next, count);
      if (reply.status !== "ok") return reply;
      const result = reply.result;
      lineCount = result.line_count ?? lineCount;
      if (end === null)
        end = lineCount === null ? next : Math.max(next, lineCount + 1);
      const start = next;
      const fits = result.rows.every(
        (row, offset) =>
          row.kind === "context" &&
          row.file_index === gap.file_index &&
          row.new_line === start + offset &&
          row.old_line === gap.old_start + (start + offset - gap.new_start),
      );
      if (!fits) return { status: "failed", message: CONTEXT_MISMATCH_MESSAGE };
      if (result.rows.length === 0) break;
      const taken = result.rows.slice(0, end - next);
      for (const row of taken) rows.push(row);
      next += taken.length;
    }
    // A gap of known length that came back short does not match the hunks.
    if (end !== null && next < end)
      return { status: "failed", message: CONTEXT_MISMATCH_MESSAGE };
    return { status: "ok", rows: Object.freeze(rows), lineCount };
  }

  /** One `diff.context` request, with every refusal worded as the terminal's. */
  private async request(
    fileIndex: number,
    newStart: number,
    count: number,
  ): Promise<
    | { readonly status: "ok"; readonly result: DiffContextResult }
    | Exclude<GapRead, { readonly status: "ok" }>
  > {
    const read = this.bridge.getDiffContext({
      review: this.review,
      snapshot: this.snapshotId,
      file_index: fileIndex,
      new_start: newStart,
      count,
    });
    this.reads.add(read.requestToken);
    let result: DiffContextResult;
    try {
      result = await read.result;
    } catch (reason) {
      if (this.closed) return { status: "stale" };
      if (isExpired(reason)) return { status: "expired" };
      return {
        status: "failed",
        message: readFailure(reason, "Could not read the file."),
      };
    } finally {
      this.reads.delete(read.requestToken);
    }
    if (this.closed) return { status: "stale" };
    if (result.status === "expired") return { status: "expired" };
    if (result.status !== "ok")
      return {
        status: "failed",
        message:
          result.message ||
          CONTEXT_STATUS_MESSAGES[result.status] ||
          "Could not read the file.",
      };
    return { status: "ok", result };
  }

  /**
   * Generates a truncated file's diff in the sidecar and reads every page of
   * the one-file snapshot it returns. The rows replace the file's rows for as
   * long as this diff is open.
   */
  async generate(file: DiffFileRow): Promise<void> {
    const fileIndex = file.file_index;
    const state = this.file(fileIndex).generation?.state;
    if (state === "running" || state === "done") return;
    const run = (this.runs += 1);
    const tokens: string[] = [];
    this.generations.set(fileIndex, { run, tokens });
    this.update(fileIndex, {
      generation: { state: "running", notice: generatingNotice(file) },
      message: null,
    });
    const live = (): boolean =>
      !this.closed && this.generations.get(fileIndex)?.run === run;
    const settle = (generation: Generation | null): void => {
      if (!live()) return;
      this.generations.delete(fileIndex);
      this.update(fileIndex, { generation });
    };
    const fail = (message: string): void =>
      settle({ state: "failed", message });
    const failed = (reason: unknown): void => {
      if (!live()) return;
      if (isExpired(reason)) {
        settle(null);
        this.expired();
        return;
      }
      fail(readFailure(reason, GENERATION_FAILED_MESSAGE));
    };
    const inconsistent = (): void =>
      fail(
        `${GENERATION_FAILED_MESSAGE} (The local service returned an inconsistent diff page.)`,
      );
    let first;
    try {
      const read = this.bridge.generateDiff({
        review: this.review,
        snapshot: this.snapshotId,
        file_index: fileIndex,
      });
      tokens.push(read.requestToken);
      first = await read.result;
    } catch (reason) {
      return failed(reason);
    }
    if (!live()) return;
    if (first.status === "expired") {
      settle(null);
      this.expired();
      return;
    }
    if (first.status !== "ok")
      return fail(first.message || GENERATION_FAILED_MESSAGE);
    if (!sameRevisionValue(first.revision, this.revision))
      return inconsistent();
    const rows: DiffWireRow[] = [...first.entries];
    let page: DiffPage<DiffWireRow> = first;
    let pages = 1;
    while (page.next_cursor !== null) {
      if (page.next_cursor <= page.cursor) return inconsistent();
      if (pages >= MAX_DIFF_PAGES || rows.length >= MAX_DIFF_ROWS)
        return fail(
          "The generated diff is too large to show in the desktop app.",
        );
      const cursor = page.next_cursor;
      try {
        const read = this.bridge.pageDiff<DiffWireRow>({
          snapshot: first.snapshot_id,
          resource: first.resource,
          cursor,
          max_items: 1000,
        });
        tokens.push(read.requestToken);
        page = await read.result;
      } catch (reason) {
        return failed(reason);
      }
      if (!live()) return;
      if (
        page.snapshot_id !== first.snapshot_id ||
        page.cursor !== cursor ||
        !sameRevisionValue(page.revision, this.revision)
      )
        return inconsistent();
      for (const row of page.entries) rows.push(row);
      pages += 1;
    }
    if (rows.length > MAX_DIFF_ROWS)
      return fail(
        "The generated diff is too large to show in the desktop app.",
      );
    if (
      rows[0]?.kind !== "file" ||
      rows.some((row) => row.file_index !== fileIndex || row.kind === "gap")
    )
      return inconsistent();
    if (!live()) return;
    settle({ state: "done", rows: Object.freeze(rows) });
    this.succeed();
  }

  /** Stops a running generation through the bridge's cancel frame. */
  cancelGeneration(fileIndex: number): void {
    const running = this.generations.get(fileIndex);
    if (!running) return;
    this.generations.delete(fileIndex);
    for (const token of running.tokens)
      void this.bridge.cancelRead(token).catch(() => false);
    this.update(fileIndex, {
      generation: { state: "failed", message: GENERATION_CANCELLED_MESSAGE },
    });
  }

  private succeed(): void {
    if (!this.closed) this.succeeded();
  }

  private update(fileIndex: number, patch: Partial<FileContextView>): void {
    if (this.closed) return;
    this.files.set(
      fileIndex,
      Object.freeze({ ...this.file(fileIndex), ...patch }),
    );
    this.changed();
  }
}

/**
 * One file's rows with its expanded gaps replaced by their unchanged lines,
 * each led by the row that hides them again.
 * A collapsed gap stays a gap row; the gap after the last hunk takes its
 * length once a read has said how long the file is, and disappears when the
 * file ends where the last hunk ends.
 */
function expandFileRows(
  rows: readonly DiffWireRow[],
  context: FileContextView,
): readonly DiffViewRow[] {
  if (!rows.some((row) => row.kind === "gap")) return rows;
  const result: DiffViewRow[] = [];
  for (const row of rows) {
    if (row.kind !== "gap") {
      result.push(row);
      continue;
    }
    const lines = context.shown.has(row.gap_index)
      ? context.lines.get(row.gap_index)
      : undefined;
    if (lines !== undefined) {
      if (lines.length > 0)
        result.push(
          Object.freeze({
            kind: "hide",
            file_index: row.file_index,
            gap_index: row.gap_index,
            count: lines.length,
          }),
        );
      for (const line of lines) result.push(line);
      continue;
    }
    const count = gapCount(row, context.lineCount);
    if (count === 0) continue;
    result.push(count === row.count ? row : Object.freeze({ ...row, count }));
  }
  return result;
}

const generatedSlices = new WeakMap<readonly DiffWireRow[], FileSlice>();

/** The slice of a generated file's rows, built once per generation. */
function generatedSlice(rows: readonly DiffWireRow[]): FileSlice {
  let slice = generatedSlices.get(rows);
  if (!slice) {
    slice = Object.freeze({
      rows,
      hunks: rows.filter((row): row is DiffHunkRow => row.kind === "hunk"),
    });
    generatedSlices.set(rows, slice);
  }
  return slice;
}

/** Where keyboard focus goes once a gap collapses: its gap row, in one pane. */
interface CollapsedFocus {
  current: { readonly gap: number; readonly side: "old" | "new" | null } | null;
}

/** Row presentation that depends on the file's context and generation. */
interface RowExtras {
  readonly expandGap: (gap: DiffGapRow) => void;
  /** Collapses an expanded gap again, with no sidecar call; false if not. */
  readonly collapseGap: (row: DiffHideRow) => boolean;
  /** Set when a gap collapses, so its gap row takes focus once it mounts. */
  readonly collapsedFocus: CollapsedFocus;
  readonly loadingGaps: ReadonlySet<number>;
  /** Shows a refusal for this file, such as a comment on an expanded line. */
  readonly refuse: (message: string) => void;
  /** True for a diff generated locally: no row is a comment anchor. */
  readonly readOnly: boolean;
  readonly generation: Generation | null;
  readonly generate: (file: DiffFileRow) => void;
}

const contextHighlights = new WeakMap<
  DiffContextRow,
  { readonly length: number; readonly lines: readonly SyntaxLine[] | null }
>();

/**
 * The colours of expanded lines. Each run of consecutive unchanged lines is
 * highlighted as one text of the new side, the first time a row of it is
 * drawn, and remembered with the run. A run past the highlighting cutoffs
 * stays plain text, as a large diff does.
 */
function contextSyntaxOf(
  file: DiffFileRow | null,
  items: readonly DiffViewRow[],
): (row: DiffContextRow) => SyntaxLine | undefined {
  const runs = new WeakMap<
    DiffContextRow,
    { start: DiffContextRow; rows: DiffContextRow[]; index: number }
  >();
  if (file === null) return () => undefined;
  let run: DiffContextRow[] = [];
  for (const row of items) {
    if (row.kind !== "context") {
      run = [];
      continue;
    }
    const last = run.at(-1);
    if (last !== undefined && last.new_line + 1 !== row.new_line) run = [];
    run.push(row);
    runs.set(row, { start: run[0]!, rows: run, index: run.length - 1 });
  }
  return (row) => {
    const place = runs.get(row);
    if (!place) return undefined;
    let cached = contextHighlights.get(place.start);
    if (cached === undefined || cached.length !== place.rows.length) {
      let characters = 0;
      for (const line of place.rows) characters += line.content.length;
      const lines =
        place.rows.length <= MAX_HIGHLIGHT_LINES &&
        characters <= MAX_HIGHLIGHT_CHARACTERS
          ? highlightLines(
              file.language,
              place.rows.map((line) => line.content),
            )
          : null;
      cached = { length: place.rows.length, lines };
      contextHighlights.set(place.start, cached);
    }
    return cached.lines?.[place.index];
  };
}

function DiffWorkspace({
  bridge,
  review,
  forge,
  loaded,
  files,
  viewed,
  reloadToken,
  anchors,
  reloadExpired,
  contextSucceeded,
  fileTools,
}: {
  readonly bridge: DesktopBridge;
  readonly review: string;
  readonly forge: RepositoryDto["forge_type"] | null;
  readonly loaded: LoadedDiff;
  /** The file rows of `loaded`, in diff order. */
  readonly files: readonly DiffFileRow[];
  /** The files the reader has marked as viewed at this revision. */
  readonly viewed: ViewedFiles;
  /** Bumped by the toolbar's Refresh, so the published threads are reread too. */
  readonly reloadToken: number;
  readonly anchors: InlineAnchorStore;
  /** Reads the diff again, as Refresh does, after the sidecar dropped it. */
  readonly reloadExpired: (snapshotId: string) => void;
  /** Called with the snapshot an expansion or a generation succeeded on. */
  readonly contextSucceeded: (snapshotId: string) => void;
  /** The toolbar slot that holds the selected file's Full file toggle. */
  readonly fileTools: HTMLElement | null;
}): ReactNode {
  const selection = useSyncExternalStore(anchors.subscribe, anchors.current);
  const selectAnchor = anchors.select;
  // Each file's rows and hunk rows, read once per loaded diff. The changed-file
  // list and the rows view take their slice from here instead of filtering all
  // rows per file on every render, which made a click cost files times rows.
  const slices = useMemo(() => fileSlices(loaded.rows), [loaded.rows]);
  const [selected, setSelected] = useState(files[0]?.file_index ?? 0);
  const [target, setTarget] = useState(0);
  // Unchanged lines and local generations belong to the open snapshot, so a
  // new snapshot starts a new store and the old one stops its reads.
  const [, setContextVersion] = useState(0);
  const contextStore = useMemo(
    () =>
      new DiffContextStore(
        bridge,
        review,
        loaded.snapshotId,
        loaded.revision,
        () => setContextVersion((value) => value + 1),
        () => reloadExpired(loaded.snapshotId),
        () => contextSucceeded(loaded.snapshotId),
      ),
    [bridge, review, loaded.snapshotId],
  );
  useEffect(() => {
    contextStore.open();
    return () => contextStore.close();
  }, [contextStore]);
  const generatedRows = (fileIndex: number): readonly DiffWireRow[] | null => {
    const generation = contextStore.file(fileIndex).generation;
    return generation?.state === "done" ? generation.rows : null;
  };
  // A generated file reads its own rows in place of the forge's.
  const sliceFor = (fileIndex: number): FileSlice => {
    const rows = generatedRows(fileIndex);
    return rows === null ? sliceOf(slices, fileIndex) : generatedSlice(rows);
  };
  const [composerAnchor, setComposerAnchor] =
    useState<InlineAnchorSelection | null>(null);
  const [editingEntry, setEditingEntry] = useState<PendingDraftEntry | null>(
    null,
  );
  const composerController = useInlineReviewComposer(
    bridge,
    review,
    loaded.revision,
    forge,
  );
  const threads = useDiscussionThreads(
    bridge,
    review,
    composerController,
    reloadToken,
  );
  const [dragging, setDragging] = useState(false);
  const dragged = useRef<DragRow | null>(null);
  // A drag can end anywhere, including outside the diff, so the release is
  // watched on the document only while a drag is actually running.
  useEffect(() => {
    if (!dragging) return;
    const release = (): void => {
      dragged.current = null;
      setDragging(false);
    };
    document.addEventListener("mouseup", release);
    return () => document.removeEventListener("mouseup", release);
  }, [dragging]);
  // A reloaded diff can retire the row the composer is anchored to. Only an
  // anchor that no longer belongs to the loaded diff is dropped, and its typed
  // text stays in the per-anchor buffer either way.
  useEffect(() => {
    if (
      composerAnchor === null ||
      anchorBelongsToLoadedDiff(composerAnchor, review, loaded)
    )
      return;
    setComposerAnchor(null);
    setEditingEntry(null);
  }, [composerAnchor, loaded, review]);
  // A new snapshot starts at its first file. The pages that arrive after the
  // first one only append rows, so they leave the reader where they are.
  useEffect(() => {
    setSelected(files[0]?.file_index ?? 0);
    setTarget(0);
  }, [loaded.snapshotId]);
  // A selection made in this diff already names a row of it, so only a new
  // diff (a Refresh, a layout switch, another page) rebinds it. Reading it from
  // the store keeps a line click from scanning every loaded row again.
  useEffect(() => {
    const current = anchors.current();
    if (!current) return;
    const rebound = rebindInlineAnchor(current, review, loaded);
    if (!rebound) {
      // The row may be on a page that has not arrived yet.
      if (!loaded.loadingMore) anchors.select(null);
    } else if (!sameAnchorIdentity(current, rebound)) anchors.select(rebound);
  }, [anchors, loaded, review]);
  useEffect(() => {
    if (!selection || selection.review !== review) return;
    const matchingFile = files.find(
      (file) => file.file_index === selection.fileIndex,
    );
    if (!matchingFile) return;
    // A viewed file draws its header alone, so a selection inside it would
    // let `c` open a composer on a row that is not drawn.
    if (viewed.files.has(viewedFileKey(matchingFile))) {
      selectAnchor(null);
      return;
    }
    const fileRows = sliceOf(slices, matchingFile.file_index).rows;
    const rowIndex = fileRows.findIndex((row) =>
      loaded.layout === "split"
        ? row.kind === "split" && row.row_index === selection.rowIndex
        : row.kind === "line" &&
          row.old_line === selection.oldLine &&
          row.new_line === selection.newLine &&
          row.line_type === selection.lineType,
    );
    setSelected(matchingFile.file_index);
    if (rowIndex >= 0) setTarget(rowIndex);
  }, [files, loaded, review, selectAnchor, selection, slices, viewed.files]);
  const forgeFile = files.find((file) => file.file_index === selected) ?? null;
  const selectedSlice = sliceFor(selected);
  const fileContext = contextStore.file(selected);
  const selectedGenerated = fileContext.generation?.state === "done";
  // A generated file's header is the generated file row.
  const selectedFile =
    selectedGenerated && selectedSlice.rows[0]?.kind === "file"
      ? selectedSlice.rows[0]
      : forgeFile;
  const selectedViewed =
    selectedFile !== null && viewed.files.has(viewedFileKey(selectedFile));
  // A viewed file collapses to its header row.
  const visible = useMemo(() => {
    const rows = selectedSlice.rows;
    return selectedViewed
      ? rows.filter((row) => row.kind === "file")
      : expandFileRows(rows, fileContext);
  }, [fileContext, selectedSlice, selectedViewed]);
  // The target is a row of the file's slice; expanded lines shift where it is
  // drawn, never which row it is.
  const visibleTarget = useMemo(() => {
    const row = selectedSlice.rows[target];
    const at = row === undefined ? -1 : visible.indexOf(row);
    return at < 0 ? 0 : at;
  }, [selectedSlice, target, visible]);
  // Full file belongs to the file it was turned on for: leaving the file turns
  // it off and brings back the gaps shown before.
  const fullFileOf = useRef(selected);
  // The gap row that takes focus after its hide row collapses it.
  const collapsedFocus = useRef<CollapsedFocus["current"]>(null);
  useEffect(() => {
    if (fullFileOf.current === selected) return;
    contextStore.leaveFull(fullFileOf.current);
    fullFileOf.current = selected;
  }, [contextStore, selected]);
  // A generated diff replaces the rows a selection or a composer stood on.
  useEffect(() => {
    if (!selectedGenerated) return;
    if (anchors.current()?.fileIndex === selected) anchors.select(null);
    setComposerAnchor((anchor) =>
      anchor !== null && anchor.fileIndex === selected ? null : anchor,
    );
  }, [anchors, selected, selectedGenerated]);
  const gapRows = useMemo(
    () =>
      selectedSlice.rows.filter((row): row is DiffGapRow => row.kind === "gap"),
    [selectedSlice],
  );
  const sourceRowCount = useMemo(
    () =>
      selectedSlice.rows.filter(
        (row) => row.kind === "line" || row.kind === "split",
      ).length,
    [selectedSlice],
  );
  const fullRefusal =
    selectedFile === null
      ? "Select a file to show it in full."
      : selectedViewed
        ? "Clear Viewed to show this file."
        : fullFileRefusal(selectedFile, selectedSlice.rows, selectedGenerated);
  const extras: RowExtras = {
    expandGap: (gap) => void contextStore.expand(gap),
    collapseGap: (row) => contextStore.collapse(row.file_index, row.gap_index),
    collapsedFocus,
    loadingGaps: fileContext.loading,
    refuse: (message) => contextStore.notify(selected, message),
    readOnly: selectedGenerated,
    generation: fileContext.generation,
    generate: (file) => void contextStore.generate(file),
  };
  // The entry the composer is editing is re-read from the controller on every
  // render, so a save that rewrote the pending list cannot leave the composer
  // holding a body the store no longer has.
  const editing =
    editingEntry === null
      ? null
      : (composerController.pending.find(
          (entry) => entry.id === editingEntry.id,
        ) ?? null);
  // A pending entry that leaves the review while its editor is open takes the
  // editor with it, rather than turning into a composer for a new comment on
  // the same line.
  useEffect(() => {
    if (
      editingEntry === null ||
      composerController.pending.some((entry) => entry.id === editingEntry.id)
    )
      return;
    setComposerAnchor(null);
    setEditingEntry(null);
  }, [composerController.pending, editingEntry]);
  // An Edit pressed in the drawer names an entry, not a row: the drawer can be
  // open over any panel, and the row the entry sits on may not be loaded yet.
  // The request is claimed only once the entry is in the adopted draft and its
  // line resolves in the loaded diff, so an Edit pressed before the diff is
  // ready opens the editor when it becomes ready rather than doing nothing. A
  // request that names an entry this review no longer holds is dropped, so it
  // cannot reopen on some later diff.
  useEffect(() => {
    const claim = (): void => {
      const entryId = peekPendingEdit(review);
      if (entryId === null) return;
      const entry = composerController.pending.find(
        (item) => item.id === entryId,
      );
      if (!entry) {
        if (composerController.pending.length > 0) takePendingEdit(review);
        return;
      }
      const anchored = pendingEntryTarget(entry);
      const resolved = anchored
        ? resolveDiscussionTarget(review, loaded, {
            discussionId: `pending:${entry.id}`,
            ...anchored,
          })
        : null;
      if (!resolved) {
        // The entry's line may be on a page that has not arrived yet.
        if (!loaded.loadingMore) takePendingEdit(review);
        return;
      }
      takePendingEdit(review);
      // An Edit asks for the entry's line, as a jump does.
      revealFile(review, loaded, resolved.fileIndex);
      composerController.clearMessage();
      // The selection the reader built is left alone, exactly as the in-diff
      // card's own Edit leaves it, and the entry keeps the anchor it was
      // saved with.
      setComposerAnchor(resolved);
      setEditingEntry(entry);
    };
    claim();
    return subscribePendingEdit(claim);
  }, [composerController, loaded, review]);
  const inline: InlineComposerSlot = {
    controller: composerController,
    anchor: composerAnchor,
    editing,
    open: (anchor, keepSelection = false, entry = null) => {
      composerController.clearMessage();
      if (!keepSelection) selectAnchor(anchor);
      setComposerAnchor(anchor);
      setEditingEntry(entry);
    },
    close: () => {
      composerController.clearMessage();
      setComposerAnchor(null);
      setEditingEntry(null);
    },
  };
  const markViewed = (file: DiffFileRow, next: boolean): void => {
    viewed.setViewed(viewedFileKey(file), next);
    if (!next || file.file_index !== selected) return;
    // Collapsing the file takes the composer and the line selection with it,
    // so neither `c` nor Ctrl+Enter can act on a row that is no longer drawn.
    // Unsent text stays in the per-anchor buffer.
    setComposerAnchor(null);
    setEditingEntry(null);
    if (selection !== null && selection.fileIndex === file.file_index)
      selectAnchor(null);
  };
  const drag: LineRangeDrag = {
    begin: (row) => {
      dragged.current = row;
      setDragging(true);
    },
    extendTo: (row, extend) => {
      const from = dragged.current;
      // A range lives on one side of one hunk, because that is the only shape
      // the anchor can carry. An overshoot into the next hunk or the other
      // side stops the range where it is instead of collapsing it to the row
      // the pointer happens to be over.
      if (!from || from.side !== row.side || from.hunkIndex !== row.hunkIndex)
        return;
      if (from.oldLine === row.oldLine && from.newLine === row.newLine) return;
      dragged.current = row;
      extend();
    },
  };
  // The review keyboard map (design 2.8). The region is the routed content
  // around the workspace, not the workspace itself, so a key typed with the
  // focus on the layout toggle or the changed-file navigation is answered the
  // same way as one typed with nothing focused at all. The rows container is
  // held separately because it is what `n`, `p`, `r` and the composer's
  // primary action reach into.
  const [workspaceNode, setWorkspaceNode] = useState<HTMLElement | null>(null);
  const [rowsNode, setRowsNode] = useState<HTMLElement | null>(null);
  const rowCursor = useRef<string | null>(null);
  const stepFile = (direction: 1 | -1): boolean => {
    // A review with one file has no next file, so the key stays unclaimed
    // rather than scrolling the one file back to its top.
    if (files.length < 2) return false;
    const at = files.findIndex((file) => file.file_index === selected);
    const from = at < 0 ? 0 : at;
    const next = files[(from + direction + files.length) % files.length];
    if (!next) return false;
    setSelected(next.file_index);
    setTarget(0);
    // The changed-file navigation drops the composer and the selection when it
    // moves to another file, and never when it stays; the key does exactly the
    // same, so a review with one file is not reset by a press that goes
    // nowhere.
    if (next.file_index !== selected) {
      setComposerAnchor(null);
      selectAnchor(null);
    }
    return true;
  };
  const stepRow = (direction: 1 | -1): boolean => {
    const step = stepReviewRow(rowsNode, direction, rowCursor.current);
    rowCursor.current = step.key;
    return step.moved;
  };
  const composeOnSelection = (): boolean => {
    // A row that holds the focus answers `c` itself, with the row's own
    // handler. This is the same key from the body: the anchor is the selection
    // the reader built, and a range composes on the whole range exactly as the
    // gutter affordance does.
    if (!selection || selection.review !== review) return false;
    const file = files.find((row) => row.file_index === selection.fileIndex);
    if (!file) return false;
    const range =
      (selection.selectedLines?.length ?? 0) > 1
        ? rangeAnchorForSelection(selection, review, loaded, file)
        : null;
    inline.open(range ?? selection, true);
    return true;
  };
  const replyToFocusedThread = (): boolean => {
    const discussion = threadRowDiscussion(
      focusedReviewRow(rowsNode) ?? rowCursor.current,
    );
    if (discussion === null) return false;
    // The key carries the same refusals the Reply button carries, rather than
    // opening a composer the thread would not take.
    if (threads.replyReason(discussion) !== null) return false;
    threads.openReply(discussion);
    return true;
  };
  const closeComposer = (): boolean => {
    // Escape used to be bound to the non-focusable `.diff-view` section, so it
    // did nothing once the focus left the diff rows. The composer's own staged
    // Escape still answers first whenever the focus is inside it (an armed
    // discard, then the overflow, then the composer), because that handler
    // calls `preventDefault` and this map never answers a key twice. The text
    // stays either way: it lives in the per-anchor buffer, not in the composer.
    if (composerAnchor === null) return false;
    inline.close();
    return true;
  };
  useReviewKeyMap(
    workspaceNode?.parentElement ?? workspaceNode,
    [
      { key: "c", run: composeOnSelection },
      {
        key: "C",
        shift: true,
        run: () => requestDrawerOpen(review),
      },
      { key: "]", run: () => stepFile(1) },
      { key: "[", run: () => stepFile(-1) },
      { key: "n", run: () => stepRow(1) },
      { key: "p", run: () => stepRow(-1) },
      { key: "r", run: replyToFocusedThread },
      {
        key: "Enter",
        primary: true,
        run: () => pressComposerPrimary(rowsNode),
      },
      { key: "Escape", run: closeComposer },
    ],
    // The review drawer is a dialog over the diff. It is not `aria-modal`,
    // because the diff behind it stays readable, but it does own the keyboard
    // while it is open: `v` and `Esc` are its keys, and a `n` typed with the
    // focus in the diff behind it must not move the diff underneath. The
    // drawer's own map claims on this same predicate, so everything this one
    // stands down for is offered there.
    { standDown: reviewDrawerIsOpen },
  );
  // The changed-file list gets one handler per store, so a memoised entry
  // re-renders only when its own file, slice or active state changes. The
  // handler reads the current file through a ref, synced after each commit,
  // instead of closing over one render's value; the store's setter is stable.
  const selectedRef = useRef(selected);
  useLayoutEffect(() => {
    selectedRef.current = selected;
  }, [selected]);
  const selectFile = useCallback(
    (fileIndex: number, rowIndex: number) => {
      const moved = fileIndex !== selectedRef.current;
      setSelected(fileIndex);
      setTarget(rowIndex);
      if (moved) {
        setComposerAnchor(null);
        anchors.select(null);
      }
    },
    [anchors],
  );
  const moveFocus = (event: KeyboardEvent<HTMLElement>): void => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const buttons = [
      ...event.currentTarget.querySelectorAll<HTMLButtonElement>("button"),
    ];
    const current = buttons.indexOf(
      document.activeElement as HTMLButtonElement,
    );
    const direction = event.key === "ArrowDown" ? 1 : -1;
    const next =
      buttons[
        (current < 0 ? 0 : current + direction + buttons.length) %
          buttons.length
      ];
    if (next) {
      event.preventDefault();
      next.focus();
      next.click();
    }
  };
  return (
    <div className="diff-workspace" ref={setWorkspaceNode}>
      <nav
        className="file-list"
        aria-label="Changed files"
        onKeyDown={moveFocus}
      >
        {files.map((file) => (
          <FileNavigation
            key={file.file_index}
            file={file}
            slice={sliceFor(file.file_index)}
            active={selected === file.file_index}
            viewed={viewed.files.has(viewedFileKey(file))}
            select={selectFile}
          />
        ))}
        {files.length === 0 && (
          <Notice kind="empty">No file metadata was returned.</Notice>
        )}
      </nav>
      <section
        className={`diff-view diff-${loaded.layout}`}
        data-layout={loaded.layout}
        ref={setRowsNode}
      >
        {threads.loadError && (
          <Notice kind="warning">{threads.loadError}</Notice>
        )}
        {/*
          A refusal raised by an Edit or a Delete pressed on a pending card is
          the controller's, and the composer used to be the only thing that
          rendered it, so a press made with no composer open was refused in
          silence. The drawer made that ordinary: it writes the same draft, so
          a save in flight there refuses a press here. The composer still shows
          its own copy while it is open, so this stands in only when it is not.
        */}
        {composerAnchor === null && composerController.message !== null && (
          <Notice kind="error">
            <span>{composerController.message}</span>
            <button
              className="button button-secondary notice-action"
              onClick={composerController.clearMessage}
            >
              Dismiss
            </button>
          </Notice>
        )}
        {forgeFile !== null && !selectedViewed && (
          <FileNotices
            file={forgeFile}
            context={fileContext}
            cancel={() => contextStore.cancelGeneration(forgeFile.file_index)}
            dismiss={() => contextStore.notify(forgeFile.file_index, null)}
          />
        )}
        <DiffRowsWindow
          review={review}
          loaded={loaded}
          items={visible}
          pageSize={200}
          targetIndex={visibleTarget}
          targetKey={`${selected}:${target}`}
          file={selectedFile}
          extras={extras}
          selection={selection}
          selectAnchor={selectAnchor}
          inline={inline}
          drag={drag}
          pending={composerController.pending}
          threads={threads}
          viewed={selectedViewed}
          markViewed={markViewed}
        />
      </section>
      {fileTools !== null &&
        createPortal(
          <FullFileToggle
            on={fileContext.full !== null}
            busy={fileContext.fullLoading}
            refusal={fullRefusal}
            toggle={() =>
              void contextStore.toggleFull(selected, gapRows, sourceRowCount)
            }
          />,
          fileTools,
        )}
    </div>
  );
}

/**
 * The toolbar's Full file toggle for the selected file. It is disabled, with
 * the reason as its tooltip, on a file whose unchanged lines cannot be shown.
 */
function FullFileToggle({
  on,
  busy,
  refusal,
  toggle,
}: {
  readonly on: boolean;
  readonly busy: boolean;
  readonly refusal: string | null;
  readonly toggle: () => void;
}): ReactNode {
  const disabled = refusal !== null && !on;
  return (
    <button
      className={`button ${on ? "button-active" : "button-secondary"} full-file-toggle`}
      aria-pressed={on}
      aria-busy={busy}
      disabled={disabled}
      title={
        disabled
          ? refusal
          : on
            ? "Show only the changed lines and the gaps you expanded"
            : "Show every line of this file"
      }
      onClick={toggle}
    >
      {busy ? "Full file…" : "Full file"}
    </button>
  );
}

/**
 * The notices above a file's rows: the truncation notice of a file the forge
 * did not send whole, with the progress and Cancel of its local generation,
 * and the last refusal or failure for the file's unchanged lines.
 */
function FileNotices({
  file,
  context,
  cancel,
  dismiss,
}: {
  /** The file as the forge sent it. */
  readonly file: DiffFileRow;
  readonly context: FileContextView;
  readonly cancel: () => void;
  readonly dismiss: () => void;
}): ReactNode {
  const generation = context.generation;
  return (
    <>
      {file.is_truncated &&
        (generation?.state === "done" ? (
          <Notice kind="generated">{GENERATED_COMMENT_MESSAGE}</Notice>
        ) : generation?.state === "running" ? (
          <Notice kind="loading">
            <span>{generation.notice}</span>
            <button
              className="button button-secondary notice-action"
              onClick={cancel}
            >
              Cancel
            </button>
          </Notice>
        ) : (
          <Notice kind="warning">
            <span>{TRUNCATED_NOTICE}</span>
            {generation?.state === "failed" && (
              <span className="generation-failure"> {generation.message}</span>
            )}
          </Notice>
        ))}
      {context.message !== null && (
        <Notice kind="warning">
          <span>{context.message}</span>
          <button
            className="button button-secondary notice-action"
            onClick={dismiss}
          >
            Dismiss
          </button>
        </Notice>
      )}
    </>
  );
}

function DiffRowsWindow({
  review,
  loaded,
  items,
  pageSize,
  targetIndex,
  targetKey,
  file,
  extras,
  selection,
  selectAnchor,
  inline,
  drag,
  pending,
  threads,
  viewed,
  markViewed,
}: {
  readonly review: string;
  readonly loaded: LoadedDiff;
  readonly items: readonly DiffViewRow[];
  readonly pageSize: number;
  readonly targetIndex: number;
  /**
   * Names the row the reader asked for. The window moves to the target only
   * when this changes, so expanding a gap leaves the window where it is even
   * though the rows below it move down.
   */
  readonly targetKey: string;
  readonly file: DiffFileRow | null;
  readonly extras: RowExtras;
  readonly selection: InlineAnchorSelection | null;
  readonly selectAnchor: (selection: InlineAnchorSelection | null) => void;
  readonly inline: InlineComposerSlot;
  readonly drag: LineRangeDrag;
  readonly pending: readonly PendingDraftEntry[];
  readonly threads: DiscussionThreadSlot;
  /** True when `file` is marked as viewed and `items` is its header alone. */
  readonly viewed: boolean;
  readonly markViewed: MarkViewed;
}): ReactNode {
  const [start, setStart] = useState(
    Math.max(0, Math.floor(targetIndex / pageSize) * pageSize),
  );
  useEffect(
    () => setStart(Math.max(0, Math.floor(targetIndex / pageSize) * pageSize)),
    // The target index is read when the target changes, not when expanded
    // lines move it.
    [pageSize, targetKey],
  );
  // Highlighted once per file and remembered, so paging through the rows or
  // selecting a line does not highlight the file again. Expanded lines are
  // coloured separately, so expanding a gap does not highlight the hunks again.
  const sourceItems = useMemo(
    () =>
      items.filter(
        (row): row is DiffRow =>
          row.kind !== "gap" && row.kind !== "context" && row.kind !== "hide",
      ),
    [items],
  );
  const syntax = useMemo(
    () => highlightFile(file, sourceItems),
    [file, sourceItems],
  );
  const contextSyntax = useMemo(
    () => contextSyntaxOf(file, items),
    [file, items],
  );
  const boundedStart = Math.min(start, Math.max(0, items.length - 1));
  const end = Math.min(items.length, boundedStart + pageSize);
  const windowed = items.slice(boundedStart, end);
  // The file header carries the Viewed checkbox and the file's counts, so a
  // window past the first one repeats it above its rows. Each row is painted
  // with its index in the file, which keys it.
  const header =
    boundedStart > 0 && items[0]?.kind === "file" ? items[0] : null;
  const painted: readonly (readonly [DiffViewRow, number])[] = [
    ...(header === null ? [] : [[header, 0] as const]),
    ...windowed.map((row, offset) => [row, boundedStart + offset] as const),
  ];
  // The card that spells the refusal out is chosen from the cards this window
  // actually paints, in the order the reader meets them. Choosing by stored
  // order instead let the sentence vanish whenever the file's first entry was
  // paged out of the window or was the entry being edited.
  const explains = firstRenderedPendingEntry(
    pending,
    loaded,
    file,
    windowed,
    inline.editing,
  );
  return (
    <>
      {loaded.layout === "split" ? (
        <div className="split-panes">
          {(["old", "new"] as const).map((side) => (
            <div className={`split-pane split-pane-${side}`} key={side}>
              <div className="split-pane-content" role="list">
                {painted.map(([row, index]) => (
                  <SplitPaneRow
                    key={`${side}:${row.kind}:${row.file_index}:${"hunk_index" in row ? row.hunk_index : 0}:${index}`}
                    review={review}
                    loaded={loaded}
                    row={row}
                    file={file}
                    side={side}
                    selection={selection}
                    selectAnchor={selectAnchor}
                    inline={inline}
                    drag={drag}
                    pending={pending}
                    explains={explains}
                    threads={threads}
                    syntax={syntax}
                    contextSyntax={contextSyntax}
                    extras={extras}
                    viewed={viewed}
                    markViewed={markViewed}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="windowed-items" role="list">
          {painted.map(([row, index]) => (
            <DiffRowView
              key={`${row.kind}:${row.file_index}:${"hunk_index" in row ? row.hunk_index : 0}:${index}`}
              review={review}
              loaded={loaded}
              row={row}
              file={file}
              selection={selection}
              selectAnchor={selectAnchor}
              inline={inline}
              drag={drag}
              pending={pending}
              explains={explains}
              threads={threads}
              syntax={syntax}
              contextSyntax={contextSyntax}
              extras={extras}
              viewed={viewed}
              markViewed={markViewed}
            />
          ))}
        </div>
      )}
      {viewed && (
        <p className="diff-viewed-note">
          This file is marked as viewed. Clear Viewed to show its changes.
        </p>
      )}
      {items.length > pageSize && (
        <nav className="window-controls" aria-label="Diff row windows">
          <button
            className="button button-secondary"
            disabled={boundedStart === 0}
            onClick={() => setStart(Math.max(0, boundedStart - pageSize))}
          >
            Previous rows
          </button>
          <span className="window-position">
            {boundedStart + 1}-{end} of {items.length}
          </span>
          <button
            className="button button-secondary"
            disabled={end === items.length}
            onClick={() => setStart(end)}
          >
            Next rows
          </button>
        </nav>
      )}
    </>
  );
}

/** One file's rows in diff order, and the hunk rows among them. */
interface FileSlice {
  readonly rows: readonly DiffWireRow[];
  readonly hunks: readonly DiffHunkRow[];
}

const EMPTY_FILES: readonly DiffFileRow[] = Object.freeze([]);

const EMPTY_SLICE: FileSlice = Object.freeze({
  rows: Object.freeze([]),
  hunks: Object.freeze([]),
});

/** Groups the loaded rows by file in one pass, keeping their order. */
function fileSlices(
  rows: readonly DiffWireRow[],
): ReadonlyMap<number, FileSlice> {
  const slices = new Map<
    number,
    { rows: DiffWireRow[]; hunks: DiffHunkRow[] }
  >();
  for (const row of rows) {
    let slice = slices.get(row.file_index);
    if (!slice) {
      slice = { rows: [], hunks: [] };
      slices.set(row.file_index, slice);
    }
    slice.rows.push(row);
    if (row.kind === "hunk") slice.hunks.push(row);
  }
  return slices;
}

function sliceOf(
  slices: ReadonlyMap<number, FileSlice>,
  fileIndex: number,
): FileSlice {
  return slices.get(fileIndex) ?? EMPTY_SLICE;
}

const FileNavigation = memo(function FileNavigation({
  file,
  slice,
  active,
  viewed,
  select,
}: {
  readonly file: DiffFileRow;
  readonly slice: FileSlice;
  readonly active: boolean;
  readonly viewed: boolean;
  readonly select: (fileIndex: number, rowIndex: number) => void;
}): ReactNode {
  return (
    <>
      <button
        className={`file-item${active ? " nav-item-active" : ""}${viewed ? " file-item-viewed" : ""}`}
        onClick={() => select(file.file_index, 0)}
      >
        <span>{file.new_path || file.old_path}</span>
        <span className="file-stats">
          {viewed && <span className="file-viewed-mark">Viewed </span>}
          {`+${file.additions} −${file.deletions}`}
        </span>
      </button>
      {/* A viewed file is collapsed, so its hunks are not offered either. */}
      {!viewed &&
        slice.hunks.map((hunk) => (
          <button
            key={hunk.hunk_index}
            className="file-item hunk-item"
            onClick={() => select(file.file_index, slice.rows.indexOf(hunk))}
          >
            {hunk.context_text || hunk.header}
          </button>
        ))}
    </>
  );
});

function DiffRowView({
  review,
  loaded,
  row,
  file,
  selection,
  selectAnchor,
  inline,
  drag,
  pending,
  explains,
  threads,
  syntax,
  contextSyntax,
  extras,
  viewed,
  markViewed,
}: {
  readonly review: string;
  readonly loaded: LoadedDiff;
  readonly row: DiffViewRow;
  readonly file: DiffFileRow | null;
  readonly selection: InlineAnchorSelection | null;
  readonly selectAnchor: (selection: InlineAnchorSelection | null) => void;
  readonly inline: InlineComposerSlot;
  readonly drag: LineRangeDrag;
  readonly pending: readonly PendingDraftEntry[];
  readonly explains: string | null;
  readonly threads: DiscussionThreadSlot;
  readonly syntax: FileSyntax | null;
  readonly contextSyntax: (row: DiffContextRow) => SyntaxLine | undefined;
  readonly extras: RowExtras;
  readonly viewed: boolean;
  readonly markViewed: MarkViewed;
}): ReactNode {
  if (row.kind === "file") {
    const path =
      row.old_path === row.new_path
        ? row.new_path
        : `${row.old_path} → ${row.new_path}`;
    return (
      <header className="diff-file" role="listitem">
        <strong className="file-path">{path}</strong>
        <span className="file-status">{row.status}</span>
        <FileMode file={row} />
        <FileBadges file={row} extras={extras} controls />
        <PendingFileCount pending={pending} file={row} />
        <ThreadFileCount threads={threads} file={row} />
        <ViewedToggle file={row} viewed={viewed} markViewed={markViewed} />
      </header>
    );
  }
  if (row.kind === "hunk")
    return (
      <div className="diff-hunk" role="listitem">
        {row.header}
      </div>
    );
  if (row.kind === "split") return null;
  if (row.kind === "gap") return <GapRowView row={row} extras={extras} />;
  if (row.kind === "hide") return <HideRowView row={row} extras={extras} />;
  if (row.kind === "context")
    return (
      <div
        className="diff-line line-context line-expanded"
        role="listitem"
        data-expanded="true"
      >
        <span className="line-number">{row.old_line}</span>
        <span className="line-number">{row.new_line}</span>
        <ExpandedLineBody
          row={row}
          line={row.new_line}
          side="new"
          syntax={contextSyntax(row)}
          refuse={extras.refuse}
        />
      </div>
    );
  // A diff generated locally has no forge diff to anchor a comment to.
  const selectable =
    row.line_type !== "no_newline" && file !== null && !extras.readOnly;
  const defaultSide =
    row.line_type === "deletion"
      ? "old"
      : row.new_line !== null
        ? "new"
        : "old";
  const choose = (side: "old" | "new", extend = false): void => {
    if (!selectable || !file) return;
    selectAnchor(
      selectionForUnifiedLine(
        review,
        loaded,
        file,
        row,
        side,
        selection,
        extend,
      ),
    );
  };
  const compose = (side: "old" | "new"): void => {
    if (!selectable || !file) return;
    // A row inside a multi-line selection composes on the whole range: the
    // composer claims it, and the app-owned selection is left as the reader
    // built it.
    const range =
      selection !== null &&
      (selection.selectedLines?.length ?? 0) > 1 &&
      selectionMatchesLine(selection, loaded, file, row, side)
        ? rangeAnchorForSelection(selection, review, loaded, file)
        : null;
    inline.open(
      range ?? selectionForUnifiedLine(review, loaded, file, row, side),
      range !== null,
    );
  };
  const selected =
    selection !== null &&
    selectionMatchesLine(selection, loaded, file, row, defaultSide);
  const composerOpen =
    inline.anchor !== null &&
    composerOnUnifiedRow(inline.anchor, loaded, file, row);
  const entries = pendingOnUnifiedRow(pending, file, row);
  const anchoredThreads = threadsOnUnifiedRow(threads, file, row);
  const editEntry = (entry: PendingDraftEntry): void => {
    if (!file || !entry.anchor) return;
    // The composer takes the row the card sits on, which is the line the
    // stored anchor names. Nothing is recaptured: the entry keeps the anchor
    // it was saved with, and the selection the reader built is left alone.
    inline.open(
      selectionForUnifiedLine(review, loaded, file, row, entry.anchor.side),
      true,
      entry,
    );
  };
  return (
    <>
      <div
        className={`diff-line line-${row.line_type}${selected ? " line-selected" : ""}`}
        role="listitem"
        data-selected-side={selected ? selection.side : undefined}
      >
        <LineNumberAnchor
          label="old"
          value={row.old_line}
          selected={
            selection !== null &&
            selectionMatchesLine(selection, loaded, file, row, "old")
          }
          selectable={selectable}
          choose={(extend) => choose("old", extend)}
          drag={drag}
          hunkIndex={row.hunk_index}
          oldLine={row.old_line}
          newLine={row.new_line}
        />
        <LineNumberAnchor
          label="new"
          value={row.new_line}
          selected={
            selection !== null &&
            selectionMatchesLine(selection, loaded, file, row, "new")
          }
          selectable={selectable}
          choose={(extend) => choose("new", extend)}
          drag={drag}
          hunkIndex={row.hunk_index}
          oldLine={row.old_line}
          newLine={row.new_line}
        />
        <GutterComment
          selectable={selectable}
          side={defaultSide}
          line={defaultSide === "old" ? row.old_line : row.new_line}
          compose={() => compose(defaultSide)}
        />
        <code
          className={`line-content${selectable ? " selectable-line" : ""}`}
          role={selectable ? "button" : undefined}
          tabIndex={selectable ? 0 : undefined}
          aria-label={
            selectable
              ? `Select ${defaultSide} line ${defaultSide === "old" ? row.old_line : row.new_line}`
              : undefined
          }
          onClick={
            selectable
              ? (event) => choose(defaultSide, event.shiftKey)
              : undefined
          }
          onKeyDown={
            selectable
              ? (event) => {
                  if (isCommentKey(event)) {
                    event.preventDefault();
                    compose(defaultSide);
                    return;
                  }
                  activateOnKeyboard(event, () =>
                    choose(defaultSide, event.shiftKey),
                  );
                }
              : undefined
          }
        >
          {marker(row.line_type)}
          <SyntaxContent line={syntax?.get(row)} text={row.content} />
        </code>
      </div>
      {composerOpen && inline.anchor && (
        <div className="inline-composer-row" role="listitem">
          <InlineComposer
            key={composerKey(inline)}
            anchor={inline.anchor}
            controller={inline.controller}
            entry={inline.editing}
            close={inline.close}
          />
        </div>
      )}
      {anchoredThreads.map((thread) => (
        <div
          className={threadRowClassName(threads, thread)}
          role="listitem"
          key={thread.discussion.id}
          {...{ [REVIEW_ROW_ATTRIBUTE]: threadRowKey(thread.discussion.id) }}
        >
          <DiffThread thread={thread} slot={threads} />
        </div>
      ))}
      {entries.map((entry) =>
        inline.editing?.id === entry.id ? null : (
          <div
            className="pending-card-row"
            role="listitem"
            key={entry.id}
            {...{ [REVIEW_ROW_ATTRIBUTE]: pendingRowKey(entry.id) }}
          >
            <PendingCard
              entry={entry}
              reason={inline.controller.entryReason}
              explain={entry.id === explains}
              busy={inline.controller.busy}
              openExternal={inline.controller.openExternal}
              edit={() => editEntry(entry)}
              remove={() => void inline.controller.removeEntry(entry.id)}
            />
          </div>
        ),
      )}
    </>
  );
}

/**
 * The composer keeps the text it is collecting in its own state, so it has to
 * be remounted whenever what it is composing changes. A new comment and one or
 * more pending cards share a row, and Edit swaps the composer's shape without
 * moving it, so without this key React reconciles the two and one comment's
 * unsent text is saved into another.
 */
function composerKey(slot: InlineComposerSlot): string {
  return slot.editing
    ? `edit:${slot.editing.id}`
    : `new:${anchorIdentity(slot.anchor)}`;
}

/** Marks one file as viewed, or clears the mark. */
export type MarkViewed = (file: DiffFileRow, viewed: boolean) => void;

/**
 * The Viewed checkbox in a file header (#195). It is a native checkbox inside
 * its label, so it takes the focus in tab order and is named "Viewed". Space
 * toggles it on the press itself; `preventDefault` stops the browser's own
 * click on key release, which would otherwise toggle it a second time.
 */
function ViewedToggle({
  file,
  viewed,
  markViewed,
}: {
  readonly file: DiffFileRow;
  readonly viewed: boolean;
  readonly markViewed: MarkViewed;
}): ReactNode {
  return (
    <label className="file-viewed">
      <input
        type="checkbox"
        checked={viewed}
        onChange={(event) => markViewed(file, event.currentTarget.checked)}
        onKeyDown={(event) => {
          if (event.key !== " ") return;
          if (event.ctrlKey || event.metaKey || event.altKey) return;
          event.preventDefault();
          // A held Space toggles once, not once per repeat.
          if (!event.repeat) markViewed(file, !viewed);
        }}
      />
      Viewed
    </label>
  );
}

/**
 * "N pending" in the file header, counting the entries anchored to this file.
 * The composer chip counts the whole review, so the header says which scope it
 * is naming.
 */
function PendingFileCount({
  pending,
  file,
}: {
  readonly pending: readonly PendingDraftEntry[];
  readonly file: DiffFileRow;
}): ReactNode {
  const count = pendingOnFile(pending, file).length;
  if (count === 0) return null;
  return (
    <span
      className="file-pending"
      aria-label={`${count} pending review comments on this file`}
    >
      {count} pending
    </span>
  );
}

/**
 * "N threads, M unresolved" in the file header, over the threads anchored
 * anywhere in this file rather than over the ones the window paints. The
 * header is what tells the reader there is more to page to.
 */
function ThreadFileCount({
  threads,
  file,
}: {
  readonly threads: DiscussionThreadSlot;
  readonly file: DiffFileRow;
}): ReactNode {
  const counts = threadFileCounts(threads, file.old_path, file.new_path);
  if (counts.threads === 0) return null;
  const text = threadFileCountText(counts);
  return (
    <span className="file-threads" aria-label={`${text} on this file`}>
      {text}
    </span>
  );
}

/**
 * The published threads that belong under one unified row. A unified row can
 * carry both sides of the diff, so both sides are read, new side first, which
 * is the order the file's own columns are in.
 */
function threadsOnUnifiedRow(
  threads: DiscussionThreadSlot,
  file: DiffFileRow | null,
  row: Extract<DiffRow, { readonly kind: "line" }>,
): readonly AnchoredThread[] {
  if (!file) return EMPTY_THREADS;
  const onNew = threadsOnLine(threads, file.new_path, "new", row.new_line);
  const onOld = threadsOnOldLine(threads, file, row.old_line);
  if (onOld.length === 0) return onNew;
  if (onNew.length === 0) return onOld;
  return [...onNew, ...onOld];
}

/** The same match on a split row, read through the cell each side owns. */
function threadsOnSplitRow(
  threads: DiscussionThreadSlot,
  file: DiffFileRow | null,
  row: SplitDiffRow,
): readonly AnchoredThread[] {
  if (!file) return EMPTY_THREADS;
  const onNew =
    row.new?.anchor_side === "new"
      ? threadsOnLine(threads, file.new_path, "new", row.new.new_line)
      : EMPTY_THREADS;
  const onOld =
    row.old?.anchor_side === "old"
      ? threadsOnOldLine(threads, file, row.old.old_line)
      : EMPTY_THREADS;
  if (onOld.length === 0) return onNew;
  if (onNew.length === 0) return onOld;
  return [...onNew, ...onOld];
}

const EMPTY_THREADS: readonly AnchoredThread[] = Object.freeze([]);

/**
 * The pending entry that carries the once-per-file refusal sentence: the first
 * one this window paints, skipping the entry whose card has given way to the
 * composer that is editing it. A sentence chosen by stored order disappeared
 * whenever that entry was outside the window or under edit, which is the S44
 * refusal going silent on a read-only review.
 */
function firstRenderedPendingEntry(
  pending: readonly PendingDraftEntry[],
  loaded: LoadedDiff,
  file: DiffFileRow | null,
  windowed: readonly DiffViewRow[],
  editing: PendingDraftEntry | null,
): string | null {
  if (!file || pending.length === 0) return null;
  for (const row of windowed) {
    const entries =
      loaded.layout === "split"
        ? row.kind === "split"
          ? pendingOnSplitRow(pending, file, row)
          : []
        : row.kind === "line"
          ? pendingOnUnifiedRow(pending, file, row)
          : [];
    for (const entry of entries) if (entry.id !== editing?.id) return entry.id;
  }
  return null;
}

/** The pending entries whose stored anchor names this file. */
function pendingOnFile(
  pending: readonly PendingDraftEntry[],
  file: DiffFileRow,
): readonly PendingDraftEntry[] {
  return pending.filter(
    (entry) =>
      entry.anchor !== null &&
      entry.anchor.old_path === file.old_path &&
      entry.anchor.new_path === file.new_path,
  );
}

/**
 * The entries that belong under one unified row. A range entry is anchored on
 * the line that closes it, so its card lands under the end of the range, which
 * is where the composer that wrote it stood.
 */
function pendingOnUnifiedRow(
  pending: readonly PendingDraftEntry[],
  file: DiffFileRow | null,
  row: Extract<DiffRow, { readonly kind: "line" }>,
): readonly PendingDraftEntry[] {
  if (!file) return [];
  return pendingOnFile(pending, file).filter((entry) => {
    const anchor = entry.anchor!;
    return anchor.side === "old"
      ? row.old_line !== null && row.old_line === anchor.old_line
      : row.new_line !== null && row.new_line === anchor.new_line;
  });
}

/** The same match on a split row, read through the cell the anchor names. */
function pendingOnSplitRow(
  pending: readonly PendingDraftEntry[],
  file: DiffFileRow | null,
  row: SplitDiffRow,
): readonly PendingDraftEntry[] {
  if (!file) return [];
  return pendingOnFile(pending, file).filter((entry) => {
    const anchor = entry.anchor!;
    const cell = row[anchor.side];
    if (!cell || cell.anchor_side !== anchor.side) return false;
    return anchor.side === "old"
      ? cell.old_line !== null && cell.old_line === anchor.old_line
      : cell.new_line !== null && cell.new_line === anchor.new_line;
  });
}

/**
 * The gutter affordance the design puts on every source row. It stays in the
 * grid when a row cannot be commented on, so the code column never shifts.
 */
function GutterComment({
  selectable,
  side,
  line,
  compose,
}: {
  readonly selectable: boolean;
  readonly side: "old" | "new";
  readonly line: number | null;
  readonly compose: () => void;
}): ReactNode {
  if (!selectable || line === null)
    return <span className="gutter-comment gutter-comment-empty" />;
  return (
    <button
      className="gutter-comment"
      aria-label={`Comment on ${side} line ${line}`}
      title="Comment on this line"
      onClick={(event) => {
        // The split cell is itself a select control, so the press has to stop
        // here: letting it bubble would re-select this one row and collapse
        // the range the composer just claimed.
        event.stopPropagation();
        compose();
      }}
      onKeyDown={(event) => {
        // The same containment for the keyboard path. Enter and Space on this
        // button used to reach the split cell's own handler, which calls
        // `preventDefault` and re-selects the one row: the range collapsed,
        // and the suppressed default meant the activation click was never
        // synthesised either, so the composer did not open at all. The press
        // is carried out here instead of being left to that synthesis, which
        // is how the row and the cell already answer their own keys.
        if (event.key !== "Enter" && event.key !== " ") return;
        if (event.ctrlKey || event.metaKey || event.altKey) return;
        event.preventDefault();
        event.stopPropagation();
        compose();
      }}
    >
      +
    </button>
  );
}

/** The design's keyboard equivalent of the gutter affordance (section 2.8). */
function isCommentKey(event: KeyboardEvent<HTMLElement>): boolean {
  return (
    event.key === "c" &&
    !event.ctrlKey &&
    !event.metaKey &&
    !event.altKey &&
    !event.shiftKey
  );
}

function LineNumberAnchor({
  label,
  value,
  selected,
  selectable,
  choose,
  drag,
  hunkIndex,
  oldLine,
  newLine,
}: {
  readonly label: "old" | "new";
  readonly value: number | null;
  readonly selected: boolean;
  readonly selectable: boolean;
  readonly choose: (extend: boolean) => void;
  readonly drag: LineRangeDrag;
  readonly hunkIndex: number;
  readonly oldLine: number | null;
  readonly newLine: number | null;
}): ReactNode {
  const dragRow: DragRow = { side: label, hunkIndex, oldLine, newLine };
  if (value === null || !selectable)
    return <span className="line-number">{value}</span>;
  return (
    <button
      className={`line-number line-anchor${selected ? " line-anchor-selected" : ""}`}
      aria-label={`Select ${label} line ${value}`}
      aria-pressed={selected}
      onMouseDown={(event) => {
        if (event.button !== 0) return;
        // Suppresses the native text selection the drag would otherwise paint
        // over the diff, and takes back the focus that suppression drops. A
        // Shift press keeps the range origin it extends from.
        event.preventDefault();
        event.currentTarget.focus();
        choose(event.shiftKey);
        drag.begin(dragRow);
      }}
      onMouseOver={(event) => {
        // Only a held primary button extends. A release the document listener
        // never saw cannot leave a plain hover rebuilding the selection.
        if ((event.buttons & 1) !== 1) return;
        drag.extendTo(dragRow, () => choose(true));
      }}
      onClick={(event) => choose(event.shiftKey)}
      onKeyDown={(event) =>
        activateOnKeyboard(event, () => choose(event.shiftKey))
      }
    >
      {value}
    </button>
  );
}

function SplitPaneRow({
  review,
  loaded,
  row,
  file,
  side,
  selection,
  selectAnchor,
  inline,
  drag,
  pending,
  explains,
  threads,
  syntax,
  contextSyntax,
  extras,
  viewed,
  markViewed,
}: {
  readonly review: string;
  readonly loaded: LoadedDiff;
  readonly row: DiffViewRow;
  readonly file: DiffFileRow | null;
  readonly side: "old" | "new";
  readonly selection: InlineAnchorSelection | null;
  readonly selectAnchor: (selection: InlineAnchorSelection | null) => void;
  readonly inline: InlineComposerSlot;
  readonly drag: LineRangeDrag;
  readonly pending: readonly PendingDraftEntry[];
  readonly explains: string | null;
  readonly threads: DiscussionThreadSlot;
  readonly syntax: FileSyntax | null;
  readonly contextSyntax: (row: DiffContextRow) => SyntaxLine | undefined;
  readonly extras: RowExtras;
  readonly viewed: boolean;
  readonly markViewed: MarkViewed;
}): ReactNode {
  if (row.kind === "file")
    return (
      <header className="diff-file split-metadata" role="listitem">
        <strong className="file-path">
          {side === "old" ? row.old_path : row.new_path}
        </strong>
        <span className="file-status">{row.status}</span>
        <FileMode file={row} />
        {/* The new side's header carries the Generate diff locally button. */}
        <FileBadges file={row} extras={extras} controls={side === "new"} />
        {side === "new" && <PendingFileCount pending={pending} file={row} />}
        {side === "new" && <ThreadFileCount threads={threads} file={row} />}
        {/* One control per file: the new side's header carries it, as it
            carries the file's counts. */}
        {side === "new" && (
          <ViewedToggle file={row} viewed={viewed} markViewed={markViewed} />
        )}
      </header>
    );
  if (row.kind === "hunk")
    return (
      <div className="diff-hunk split-metadata" role="listitem">
        {row.header}
      </div>
    );
  // A gap spans both panes, so each pane draws it and either one expands it.
  if (row.kind === "gap")
    return <GapRowView row={row} extras={extras} side={side} />;
  if (row.kind === "hide")
    return <HideRowView row={row} extras={extras} side={side} />;
  if (row.kind === "context") {
    const line = side === "old" ? row.old_line : row.new_line;
    return (
      <div
        className={`split-cell split-${side} line-context line-expanded`}
        role="listitem"
        data-expanded="true"
        data-old-line={row.old_line}
        data-new-line={row.new_line}
      >
        <span className="line-number">{line}</span>
        <ExpandedLineBody
          row={row}
          line={line}
          side={side}
          syntax={contextSyntax(row)}
          refuse={extras.refuse}
        />
      </div>
    );
  }
  if (row.kind !== "split") return null;
  const composerOpen =
    inline.anchor !== null &&
    composerOnSplitRow(inline.anchor, loaded, file, row);
  // Both panes read the same entry list for the same row, so the pane that
  // does not own the anchor side renders one spacer per card and the two
  // independent pane grids stay on the same rows.
  const entries = pendingOnSplitRow(pending, file, row);
  // Both panes read the same thread list for the same row, and the pane that
  // does not own the anchored side renders one spacer per thread, so the two
  // grids stay on the same rows however many threads a line carries.
  const anchoredThreads = threadsOnSplitRow(threads, file, row);
  const editEntry = (entry: PendingDraftEntry): void => {
    const cell = entry.anchor ? row[entry.anchor.side] : null;
    if (!file || !entry.anchor || !cell) return;
    inline.open(
      selectionForSplitCell(review, loaded, file, row, cell),
      true,
      entry,
    );
  };
  return (
    <>
      <SplitCell
        review={review}
        loaded={loaded}
        row={row}
        file={file}
        cell={row[side]}
        visualSide={side}
        selection={selection}
        selectAnchor={selectAnchor}
        inline={inline}
        drag={drag}
        syntax={syntax}
        readOnly={extras.readOnly}
      />
      {composerOpen &&
        inline.anchor &&
        (inline.anchor.side === side ? (
          <div className="inline-composer-row" role="listitem">
            <InlineComposer
              key={composerKey(inline)}
              anchor={inline.anchor}
              controller={inline.controller}
              entry={inline.editing}
              close={inline.close}
            />
          </div>
        ) : (
          // The panes are two independent columns, so the pane that does not
          // hold the composer keeps its rows aligned with a spacer of the same
          // fixed height.
          <div
            className="inline-composer-mirror"
            role="presentation"
            aria-hidden="true"
          />
        ))}
      {anchoredThreads.map((thread) =>
        thread.target.side === side ? (
          <div
            className={threadRowClassName(threads, thread)}
            role="listitem"
            key={thread.discussion.id}
            {...{ [REVIEW_ROW_ATTRIBUTE]: threadRowKey(thread.discussion.id) }}
          >
            <DiffThread thread={thread} slot={threads} />
          </div>
        ) : (
          <DiffThreadMirror
            key={thread.discussion.id}
            expanded={threadRowExpanded(threads, thread)}
          />
        ),
      )}
      {entries.map((entry) =>
        inline.editing?.id === entry.id ? null : entry.anchor?.side === side ? (
          <div
            className="pending-card-row"
            role="listitem"
            key={entry.id}
            {...{ [REVIEW_ROW_ATTRIBUTE]: pendingRowKey(entry.id) }}
          >
            <PendingCard
              entry={entry}
              reason={inline.controller.entryReason}
              explain={entry.id === explains}
              busy={inline.controller.busy}
              openExternal={inline.controller.openExternal}
              edit={() => editEntry(entry)}
              remove={() => void inline.controller.removeEntry(entry.id)}
            />
          </div>
        ) : (
          <PendingCardMirror key={entry.id} />
        ),
      )}
    </>
  );
}

// The sidecar derives every shape the forge payload determines, so each badge
// states a fact about the file.  The last entry is the honest residue: GitHub's
// files endpoint withholds the patch for binary content and for a mode-only
// change without distinguishing them, so the badge names the forge as the limit
// instead of implying the client failed to load the file.
const FILE_BADGES: ReadonlyArray<
  readonly [(file: DiffFileRow) => boolean, string, string?]
> = [
  [(file) => file.is_binary, "Binary"],
  [(file) => file.is_truncated, "Truncated"],
  [(file) => file.is_empty, "Empty"],
  [(file) => file.is_mode_only, "Mode only"],
  [(file) => file.is_rename_only, "Rename only"],
  [
    (file) => file.is_unavailable,
    "Not exposed by forge",
    "The forge sent no diff content for this file and did not report whether it is a binary or a mode-only change.",
  ],
];

// GitLab and git patches report file modes; GitHub's files endpoint does not,
// so its rows carry null and show nothing here.
function fileModeChange(file: DiffFileRow): string {
  const oldMode = file.old_mode ?? null;
  const newMode = file.new_mode ?? null;
  if (oldMode === null || newMode === null || oldMode === newMode) return "";
  return `mode ${oldMode} -> ${newMode}`;
}

function FileMode({ file }: { readonly file: DiffFileRow }): ReactNode {
  const change = fileModeChange(file);
  return change ? (
    <span className="file-status file-mode">{change}</span>
  ) : null;
}

/**
 * The file's badges. A generated file says so. In the header that carries the
 * file's controls, a truncated file's badge is the Generate diff locally
 * button instead.
 */
function FileBadges({
  file,
  extras,
  controls,
}: {
  readonly file: DiffFileRow;
  readonly extras: RowExtras;
  readonly controls: boolean;
}): ReactNode {
  const generation = extras.generation;
  return (
    <>
      {FILE_BADGES.map(([matches, label, hint]) =>
        !matches(file) ? null : label === "Truncated" && controls ? (
          <button
            key={label}
            className="button button-secondary file-generate"
            title={TRUNCATED_NOTICE}
            disabled={generation?.state === "running"}
            onClick={() => extras.generate(file)}
          >
            Generate diff locally
          </button>
        ) : (
          <span key={label} className="badge" title={hint}>
            {label}
          </span>
        ),
      )}
      {generation?.state === "done" && (
        <span className="badge" title={GENERATED_COMMENT_MESSAGE}>
          {GENERATED_LABEL}
        </span>
      )}
    </>
  );
}

/**
 * A collapsed gap: activating it reads and shows its unchanged lines. `side`
 * names the split pane that draws it; a unified row has none.
 */
function GapRowView({
  row,
  extras,
  side = null,
}: {
  readonly row: DiffGapRow;
  readonly extras: RowExtras;
  readonly side?: "old" | "new" | null;
}): ReactNode {
  const loading = extras.loadingGaps.has(row.gap_index);
  const expand = (): void => extras.expandGap(row);
  const button = useRef<HTMLButtonElement>(null);
  // A gap that was just collapsed from its hide row takes the focus that row
  // had, in the same pane, so the keyboard stays where the reader was.
  useEffect(() => {
    const target = extras.collapsedFocus.current;
    if (target === null || target.gap !== row.gap_index || target.side !== side)
      return;
    extras.collapsedFocus.current = null;
    button.current?.focus();
  }, [extras.collapsedFocus, row.gap_index, side]);
  return (
    <div
      className={`diff-hunk diff-gap${side === null ? "" : " split-metadata"}`}
      role="listitem"
      data-gap-index={row.gap_index}
      data-gap-position={row.position}
    >
      <button
        ref={button}
        className="button button-quiet diff-gap-button"
        aria-busy={loading}
        aria-label={
          row.count === null
            ? "Expand the lines below"
            : `Expand ${gapLabel(row.count)}`
        }
        onClick={expand}
        onKeyDown={(event) => activateOnKeyboard(event, expand)}
      >
        {loading ? "Loading…" : gapLabel(row.count)}
      </button>
    </div>
  );
}

/** The row above an expanded gap: activating it collapses the gap again. */
function HideRowView({
  row,
  extras,
  side = null,
}: {
  readonly row: DiffHideRow;
  readonly extras: RowExtras;
  readonly side?: "old" | "new" | null;
}): ReactNode {
  const hide = (): void => {
    extras.collapsedFocus.current = { gap: row.gap_index, side };
    if (!extras.collapseGap(row)) extras.collapsedFocus.current = null;
  };
  return (
    <div
      className={`diff-hunk diff-gap diff-gap-hide${side === null ? "" : " split-metadata"}`}
      role="listitem"
      data-gap-index={row.gap_index}
    >
      <button
        className="button button-quiet diff-gap-hide-button"
        aria-label={`Hide ${row.count.toLocaleString("en-US")} unchanged ${row.count === 1 ? "line" : "lines"}`}
        onClick={hide}
        onKeyDown={(event) => activateOnKeyboard(event, hide)}
      >
        {hideLabel(row.count)}
      </button>
    </div>
  );
}

/**
 * The gutter and code of an expanded line. It is never a comment anchor: the
 * gutter button and `c` explain that instead of opening a composer.
 */
function ExpandedLineBody({
  row,
  line,
  side,
  syntax,
  refuse,
}: {
  readonly row: DiffContextRow;
  readonly line: number;
  readonly side: "old" | "new";
  readonly syntax: SyntaxLine | undefined;
  readonly refuse: (message: string) => void;
}): ReactNode {
  return (
    <>
      <button
        className="gutter-comment gutter-comment-readonly"
        aria-label={`Comment on ${side} line ${line}`}
        title={EXPANDED_READ_ONLY_MESSAGE}
        onClick={() => refuse(EXPANDED_READ_ONLY_MESSAGE)}
        onKeyDown={(event) => {
          if (event.key !== "Enter" && event.key !== " ") return;
          if (event.ctrlKey || event.metaKey || event.altKey) return;
          event.preventDefault();
          refuse(EXPANDED_READ_ONLY_MESSAGE);
        }}
      >
        +
      </button>
      <code
        className="line-content"
        tabIndex={0}
        onKeyDown={(event) => {
          if (!isCommentKey(event)) return;
          event.preventDefault();
          refuse(EXPANDED_READ_ONLY_MESSAGE);
        }}
      >
        {marker(row.kind)}
        <SyntaxContent line={syntax} text={row.content} />
      </code>
    </>
  );
}

function SplitCell({
  review,
  loaded,
  row,
  file,
  cell,
  visualSide,
  selection,
  selectAnchor,
  inline,
  drag,
  syntax,
  readOnly,
}: {
  readonly review: string;
  readonly loaded: LoadedDiff;
  readonly row: SplitDiffRow;
  readonly file: DiffFileRow | null;
  readonly cell: SplitDiffCell | null;
  readonly visualSide: "old" | "new";
  readonly selection: InlineAnchorSelection | null;
  readonly selectAnchor: (selection: InlineAnchorSelection | null) => void;
  readonly inline: InlineComposerSlot;
  readonly drag: LineRangeDrag;
  readonly syntax: FileSyntax | null;
  /** True for a diff generated locally, whose cells are never anchors. */
  readonly readOnly: boolean;
}): ReactNode {
  const cellNode = useRef<HTMLDivElement | null>(null);
  const selectable =
    cell?.anchor_side !== null && cell !== null && file !== null && !readOnly;
  const selected =
    selectable &&
    selection !== null &&
    selectionMatchesSplitCell(selection, loaded, file, row, cell);
  const choose = (extend = false): void => {
    if (!selectable || !cell || !file || !cell.anchor_side) return;
    selectAnchor(
      selectionForSplitCell(review, loaded, file, row, cell, selection, extend),
    );
  };
  const draggable = selectable && cell !== null && cell.anchor_side !== null;
  const dragRow: DragRow | null = draggable
    ? {
        side: cell.anchor_side,
        hunkIndex: row.hunk_index,
        oldLine: cell.old_line,
        newLine: cell.new_line,
      }
    : null;
  const compose = (): void => {
    if (!selectable || !cell || !file || !cell.anchor_side) return;
    const range =
      selection !== null &&
      (selection.selectedLines?.length ?? 0) > 1 &&
      selectionMatchesSplitCell(selection, loaded, file, row, cell)
        ? rangeAnchorForSelection(selection, review, loaded, file)
        : null;
    inline.open(
      range ?? selectionForSplitCell(review, loaded, file, row, cell),
      range !== null,
    );
  };
  return (
    <div
      ref={cellNode}
      className={`split-cell split-${visualSide}${cell ? ` line-${cell.line_type}` : " split-empty"}${selectable ? " selectable-line" : ""}${selected ? " line-selected" : ""}`}
      role={selectable ? "button" : "listitem"}
      tabIndex={selectable ? 0 : undefined}
      aria-label={
        selectable && cell?.anchor_side
          ? `Select ${cell.anchor_side} line ${cell.anchor_side === "old" ? cell.old_line : cell.new_line}`
          : undefined
      }
      aria-pressed={selectable ? selected : undefined}
      data-anchor-side={cell?.anchor_side ?? undefined}
      data-old-line={cell?.old_line ?? undefined}
      data-new-line={cell?.new_line ?? undefined}
      onClick={selectable ? (event) => choose(event.shiftKey) : undefined}
      onKeyDown={
        selectable
          ? (event) => {
              if (isCommentKey(event)) {
                event.preventDefault();
                compose();
                return;
              }
              activateOnKeyboard(event, () => choose(event.shiftKey));
            }
          : undefined
      }
    >
      {cell && (
        <>
          <span
            className="line-number"
            // The drag affordance is the number column on both layouts
            // (design 2.1). Binding it to the whole cell would suppress the
            // native text selection over the code column, which is how a
            // reader copies a line out of the diff.
            onMouseDown={
              draggable
                ? (event) => {
                    if (event.button !== 0) return;
                    event.preventDefault();
                    cellNode.current?.focus();
                    choose(event.shiftKey);
                    drag.begin(dragRow!);
                  }
                : undefined
            }
            onMouseOver={
              draggable
                ? (event) => {
                    if ((event.buttons & 1) !== 1) return;
                    drag.extendTo(dragRow!, () => choose(true));
                  }
                : undefined
            }
          >
            {visualSide === "old" ? cell.old_line : cell.new_line}
          </span>
          <GutterComment
            selectable={selectable && cell.anchor_side === visualSide}
            side={visualSide}
            line={visualSide === "old" ? cell.old_line : cell.new_line}
            compose={compose}
          />
          <code className="line-content">
            {marker(cell.line_type)}
            <SyntaxContent line={syntax?.get(cell)} text={cell.content} />
          </code>
        </>
      )}
    </div>
  );
}

function activateOnKeyboard(
  event: KeyboardEvent<HTMLElement>,
  choose: () => void,
): void {
  if (event.key !== "Enter" && event.key !== " ") return;
  // Shift extends the range, so it stays part of a plain activation. Ctrl or
  // Cmd does not: that combination is the composer's primary action in the
  // review keyboard map, and a row that answered it would select itself and
  // swallow the write.
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  event.preventDefault();
  choose();
}

export function selectionForUnifiedLine(
  review: string,
  loaded: LoadedDiff,
  file: DiffFileRow,
  row: Extract<DiffRow, { readonly kind: "line" }>,
  side: "old" | "new",
  existing: InlineAnchorSelection | null = null,
  extend = false,
): InlineAnchorSelection {
  const candidates = unifiedSideRows(loaded, file, row.hunk_index, side);
  const targetIndex = candidates.indexOf(row);
  const originIndex = extend
    ? rangeOriginIndex(
        existing,
        review,
        loaded,
        file,
        row.hunk_index,
        side,
        candidates,
      )
    : -1;
  const selected =
    targetIndex >= 0 && originIndex >= 0
      ? candidates.slice(
          Math.min(targetIndex, originIndex),
          Math.max(targetIndex, originIndex) + 1,
        )
      : [row];
  const origin = originIndex >= 0 ? candidates[originIndex]! : row;
  const context = unifiedRangeContext(loaded, file, selected, side);
  return Object.freeze({
    review,
    snapshotId: loaded.snapshotId,
    resource: loaded.resource,
    revision: loaded.revision,
    fileIndex: file.file_index,
    hunkIndex: row.hunk_index,
    rowIndex: null,
    oldPath: file.old_path,
    newPath: file.new_path,
    side,
    oldLine: row.old_line,
    newLine: row.new_line,
    lineType: row.line_type,
    contextLines: context.lines,
    contextComplete: context.complete,
    rangeOriginOldLine: origin.old_line,
    rangeOriginNewLine: origin.new_line,
    selectedLines: Object.freeze(selected.map(selectedLine)),
  });
}

export function selectionForSplitCell(
  review: string,
  loaded: LoadedDiff,
  file: DiffFileRow,
  row: SplitDiffRow,
  cell: SplitDiffCell,
  existing: InlineAnchorSelection | null = null,
  extend = false,
): InlineAnchorSelection {
  const side = cell.anchor_side;
  if (!side) throw new Error("Cannot select an unanchored split cell");
  const candidates = splitSideRows(loaded, file, row.hunk_index, side);
  const targetIndex = candidates.findIndex((item) => item.row === row);
  const cells = candidates.map((item) => item.cell);
  const originIndex = extend
    ? rangeOriginIndex(
        existing,
        review,
        loaded,
        file,
        row.hunk_index,
        side,
        cells,
      )
    : -1;
  const selected =
    targetIndex >= 0 && originIndex >= 0
      ? candidates.slice(
          Math.min(targetIndex, originIndex),
          Math.max(targetIndex, originIndex) + 1,
        )
      : [{ row, cell }];
  const origin = originIndex >= 0 ? candidates[originIndex]!.cell : cell;
  const context = splitRangeContext(loaded, file, selected, side);
  return Object.freeze({
    review,
    snapshotId: loaded.snapshotId,
    resource: loaded.resource,
    revision: loaded.revision,
    fileIndex: file.file_index,
    hunkIndex: row.hunk_index,
    rowIndex: row.row_index,
    oldPath: file.old_path,
    newPath: file.new_path,
    side,
    oldLine: cell.old_line,
    newLine: cell.new_line,
    lineType: cell.line_type,
    contextLines: context.lines,
    contextComplete: context.complete,
    rangeOriginOldLine: origin.old_line,
    rangeOriginNewLine: origin.new_line,
    selectedLines: Object.freeze(
      selected.map((item) => selectedLine(item.cell)),
    ),
  });
}

/**
 * The selection's span, re-anchored on its last line with its origin on the
 * first, whichever way the reader built it. Every layer under the composer
 * reads the anchored line as the end of a range and `start_line` as its
 * opening, so a drag upward must not hand them a reversed pair, and the
 * composer row belongs under the bottom of the span either way.
 */
function rangeAnchorForSelection(
  selection: InlineAnchorSelection,
  review: string,
  loaded: LoadedDiff,
  file: DiffFileRow,
): InlineAnchorSelection | null {
  // `selectedLines` is always a slice of the side's rows in document order
  // (`selectionForUnifiedLine`, `selectionForSplitCell`), so the first entry
  // opens the span and the last one closes it. `rangeEndpoints` in the
  // composer orders by line number and agrees with this for the same reason.
  const lines = selection.selectedLines ?? [];
  const opening = lines[0];
  const closing = lines.at(-1);
  if (lines.length < 2 || !opening || !closing) return null;
  if (loaded.layout === "split") {
    const cells = splitSideRows(
      loaded,
      file,
      selection.hunkIndex,
      selection.side,
    );
    const first = cells.find((item) => sameSelectedLine(item.cell, opening));
    const last = cells.find((item) => sameSelectedLine(item.cell, closing));
    if (!first || !last) return null;
    const base = selectionForSplitCell(
      review,
      loaded,
      file,
      first.row,
      first.cell,
    );
    return selectionForSplitCell(
      review,
      loaded,
      file,
      last.row,
      last.cell,
      base,
      true,
    );
  }
  const rows = unifiedSideRows(
    loaded,
    file,
    selection.hunkIndex,
    selection.side,
  );
  const first = rows.find((row) => sameSelectedLine(row, opening));
  const last = rows.find((row) => sameSelectedLine(row, closing));
  if (!first || !last) return null;
  const base = selectionForUnifiedLine(
    review,
    loaded,
    file,
    first,
    selection.side,
  );
  return selectionForUnifiedLine(
    review,
    loaded,
    file,
    last,
    selection.side,
    base,
    true,
  );
}

function sameSelectedLine(
  row: {
    readonly old_line: number | null;
    readonly new_line: number | null;
    readonly line_type: string;
    readonly content: string;
  },
  line: InlineSelectedLine,
): boolean {
  return (
    row.old_line === line.oldLine &&
    row.new_line === line.newLine &&
    row.line_type === line.lineType &&
    row.content === line.content
  );
}

type SourceRow = Extract<DiffRow, { readonly kind: "line" }>;

function selectedLine(line: {
  readonly old_line: number | null;
  readonly new_line: number | null;
  readonly line_type: string;
  readonly content: string;
}): InlineSelectedLine {
  return Object.freeze({
    oldLine: line.old_line,
    newLine: line.new_line,
    lineType: line.line_type,
    content: line.content,
  });
}

function unifiedSideRows(
  loaded: LoadedDiff,
  file: DiffFileRow,
  hunkIndex: number,
  side: "old" | "new",
): SourceRow[] {
  return loaded.rows.filter(
    (candidate): candidate is SourceRow =>
      candidate.kind === "line" &&
      candidate.file_index === file.file_index &&
      candidate.hunk_index === hunkIndex &&
      candidate.line_type !== "no_newline" &&
      (side === "old"
        ? candidate.old_line !== null
        : candidate.new_line !== null),
  );
}

function splitSideRows(
  loaded: LoadedDiff,
  file: DiffFileRow,
  hunkIndex: number,
  side: "old" | "new",
): { readonly row: SplitDiffRow; readonly cell: SplitDiffCell }[] {
  return loaded.rows
    .filter(
      (candidate): candidate is SplitDiffRow =>
        candidate.kind === "split" &&
        candidate.file_index === file.file_index &&
        candidate.hunk_index === hunkIndex,
    )
    .map((candidate) => ({ row: candidate, cell: candidate[side] }))
    .filter(
      (
        item,
      ): item is { readonly row: SplitDiffRow; readonly cell: SplitDiffCell } =>
        item.cell !== null &&
        item.cell.line_type !== "no_newline" &&
        (side === "old"
          ? item.cell.old_line !== null
          : item.cell.new_line !== null),
    );
}

function rangeOriginIndex<
  T extends {
    readonly old_line: number | null;
    readonly new_line: number | null;
  },
>(
  existing: InlineAnchorSelection | null,
  review: string,
  loaded: LoadedDiff,
  file: DiffFileRow,
  hunkIndex: number,
  side: "old" | "new",
  candidates: readonly T[],
): number {
  if (
    !existing ||
    existing.review !== review ||
    existing.snapshotId !== loaded.snapshotId ||
    existing.resource !== loaded.resource ||
    !sameRevisionValue(existing.revision, loaded.revision) ||
    existing.fileIndex !== file.file_index ||
    existing.hunkIndex !== hunkIndex ||
    existing.side !== side
  )
    return -1;
  const oldLine =
    existing.rangeOriginOldLine !== undefined
      ? existing.rangeOriginOldLine
      : existing.oldLine;
  const newLine =
    existing.rangeOriginNewLine !== undefined
      ? existing.rangeOriginNewLine
      : existing.newLine;
  return candidates.findIndex(
    (candidate) =>
      candidate.old_line === oldLine && candidate.new_line === newLine,
  );
}

function unifiedRangeContext(
  loaded: LoadedDiff,
  file: DiffFileRow,
  selected: readonly SourceRow[],
  side: "old" | "new",
): SourceContext {
  const first = selected[0];
  const last = selected.at(-1);
  if (!first || !last)
    return Object.freeze({ lines: Object.freeze([]), complete: false });
  const candidates = unifiedSideRows(loaded, file, first.hunk_index, side);
  const firstIndex = candidates.indexOf(first);
  const lastIndex = candidates.indexOf(last);
  if (firstIndex < 0 || lastIndex < 0)
    return Object.freeze({ lines: Object.freeze([]), complete: false });
  return Object.freeze({
    lines: Object.freeze(
      candidates
        .slice(Math.max(0, firstIndex - 2), lastIndex + 3)
        .map((candidate) => candidate.content),
    ),
    complete: contextIsComplete(loaded, file),
  });
}

function splitRangeContext(
  loaded: LoadedDiff,
  file: DiffFileRow,
  selected: readonly {
    readonly row: SplitDiffRow;
    readonly cell: SplitDiffCell;
  }[],
  side: "old" | "new",
): SourceContext {
  const first = selected[0];
  const last = selected.at(-1);
  if (!first || !last)
    return Object.freeze({ lines: Object.freeze([]), complete: false });
  const candidates = splitSideRows(loaded, file, first.row.hunk_index, side);
  const firstIndex = candidates.findIndex((item) => item.row === first.row);
  const lastIndex = candidates.findIndex((item) => item.row === last.row);
  if (firstIndex < 0 || lastIndex < 0)
    return Object.freeze({ lines: Object.freeze([]), complete: false });
  return Object.freeze({
    lines: Object.freeze(
      candidates
        .slice(Math.max(0, firstIndex - 2), lastIndex + 3)
        .map((item) => item.cell.content),
    ),
    complete: contextIsComplete(loaded, file),
  });
}

export interface SourceContext {
  readonly lines: readonly string[];
  readonly complete: boolean;
}

export function unifiedSourceContext(
  loaded: LoadedDiff,
  file: DiffFileRow,
  target: Extract<DiffRow, { readonly kind: "line" }>,
  side: "old" | "new",
): SourceContext {
  const candidates = unifiedSideRows(loaded, file, target.hunk_index, side);
  const index = candidates.indexOf(target);
  if (index < 0)
    return Object.freeze({ lines: Object.freeze([]), complete: false });
  return Object.freeze({
    lines: Object.freeze(
      candidates
        .slice(Math.max(0, index - 2), index + 3)
        .map((row) => row.content),
    ),
    complete: contextIsComplete(loaded, file),
  });
}

export function splitSourceContext(
  loaded: LoadedDiff,
  file: DiffFileRow,
  target: SplitDiffRow,
  side: "old" | "new",
): SourceContext {
  const candidates = splitSideRows(loaded, file, target.hunk_index, side);
  const index = candidates.findIndex((item) => item.row === target);
  if (index < 0)
    return Object.freeze({ lines: Object.freeze([]), complete: false });
  return Object.freeze({
    lines: Object.freeze(
      candidates
        .slice(Math.max(0, index - 2), index + 3)
        .map((item) => item.cell.content),
    ),
    complete: contextIsComplete(loaded, file),
  });
}

function contextIsComplete(loaded: LoadedDiff, file: DiffFileRow): boolean {
  return (
    loaded.partialError === null &&
    !loaded.loadingMore &&
    !file.is_truncated &&
    !file.is_unavailable
  );
}

export function resolveDiscussionTarget(
  review: string,
  loaded: LoadedDiff,
  target: DiscussionDiffTarget,
): InlineAnchorSelection | null {
  if (!Number.isInteger(target.line) || target.line <= 0) return null;
  const files = loaded.rows.filter(
    (row): row is DiffFileRow => row.kind === "file",
  );
  // Both forges store a comment's new path, even on the old side of a renamed
  // file, so an old-side target also matches the file it was renamed to. The
  // file whose old path matches still wins, which is what the old side means.
  const file =
    target.side === "new"
      ? files.find((row) => row.new_path === target.path)
      : (files.find((row) => row.old_path === target.path) ??
        files.find((row) => row.new_path === target.path));
  if (
    !file ||
    file.is_binary ||
    file.is_truncated ||
    file.is_unavailable ||
    file.is_empty ||
    file.is_mode_only ||
    // A rename-only file projects no source rows, so no discussion line can
    // resolve into it. It is deliberately absent from `contextIsComplete`:
    // that flag means the held content is partial, which a rename is not.
    file.is_rename_only
  )
    return null;
  if (loaded.layout === "split") {
    for (const row of loaded.rows) {
      if (row.kind !== "split" || row.file_index !== file.file_index) continue;
      const cell = row[target.side];
      if (
        cell?.anchor_side === target.side &&
        (target.side === "new" ? cell.new_line : cell.old_line) === target.line
      )
        return selectionForSplitCell(review, loaded, file, row, cell);
    }
    return null;
  }
  const row = loaded.rows.find(
    (candidate): candidate is SourceRow =>
      candidate.kind === "line" &&
      candidate.file_index === file.file_index &&
      candidate.line_type !== "no_newline" &&
      (target.side === "new" ? candidate.new_line : candidate.old_line) ===
        target.line,
  );
  return row
    ? selectionForUnifiedLine(review, loaded, file, row, target.side)
    : null;
}

export function rebindInlineAnchor(
  selection: InlineAnchorSelection,
  review: string,
  loaded: LoadedDiff,
): InlineAnchorSelection | null {
  if (
    selection.review !== review ||
    !sameRevisionValue(selection.revision, loaded.revision)
  )
    return null;
  const file = loaded.rows.find(
    (row): row is DiffFileRow =>
      row.kind === "file" &&
      row.old_path === selection.oldPath &&
      row.new_path === selection.newPath,
  );
  if (!file) return null;
  if (loaded.layout === "split") {
    const target = loaded.rows.find(
      (row): row is SplitDiffRow =>
        row.kind === "split" &&
        row.file_index === file.file_index &&
        [row.old, row.new].some(
          (cell) =>
            cell?.anchor_side === selection.side &&
            cell.old_line === selection.oldLine &&
            cell.new_line === selection.newLine &&
            cell.line_type === selection.lineType,
        ),
    );
    if (!target) return null;
    const cell = target[selection.side];
    if (!cell || cell.anchor_side !== selection.side) return null;
    const origin = splitSideRows(
      loaded,
      file,
      target.hunk_index,
      selection.side,
    ).find(
      (item) =>
        item.cell.old_line ===
          (selection.rangeOriginOldLine !== undefined
            ? selection.rangeOriginOldLine
            : selection.oldLine) &&
        item.cell.new_line ===
          (selection.rangeOriginNewLine !== undefined
            ? selection.rangeOriginNewLine
            : selection.newLine),
    );
    if (!origin) return null;
    const base = selectionForSplitCell(
      review,
      loaded,
      file,
      origin.row,
      origin.cell,
    );
    const rebound = selectionForSplitCell(
      review,
      loaded,
      file,
      target,
      cell,
      base,
      Boolean(selection.selectedLines && selection.selectedLines.length > 1),
    );
    return sameSelectedSource(selection, rebound) ? rebound : null;
  }
  const row = loaded.rows.find(
    (candidate): candidate is Extract<DiffRow, { readonly kind: "line" }> =>
      candidate.kind === "line" &&
      candidate.file_index === file.file_index &&
      candidate.old_line === selection.oldLine &&
      candidate.new_line === selection.newLine &&
      candidate.line_type === selection.lineType &&
      candidate.line_type !== "no_newline" &&
      (selection.side === "old"
        ? candidate.old_line !== null
        : candidate.new_line !== null),
  );
  if (!row) return null;
  const origin = unifiedSideRows(
    loaded,
    file,
    row.hunk_index,
    selection.side,
  ).find(
    (candidate) =>
      candidate.old_line ===
        (selection.rangeOriginOldLine !== undefined
          ? selection.rangeOriginOldLine
          : selection.oldLine) &&
      candidate.new_line ===
        (selection.rangeOriginNewLine !== undefined
          ? selection.rangeOriginNewLine
          : selection.newLine),
  );
  if (!origin) return null;
  const base = selectionForUnifiedLine(
    review,
    loaded,
    file,
    origin,
    selection.side,
  );
  const rebound = selectionForUnifiedLine(
    review,
    loaded,
    file,
    row,
    selection.side,
    base,
    Boolean(selection.selectedLines && selection.selectedLines.length > 1),
  );
  return sameSelectedSource(selection, rebound) ? rebound : null;
}

function anchorBelongsToLoadedDiff(
  anchor: InlineAnchorSelection,
  review: string,
  loaded: LoadedDiff,
): boolean {
  return (
    anchor.review === review &&
    anchor.snapshotId === loaded.snapshotId &&
    anchor.resource === loaded.resource &&
    sameRevisionValue(anchor.revision, loaded.revision)
  );
}

function composerOnUnifiedRow(
  anchor: InlineAnchorSelection,
  loaded: LoadedDiff,
  file: DiffFileRow | null,
  row: Extract<DiffRow, { readonly kind: "line" }>,
): boolean {
  return (
    file !== null &&
    anchor.rowIndex === null &&
    selectionBelongsToLoadedFile(anchor, loaded, file) &&
    anchor.hunkIndex === row.hunk_index &&
    anchor.oldLine === row.old_line &&
    anchor.newLine === row.new_line &&
    anchor.lineType === row.line_type
  );
}

function composerOnSplitRow(
  anchor: InlineAnchorSelection,
  loaded: LoadedDiff,
  file: DiffFileRow | null,
  row: SplitDiffRow,
): boolean {
  return (
    file !== null &&
    anchor.rowIndex === row.row_index &&
    selectionBelongsToLoadedFile(anchor, loaded, file) &&
    anchor.hunkIndex === row.hunk_index
  );
}

function selectionMatchesLine(
  selection: InlineAnchorSelection,
  loaded: LoadedDiff,
  file: DiffFileRow | null,
  row: Extract<DiffRow, { readonly kind: "line" }>,
  side: "old" | "new",
): boolean {
  if (
    !file ||
    selection.side !== side ||
    !selectionBelongsToLoadedFile(selection, loaded, file)
  )
    return false;
  if (selection.selectedLines)
    return selection.selectedLines.some(
      (line) =>
        line.oldLine === row.old_line &&
        line.newLine === row.new_line &&
        line.lineType === row.line_type &&
        line.content === row.content,
    );
  return sameAnchorIdentity(
    selection,
    selectionForUnifiedLine(selection.review, loaded, file, row, side),
  );
}

function selectionMatchesSplitCell(
  selection: InlineAnchorSelection,
  loaded: LoadedDiff,
  file: DiffFileRow | null,
  row: SplitDiffRow,
  cell: SplitDiffCell,
): boolean {
  if (
    !file ||
    cell.anchor_side !== selection.side ||
    !selectionBelongsToLoadedFile(selection, loaded, file)
  )
    return false;
  if (selection.selectedLines)
    return selection.selectedLines.some(
      (line) =>
        line.oldLine === cell.old_line &&
        line.newLine === cell.new_line &&
        line.lineType === cell.line_type &&
        line.content === cell.content,
    );
  return sameAnchorIdentity(
    selection,
    selectionForSplitCell(selection.review, loaded, file, row, cell),
  );
}

function selectionBelongsToLoadedFile(
  selection: InlineAnchorSelection,
  loaded: LoadedDiff,
  file: DiffFileRow,
): boolean {
  return (
    selection.snapshotId === loaded.snapshotId &&
    selection.resource === loaded.resource &&
    sameRevisionValue(selection.revision, loaded.revision) &&
    selection.fileIndex === file.file_index &&
    selection.oldPath === file.old_path &&
    selection.newPath === file.new_path
  );
}

function sameSelectedSource(
  left: InlineAnchorSelection,
  right: InlineAnchorSelection,
): boolean {
  if (!left.selectedLines) return true;
  return Boolean(
    right.selectedLines &&
    left.selectedLines.length === right.selectedLines.length &&
    left.selectedLines.every((line, index) => {
      const candidate = right.selectedLines![index];
      return (
        candidate !== undefined &&
        line.oldLine === candidate.oldLine &&
        line.newLine === candidate.newLine &&
        line.lineType === candidate.lineType &&
        line.content === candidate.content
      );
    }),
  );
}

function sameAnchorIdentity(
  left: InlineAnchorSelection,
  right: InlineAnchorSelection,
): boolean {
  return (
    left.review === right.review &&
    left.snapshotId === right.snapshotId &&
    left.resource === right.resource &&
    sameRevisionValue(left.revision, right.revision) &&
    left.fileIndex === right.fileIndex &&
    left.hunkIndex === right.hunkIndex &&
    left.rowIndex === right.rowIndex &&
    left.oldPath === right.oldPath &&
    left.newPath === right.newPath &&
    left.side === right.side &&
    left.oldLine === right.oldLine &&
    left.newLine === right.newLine &&
    left.lineType === right.lineType &&
    left.contextComplete === right.contextComplete &&
    left.rangeOriginOldLine === right.rangeOriginOldLine &&
    left.rangeOriginNewLine === right.rangeOriginNewLine &&
    sameSelectedSource(left, right) &&
    left.contextLines.length === right.contextLines.length &&
    left.contextLines.every((line, index) => line === right.contextLines[index])
  );
}
function marker(type: string): string {
  return type === "addition"
    ? "+"
    : type === "deletion"
      ? "−"
      : type === "no_newline"
        ? "\\ "
        : " ";
}
function sameRevision(
  left: DiffPage<DiffWireRow>,
  right: DiffPage<DiffWireRow>,
): boolean {
  return sameRevisionValue(left.revision, right.revision);
}
function sameRevisionValue(
  left: ReviewRevisionDto,
  right: ReviewRevisionDto,
): boolean {
  return (
    left.head_sha === right.head_sha &&
    left.base_sha === right.base_sha &&
    left.start_sha === right.start_sha
  );
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
