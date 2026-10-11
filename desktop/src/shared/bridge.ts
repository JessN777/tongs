import type { CIDesktopBridge } from "./ci.js";
import type { ReviewDesktopBridge } from "./review.js";
import type { WorkspaceUtilityBridge } from "./utilities.js";

export const PROTOCOL_MAJOR = 1 as const;
export const REQUIRED_CAPABILITIES = Object.freeze([
  "assets", "cancellation", "ci_mutations", "events", "opaque_handles", "paged_diffs",
  "paged_discussions", "paged_logs", "plugins", "review_mutations",
  "workspace_utilities",
] as const);

/**
 * Protocol error code for a response that exceeds the negotiated byte, JSON
 * value-count, or JSON depth budget. The sidecar sends it in place of the
 * oversized result, and the main process raises it when a frame still
 * overflows, so only that one request fails and the service stays up.
 */
export const RESPONSE_TOO_LARGE = "response_too_large" as const;

export type JsonScalar = string | number | boolean | null;
export type JsonValue = JsonScalar | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };
export type OpaqueHandle = string;

export interface ServiceErrorDto { readonly code: string; readonly message: string; readonly retryable: boolean; }
export interface UserDto { readonly username: string; readonly display_name: string; }
export interface ReviewRevisionDto { readonly head_sha: string; readonly base_sha: string; readonly start_sha: string | null; }
export interface ForgeCapabilitiesDto { readonly batched_review: boolean; readonly thread_resolution: boolean; readonly draft_notes: boolean; readonly unapprove: boolean; readonly job_cancel: boolean; }
export interface RepositoryDto {
  readonly handle: OpaqueHandle;
  readonly display_name: string;
  readonly forge_type: "github" | "gitlab";
  /** Origin host from current sidecars. Older protocol-major-1 sidecars may omit it. */
  readonly hostname?: string;
}
export interface RepositoryListResult { readonly repositories: readonly RepositoryDto[]; }
export interface OpenRepositoryParams { readonly hostname: string; readonly project_path: string; }

export interface ReviewSummaryDto {
  readonly number: number; readonly title: string; readonly author: UserDto;
  readonly state: string; readonly is_draft: boolean; readonly source_branch: string;
  readonly target_branch: string; readonly ci_status: string; readonly created_at: string;
  readonly updated_at: string; readonly web_url: string; readonly comment_count: number;
  readonly has_conflicts: boolean; readonly labels: readonly string[];
  readonly review_decision: string | null; readonly additions: number | null;
  readonly deletions: number | null;
}
export interface ReviewDetailDto extends ReviewSummaryDto {
  readonly description: string; readonly merge_status: string | null;
  readonly approvals: readonly UserDto[]; readonly reviewers: readonly UserDto[];
  readonly assignees: readonly UserDto[]; readonly changes_count: number;
  readonly detailed_merge_status: string | null; readonly draft_notes_count: number | null;
  readonly status_check_rollup: string | null;
  /** True when the forge capped the changed file list; absent means false. */
  readonly changes_overflow?: boolean;
}
export interface ReviewListItemDto { readonly handle: OpaqueHandle; readonly repository: OpaqueHandle; readonly summary: ReviewSummaryDto; }
export interface ReviewFailureDto extends ServiceErrorDto { readonly repository: OpaqueHandle | null; }
/** `fresh` marks an explicit refresh, which reads past the service's list cache. */
export interface ListReviewsParams { readonly scope: string; readonly repository?: OpaqueHandle; readonly state?: string; readonly per_page?: number; readonly cursor?: string; readonly fresh?: boolean; }
/** `next_cursor` is set when a repository-scoped All Open read has another page. */
export interface ReviewListResult { readonly items: readonly ReviewListItemDto[]; readonly failures: readonly ReviewFailureDto[]; readonly next_cursor?: string | null; }
/** A list read leaves CI `unknown` where the forge would need a request per row. */
export type CIStatusValue = "pending" | "running" | "success" | "failed" | "canceled" | "skipped" | "unknown";
export const CI_STATUS_VALUES: readonly CIStatusValue[] = Object.freeze([
  "pending", "running", "success", "failed", "canceled", "skipped", "unknown",
]);
/** `fresh` reads past the service's CI cache, as after an explicit refresh. */
export interface ReviewCIStatusParams { readonly review: OpaqueHandle; readonly fresh?: boolean; }
/**
 * `retry_after_seconds` is null unless the forge host is rate limited: the
 * lookup then made no status known (`ci_status` is `unknown`) and lookups can
 * start again after that many seconds.
 */
