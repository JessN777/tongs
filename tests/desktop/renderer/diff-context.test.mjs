// Renderer tests for #383: gap rows, expanding unchanged lines, the Full file
// toggle and generating a truncated file's diff locally. Every assertion reads
// strings, counts, attributes or serialized payloads, never a DOM node.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";
import { InlineAnchorStore } from "../../../desktop/dist/src/renderer/core/navigation.js";
import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import {
  CONTEXT_STATUS_MESSAGES,
  DIFF_RELOADED_MESSAGE,
  EXPANDED_READ_ONLY_MESSAGE,
  FULL_FILE_MAX_ROWS,
  GENERATED_COMMENT_MESSAGE,
  GENERATED_LABEL,
  GENERATION_CANCELLED_MESSAGE,
  NO_MORE_LINES_MESSAGE,
  TRUNCATED_NOTICE,
  createDiffFeature,
  fullFileTooLargeMessage,
  gapLabel,
  hideLabel,
  resolveDiscussionTarget,
} from "../../../desktop/dist/src/renderer/features/diff/index.js";
import { clearPendingEdit } from "../../../desktop/dist/src/renderer/features/review/drawer.js";

const desktopRequire = createRequire(
  new URL("../../../desktop/package.json", import.meta.url),
);
const { JSDOM } = desktopRequire("jsdom");
const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://app.invalid/",
});
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  Node: dom.window.Node,
});
const { cleanup, fireEvent, render, waitFor } = desktopRequire("@testing-library/react");
const React = desktopRequire("react");
afterEach(() => {
  cleanup();
  clearPendingEdit();
});

const revision = { head_sha: "head", base_sha: "base", start_sha: null };

// ---------------------------------------------------------------- fixtures

function fileRow(index, overrides = {}) {
  return {
    kind: "file",
    file_index: index,
    old_path: `src/file_${index}.py`,
    new_path: `src/file_${index}.py`,
    status: "modified",
    additions: 2,
    deletions: 1,
    is_binary: false,
    language: "python",
    is_truncated: false,
    is_empty: false,
    is_mode_only: false,
    is_rename_only: false,
    is_unavailable: false,
    old_mode: null,
    new_mode: null,
    ...overrides,
  };
}

function gap(fileIndex, gapIndex, position, oldStart, newStart, count) {
  return {
    kind: "gap",
    file_index: fileIndex,
    gap_index: gapIndex,
    position,
    old_start: oldStart,
    new_start: newStart,
    count,
  };
}

function hunk(fileIndex, hunkIndex, oldStart, oldCount, newStart, newCount) {
  return {
    kind: "hunk",
    file_index: fileIndex,
    hunk_index: hunkIndex,
    header: `@@ -${oldStart},${oldCount} +${newStart},${newCount} @@`,
    old_start: oldStart,
    old_count: oldCount,
    new_start: newStart,
    new_count: newCount,
    context_text: "",
  };
}

function line(fileIndex, hunkIndex, oldLine, newLine, lineType, content) {
  return {
    kind: "line",
    file_index: fileIndex,
    hunk_index: hunkIndex,
    old_line: oldLine,
    new_line: newLine,
    content,
    line_type: lineType,
  };
}

function cell(oldLine, newLine, lineType, content, anchorSide) {
  return {
    old_line: oldLine,
    new_line: newLine,
    content,
    line_type: lineType,
    anchor_side: anchorSide,
  };
}

function split(fileIndex, hunkIndex, rowIndex, old, fresh) {
  return {
    kind: "split",
    file_index: fileIndex,
    hunk_index: hunkIndex,
    row_index: rowIndex,
    old,
    new: fresh,
  };
}

/**
 * A modified file of FILE_LINES new lines: four unchanged lines, a hunk at new
 * lines 5-8, three unchanged lines, a hunk at new lines 12-13 and the lines
 * after it, whose count the sidecar does not know until the file is read.
 */
const FILE_LINES = 20;

function expandableRows(layout, fileIndex = 0) {
  const rows = [
    fileRow(fileIndex),
    gap(fileIndex, 0, "before", 1, 1, 4),
    hunk(fileIndex, 0, 5, 3, 5, 4),
  ];
  if (layout === "split")
    rows.push(
      split(fileIndex, 0, 0, cell(5, 5, "context", "line 5", "old"), cell(5, 5, "context", "line 5", "new")),
      split(fileIndex, 0, 1, null, cell(null, 6, "addition", "line 6", "new")),
      split(fileIndex, 0, 2, cell(6, 7, "context", "line 7", "old"), cell(6, 7, "context", "line 7", "new")),
      split(fileIndex, 0, 3, cell(7, 8, "context", "line 8", "old"), cell(7, 8, "context", "line 8", "new")),
    );
  else
    rows.push(
      line(fileIndex, 0, 5, 5, "context", "line 5"),
      line(fileIndex, 0, null, 6, "addition", "line 6"),
      line(fileIndex, 0, 6, 7, "context", "line 7"),
      line(fileIndex, 0, 7, 8, "context", "line 8"),
    );
  rows.push(gap(fileIndex, 1, "between", 8, 9, 3), hunk(fileIndex, 1, 11, 2, 12, 2));
  if (layout === "split")
    rows.push(
      split(fileIndex, 1, 0, cell(11, 12, "context", "line 12", "old"), cell(11, 12, "context", "line 12", "new")),
      split(fileIndex, 1, 1, cell(12, null, "deletion", "old 12", "old"), cell(null, 13, "addition", "line 13", "new")),
    );
  else
    rows.push(
      line(fileIndex, 1, 11, 12, "context", "line 12"),
      line(fileIndex, 1, 12, null, "deletion", "old 12"),
      line(fileIndex, 1, null, 13, "addition", "line 13"),
    );
  rows.push(gap(fileIndex, 2, "after", 13, 14, null));
  return rows;
}

/** The gap a new line falls in, from the fixture rows. */
function gapOf(rows, fileIndex, newLine, lineCount) {
  for (const row of rows) {
    if (row.kind !== "gap" || row.file_index !== fileIndex) continue;
    const count = row.count ?? lineCount - row.new_start + 1;
    if (newLine >= row.new_start && newLine < row.new_start + count) return row;
  }
  return null;
}

/**
 * Answers `diff.context` like the sidecar: lines from `new_start`, at most
 * `count`, stopping at the end of the gap, and at most `perReply` per reply,
 * as the response size bound would.
 */
