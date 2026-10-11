import type { IpcMainInvokeEvent, WebContents } from "electron";
import {
  CI_STATUS_VALUES,
  DIFF_CONTEXT_STATUSES,
  DIFF_GAP_POSITIONS,
  GENERATE_DIFF_STATUSES,
  MAX_DIFF_CONTEXT_LINES,
  type JsonObject,
  type JsonValue,
} from "../shared/bridge.js";

export const APP_ORIGIN = "tongs://app";
export const APP_DOCUMENT = `${APP_ORIGIN}/index.html`;
export const APP_SCHEME_REGISTRATION = Object.freeze({
  scheme: "tongs",
  privileges: Object.freeze({
    standard: true,
    secure: true,
    supportFetchAPI: true,
    bypassCSP: false,
    allowServiceWorkers: false,
    codeCache: true,
  }),
});
export const CONTENT_SECURITY_POLICY = [
  "default-src 'none'", "script-src 'self'", "style-src 'self'",
  "img-src 'self' data:", "font-src 'self'", "connect-src 'self'",
  "object-src 'none'", "base-uri 'none'", "form-action 'none'", "frame-ancestors 'none'",
].join("; ");

const MAX_IPC_BYTES = 256 * 1024;
const MAX_TEXT = 2048;
// The longest rate-limit wait a CI lookup may name (a day, in seconds).
const MAX_RATE_LIMIT_WAIT_SECONDS = 86_400;
// Bounds the service applies to a file index and to the line count of a file
// it reads, and the most encoded bytes of unchanged lines one context read
// returns. Line numbers in a diff have no upper bound: a small patch to a long
// file still opens, and a read past the cap comes back as too_large.
const MAX_FILE_INDEX = 100_000;
const MAX_LINE_COUNT = 1_000_000;
const MAX_CONTEXT_BYTES = 4 * 1024 * 1024;
const SNAPSHOT_ID = /^[A-Za-z0-9_-]{1,64}$/;

export function assertAuthorizedSender(event: IpcMainInvokeEvent, owner: WebContents): void {
  if (event.sender !== owner || event.senderFrame !== owner.mainFrame || event.senderFrame.url !== APP_DOCUMENT) {
    throw new Error("Unauthorized desktop IPC sender");
  }
}