export interface ReviewCIStatusResult { readonly ci_status: CIStatusValue; readonly retry_after_seconds: number | null; }
export interface ReviewSnapshotDto {
  readonly handle: OpaqueHandle; readonly repository: OpaqueHandle;
  readonly detail: ReviewDetailDto; readonly capabilities: ForgeCapabilitiesDto;
  readonly revision: ReviewRevisionDto | null; readonly revision_error: ServiceErrorDto | null;
}

export interface DiffFileRow { readonly kind: "file"; readonly file_index: number; readonly old_path: string; readonly new_path: string; readonly status: string; readonly additions: number; readonly deletions: number; readonly is_binary: boolean; readonly language: string | null; readonly is_truncated: boolean; readonly is_empty: boolean; readonly is_mode_only: boolean; readonly is_rename_only: boolean; readonly is_unavailable: boolean; readonly old_mode: string | null; readonly new_mode: string | null; }
export interface DiffHunkRow { readonly kind: "hunk"; readonly file_index: number; readonly hunk_index: number; readonly header: string; readonly old_start: number; readonly old_count: number; readonly new_start: number; readonly new_count: number; readonly context_text: string; }
export interface DiffLineRow { readonly kind: "line"; readonly file_index: number; readonly hunk_index: number; readonly old_line: number | null; readonly new_line: number | null; readonly content: string; readonly line_type: string; }
export type DiffLayout = "unified" | "split";
export interface SplitDiffCell { readonly old_line: number | null; readonly new_line: number | null; readonly content: string; readonly line_type: string; readonly anchor_side: "old" | "new" | null; }
export interface SplitDiffRow { readonly kind: "split"; readonly file_index: number; readonly hunk_index: number; readonly row_index: number; readonly old: SplitDiffCell | null; readonly new: SplitDiffCell | null; }
export type DiffRow = DiffFileRow | DiffHunkRow | DiffLineRow | SplitDiffRow;
export type DiffGapPosition = "before" | "between" | "after";
export const DIFF_GAP_POSITIONS: readonly DiffGapPosition[] = Object.freeze(["before", "between", "after"]);
/**
 * Unchanged lines the diff does not show, before the hunk that follows it or,
 * for `after`, past the file's last hunk. Only a diff opened with `gaps: true`
 * carries these rows. `count` is null only for `after`, whose length is known
 * once the file is read (`line_count` of a context read), and may be zero.
 */
export interface DiffGapRow { readonly kind: "gap"; readonly file_index: number; readonly gap_index: number; readonly position: DiffGapPosition; readonly old_start: number; readonly new_start: number; readonly count: number | null; }
/** Every row a diff snapshot can hold, gap rows included. */
export type DiffWireRow = DiffRow | DiffGapRow;
export interface SnapshotPage<T, R extends JsonObject> { readonly snapshot_id: string; readonly resource: OpaqueHandle; readonly revision: R; readonly cursor: number; readonly next_cursor: number | null; readonly entries: readonly T[]; }
export type DiffPage<T extends DiffWireRow = DiffRow> = SnapshotPage<T, ReviewRevisionDto & JsonObject>;
/**
 * `from_snapshot` names a diff snapshot already open for this review. The
 * sidecar then projects the same read again in `layout`, with no forge request,
 * or reads the forge afresh when it no longer holds that read. `gaps` adds gap
 * rows around the hunks of every file whose unchanged lines can be shown.
 */
export interface OpenDiffParams { readonly review: OpaqueHandle; readonly layout?: DiffLayout; readonly max_items?: number; readonly from_snapshot?: string; readonly gaps?: boolean; }
/** The most unchanged lines one `diff.context` read returns. */
export const MAX_DIFF_CONTEXT_LINES = 1000;
/**
 * Unchanged lines from `new_start` in one gap of file `file_index` of the
 * open diff `snapshot`. The service never takes a path or a commit.
 */