function contextReply(rows, params, { lineCount = FILE_LINES, perReply = Infinity } = {}) {
  const found = gapOf(rows, params.file_index, params.new_start, lineCount);
  if (found === null)
    return { status: "ok", message: null, line_count: lineCount, rows: [] };
  const gapEnd =
    found.new_start + (found.count ?? lineCount - found.new_start + 1);
  const end = Math.min(params.new_start + params.count, gapEnd, params.new_start + perReply);
  const result = [];
  for (let newLine = params.new_start; newLine < end; newLine += 1)
    result.push({
      kind: "context",
      file_index: params.file_index,
      old_line: found.old_start + (newLine - found.new_start),
      new_line: newLine,
      content: `line ${newLine}`,
    });
  return { status: "ok", message: null, line_count: lineCount, rows: result };
}

function settledRead(value) {
  return { requestToken: crypto.randomUUID(), result: Promise.resolve(value) };
}

function page(review, snapshot, entries, next = null, cursor = 0) {
  return {
    snapshot_id: snapshot,
    resource: review,
    revision,
    cursor,
    next_cursor: next,
    entries,
  };
}

function detail(review) {
  return {
    handle: review,
    repository: "repo",
    detail: {
      ...summary(),
      reviewers: [],
      assignees: [],
      changes_count: 1,
      detailed_merge_status: "mergeable",
      draft_notes_count: null,
      status_check_rollup: null,
    },
    capabilities: {
      batched_review: true,
      thread_resolution: true,
      draft_notes: true,
      unapprove: true,
      job_cancel: true,
    },
    revision,
    revision_error: null,
  };
}

function summary() {
  return {
    number: 7,
    title: "Tidy the parser",
    author: { username: "alice", display_name: "Alice Dev" },
    state: "open",
    is_draft: false,
    source_branch: "feature/parser",
    target_branch: "main",
    ci_status: "success",
    created_at: "2026-06-01T09:00:00Z",
    updated_at: "2026-06-02T14:30:00Z",
    web_url: "https://gitlab.example.com/acme/widgets/-/merge_requests/7",
    comment_count: 0,
    has_conflicts: false,
    labels: [],
    review_decision: null,
    additions: null,
    deletions: null,
    description: "",
    merge_status: "can_be_merged",
    approvals: [],
  };
}

/**
 * A bridge whose diff holds `entries` in one page per layout. `rowsFor`
 * gives the rows of a layout, and every request is recorded in `calls`.
 */
function diffBridge(review, rowsFor, overrides = {}) {
  const calls = { open: [], context: [], generate: [], page: [], cancel: [] };
  let snapshots = 0;
  const bridge = {
    calls,
    cancelRead: async (token) => {
      calls.cancel.push(token);
      return true;
    },
    openExternal: async () => true,
    getReview: (handle) => settledRead(detail(handle)),
    openDiff: (params) => {
      calls.open.push({ ...params });
      snapshots += 1;
      return settledRead(
        page(review, `snapshot-${snapshots}`, rowsFor(params.layout)),
      );
    },
    pageDiff: (params) => {
      calls.page.push({ ...params });
      throw new Error("the fixture diff has one page");
    },
    getDiffContext: (params) => {
      calls.context.push({ ...params });
      return settledRead(contextReply(rowsFor("unified"), params));
    },
    generateDiff: (params) => {
      calls.generate.push({ ...params });
      throw new Error("the fixture does not generate");
    },
    listDiscussions: () =>
      settledRead({
        snapshot_id: "discussions-snapshot",
        resource: review,
        revision: { discussion_count: 0 },
        cursor: 0,
        next_cursor: null,
        discussions: [],
      }),
    pageDiscussions: () => {
      throw new Error("the fixture discussions have one page");
    },
    getReviewMutationCapabilities: () =>
      settledRead({
        review,
        capabilities: {
          general_comment: true,
          inline_comment: true,
          multiline_comment: true,
          reply: true,
          resolve: true,
          approve: true,
          request_changes: true,
          comment_verdict: true,
          atomic_review_batch: false,
        },
      }),
    listReviewDrafts: () =>
      settledRead({ cursor: 0, next_cursor: null, drafts: [] }),
  };
  for (const [name, value] of Object.entries(overrides)) {
    const wrapped = value;
    bridge[name] =
      name === "cancelRead"
        ? value
        : (params) => {
            const key = {
              getDiffContext: "context",
              generateDiff: "generate",
              pageDiff: "page",
              openDiff: "open",
            }[name];
            if (key) calls[key].push({ ...params });
            return wrapped(params);
          };
  }
  return bridge;
}

function renderDiff(review, bridge) {
  const feature = createDiffFeature();
  const route = {
    kind: "review",
    item: { handle: review, repository: "repo", summary: summary() },
    panel: "diff",
  };
  function Harness() {
    const inlineAnchors = React.useMemo(() => new InlineAnchorStore(), []);
    const queries = React.useMemo(() => new QueryCoordinator(bridge), []);
    return feature.render(
      {
        bridge,
        queries,
        repositories: [
          { handle: "repo", display_name: "acme/widgets", forge_type: "gitlab" },
        ],
        repositoriesReady: true,
        repositoryGeneration: 1,
        reviewPanels: [{ id: "diff", label: "Files changed", order: 20 }],
        inlineAnchors,
        navigate: () => {},
      },
      route,
    );
  }
  return render(React.createElement(Harness));
}

// ---------------------------------------------------------------- readers

function texts(view, selector) {
  return [...view.container.querySelectorAll(selector)].map(
    (node) => node.textContent,
  );
}

function gapTexts(view) {
  return texts(view, ".diff-unified .diff-gap-button");
}

/** "old/new: content" for each expanded unified line. */
function expandedLines(view) {
  return [...view.container.querySelectorAll(".diff-unified .line-expanded")].map(
    (node) => {
      const [oldLine, newLine] = [...node.querySelectorAll(".line-number")].map(
        (number) => number.textContent,
      );
      return `${oldLine}/${newLine}:${node.querySelector(".line-content").textContent.trim()}`;
    },
  );
}

function fullFileState(view) {
  const button = view.container.querySelector(".full-file-toggle");
  return {
    text: button.textContent,
    pressed: button.getAttribute("aria-pressed"),
    disabled: button.hasAttribute("disabled"),
    title: button.getAttribute("title"),
  };
}

function clickText(view, selector, text) {
  const node = [...view.container.querySelectorAll(selector)].find(
    (candidate) => candidate.textContent === text,
  );
  if (!node) throw new Error(`no ${selector} reads ${text}`);
  fireEvent.click(node);
}

function hideTexts(view) {
  return texts(view, ".diff-unified .diff-gap-hide-button");
}

function warnings(view) {
  return texts(view, ".notice-warning > span:first-child");
}

// ---------------------------------------------------------------- gap rows