export function assertParams(method: string, value: unknown): asserts value is JsonObject {
  if (!isRecord(value)) throw new Error("Desktop parameters must be an object");
  let encoded: string;
  try { encoded = JSON.stringify(value); } catch { throw new Error("Invalid desktop parameters"); }
  if (Buffer.byteLength(encoded, "utf8") > MAX_IPC_BYTES) throw new Error("Desktop parameters are too large");
  const specs: Record<string, { required: readonly string[]; optional?: readonly string[] }> = {
    "repositories.discover": { required: [] },
    "repositories.open": { required: ["hostname", "project_path"] },
    "reviews.list": { required: ["scope"], optional: ["repository", "state", "per_page", "cursor", "fresh"] },
    "reviews.get": { required: ["review"] }, "discussions.list": { required: ["review"] },
    "reviews.ci_status": { required: ["review"], optional: ["fresh"] },
    "commits.list": { required: ["review"] }, "jobs.list": { required: ["pipeline"] },
    "diff.open": { required: ["review"], optional: ["layout", "max_items", "from_snapshot", "gaps"] },
    "diff.context": { required: ["review", "snapshot", "file_index", "new_start", "count"] },
    "diff.generate": { required: ["review", "snapshot", "file_index"] },
    "logs.open": { required: ["job"], optional: ["max_items"] },
    "diff.page": { required: ["snapshot", "resource", "cursor"], optional: ["max_items"] },
    "discussions.page": { required: ["snapshot", "resource", "cursor"], optional: ["max_items"] },
    "logs.page": { required: ["snapshot", "resource", "cursor"], optional: ["max_items"] },
    "pipelines.list": { required: ["repository"], optional: ["per_page"] },
    "review_pipelines.list": { required: ["review"], optional: ["per_page"] },
    "plugins.list": { required: [] }, "assets.list": { required: [] },
    "plugins.invoke": { required: ["plugin", "method", "params"] },
    "host.set_location": { required: ["location"] },
  };
  const spec = specs[method];
  if (!spec) throw new Error("Unsupported desktop operation");
  const allowed = new Set([...spec.required, ...(spec.optional ?? [])]);
  if (Object.keys(value).some((key) => !allowed.has(key)) || spec.required.some((key) => !Object.hasOwn(value, key))) throw new Error("Invalid desktop parameter fields");
  for (const [key, item] of Object.entries(value)) {
    if (method === "reviews.list" && key === "cursor") {
      // An opaque cursor the service sealed; the service rejects forged ones.
      if (item !== null && (typeof item !== "string" || !/^[A-Za-z0-9._-]{1,64}$/.test(item))) throw new Error("Invalid desktop cursor parameter");
    } else if ((method === "diff.open" && key === "from_snapshot") || ((method === "diff.context" || method === "diff.generate") && key === "snapshot")) {
      // The snapshot id a diff open issued, for a layout switch that reuses it
      // or for a context read or generation that names a file of it.
      if (typeof item !== "string" || !SNAPSHOT_ID.test(item)) throw new Error("Invalid desktop snapshot parameter");
    } else if (method === "diff.open" && key === "gaps") {
      if (typeof item !== "boolean") throw new Error("Invalid desktop boolean parameter");
    } else if ((method === "diff.context" || method === "diff.generate") && ["file_index", "new_start", "count"].includes(key)) {
      const [minimum, maximum] = key === "file_index" ? [0, MAX_FILE_INDEX] : key === "new_start" ? [1, Number.MAX_SAFE_INTEGER] : [1, MAX_DIFF_CONTEXT_LINES];
      if (!Number.isSafeInteger(item) || Number(item) < minimum || Number(item) > maximum) throw new Error("Invalid desktop numeric parameter");
    } else if ((method === "reviews.list" || method === "reviews.ci_status") && key === "fresh") {
      // An explicit refresh that reads past the service's list cache.
      if (typeof item !== "boolean") throw new Error("Invalid desktop boolean parameter");
    } else if (["per_page", "max_items", "cursor"].includes(key)) {
      const maximum = key === "cursor" ? Number.MAX_SAFE_INTEGER : key === "per_page" ? 100 : 1000;
      if (!Number.isSafeInteger(item) || Number(item) < (key === "cursor" ? 0 : 1) || Number(item) > maximum) throw new Error("Invalid desktop numeric parameter");
    } else if (key === "params" || key === "location") {
      if (item !== null && !isRecord(item)) throw new Error("Invalid desktop object parameter");
      assertJson(item);
    } else if (item !== null && (typeof item !== "string" || item.length === 0 || item.length > MAX_TEXT)) {
      throw new Error("Invalid desktop text parameter");
    }
  }
  if (method === "reviews.list" && !["all_open", "my_reviews", "my_mrs"].includes(String(value.scope))) throw new Error("Invalid review scope");
  if (value.layout !== undefined && value.layout !== "unified" && value.layout !== "split") throw new Error("Invalid diff layout");
  if (value.state !== undefined && !["open", "closed", "merged"].includes(String(value.state))) throw new Error("Invalid review state");
}

export function assertResult(method: string, value: unknown): asserts value is JsonValue {
  // Every failure names the operation and the field that broke the contract.  A
  // bare "invalid result" hides which row of a paged read was unusable, so the
  // renderer can only show a generic sentence and the cause is lost.
  try {
    assertResultValue(method, value);
  } catch (error) {
    throw new Error(`Invalid ${method} result: ${detailOf(error)}`);
  }
}