export interface DiffContextParams { readonly review: OpaqueHandle; readonly snapshot: string; readonly file_index: number; readonly new_start: number; readonly count: number; }
export type DiffContextStatus = "ok" | "too_large" | "binary" | "not_found" | "mismatch" | "unsupported" | "expired";
export const DIFF_CONTEXT_STATUSES: readonly DiffContextStatus[] = Object.freeze(["ok", "too_large", "binary", "not_found", "mismatch", "unsupported", "expired"]);
/** One unchanged, read-only line: never a comment anchor. */
export interface DiffContextRow { readonly kind: "context"; readonly file_index: number; readonly old_line: number; readonly new_line: number; readonly content: string; }
/**
 * `rows` stop at the end of the gap, at `count`, or at the response size
 * bound; ask again from the next line for the rest. `line_count` is the new
 * file's length when the read succeeded. Any other status has a `message` and
 * no rows; `expired` means the open diff is gone and must be reloaded.
 */
export interface DiffContextResult { readonly status: DiffContextStatus; readonly message: string | null; readonly line_count: number | null; readonly rows: readonly DiffContextRow[]; }
export interface GenerateDiffParams { readonly review: OpaqueHandle; readonly snapshot: string; readonly file_index: number; }
export type GenerateDiffStatus = "ok" | "too_large" | "binary" | "not_found" | "unsupported" | "not_truncated" | "no_merge_base" | "expired";
export const GENERATE_DIFF_STATUSES: readonly GenerateDiffStatus[] = Object.freeze(["ok", "too_large", "binary", "not_found", "unsupported", "not_truncated", "no_merge_base", "expired"]);
/**
 * A generated diff is the first page of a one-file diff snapshot at the
 * file's original index; later pages come from `pageDiff`. Any other status
 * carries a message and no snapshot.
 */
export type GenerateDiffResult =
  | (DiffPage & { readonly status: "ok"; readonly message: null; readonly generated: true })
  | {
      readonly status: Exclude<GenerateDiffStatus, "ok">; readonly message: string; readonly generated: false;
      readonly snapshot_id: null; readonly resource: null; readonly revision: null;
      readonly cursor: null; readonly next_cursor: null; readonly entries: readonly [];
    };
export interface PageParams { readonly snapshot: string; readonly resource: OpaqueHandle; readonly cursor: number; readonly max_items?: number; }

/** `file_path` is null on a review-level note, which carries no inline position. */
export interface InlineCommentDto { readonly id: string; readonly author: UserDto; readonly body: string; readonly created_at: string; readonly file_path: string | null; readonly old_line: number | null; readonly new_line: number | null; readonly is_resolved: boolean; readonly replies: readonly InlineCommentDto[]; }
export interface DiscussionDto { readonly id: string; readonly is_inline: boolean; readonly root_comment: InlineCommentDto; readonly is_resolved: boolean; readonly resolvable: boolean; }
export interface DiscussionsResult { readonly discussions: readonly DiscussionDto[]; }
export interface DiscussionsRevisionDto extends JsonObject { readonly discussion_count: number; }
/**
 * One page of a review's discussions snapshot. The sidecar keeps every page
 * well inside the negotiated JSON value budget, so a review with thousands of
 * threads arrives over several pages rather than as one rejected frame.
 */