test("gap rows read their unchanged line count, or More lines below", async () => {
  const review = "review-gaps";
  const bridge = diffBridge(review, (layout) => expandableRows(layout));
  const view = renderDiff(review, bridge);

  await waitFor(() =>
    assert.deepEqual(gapTexts(view), [
      "4 unchanged lines",
      "3 unchanged lines",
      "More lines below",
    ]),
  );
  // The diff view always opens with gap rows, and opening reads no file.
  assert.deepEqual(bridge.calls.open, [
    { review, layout: "unified", max_items: 1000, gaps: true },
  ]);
  assert.equal(bridge.calls.context.length, 0);
  assert.equal(gapLabel(1), "1 unchanged line");
  assert.equal(gapLabel(12_345), "12,345 unchanged lines");
  assert.equal(gapLabel(null), "More lines below");
});

test("expanding a middle gap inserts its lines read-only with both numbers", async () => {
  const review = "review-expand";
  const bridge = diffBridge(review, (layout) => expandableRows(layout));
  const view = renderDiff(review, bridge);
  await waitFor(() => assert.equal(gapTexts(view).length, 3));

  clickText(view, ".diff-gap-button", "3 unchanged lines");
  await waitFor(() =>
    assert.deepEqual(expandedLines(view), [
      "8/9:line 9",
      "9/10:line 10",
      "10/11:line 11",
    ]),
  );
  assert.deepEqual(bridge.calls.context, [
    { review, snapshot: "snapshot-1", file_index: 0, new_start: 9, count: 3 },
  ]);
  // The read said how long the file is, so the last gap now has a count.
  assert.deepEqual(gapTexts(view), ["4 unchanged lines", "7 unchanged lines"]);
  // The expanded lines sit between the two hunks, in file order.
  assert.deepEqual(
    [
      ...view.container.querySelectorAll(
        ".diff-unified .diff-hunk:not(.diff-gap), .diff-unified .line-expanded",
      ),
    ].map((node) => (node.classList.contains("diff-hunk") ? "hunk" : "expanded")),
    ["hunk", "expanded", "expanded", "expanded", "hunk"],
  );
  // No expanded line offers a line selection.
  assert.equal(
    view.container.querySelectorAll(".line-expanded .line-anchor, .line-expanded .selectable-line").length,
    0,
  );

  // Enter on a gap row, through the row key handling, expands it too.
  const before = [...view.container.querySelectorAll(".diff-gap-button")].find(
    (node) => node.textContent === "4 unchanged lines",
  );
  fireEvent.keyDown(before, { key: "Enter" });
  await waitFor(() => assert.equal(expandedLines(view).length, 7));
  assert.deepEqual(expandedLines(view).slice(0, 4), [
    "1/1:line 1",
    "2/2:line 2",
    "3/3:line 3",
    "4/4:line 4",
  ]);
  assert.equal(bridge.calls.context.length, 2);
});

test("a gap longer than one read is read in chunks and short replies continue", async () => {
  const review = "review-chunks";
  const rows = (layout) => [
    fileRow(0),
    gap(0, 0, "before", 1, 1, 2500),
    hunk(0, 0, 2501, 1, 2501, 1),
    layout === "split"
      ? split(0, 0, 0, cell(2501, null, "deletion", "old", "old"), cell(null, 2501, "addition", "new", "new"))
      : line(0, 0, null, 2501, "addition", "new"),
  ];
  const bridge = diffBridge(review, rows, {
    getDiffContext: (params) =>
      settledRead(contextReply(rows("unified"), params, { lineCount: 2501, perReply: 700 })),
  });
  const view = renderDiff(review, bridge);
  await waitFor(() => assert.deepEqual(gapTexts(view), ["2,500 unchanged lines"]));

  clickText(view, ".diff-gap-button", "2,500 unchanged lines");
  await waitFor(() => assert.equal(gapTexts(view).length, 0));
  assert.deepEqual(
    bridge.calls.context.map((call) => [call.new_start, call.count]),
    [
      [1, 1000],
      [701, 1000],
      [1401, 1000],
      [2101, 400],
    ],
  );
  // The rows window draws the first 200 rows of the 2,500 expanded ones.
  assert.equal(expandedLines(view)[0], "1/1:line 1");
  // The window counts the hide row above the expanded lines too.
  assert.deepEqual(texts(view, ".window-position"), ["1-200 of 2504"]);
});

test("an empty gap after the last hunk says there are no more lines", async () => {
  const review = "review-after-empty";
  const rows = (layout) => expandableRows(layout);
  const bridge = diffBridge(review, rows, {
    getDiffContext: (params) =>
      settledRead(contextReply(rows("unified"), params, { lineCount: 13 })),
  });
  const view = renderDiff(review, bridge);
  await waitFor(() => assert.equal(gapTexts(view).length, 3));

  clickText(view, ".diff-gap-button", "More lines below");
  await waitFor(() => assert.deepEqual(warnings(view), [NO_MORE_LINES_MESSAGE]));
  assert.deepEqual(gapTexts(view), ["4 unchanged lines", "3 unchanged lines"]);
  assert.deepEqual(bridge.calls.context, [
    { review, snapshot: "snapshot-1", file_index: 0, new_start: 14, count: 1000 },
  ]);
});

test("a refused read shows the terminal's message and leaves the gap", async () => {
  const review = "review-refused";
  const replies = [
    { status: "not_found", message: null, line_count: null, rows: [] },
    {
      status: "too_large",
      message: "File too large to expand (over 2 MiB or 20,000 lines).",
      line_count: null,
      rows: [],
    },
    {
      status: "mismatch",
      message:
        "The file at this commit does not match the diff. Refresh the review.",
      line_count: null,
      rows: [],
    },
    { status: "binary", message: null, line_count: null, rows: [] },
  ];
  const bridge = diffBridge(review, (layout) => expandableRows(layout), {
    getDiffContext: () => settledRead(replies.shift()),
  });
  const view = renderDiff(review, bridge);
  await waitFor(() => assert.equal(gapTexts(view).length, 3));

  const expected = [
    CONTEXT_STATUS_MESSAGES.not_found,
    "File too large to expand (over 2 MiB or 20,000 lines).",
    "The file at this commit does not match the diff. Refresh the review.",
    CONTEXT_STATUS_MESSAGES.binary,
  ];
  for (const message of expected) {
    clickText(view, ".diff-gap-button", "3 unchanged lines");
    await waitFor(() => assert.deepEqual(warnings(view), [message]));
    assert.deepEqual(gapTexts(view), [
      "4 unchanged lines",
      "3 unchanged lines",
      "More lines below",
    ]);
  }
  assert.equal(CONTEXT_STATUS_MESSAGES.not_found, "The file was not found at this commit.");
  assert.equal(CONTEXT_STATUS_MESSAGES.binary, "Binary file, there are no lines to expand.");
  assert.equal(expandedLines(view).length, 0);
});