function detailOf(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  return message.length > 0 && message.length <= 200
    ? message
    : "the result did not match the desktop contract";
}

function assertResultValue(method: string, value: unknown): asserts value is JsonValue {
  assertJson(value);
  if (method === "repositories.discover") return assertArrayField(value, "repositories", assertRepository);
  if (method === "repositories.open") return assertRepository(value);
  if (method === "reviews.list") {
    assertKeys(value, ["next_cursor", "items", "failures"]);
    if (value.next_cursor !== null && (typeof value.next_cursor !== "string" || !/^[A-Za-z0-9._-]{1,64}$/.test(value.next_cursor))) fail("next_cursor", "must be null or an opaque cursor of at most 64 characters");
    assertArray(value.items, (item) => { assertKeys(item, ["handle", "repository", "summary"]); text(item.handle); text(item.repository); assertReviewSummary(item.summary); });
    return assertArray(value.failures, (failure) => { assertKeys(failure, ["code", "message", "retryable", "repository"]); text(failure.code); text(failure.message); bool(failure.retryable); nullableText(failure.repository); });
  }
  if (method === "reviews.ci_status") {
    assertKeys(value, ["ci_status", "retry_after_seconds"]);
    if (!(CI_STATUS_VALUES as readonly unknown[]).includes(value.ci_status)) fail("ci_status", "must be a known CI status");
    if (value.retry_after_seconds === null) return;
    // A rate-limited lookup knows no status and names a wait of at most a day.
    integer(value.retry_after_seconds, 1, "retry_after_seconds");
    if (Number(value.retry_after_seconds) > MAX_RATE_LIMIT_WAIT_SECONDS) fail("retry_after_seconds", `must be at most ${MAX_RATE_LIMIT_WAIT_SECONDS}`);
    if (value.ci_status !== "unknown") fail("ci_status", "must be unknown while rate limited");
    return;
  }
  if (method === "reviews.get") {
    assertKeys(value, ["handle", "repository", "detail", "capabilities", "revision", "revision_error"]);
    text(value.handle); text(value.repository); assertReviewDetail(value.detail);
    assertExactRecord(value.capabilities, ["batched_review", "thread_resolution", "draft_notes", "unapprove", "job_cancel"], bool);
    if (value.revision !== null) assertRevision(value.revision);
    if (value.revision_error !== null) assertServiceError(value.revision_error);
    return;
  }
  if (method === "diff.open" || method === "diff.page") return assertSnapshot(value, assertDiffRow, assertRevision);
  if (method === "diff.context") return assertDiffContext(value);
  if (method === "diff.generate") return assertGeneratedDiff(value);
  if (method === "logs.open" || method === "logs.page") return assertSnapshot(value, (row) => { assertKeys(row, ["text"]); boundedText(row.text, 512 * 1024, "text"); }, (revision) => { assertKeys(revision, ["sha256", "byte_count"]); digest(revision.sha256, "sha256"); integer(revision.byte_count, 0, "byte_count"); });
  if (method === "discussions.list" || method === "discussions.page") return assertDiscussionsPage(value);
  if (method === "commits.list") return assertArrayField(value, "commits", assertCommit);
  if (method === "pipelines.list" || method === "review_pipelines.list") return assertArrayField(value, "pipelines", (item) => { assertKeys(item, ["handle", "value"]); text(item.handle); assertPipeline(item.value); });
  if (method === "jobs.list") return assertArrayField(value, "jobs", (item) => { assertKeys(item, ["handle", "value"]); text(item.handle); assertJob(item.value); });
  if (method === "plugins.list") return assertArrayField(value, "plugins", assertPlugin);
  if (method === "plugins.invoke") { assertKeys(value, ["value"]); return; }
  if (method === "host.set_location") { assertKeys(value, ["accepted"]); if (value.accepted !== true) throw new Error("Invalid desktop result"); return; }
  throw new Error("Unsupported desktop result");
}