export interface DiscussionsPage extends DiscussionsResult { readonly snapshot_id: string; readonly resource: OpaqueHandle; readonly revision: DiscussionsRevisionDto; readonly cursor: number; readonly next_cursor: number | null; }
export interface CommitDto { readonly sha: string; readonly short_sha: string; readonly title: string; readonly message: string; readonly author: UserDto; readonly created_at: string | null; readonly web_url: string | null; }
export interface CommitsResult { readonly commits: readonly CommitDto[]; }
export interface PipelineDto { readonly id: number; readonly status: string; readonly ref: string; readonly sha: string; readonly web_url: string | null; readonly source: string | null; readonly created_at: string | null; readonly finished_at: string | null; readonly duration_seconds: number | null; }
export interface PipelineItemDto { readonly handle: OpaqueHandle; readonly value: PipelineDto; }
export interface PipelinesResult { readonly pipelines: readonly PipelineItemDto[]; }
export interface ListPipelinesParams { readonly repository: OpaqueHandle; readonly per_page?: number; }
export interface ListReviewPipelinesParams { readonly review: OpaqueHandle; readonly per_page?: number; }
export interface JobDto { readonly id: number; readonly name: string; readonly stage: string | null; readonly status: string; readonly web_url: string | null; readonly started_at: string | null; readonly finished_at: string | null; readonly duration_seconds: number | null; readonly allow_failure: boolean; }
export interface JobItemDto { readonly handle: OpaqueHandle; readonly value: JobDto; }
export interface JobsResult { readonly jobs: readonly JobItemDto[]; }
export interface LogRow { readonly text: string; }
export interface LogRevisionDto extends JsonObject { readonly sha256: string; readonly byte_count: number; }
export type LogPage = SnapshotPage<LogRow, LogRevisionDto>;
export interface OpenLogParams { readonly job: OpaqueHandle; readonly max_items?: number; }

export interface PluginManifestDto { readonly title: string; readonly version: string; readonly api_major: number; readonly modules: readonly JsonObject[]; readonly navigation: readonly JsonObject[]; readonly commands: readonly JsonObject[]; readonly methods: readonly string[]; readonly events: readonly string[]; readonly focus_targets: readonly JsonObject[]; readonly help_asset: OpaqueHandle | null; readonly reads: readonly string[]; readonly assets_available: boolean; }
export interface PluginDto { readonly plugin_id: string; readonly state: string; readonly has_terminal_entry_point: boolean; readonly has_desktop_entry_point: boolean; readonly error: ServiceErrorDto | null; readonly manifest: PluginManifestDto | null; }
export interface PluginsResult { readonly plugins: readonly PluginDto[]; }
export interface InvokePluginParams { readonly plugin: string; readonly method: string; readonly params: JsonObject; }
export interface PluginResult { readonly value: JsonValue; }
export interface LocationParams { readonly location: JsonObject | null; }
export interface AcceptedResult { readonly accepted: true; }

export interface DesktopEvent { readonly sequence: number; readonly name: string; readonly data: JsonValue; }
/** Whether the main process currently holds a live connection to the local service. */
export type ServiceState = "connected" | "stopped";
/** A service state change; `revision` only grows, so a stale snapshot can be ignored. */
/** `notices` are this app run's service startup warnings, shown until dismissed. */
export interface ServiceStatusDto {
  readonly state: ServiceState;
  readonly revision: number;
  readonly notices: readonly string[];
}
/**
 * The desktop colour scheme (#226): follow the operating system, or force
 * light or dark. `system` is the default of `[ui] color_scheme`.
 */
export type ColorScheme = "system" | "light" | "dark";
export const COLOR_SCHEMES: readonly ColorScheme[] = Object.freeze(["system", "light", "dark"]);
/** A choice the toggle saves; choosing `system` clears the saved choice. */
export type SavedColorScheme = Exclude<ColorScheme, "system">;
/**
 * `configured` is `[ui] color_scheme` from config.toml. `saved` is the
 * toggle's saved choice, or null when the window follows the configured value.
 * `effective` is what applies: the saved choice when there is one, else the
 * configured value. `revision` only grows, so a stale copy can be ignored.
 */
export interface AppearanceDto {
  readonly configured: ColorScheme;
  readonly saved: SavedColorScheme | null;
  readonly effective: ColorScheme;
  readonly revision: number;
}

export function isColorScheme(value: unknown): value is ColorScheme {
  return typeof value === "string" && (COLOR_SCHEMES as readonly string[]).includes(value);
}

/** Whether `value` is an appearance whose fields agree with each other. */
export function isAppearance(value: unknown): value is AppearanceDto {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const { configured, saved, effective, revision } = value as Record<string, unknown>;
  return (
    Object.keys(value).length === 4 &&
    isColorScheme(configured) &&
    (saved === null || saved === "light" || saved === "dark") &&
    effective === (saved ?? configured) &&
    Number.isSafeInteger(revision) &&
    (revision as number) >= 0
  );
}