// ---------------------------------------------------------------- collapse

test("a hide row collapses an expanded gap and expanding again reads nothing", async () => {
  const review = "review-collapse";
  const bridge = diffBridge(review, (layout) => expandableRows(layout));
  const view = renderDiff(review, bridge);
  await waitFor(() => assert.equal(gapTexts(view).length, 3));
  assert.deepEqual(hideTexts(view), []);

  clickText(view, ".diff-gap-button", "3 unchanged lines");
  await waitFor(() => assert.equal(expandedLines(view).length, 3));
  assert.deepEqual(hideTexts(view), ["... hide 3 lines"]);
  // The hide row leads the expanded lines and is a button with a name.
  assert.deepEqual(
    [
      ...view.container.querySelectorAll(
        ".diff-unified .diff-hunk:not(.diff-gap), .diff-unified .diff-gap-hide, .diff-unified .line-expanded",
      ),
    ].map((node) =>
      node.classList.contains("diff-gap-hide")
        ? "hide"
        : node.classList.contains("diff-hunk")
          ? "hunk"
          : "expanded",
    ),
    ["hunk", "hide", "expanded", "expanded", "expanded", "hunk"],
  );
  const hide = view.container.querySelector(".diff-gap-hide-button");
  assert.equal(hide.tagName, "BUTTON");
  assert.equal(hide.getAttribute("aria-label"), "Hide 3 unchanged lines");
  assert.equal(hideLabel(1), "... hide 1 line");
  assert.equal(hideLabel(12_345), "... hide 12,345 lines");
  assert.ok([...hideLabel(12_345)].every((char) => char.charCodeAt(0) < 128));

  // Enter on the hide row collapses the gap; focus moves to its gap row.
  hide.focus();
  fireEvent.keyDown(hide, { key: "Enter" });
  await waitFor(() => assert.equal(expandedLines(view).length, 0));
  assert.deepEqual(hideTexts(view), []);
  assert.deepEqual(gapTexts(view), [
    "4 unchanged lines",
    "3 unchanged lines",
    "7 unchanged lines",
  ]);
  assert.equal(document.activeElement?.textContent, "3 unchanged lines");
  assert.equal(bridge.calls.context.length, 1);

  // Expanding again uses the lines already read.
  clickText(view, ".diff-gap-button", "3 unchanged lines");
  await waitFor(() => assert.equal(expandedLines(view).length, 3));
  assert.deepEqual(hideTexts(view), ["... hide 3 lines"]);
  assert.equal(bridge.calls.context.length, 1);

  // A click collapses it too.
  clickText(view, ".diff-gap-hide-button", "... hide 3 lines");
  await waitFor(() => assert.equal(expandedLines(view).length, 0));
  assert.equal(bridge.calls.context.length, 1);
});

test("hiding a gap with Full file on turns it off and keeps the gaps shown before", async () => {
  const review = "review-collapse-full";
  const bridge = diffBridge(review, (layout) => expandableRows(layout));
  const view = renderDiff(review, bridge);
  await waitFor(() => assert.equal(gapTexts(view).length, 3));

  clickText(view, ".diff-gap-button", "3 unchanged lines");
  await waitFor(() => assert.equal(expandedLines(view).length, 3));
  fireEvent.click(view.getByText("Full file"));
  await waitFor(() => assert.equal(expandedLines(view).length, 14));
  assert.deepEqual(hideTexts(view), [
    "... hide 4 lines",
    "... hide 3 lines",
    "... hide 7 lines",
  ]);
  const reads = bridge.calls.context.length;

  clickText(view, ".diff-gap-hide-button", "... hide 4 lines");
  await waitFor(() => assert.equal(fullFileState(view).pressed, "false"));
  // The middle gap was expanded before Full file, so it stays expanded.
  assert.deepEqual(gapTexts(view), ["4 unchanged lines", "7 unchanged lines"]);
  assert.deepEqual(expandedLines(view), [
    "8/9:line 9",
    "9/10:line 10",
    "10/11:line 11",
  ]);
  assert.deepEqual(hideTexts(view), ["... hide 3 lines"]);
  assert.equal(bridge.calls.context.length, reads);

  // Full file on and off again behaves as before.
  fireEvent.click(view.getByText("Full file"));
  await waitFor(() => assert.equal(expandedLines(view).length, 14));
  fireEvent.click(view.getByText("Full file"));
  await waitFor(() => assert.equal(fullFileState(view).pressed, "false"));
  assert.equal(expandedLines(view).length, 3);
  assert.equal(bridge.calls.context.length, reads);
});

test("split view draws the hide row in both panes and either one collapses", async () => {
  const review = "review-collapse-split";
  const bridge = diffBridge(review, (layout) => expandableRows(layout));
  const view = renderDiff(review, bridge);
  await waitFor(() => assert.equal(gapTexts(view).length, 3));
  fireEvent.click(view.getByText("Split"));
  await waitFor(() =>
    assert.equal(texts(view, ".split-pane-new .diff-gap-button").length, 3),
  );

  clickText(view, ".split-pane-new .diff-gap-button", "3 unchanged lines");
  await waitFor(() =>
    assert.equal(texts(view, ".split-pane-new .line-expanded").length, 3),
  );
  assert.deepEqual(texts(view, ".split-pane-old .diff-gap-hide-button"), [
    "... hide 3 lines",
  ]);
  assert.deepEqual(texts(view, ".split-pane-new .diff-gap-hide-button"), [
    "... hide 3 lines",
  ]);

  clickText(view, ".split-pane-old .diff-gap-hide-button", "... hide 3 lines");
  await waitFor(() =>
    assert.equal(texts(view, ".split-pane-new .line-expanded").length, 0),
  );
  assert.equal(texts(view, ".diff-gap-hide-button").length, 0);
  assert.equal(texts(view, ".split-pane-old .diff-gap-button").length, 3);
  // Focus lands on the gap row of the pane the reader used.
  assert.equal(document.activeElement?.textContent, "3 unchanged lines");
  assert.equal(
    document.activeElement?.closest(".split-pane")?.classList.contains("split-pane-old"),
    true,
  );
  assert.equal(bridge.calls.context.length, 1);
});

// ---------------------------------------------------------------- Full file