export function assertHttpsExternalUrl(raw: unknown): string {
  if (typeof raw !== "string" || raw.length > 4096) throw new Error("Invalid external URL");
  const value = new URL(raw);
  if (value.protocol !== "https:" || value.username || value.password) throw new Error("Only credential-free HTTPS links are allowed");
  return value.href;
}

export function isAllowedAppUrl(raw: string): boolean {
  try { const value = new URL(raw); return value.protocol === "tongs:" && value.host === "app"; }
  catch { return false; }
}

function assertJson(value: unknown, depth = 0): asserts value is JsonValue {
  if (depth > 24) throw new Error("Desktop JSON is too deep");
  if (value === null || typeof value === "string" || typeof value === "boolean") return;
  if (typeof value === "number" && Number.isFinite(value)) return;
  if (Array.isArray(value)) { for (const item of value) assertJson(item, depth + 1); return; }
  if (!isRecord(value)) throw new Error("Invalid desktop JSON");
  for (const [key, item] of Object.entries(value)) {
    if (["__proto__", "prototype", "constructor"].includes(key)) throw new Error("Invalid desktop JSON key");
    assertJson(item, depth + 1);
  }
}

type RecordValue = Record<string, unknown>;
function assertKeys(value: unknown, keys: readonly string[]): asserts value is RecordValue {
  if (!isRecord(value) || Object.keys(value).length !== keys.length || keys.some((key) => !Object.hasOwn(value, key))) throw new Error("Invalid desktop result shape");
}
function assertArray(value: unknown, validate: (item: unknown) => void, maximum = 10_000): asserts value is unknown[] {
  if (!Array.isArray(value) || value.length > maximum) throw new Error("Invalid desktop result array");
  for (const item of value) validate(item);
}
function assertArrayField(value: unknown, key: string, validate: (item: unknown) => void): void { assertKeys(value, [key]); assertArray(value[key], validate); }
function assertExactRecord(value: unknown, keys: readonly string[], validate: (item: unknown) => void): void { assertKeys(value, keys); for (const key of keys) validate(value[key]); }
function fail(field: string | undefined, requirement: string): never {
  throw new Error(`${field === undefined ? "a result field" : `field "${field}"`} ${requirement}`);
}
function text(value: unknown, maximum = MAX_TEXT, field?: string): asserts value is string { if (typeof value !== "string" || value.length === 0 || value.length > maximum) fail(field, `must be text of 1 to ${maximum} characters`); }
function boundedText(value: unknown, maximum: number, field?: string): void { if (typeof value !== "string" || value.length > maximum) fail(field, `must be text of at most ${maximum} characters`); }
function nullableText(value: unknown, field?: string): void { if (value !== null && (typeof value !== "string" || value.length === 0 || value.length > MAX_TEXT)) fail(field, "must be null or non-empty text"); }
function bool(value: unknown, field?: string): asserts value is boolean { if (typeof value !== "boolean") fail(field, "must be a boolean"); }
function integer(value: unknown, minimum = 0, field?: string): asserts value is number { if (!Number.isSafeInteger(value) || Number(value) < minimum) fail(field, `must be an integer of at least ${minimum}`); }
function numberOrNull(value: unknown, field?: string): void { if (value !== null && (typeof value !== "number" || !Number.isFinite(value))) fail(field, "must be null or a finite number"); }
function digest(value: unknown, field?: string): void { if (typeof value !== "string" || !/^[a-f0-9]{64}$/.test(value)) fail(field, "must be a 64-character hex digest"); }
function assertUser(value: unknown): void {
  assertKeys(value, ["username", "display_name"]); text(value.username);
  if (typeof value.display_name !== "string" || value.display_name.length > MAX_TEXT) throw new Error("Invalid desktop user display name");
}
function assertRepository(value: unknown): void {
  if (!isRecord(value)) throw new Error("Invalid desktop result shape");
  const keys = Object.hasOwn(value, "hostname")
    ? ["handle", "display_name", "forge_type", "hostname"]
    : ["handle", "display_name", "forge_type"];
  assertKeys(value, keys);
  text(value.handle);
  text(value.display_name);
  if (value.forge_type !== "github" && value.forge_type !== "gitlab")
    throw new Error("Invalid forge type");
  if (Object.hasOwn(value, "hostname")) text(value.hostname, 253);
}
function assertRevision(value: unknown): void { assertKeys(value, ["head_sha", "base_sha", "start_sha"]); text(value.head_sha, MAX_TEXT, "head_sha"); text(value.base_sha, MAX_TEXT, "base_sha"); nullableText(value.start_sha, "start_sha"); }
function assertServiceError(value: unknown): void { assertKeys(value, ["code", "message", "retryable"]); text(value.code); text(value.message); bool(value.retryable); }
const SUMMARY_KEYS = ["number", "title", "author", "state", "is_draft", "source_branch", "target_branch", "ci_status", "created_at", "updated_at", "web_url", "comment_count", "has_conflicts", "labels", "review_decision", "additions", "deletions"] as const;
function assertReviewSummary(value: unknown): void {
  assertKeys(value, SUMMARY_KEYS); integer(value.number); text(value.title); assertUser(value.author); text(value.state); bool(value.is_draft);
  for (const key of ["source_branch", "target_branch", "ci_status", "created_at", "updated_at", "web_url"] as const) text(value[key]);
  integer(value.comment_count); bool(value.has_conflicts); assertArray(value.labels, text, 256); nullableText(value.review_decision); numberOrNull(value.additions); numberOrNull(value.deletions);
}
function assertReviewDetail(value: unknown): void {
  if (!isRecord(value)) throw new Error("Invalid desktop result shape");
  const extra = ["description", "merge_status", "approvals", "reviewers", "assignees", "changes_count", "detailed_merge_status", "draft_notes_count", "status_check_rollup"] as const;
  // `changes_overflow` is additive: a core that predates it omits the key,
  // which the shell reads as a complete file list.
  const overflow = Object.hasOwn(value, "changes_overflow");
  assertKeys(value, overflow ? [...SUMMARY_KEYS, ...extra, "changes_overflow"] : [...SUMMARY_KEYS, ...extra]);
  if (overflow) bool(value.changes_overflow, "changes_overflow");
  const summary = Object.fromEntries(SUMMARY_KEYS.map((key) => [key, value[key]])); assertReviewSummary(summary);
  if (typeof value.description !== "string" || value.description.length > MAX_IPC_BYTES) throw new Error("Invalid review description");
  nullableText(value.merge_status, "merge_status"); for (const key of ["approvals", "reviewers", "assignees"] as const) assertArray(value[key], assertUser, 1000);
  integer(value.changes_count); nullableText(value.detailed_merge_status); numberOrNull(value.draft_notes_count); nullableText(value.status_check_rollup);
}
function assertSnapshot(value: unknown, entry: (item: unknown) => void, revision: (item: unknown) => void): void {
  assertKeys(value, ["snapshot_id", "resource", "revision", "cursor", "next_cursor", "entries"]); text(value.snapshot_id, MAX_TEXT, "snapshot_id"); text(value.resource, MAX_TEXT, "resource"); revision(value.revision); integer(value.cursor, 0, "cursor"); if (value.next_cursor !== null) integer(value.next_cursor, 0, "next_cursor"); assertArray(value.entries, entry);
}
function assertDiscussionsPage(value: unknown): void {
  assertKeys(value, ["snapshot_id", "resource", "revision", "cursor", "next_cursor", "discussions"]); text(value.snapshot_id, MAX_TEXT, "snapshot_id"); text(value.resource, MAX_TEXT, "resource");
  assertKeys(value.revision, ["discussion_count"]); integer(value.revision.discussion_count, 0, "discussion_count");
  integer(value.cursor, 0, "cursor"); if (value.next_cursor !== null) { integer(value.next_cursor, 0, "next_cursor"); if (value.next_cursor <= value.cursor) fail("next_cursor", "must advance past the cursor"); }
  assertArray(value.discussions, assertDiscussion);
}
function assertDiffRow(value: unknown): void {
  if (!isRecord(value) || typeof value.kind !== "string") throw new Error("Invalid diff row");
  const common = ["kind", "file_index"];
  if (value.kind === "file") { assertKeys(value, [...common, "old_path", "new_path", "status", "additions", "deletions", "is_binary", "language", "is_truncated", "is_empty", "is_mode_only", "is_rename_only", "is_unavailable", "old_mode", "new_mode"]); integer(value.file_index, 0, "file_index"); for (const key of ["old_path", "new_path", "status"] as const) text(value[key], MAX_TEXT, key); integer(value.additions, 0, "additions"); integer(value.deletions, 0, "deletions"); for (const key of ["is_binary", "is_truncated", "is_empty", "is_mode_only", "is_rename_only", "is_unavailable"] as const) bool(value[key], key); nullableText(value.language, "language"); for (const key of ["old_mode", "new_mode"] as const) nullableText(value[key], key); return; }
  if (value.kind === "hunk") { assertKeys(value, [...common, "hunk_index", "header", "old_start", "old_count", "new_start", "new_count", "context_text"]); for (const key of ["file_index", "hunk_index", "old_start", "old_count", "new_start", "new_count"] as const) integer(value[key], 0, key); text(value.header, MAX_TEXT, "header"); if (typeof value.context_text !== "string") fail("context_text", "must be text"); return; }
  if (value.kind === "line") { assertKeys(value, [...common, "hunk_index", "old_line", "new_line", "content", "line_type"]); integer(value.file_index, 0, "file_index"); integer(value.hunk_index, 0, "hunk_index"); numberOrNull(value.old_line, "old_line"); numberOrNull(value.new_line, "new_line"); if (typeof value.content !== "string") fail("content", "must be text"); text(value.line_type, MAX_TEXT, "line_type"); return; }
  if (value.kind === "gap") {
    assertKeys(value, [...common, "gap_index", "position", "old_start", "new_start", "count"]);
    integer(value.file_index, 0, "file_index"); integer(value.gap_index, 0, "gap_index");
    integer(value.old_start, 1, "old_start"); integer(value.new_start, 1, "new_start");
    if (!(DIFF_GAP_POSITIONS as readonly unknown[]).includes(value.position)) fail("position", "must be before, between or after");
    // Only the gap after the last hunk has an unknown length.
    if (value.count === null) { if (value.position !== "after") fail("count", "must be a line count before or between hunks"); }
    else integer(value.count, 1, "count");
    return;
  }
  if (value.kind === "split") {
    assertKeys(value, [...common, "hunk_index", "row_index", "old", "new"]);
    integer(value.file_index, 0, "file_index"); integer(value.hunk_index, 0, "hunk_index"); integer(value.row_index, 0, "row_index");
    if (value.old !== null) assertSplitCell(value.old);
    if (value.new !== null) assertSplitCell(value.new);
    if (value.old === null && value.new === null) throw new Error("Invalid empty split row");
    return;
  }
  throw new Error("Invalid diff row kind");
}
function assertDiffContext(value: unknown): void {
  assertKeys(value, ["status", "message", "line_count", "rows"]);
  if (!(DIFF_CONTEXT_STATUSES as readonly unknown[]).includes(value.status)) fail("status", "must be a known context status");
  if (value.status !== "ok") {
    // A refused read explains itself and carries no lines.
    text(value.message, MAX_TEXT, "message");
    if (value.line_count !== null) fail("line_count", "must be null when the read failed");
    assertArray(value.rows, () => fail("rows", "must be empty when the read failed"), 0);
    return;
  }
  if (value.message !== null) fail("message", "must be null when the read succeeded");
  integer(value.line_count, 0, "line_count");
  if (Number(value.line_count) > MAX_LINE_COUNT) fail("line_count", `must be at most ${MAX_LINE_COUNT}`);
  let bytes = 0;
  let previous: number | null = null;
  assertArray(value.rows, (row) => {
    assertKeys(row, ["kind", "file_index", "old_line", "new_line", "content"]);
    if (row.kind !== "context") fail("kind", "must be context");
    integer(row.file_index, 0, "file_index");
    integer(row.old_line, 1, "old_line"); integer(row.new_line, 1, "new_line");
    if (Number(row.new_line) > Number(value.line_count)) fail("new_line", "must be inside the file");
    // Rows are one consecutive run of lines inside a single file.
    if (previous !== null && Number(row.new_line) !== previous + 1) fail("new_line", "must follow the previous row");
    previous = Number(row.new_line);
    if (typeof row.content !== "string") fail("content", "must be text");
    bytes += Buffer.byteLength(row.content, "utf8");
    if (bytes > MAX_CONTEXT_BYTES) fail("rows", `must hold at most ${MAX_CONTEXT_BYTES} bytes of content`);
  }, MAX_DIFF_CONTEXT_LINES);
  const rows = value.rows as RecordValue[];
  if (rows.some((row) => row.file_index !== rows[0]?.file_index)) fail("file_index", "must name one file");
}
function assertGeneratedDiff(value: unknown): void {
  assertKeys(value, ["status", "message", "generated", "snapshot_id", "resource", "revision", "cursor", "next_cursor", "entries"]);
  if (!(GENERATE_DIFF_STATUSES as readonly unknown[]).includes(value.status)) fail("status", "must be a known generation status");
  if (value.status !== "ok") {
    text(value.message, MAX_TEXT, "message");
    if (value.generated !== false) fail("generated", "must be false when nothing was generated");
    for (const key of ["snapshot_id", "resource", "revision", "cursor", "next_cursor"] as const) if (value[key] !== null) fail(key, "must be null when nothing was generated");
    assertArray(value.entries, () => fail("entries", "must be empty when nothing was generated"), 0);
    return;
  }
  if (value.message !== null) fail("message", "must be null when the diff was generated");
  if (value.generated !== true) fail("generated", "must be true for a generated diff");
  const page = Object.fromEntries(["snapshot_id", "resource", "revision", "cursor", "next_cursor", "entries"].map((key) => [key, value[key]]));
  assertSnapshot(page, assertDiffRow, assertRevision);
  const entries = value.entries as RecordValue[];
  // A generated diff is one file, with no gap rows, starting at its file row.
  if (Number(value.cursor) !== 0 || entries[0]?.kind !== "file") fail("entries", "must start with the generated file's row");
  if (entries.some((entry) => entry.kind === "gap" || entry.file_index !== entries[0]?.file_index)) fail("entries", "must hold the rows of one file");
}
function assertSplitCell(value: unknown): void {
  assertKeys(value, ["old_line", "new_line", "content", "line_type", "anchor_side"]);
  numberOrNull(value.old_line, "old_line"); numberOrNull(value.new_line, "new_line");
  if (typeof value.content !== "string") fail("content", "must be text");
  text(value.line_type, MAX_TEXT, "line_type");
  if (value.anchor_side !== null && value.anchor_side !== "old" && value.anchor_side !== "new") throw new Error("Invalid diff anchor side");
}
function assertDiscussion(value: unknown): void { assertKeys(value, ["id", "is_inline", "root_comment", "is_resolved", "resolvable"]); text(value.id, MAX_TEXT, "id"); bool(value.is_inline, "is_inline"); assertComment(value.root_comment); bool(value.is_resolved, "is_resolved"); bool(value.resolvable, "resolvable"); }
function assertComment(value: unknown, depth = 0): void { if (depth > 16) throw new Error("Discussion replies are too deep"); assertKeys(value, ["id", "author", "body", "created_at", "file_path", "old_line", "new_line", "is_resolved", "replies"]); text(value.id, MAX_TEXT, "id"); assertUser(value.author); boundedText(value.body, MAX_IPC_BYTES, "body"); text(value.created_at, MAX_TEXT, "created_at"); nullableText(value.file_path, "file_path"); numberOrNull(value.old_line, "old_line"); numberOrNull(value.new_line, "new_line"); bool(value.is_resolved, "is_resolved"); assertArray(value.replies, (reply) => assertComment(reply, depth + 1), 1000); }
function assertCommit(value: unknown): void { assertKeys(value, ["sha", "short_sha", "title", "message", "author", "created_at", "web_url"]); for (const key of ["sha", "short_sha", "title", "message"] as const) text(value[key], MAX_IPC_BYTES); if (value.web_url !== null) text(value.web_url, MAX_IPC_BYTES, "web_url"); assertUser(value.author); nullableText(value.created_at); }
function assertPipeline(value: unknown): void { assertKeys(value, ["id", "status", "ref", "sha", "web_url", "source", "created_at", "finished_at", "duration_seconds"]); integer(value.id); for (const key of ["status", "ref", "sha"] as const) text(value[key], MAX_TEXT, key); nullableText(value.web_url, "web_url"); nullableText(value.source, "source"); nullableText(value.created_at); nullableText(value.finished_at); numberOrNull(value.duration_seconds); }
function assertJob(value: unknown): void { assertKeys(value, ["id", "name", "stage", "status", "web_url", "started_at", "finished_at", "duration_seconds", "allow_failure"]); integer(value.id); for (const key of ["name", "status"] as const) text(value[key], MAX_TEXT, key); nullableText(value.stage, "stage"); nullableText(value.web_url, "web_url"); nullableText(value.started_at); nullableText(value.finished_at); numberOrNull(value.duration_seconds); bool(value.allow_failure); }
function assertPlugin(value: unknown): void {
  assertKeys(value, ["plugin_id", "state", "has_terminal_entry_point", "has_desktop_entry_point", "error", "manifest"]);
  text(value.plugin_id);
  text(value.state);
  bool(value.has_terminal_entry_point);
  bool(value.has_desktop_entry_point);
  if (value.error !== null) assertServiceError(value.error);
  if (value.manifest !== null) assertPluginManifest(value.manifest);
}