export interface AssetDescriptor { readonly source: "core" | "plugin"; readonly asset_id: string; readonly plugin_id: string | null; readonly kind: string; readonly media_type: string; readonly byte_count: number; readonly sha256: string; readonly url: string; }
export interface DesktopRead<T> { readonly requestToken: string; readonly result: Promise<T>; }

export interface DesktopBridge extends CIDesktopBridge, ReviewDesktopBridge, WorkspaceUtilityBridge {
  discoverRepositories(): DesktopRead<RepositoryListResult>;
  openRepository(params: OpenRepositoryParams): DesktopRead<RepositoryDto>;
  listReviews(params: ListReviewsParams): DesktopRead<ReviewListResult>;
  getReview(review: OpaqueHandle): DesktopRead<ReviewSnapshotDto>;
  getReviewCIStatus(params: ReviewCIStatusParams): DesktopRead<ReviewCIStatusResult>;
  openDiff(params: OpenDiffParams & { readonly gaps: true }): DesktopRead<DiffPage<DiffWireRow>>;
  openDiff(params: OpenDiffParams): DesktopRead<DiffPage>;
  /** Pages of a snapshot opened with `gaps: true` hold gap rows too: pass `DiffWireRow`. */
  pageDiff<T extends DiffWireRow = DiffRow>(params: PageParams): DesktopRead<DiffPage<T>>;
  getDiffContext(params: DiffContextParams): DesktopRead<DiffContextResult>;
  generateDiff(params: GenerateDiffParams): DesktopRead<GenerateDiffResult>;
  listDiscussions(review: OpaqueHandle): DesktopRead<DiscussionsPage>;
  pageDiscussions(params: PageParams): DesktopRead<DiscussionsPage>;
  listCommits(review: OpaqueHandle): DesktopRead<CommitsResult>;
  listPipelines(params: ListPipelinesParams): DesktopRead<PipelinesResult>;
  listReviewPipelines(params: ListReviewPipelinesParams): DesktopRead<PipelinesResult>;
  listJobs(pipeline: OpaqueHandle): DesktopRead<JobsResult>;
  openLog(params: OpenLogParams): DesktopRead<LogPage>;
  pageLog(params: PageParams): DesktopRead<LogPage>;
  listPlugins(): DesktopRead<PluginsResult>;
  invokePlugin(params: InvokePluginParams): DesktopRead<PluginResult>;
  setLocation(params: LocationParams): Promise<AcceptedResult>;
  listAssets(): DesktopRead<readonly AssetDescriptor[]>;
  cancelRead(requestToken: string): Promise<boolean>;
  onEvent(listener: (event: DesktopEvent) => void): () => void;
  getServiceStatus(): Promise<ServiceStatusDto>;
  onServiceStatus(listener: (status: ServiceStatusDto) => void): () => void;
  openExternal(url: string): Promise<boolean>;
  getAppearance(): Promise<AppearanceDto>;
  /** Saves `choice` and applies it at once; `system` clears the saved choice. */
  setColorScheme(choice: ColorScheme): Promise<AppearanceDto>;
  onAppearance(listener: (appearance: AppearanceDto) => void): () => void;
}

export const IPC_CHANNELS = Object.freeze({
  discoverRepositories: "tongs:repositories.discover", openRepository: "tongs:repositories.open",
  listReviews: "tongs:reviews.list", getReview: "tongs:reviews.get",
  getReviewCIStatus: "tongs:reviews.ci-status", openDiff: "tongs:diff.open",
  pageDiff: "tongs:diff.page", getDiffContext: "tongs:diff.context", generateDiff: "tongs:diff.generate", listDiscussions: "tongs:discussions.list",
  pageDiscussions: "tongs:discussions.page", listCommits: "tongs:commits.list",
  listPipelines: "tongs:pipelines.list", listReviewPipelines: "tongs:review-pipelines.list", listJobs: "tongs:jobs.list",
  openLog: "tongs:logs.open", pageLog: "tongs:logs.page", listPlugins: "tongs:plugins.list",
  invokePlugin: "tongs:plugins.invoke", setLocation: "tongs:host.set-location", listAssets: "tongs:assets.list",
  cancelRead: "tongs:read.cancel", openExternal: "tongs:external.open", event: "tongs:event",
  serviceStatus: "tongs:service.status", getServiceStatus: "tongs:service.status.get",
  appearance: "tongs:appearance", getAppearance: "tongs:appearance.get",
  setColorScheme: "tongs:appearance.set-color-scheme",
} as const);