test("Full file expands every gap and turning it off restores the gaps shown before", async () => {
  const review = "review-full";
  const bridge = diffBridge(review, (layout) => expandableRows(layout));
  const view = renderDiff(review, bridge);
  await waitFor(() => assert.equal(gapTexts(view).length, 3));
  assert.deepEqual(fullFileState(view), {
    text: "Full file",
    pressed: "false",
    disabled: false,
    title: "Show every line of this file",
  });

  // The reader expands the middle gap first.
  clickText(view, ".diff-gap-button", "3 unchanged lines");
  await waitFor(() => assert.equal(expandedLines(view).length, 3));

  fireEvent.click(view.getByText("Full file"));
  await waitFor(() => assert.equal(fullFileState(view).pressed, "true"));
  await waitFor(() => assert.equal(gapTexts(view).length, 0));
  // Every line of the 20-line file: 4 + 3 + 7 expanded beside the hunks.
  assert.equal(expandedLines(view).length, 14);
  assert.deepEqual(expandedLines(view).slice(-2), ["18/19:line 19", "19/20:line 20"]);
  // The middle gap's read already said how long the file is, so the gap
  // after the last hunk is read for exactly its 7 lines, and the middle gap is
  // not read again.
  assert.deepEqual(
    bridge.calls.context.map((call) => [call.new_start, call.count]),
    [
      [9, 3],
      [1, 4],
      [14, 7],
    ],
  );

  fireEvent.click(view.getByText("Full file"));
  await waitFor(() => assert.equal(fullFileState(view).pressed, "false"));
  assert.deepEqual(gapTexts(view), ["4 unchanged lines", "7 unchanged lines"]);
  assert.deepEqual(expandedLines(view), [
    "8/9:line 9",
    "9/10:line 10",
    "10/11:line 11",
  ]);

  // On again: every gap is cached for this snapshot, so nothing is read.
  fireEvent.click(view.getByText("Full file"));
  await waitFor(() => assert.equal(expandedLines(view).length, 14));
  assert.equal(bridge.calls.context.length, 3);
});

test("Full file on an unread file learns its length with a one-line read", async () => {
  const review = "review-full-probe";
  const bridge = diffBridge(review, (layout) => expandableRows(layout));
  const view = renderDiff(review, bridge);
  await waitFor(() => assert.equal(gapTexts(view).length, 3));

  fireEvent.click(view.getByText("Full file"));
  await waitFor(() => assert.equal(expandedLines(view).length, 14));
  assert.deepEqual(
    bridge.calls.context.map((call) => [call.new_start, call.count]),
    [
      [14, 1],
      [1, 4],
      [9, 3],
      [14, 7],
    ],
  );
  assert.equal(fullFileState(view).pressed, "true");
});

test("Full file turns off when the reader moves to another file", async () => {
  const review = "review-full-switch";
  const bridge = diffBridge(review, (layout) => [
    ...expandableRows(layout, 0),
    ...expandableRows(layout, 1),
  ]);
  const view = renderDiff(review, bridge);
  await waitFor(() => assert.equal(gapTexts(view).length, 3));

  fireEvent.click(view.getByText("Full file"));
  await waitFor(() => assert.equal(expandedLines(view).length, 14));
  clickText(view, ".file-item:not(.hunk-item) > span:first-child", "src/file_1.py");
  await waitFor(() => assert.equal(fullFileState(view).pressed, "false"));
  assert.equal(expandedLines(view).length, 0);
  clickText(view, ".file-item:not(.hunk-item) > span:first-child", "src/file_0.py");
  await waitFor(() => assert.equal(gapTexts(view).length, 3));
  assert.equal(expandedLines(view).length, 0);
  assert.equal(fullFileState(view).pressed, "false");
});

test("Full file refuses a file past the row cap, before or after the file is read", async () => {
  assert.equal(FULL_FILE_MAX_ROWS, 20_000);
  assert.equal(
    fullFileTooLargeMessage(20_005),
    "File too large to show in full (20,005 lines). Expand its gaps one at a time.",
  );
  // Known gap lengths already pass the cap: nothing is read.
  const review = "review-cap";
  const big = (layout) => [
    fileRow(0),
    gap(0, 0, "before", 1, 1, 20_000),
    hunk(0, 0, 20_001, 1, 20_001, 1),
    layout === "split"
      ? split(0, 0, 0, null, cell(null, 20_001, "addition", "new", "new"))
      : line(0, 0, null, 20_001, "addition", "new"),
  ];
  const bridge = diffBridge(review, big);
  const view = renderDiff(review, bridge);
  await waitFor(() => assert.deepEqual(gapTexts(view), ["20,000 unchanged lines"]));
  fireEvent.click(view.getByText("Full file"));
  await waitFor(() =>
    assert.deepEqual(warnings(view), [fullFileTooLargeMessage(20_001)]),
  );
  assert.equal(bridge.calls.context.length, 0);
  assert.equal(fullFileState(view).pressed, "false");
  assert.deepEqual(gapTexts(view), ["20,000 unchanged lines"]);
  cleanup();

  // The gap after the last hunk reveals a file too long to show whole.
  const longReview = "review-cap-after";
  const rows = (layout) => expandableRows(layout);
  const longBridge = diffBridge(longReview, rows, {
    getDiffContext: (params) =>
      settledRead(contextReply(rows("unified"), params, { lineCount: 30_000 })),
  });
  const longView = renderDiff(longReview, longBridge);
  await waitFor(() => assert.equal(gapTexts(longView).length, 3));
  fireEvent.click(longView.getByText("Full file"));
  await waitFor(() =>
    assert.deepEqual(warnings(longView), [
      // 7 hunk lines, 4 + 3 in known gaps and 29,987 after the last hunk.
      fullFileTooLargeMessage(30_001),
    ]),
  );
  assert.deepEqual(
    longBridge.calls.context.map((call) => [call.new_start, call.count]),
    [[14, 1]],
  );
  assert.equal(fullFileState(longView).pressed, "false");
  // The read taught the gap its length; it stays collapsed.
  assert.deepEqual(gapTexts(longView), [
    "4 unchanged lines",
    "3 unchanged lines",
    "29,987 unchanged lines",
  ]);
});

test("Full file is disabled with the reason on a file it cannot show", async () => {
  const cases = [
    [fileRow(0, { status: "added" }), "The diff already shows every line of an added file."],
    [fileRow(0, { is_binary: true }), "Binary file, there are no lines to show."],
    [
      fileRow(0, { is_truncated: true }),
      "The forge did not send this whole diff. Use Generate diff locally to build it.",
    ],
  ];
  for (const [file, reason] of cases) {
    const review = `review-disabled-${file.status}-${file.is_binary}`;
    const bridge = diffBridge(review, () => [file]);
    const view = renderDiff(review, bridge);
    await waitFor(() => assert.equal(view.container.querySelectorAll(".full-file-toggle").length, 1));
    await waitFor(() => assert.equal(texts(view, ".file-path").length, 1));
    assert.deepEqual(fullFileState(view), {
      text: "Full file",
      pressed: "false",
      disabled: true,
      title: reason,
    });
    cleanup();
  }
});