function assertPluginManifest(value: unknown): void {
  assertKeys(value, ["title", "version", "api_major", "modules", "navigation", "commands", "methods", "events", "focus_targets", "help_asset", "reads", "assets_available"]);
  text(value.title);
  text(value.version);
  integer(value.api_major, 1);
  assertArray(value.modules, (item) => {
    assertKeys(item, ["id", "title", "entry_asset", "stylesheets"]);
    text(item.id);
    text(item.title);
    nullableText(item.entry_asset);
    assertArray(item.stylesheets, nullableText, 64);
  }, 128);
  assertArray(value.navigation, (item) => {
    assertKeys(item, ["id", "title", "module_id"]);
    text(item.id);
    text(item.title);
    text(item.module_id);
  }, 128);
  assertArray(value.commands, (item) => {
    assertKeys(item, ["id", "title", "navigation_id", "help_text"]);
    text(item.id);
    text(item.title);
    text(item.navigation_id);
    if (typeof item.help_text !== "string" || item.help_text.length > MAX_TEXT) throw new Error("Invalid plugin command help");
  }, 128);
  assertArray(value.methods, text, 256);
  assertArray(value.events, text, 256);
  assertArray(value.focus_targets, (item) => {
    assertKeys(item, ["id", "title", "module_id"]);
    text(item.id);
    text(item.title);
    text(item.module_id);
  }, 128);
  nullableText(value.help_asset);
  assertArray(value.reads, text, 64);
  bool(value.assets_available);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