/**
 * What a main-process read handler resolves with. A handler never rejects:
 * Electron logs every rejected handler as an error, and an expected failure,
 * such as a read the renderer cancelled, is not one. Exactly one of `result`
 * and `error` is set. The preload checks the envelope and turns `error` back
 * into the encoded failure below, so the renderer API does not change.
 */
export type ReadIPCResult<T = JsonValue> =
  | { readonly result: T; readonly error: null }
  | { readonly result: null; readonly error: ServiceErrorDto };

/**
 * Electron rebuilds a rejected `invoke` as a fresh Error and keeps only its
 * message, and a preload rejection reaches the renderer the same way, so a
 * `code` or `retryable` property never survives the read boundary. The preload
 * therefore encodes the failure into the message and the renderer decodes it,
 * which is what lets a read report its real cause instead of one generic
 * sentence for every failure. The sandboxed preload cannot import this module
 * and repeats the marker, code pattern and detail bound; the electron IPC
 * tests check that its encoding decodes here.
 */
const READ_FAILURE_MARKER = "tongs-read-failure:";
const READ_FAILURE_PATTERN = /tongs-read-failure:([%A-Za-z0-9!'()*\-._~]+)/;
const READ_FAILURE_CODE = /^[a-z][a-z0-9_]{0,63}$/;
const MAX_READ_FAILURE_DETAIL = 240;

/** The bounded failure a read reports, with an unknown code as invalid_response. */
export function readFailureOf(error: unknown): ServiceErrorDto {
  const source: Record<string, unknown> =
    error !== null && typeof error === "object"
      ? (error as Record<string, unknown>)
      : {};
  const candidate = typeof source.code === "string" ? source.code : "";
  const detail = typeof source.message === "string" ? source.message : "";
  return {
    code: READ_FAILURE_CODE.test(candidate) ? candidate : "invalid_response",
    message: readableDetail(detail),
    retryable: source.retryable === true,
  };
}

export function encodeReadFailure(error: unknown): Error {
  const failure = readFailureOf(error);
  const encoded = encodeURIComponent(JSON.stringify(failure));
  return new Error(`${READ_FAILURE_MARKER}${encoded} ${failure.message}`);
}

export function decodeReadFailure(error: unknown): ServiceErrorDto | null {
  const message =
    error instanceof Error
      ? error.message
      : typeof error === "string"
        ? error
        : null;
  const encoded = message === null ? null : READ_FAILURE_PATTERN.exec(message);
  const payload = encoded === null ? undefined : encoded[1];
  if (payload === undefined) return null;
  try {
    const parsed: unknown = JSON.parse(decodeURIComponent(payload));
    if (parsed === null || typeof parsed !== "object") return null;
    const { code, message: detail, retryable } = parsed as Record<string, unknown>;
    if (
      typeof code !== "string" ||
      !READ_FAILURE_CODE.test(code) ||
      typeof detail !== "string" ||
      detail.length === 0 ||
      detail.length > MAX_READ_FAILURE_DETAIL ||
      typeof retryable !== "boolean"
    )
      return null;
    return Object.freeze({ code, message: detail, retryable });
  } catch {
    return null;
  }
}

function readableDetail(detail: string): string {
  const collapsed = detail.replace(/\s+/g, " ").trim();
  return collapsed.length === 0
    ? "The desktop read did not complete."
    : collapsed.slice(0, MAX_READ_FAILURE_DETAIL);
}

declare global { interface Window { readonly tongs: DesktopBridge; } }