// ---------------------------------------------------------------- read-only

test("a comment on an expanded line is refused and opens no composer", async () => {
  const review = "review-readonly";
  const bridge = diffBridge(review, (layout) => expandableRows(layout));
  const view = renderDiff(review, bridge);
  await waitFor(() => assert.equal(gapTexts(view).length, 3));
  clickText(view, ".diff-gap-button", "3 unchanged lines");
  await waitFor(() => assert.equal(expandedLines(view).length, 3));

  fireEvent.click(view.getByLabelText("Comment on new line 10"));
  await waitFor(() =>
    assert.deepEqual(warnings(view), [EXPANDED_READ_ONLY_MESSAGE]),
  );
  assert.equal(
    EXPANDED_READ_ONLY_MESSAGE,
    "Expanded lines are read-only. Comment on a line inside a hunk.",
  );
  assert.equal(view.container.querySelectorAll(".inline-composer-row").length, 0);

  fireEvent.click(view.getByText("Dismiss"));
  await waitFor(() => assert.deepEqual(warnings(view), []));
  const code = view.container.querySelectorAll(".line-expanded .line-content")[1];
  fireEvent.keyDown(code, { key: "c" });
  await waitFor(() =>
    assert.deepEqual(warnings(view), [EXPANDED_READ_ONLY_MESSAGE]),
  );
  assert.equal(view.container.querySelectorAll(".inline-composer-row").length, 0);

  // A hunk line still opens the composer.
  fireEvent.click(view.getByLabelText("Comment on new line 6"));
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".inline-composer-row").length, 1),
  );
});

test("a discussion on a line only a gap holds never resolves to an anchor", () => {
  const loaded = {
    layout: "unified",
    snapshotId: "snapshot-1",
    resource: "review",
    revision,
    rows: expandableRows("unified"),
    partialError: null,
  };
  const at = (side, lineNumber) =>
    resolveDiscussionTarget("review", loaded, {
      discussionId: "d",
      path: "src/file_0.py",
      side,
      line: lineNumber,
    });
  // New line 10 and old line 9 are in the middle gap; line 6 is in a hunk.
  assert.equal(at("new", 10), null);
  assert.equal(at("old", 9), null);
  assert.equal(at("new", 2), null);
  assert.equal(at("new", 6)?.newLine, 6);
  assert.equal(at("new", 6)?.contextComplete, true);
});

test("a thread on an expanded line is drawn under it, as under a hunk line", async () => {
  const review = "review-expanded-thread";
  // One published thread on new line 10, which only the middle gap holds: an
  // unchanged line now, commented on an earlier revision.
  const discussion = {
    id: "d1",
    is_inline: true,
    is_resolved: false,
    resolvable: true,
    root_comment: {
      id: "d1-root",
      author: { username: "bob", display_name: "" },
      body: "Left on an earlier revision",
      created_at: "2026-09-09T00:00:00Z",
      file_path: "src/file_0.py",
      old_line: null,
      new_line: 10,
      is_resolved: false,
      replies: [],
    },
  };
  const bridge = diffBridge(review, (layout) => expandableRows(layout), {
    listDiscussions: () =>
      settledRead({
        snapshot_id: "discussions-snapshot",
        resource: review,
        revision: { discussion_count: 1 },
        cursor: 0,
        next_cursor: null,
        discussions: [discussion],
      }),
  });
  const view = renderDiff(review, bridge);
  // Wait for the discussions to arrive, so the next check is not vacuous.
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".file-threads").length, 1),
  );
  await waitFor(() => assert.equal(gapTexts(view).length, 3));
  // The gap row still hides the thread: the line it points at is not drawn.
  assert.equal(view.container.querySelectorAll(".diff-thread").length, 0);

  clickText(view, ".diff-gap-button", "3 unchanged lines");
  await view.findByLabelText("Discussion thread on new line 10");
  assert.equal(view.container.querySelectorAll(".diff-thread").length, 1);
  // The thread row sits directly under its expanded line, in file order.
  assert.deepEqual(
    [
      ...view.container.querySelectorAll(
        ".diff-unified .line-expanded, .diff-unified .thread-row",
      ),
    ].map((node) => (node.classList.contains("thread-row") ? "thread" : "line")),
    ["line", "line", "thread", "line"],
  );
  // The thread row carries the review-row key n and p navigate by.
  assert.equal(
    view.container
      .querySelector(".diff-unified .thread-row")
      .getAttribute("data-review-row"),
    "thread:d1",
  );
});

// ---------------------------------------------------------------- split

test("split view expands a gap into both panes with each side's numbers", async () => {
  const review = "review-split";
  const bridge = diffBridge(review, (layout) => expandableRows(layout));
  const view = renderDiff(review, bridge);
  await waitFor(() => assert.equal(gapTexts(view).length, 3));
  fireEvent.click(view.getByText("Split"));
  await waitFor(() =>
    assert.deepEqual(texts(view, ".split-pane-new .diff-gap-button"), [
      "4 unchanged lines",
      "3 unchanged lines",
      "More lines below",
    ]),
  );
  assert.equal(texts(view, ".split-pane-old .diff-gap-button").length, 3);

  clickText(view, ".split-pane-old .diff-gap-button", "3 unchanged lines");
  await waitFor(() =>
    assert.deepEqual(texts(view, ".split-pane-old .line-expanded .line-number"), [
      "8",
      "9",
      "10",
    ]),
  );
  assert.deepEqual(texts(view, ".split-pane-new .line-expanded .line-number"), [
    "9",
    "10",
    "11",
  ]);
  // The layout switch made a new snapshot, which the read names.
  assert.deepEqual(bridge.calls.context, [
    { review, snapshot: "snapshot-2", file_index: 0, new_start: 9, count: 3 },
  ]);
});

// ---------------------------------------------------------------- expired

/** Whether the "reloaded, try again" notice is on screen. */
function showsReloaded(view) {
  return texts(view, ".notice-warning").includes(DIFF_RELOADED_MESSAGE);
}

test("an expired diff takes the reload path and the gap can be expanded again", async () => {
  const review = "review-expired";
  const replies = [
    () =>
      settledRead({
        status: "expired",
        message: "This diff is no longer open. Reload it to continue.",
        line_count: null,
        rows: [],
      }),
    () =>
      settledRead({ status: "not_found", message: null, line_count: null, rows: [] }),
  ];
  const rows = (layout) => expandableRows(layout);
  const bridge = diffBridge(review, rows, {
    getDiffContext: (params) => {
      const next = replies.shift();
      return next ? next(params) : settledRead(contextReply(rows("unified"), params));
    },
  });
  const view = renderDiff(review, bridge);
  await waitFor(() => assert.equal(gapTexts(view).length, 3));

  clickText(view, ".diff-gap-button", "3 unchanged lines");
  await waitFor(() =>
    assert.deepEqual(texts(view, ".notice-warning"), [DIFF_RELOADED_MESSAGE]),
  );
  // The diff is read again from the forge, as Refresh reads it.
  await waitFor(() => assert.equal(bridge.calls.open.length, 2));
  assert.deepEqual(bridge.calls.open[1], {
    review,
    layout: "unified",
    max_items: 1000,
    gaps: true,
  });
  await waitFor(() => assert.equal(gapTexts(view).length, 3));
  assert.equal(expandedLines(view).length, 0);

  // A retry that fails on the new snapshot leaves the notice up.
  clickText(view, ".diff-gap-button", "3 unchanged lines");
  await waitFor(() =>
    assert.deepEqual(warnings(view), [CONTEXT_STATUS_MESSAGES.not_found]),
  );
  assert.equal(showsReloaded(view), true);

  // The next retry succeeds, so the notice no longer asks to try again.
  clickText(view, ".diff-gap-button", "3 unchanged lines");
  await waitFor(() => assert.equal(expandedLines(view).length, 3));
  await waitFor(() => assert.deepEqual(texts(view, ".notice-warning"), []));
  assert.deepEqual(
    bridge.calls.context.map((call) => call.snapshot),
    ["snapshot-1", "snapshot-2", "snapshot-2"],
  );
});

test("Refresh clears the reloaded notice", async () => {
  const review = "review-expired-refresh";
  let expired = true;
  const rows = (layout) => expandableRows(layout);
  const bridge = diffBridge(review, rows, {
    getDiffContext: (params) => {
      if (expired) {
        expired = false;
        return settledRead({
          status: "expired",
          message: "This diff is no longer open. Reload it to continue.",
          line_count: null,
          rows: [],
        });
      }
      return settledRead(contextReply(rows("unified"), params));
    },
  });
  const view = renderDiff(review, bridge);
  await waitFor(() => assert.equal(gapTexts(view).length, 3));
  clickText(view, ".diff-gap-button", "3 unchanged lines");
  await waitFor(() => assert.equal(bridge.calls.open.length, 2));
  await waitFor(() => assert.equal(showsReloaded(view), true));

  fireEvent.click(view.getByText("Refresh"));
  await waitFor(() => assert.equal(bridge.calls.open.length, 3));
  await waitFor(() => assert.deepEqual(texts(view, ".notice-warning"), []));
});

test("Full file on the reloaded snapshot clears the reloaded notice", async () => {
  const review = "review-expired-full";
  let expired = true;
  const rows = (layout) => expandableRows(layout);
  const bridge = diffBridge(review, rows, {
    getDiffContext: (params) => {
      if (expired) {
        expired = false;
        return settledRead({
          status: "expired",
          message: "This diff is no longer open. Reload it to continue.",
          line_count: null,
          rows: [],
        });
      }
      return settledRead(contextReply(rows("unified"), params));
    },
  });
  const view = renderDiff(review, bridge);
  await waitFor(() => assert.equal(gapTexts(view).length, 3));
  clickText(view, ".diff-gap-button", "3 unchanged lines");
  await waitFor(() => assert.equal(bridge.calls.open.length, 2));
  await waitFor(() => assert.equal(showsReloaded(view), true));
  await waitFor(() => assert.equal(gapTexts(view).length, 3));

  fireEvent.click(view.getByText("Full file"));
  await waitFor(() => assert.equal(fullFileState(view).pressed, "true"));
  // Every unchanged line of the 20-line file: 4 + 3 + 7.
  await waitFor(() => assert.equal(expandedLines(view).length, 14));
  assert.deepEqual(texts(view, ".notice-warning"), []);
});

// ---------------------------------------------------------------- generate

function truncatedRows(layout, partial = false) {
  const rows = [fileRow(0, { is_truncated: true, additions: 0, deletions: 0 })];
  if (partial) {
    rows.push(hunk(0, 0, 1, 1, 1, 1));
    rows.push(
      layout === "split"
        ? split(0, 0, 0, cell(1, null, "deletion", "first old", "old"), cell(null, 1, "addition", "first new", "new"))
        : line(0, 0, null, 1, "addition", "first new"),
    );
  }
  return rows;
}

function generatedRows(fileIndex = 0) {
  return [
    fileRow(fileIndex, { additions: 2, deletions: 1 }),
    hunk(fileIndex, 0, 1, 2, 1, 3),
    line(fileIndex, 0, 1, 1, "context", "kept"),
    line(fileIndex, 0, 2, null, "deletion", "removed"),
    line(fileIndex, 0, null, 2, "addition", "added one"),
  ];
}

function generatedPage(review, entries, next = null, cursor = 0) {
  return {
    status: "ok",
    message: null,
    generated: true,
    ...page(review, "generated-1", entries, next, cursor),
  };
}

function generateFailure(status, message) {
  return {
    status,
    message,
    generated: false,
    snapshot_id: null,
    resource: null,
    revision: null,
    cursor: null,
    next_cursor: null,
    entries: [],
  };
}

test("a truncated file offers Generate diff locally and swaps in the generated rows", async () => {
  const review = "review-generate";
  let release = () => {};
  const bridge = diffBridge(review, (layout) => truncatedRows(layout), {
    generateDiff: () => ({
      requestToken: "generate-token",
      result: new Promise((resolve) => {
        release = () =>
          resolve(generatedPage(review, generatedRows().slice(0, 3), 3));
      }),
    }),
    pageDiff: (params) =>
      settledRead(page(review, "generated-1", generatedRows().slice(3), null, params.cursor)),
  });
  const view = renderDiff(review, bridge);
  await waitFor(() => assert.deepEqual(texts(view, ".file-generate"), ["Generate diff locally"]));
  assert.deepEqual(warnings(view), [TRUNCATED_NOTICE]);
  assert.equal(
    TRUNCATED_NOTICE,
    "This file's diff is too large for the forge to send. Use Generate diff locally to build it here (downloads both versions of the file).",
  );
  // The button stands in for the Truncated badge.
  assert.deepEqual(texts(view, ".diff-file .badge"), []);

  fireEvent.click(view.getByText("Generate diff locally"));
  await waitFor(() =>
    assert.deepEqual(texts(view, ".notice-loading > span"), [
      "Generating diff locally (downloading 2 files).",
    ]),
  );
  assert.deepEqual(texts(view, ".notice-loading button"), ["Cancel"]);
  assert.equal(
    view.container.querySelector(".file-generate").hasAttribute("disabled"),
    true,
  );
  assert.deepEqual(bridge.calls.generate, [
    { review, snapshot: "snapshot-1", file_index: 0 },
  ]);

  release();
  await waitFor(() =>
    assert.deepEqual(texts(view, ".diff-unified .line-content"), [
      " kept",
      "−removed",
      "+added one",
    ]),
  );
  assert.deepEqual(bridge.calls.page, [
    { snapshot: "generated-1", resource: review, cursor: 3, max_items: 1000 },
  ]);
  assert.deepEqual(texts(view, ".diff-file .badge"), [GENERATED_LABEL]);
  assert.deepEqual(texts(view, ".file-generate"), []);
  assert.deepEqual(texts(view, ".notice-generated"), [GENERATED_COMMENT_MESSAGE]);
  assert.equal(
    GENERATED_COMMENT_MESSAGE,
    "Comments are not available on a diff generated locally.",
  );
  // No generated row is a comment anchor or a line selection.
  assert.equal(
    view.container.querySelectorAll(".gutter-comment:not(.gutter-comment-empty)").length,
    0,
  );
  assert.equal(view.container.querySelectorAll(".line-anchor").length, 0);
  // The hunk navigation follows the generated hunks.
  assert.deepEqual(texts(view, ".hunk-item"), ["@@ -1,2 +1,3 @@"]);
  assert.equal(fullFileState(view).disabled, true);
});

test("Cancel stops a running generation through the bridge cancel frame", async () => {
  const review = "review-generate-cancel";
  let release = () => {};
  const bridge = diffBridge(review, (layout) => truncatedRows(layout), {
    generateDiff: () => ({
      requestToken: "generate-token",
      result: new Promise((resolve) => {
        release = () => resolve(generatedPage(review, generatedRows()));
      }),
    }),
  });
  const view = renderDiff(review, bridge);
  await waitFor(() => assert.equal(texts(view, ".file-generate").length, 1));
  fireEvent.click(view.getByText("Generate diff locally"));
  await waitFor(() => assert.deepEqual(texts(view, ".notice-loading button"), ["Cancel"]));

  fireEvent.click(view.getByText("Cancel"));
  await waitFor(() =>
    assert.deepEqual(texts(view, ".notice-warning"), [
      `${TRUNCATED_NOTICE} ${GENERATION_CANCELLED_MESSAGE}`,
    ]),
  );
  assert.equal(bridge.calls.cancel.includes("generate-token"), true);
  // A reply that lands after the cancel is dropped.
  release();
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.deepEqual(texts(view, ".diff-unified .line-content"), []);
  assert.deepEqual(texts(view, ".file-generate"), ["Generate diff locally"]);
  assert.equal(
    view.container.querySelector(".file-generate").hasAttribute("disabled"),
    false,
  );
});

test("a refused or failed generation keeps the notice and says why", async () => {
  const review = "review-generate-fail";
  const replies = [
    () =>
      settledRead(
        generateFailure(
          "binary",
          "This file is binary, so there is no text diff to generate.",
        ),
      ),
    () =>
      settledRead(
        generateFailure(
          "too_large",
          "The generated diff is too large to show with the other diffs already open. Close one and try again.",
        ),
      ),
    () =>
      settledRead(
        generateFailure(
          "too_large",
          "This file is too large to generate locally (limit 5 MiB or 100,000 lines per version).",
        ),
      ),
    () => ({
      requestToken: crypto.randomUUID(),
      result: Promise.reject({
        code: "forge_unavailable",
        message: "The forge could not be reached.",
        retryable: true,
      }),
    }),
  ];
  const bridge = diffBridge(review, (layout) => truncatedRows(layout), {
    generateDiff: () => replies.shift()(),
  });
  const view = renderDiff(review, bridge);
  await waitFor(() => assert.equal(texts(view, ".file-generate").length, 1));

  for (const reason of [
    "This file is binary, so there is no text diff to generate.",
    "The generated diff is too large to show with the other diffs already open. Close one and try again.",
    "This file is too large to generate locally (limit 5 MiB or 100,000 lines per version).",
    "Could not generate the diff. (The forge could not be reached.)",
  ]) {
    fireEvent.click(view.getByText("Generate diff locally"));
    await waitFor(() =>
      assert.deepEqual(texts(view, ".notice-warning"), [
        `${TRUNCATED_NOTICE} ${reason}`,
      ]),
    );
    assert.deepEqual(texts(view, ".file-generate"), ["Generate diff locally"]);
  }
  assert.equal(bridge.calls.generate.length, 4);
  assert.deepEqual(texts(view, ".diff-file .badge"), []);
});

test("a partly truncated file shows the notice above its hunks", async () => {
  const review = "review-partial-truncation";
  const bridge = diffBridge(review, (layout) => truncatedRows(layout, true));
  const view = renderDiff(review, bridge);
  await waitFor(() =>
    assert.deepEqual(texts(view, ".diff-unified .line-content"), ["+first new"]),
  );
  assert.deepEqual(
    [
      ...view.container.querySelectorAll(
        ".diff-view .notice-warning, .diff-view .diff-hunk",
      ),
    ].map((node) => (node.classList.contains("notice") ? node.textContent : "hunk")),
    [TRUNCATED_NOTICE, "hunk"],
  );
});

test("an expired generation takes the reload path", async () => {
  const review = "review-generate-expired";
  const replies = [
    () =>
      settledRead(
        generateFailure(
          "expired",
          "This diff is no longer open. Reload it to continue.",
        ),
      ),
    () => settledRead(generatedPage(review, generatedRows())),
  ];
  const bridge = diffBridge(review, (layout) => truncatedRows(layout), {
    generateDiff: () => replies.shift()(),
  });
  const view = renderDiff(review, bridge);
  await waitFor(() => assert.equal(texts(view, ".file-generate").length, 1));
  fireEvent.click(view.getByText("Generate diff locally"));
  await waitFor(() => assert.equal(bridge.calls.open.length, 2));
  await waitFor(() =>
    assert.deepEqual(texts(view, ".notice-warning"), [
      DIFF_RELOADED_MESSAGE,
      TRUNCATED_NOTICE,
    ]),
  );

  // Generating again on the reloaded snapshot succeeds and clears the notice.
  fireEvent.click(view.getByText("Generate diff locally"));
  await waitFor(() =>
    assert.deepEqual(texts(view, ".diff-unified .line-content"), [
      " kept",
      "−removed",
      "+added one",
    ]),
  );
  assert.deepEqual(texts(view, ".notice-warning"), []);
  assert.deepEqual(
    bridge.calls.generate.map((call) => call.snapshot),
    ["snapshot-1", "snapshot-2"],
  );
});
