import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";
import { InlineAnchorStore } from "../../../desktop/dist/src/renderer/core/navigation.js";
import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import {
  CHANGES_OVERFLOW_NOTICE,
  createDiffFeature,
  loadAllPages,
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

test("diff reconstruction follows next_cursor across page boundaries", async () => {
  const calls = [];
  const bridge = {
    cancelRead: async () => true,
    openDiff: (params) => {
      calls.push(["open", params]);
      return read("open", {
        snapshot_id: "s1",
        resource: "review",
        revision,
        cursor: 0,
        next_cursor: 9,
        entries: [
          {
            kind: "file",
            file_index: 0,
            old_path: "a.py",
            new_path: "a.py",
            status: "modified",
            additions: 1,
            deletions: 1,
            is_binary: false,
            language: "python",
            is_truncated: false,
            is_empty: false,
            is_mode_only: false,
            is_rename_only: false,
            is_unavailable: false,
          },
        ],
      });
    },
    pageDiff: (params) => {
      calls.push(["page", params]);
      return read("page", {
        snapshot_id: "s1",
        resource: "review",
        revision,
        cursor: 9,
        next_cursor: null,
        entries: [
          {
            kind: "hunk",
            file_index: 0,
            hunk_index: 0,
            header: "@@ -1 +1 @@",
            old_start: 1,
            old_count: 1,
            new_start: 1,
            new_count: 1,
            context_text: "",
          },
        ],
      });
    },
  };
  const result = await loadAllPages(
    "review",
    "split",
    bridge,
    new QueryCoordinator(bridge),
  );
  assert.equal(result.layout, "split");
  assert.equal(result.rows.length, 2);
  assert.deepEqual(calls, [
    ["open", { review: "review", layout: "split", max_items: 1000 }],
    [
      "page",
      { snapshot: "s1", resource: "review", cursor: 9, max_items: 1000 },
    ],
  ]);
});

test("diff reconstruction rejects a revision change between pages", async () => {
  const bridge = {
    cancelRead: async () => true,
    openDiff: () =>
      read("open", {
        snapshot_id: "s1",
        resource: "review",
        revision,
        cursor: 0,
        next_cursor: 1,
        entries: [],
      }),
    pageDiff: () =>
      read("page", {
        snapshot_id: "s1",
        resource: "review",
        revision: { ...revision, head_sha: "changed" },
        cursor: 1,
        next_cursor: null,
        entries: [],
      }),
  };
  await assert.rejects(
    loadAllPages("review", "unified", bridge, new QueryCoordinator(bridge)),
    (error) => error.code === "revision_changed" && error.retryable === false,
  );
});

test("diff reconstruction rejects nonprogressing and mismatched page cursors", async () => {
  for (const page of [
    {
      snapshot_id: "s1",
      resource: "review",
      revision,
      cursor: 1,
      next_cursor: 1,
      entries: [],
    },
    {
      snapshot_id: "s1",
      resource: "review",
      revision,
      cursor: 2,
      next_cursor: null,
      entries: [],
    },
  ]) {
    let calls = 0;
    const bridge = {
      cancelRead: async () => true,
      openDiff: () =>
        read("open", {
          snapshot_id: "s1",
          resource: "review",
          revision,
          cursor: 0,
          next_cursor: 1,
          entries: [],
        }),
      pageDiff: () => {
        calls += 1;
        return read("page", page);
      },
    };
    await assert.rejects(
      loadAllPages("review", "unified", bridge, new QueryCoordinator(bridge)),
      (error) => error.code === "invalid_diff_page",
    );
    assert.equal(calls, 1);
  }
});

test("diff reconstruction rejects a backward first cursor", async () => {
  const bridge = {
    cancelRead: async () => true,
    openDiff: () =>
      read("open", {
        snapshot_id: "s1",
        resource: "review",
        revision,
        cursor: 0,
        next_cursor: 0,
        entries: [],
      }),
  };
  await assert.rejects(
    loadAllPages("review", "unified", bridge, new QueryCoordinator(bridge)),
    (error) => error.code === "invalid_diff_page",
  );
});

test("diff reconstruction retains a bounded partial result at the row cap", async () => {
  const row = {
    kind: "line",
    file_index: 0,
    hunk_index: 0,
    old_line: 1,
    new_line: 1,
    content: "context",
    line_type: "context",
  };
  let pageCalls = 0;
  const bridge = {
    cancelRead: async () => true,
    openDiff: () =>
      read("open", {
        snapshot_id: "s1",
        resource: "review",
        revision,
        cursor: 0,
        next_cursor: 1000,
        entries: Array(100_000).fill(row),
      }),
    pageDiff: () => {
      pageCalls += 1;
      throw new Error("must stop at the renderer bound");
    },
  };
  const result = await loadAllPages(
    "review",
    "unified",
    bridge,
    new QueryCoordinator(bridge),
  );
  assert.equal(result.rows.length, 100_000);
  assert.equal(result.partialError.code, "pagination_limit");
  assert.equal(pageCalls, 0);
});

function read(requestToken, value) {
  return { requestToken, result: Promise.resolve(value) };
}

// The truncated file list banner. A GitLab review whose changes_count is
// "1000+" reaches the shell with changes_overflow set on its detail.

test("a review whose forge capped the file list shows the truncation banner", async () => {
  const detail = trackedDetail({ changes_overflow: true, changes_count: 1000 });
  const view = renderOverflowDiff("overflowed", detail);

  await waitFor(() =>
    assert.deepEqual(warningTexts(view), [CHANGES_OVERFLOW_NOTICE]),
  );
  assert.equal(detail.calls(), 1);
  assert.equal(
    CHANGES_OVERFLOW_NOTICE,
    "The forge returned only part of the changed files, so this list is incomplete. Use Open on forge to see every file.",
  );
  // The banner sits beside the files rather than in place of them.
  assert.equal(view.container.querySelectorAll(".file-path").length > 0, true);
});

test("a complete file list shows no truncation banner", async () => {
  for (const overrides of [{ changes_overflow: false }, {}]) {
    const detail = trackedDetail(overrides);
    const view = renderOverflowDiff("complete", detail);

    await waitFor(() =>
      assert.equal(view.container.querySelectorAll(".file-path").length > 0, true),
    );
    await detail.settled();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.deepEqual(warningTexts(view), []);
    assert.equal(
      view.container.textContent.includes("only part of the changed files"),
      false,
    );
    cleanup();
  }
});

function warningTexts(view) {
  return [...view.container.querySelectorAll(".notice-warning")].map(
    (node) => node.textContent,
  );
}

function trackedDetail(overrides) {
  let calls = 0;
  let last = Promise.resolve();
  return {
    calls: () => calls,
    settled: () => last,
    getReview: (review) => {
      calls += 1;
      const result = Promise.resolve({
        handle: review,
        repository: "repo",
        detail: { ...overflowSummary(), ...overrides },
        capabilities: {
          batched_review: true,
          thread_resolution: true,
          draft_notes: true,
          unapprove: true,
          job_cancel: true,
        },
        revision,
        revision_error: null,
      });
      last = result;
      return { requestToken: crypto.randomUUID(), result };
    },
  };
}

function renderOverflowDiff(review, detail) {
  const bridge = {
    cancelRead: async () => true,
    openExternal: async () => true,
    getReview: detail.getReview,
    openDiff: () => settledRead(overflowDiffPage(review)),
    pageDiff: () => {
      throw new Error("the fixture diff has one page");
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
  const feature = createDiffFeature();
  const route = {
    kind: "review",
    item: { handle: review, repository: "repo", summary: overflowSummary() },
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

function settledRead(value) {
  return { requestToken: crypto.randomUUID(), result: Promise.resolve(value) };
}

function overflowSummary() {
  return {
    number: 42,
    title: "Vendor a large dependency",
    author: { username: "alice", display_name: "Alice Dev" },
    state: "open",
    is_draft: false,
    source_branch: "feature/vendor",
    target_branch: "main",
    ci_status: "success",
    created_at: "2026-06-01T09:00:00Z",
    updated_at: "2026-06-02T14:30:00Z",
    web_url: "https://gitlab.example.com/acme/widgets/-/merge_requests/42",
    comment_count: 0,
    has_conflicts: false,
    labels: [],
    review_decision: null,
    additions: null,
    deletions: null,
    description: "",
    merge_status: "can_be_merged",
    approvals: [],
    reviewers: [],
    assignees: [],
    changes_count: 3,
    detailed_merge_status: "mergeable",
    draft_notes_count: null,
    status_check_rollup: null,
  };
}

function overflowDiffPage(review) {
  return {
    snapshot_id: "snapshot-overflow",
    resource: review,
    revision,
    cursor: 0,
    next_cursor: null,
    entries: [
      {
        kind: "file",
        file_index: 0,
        old_path: "vendor/file_0.py",
        new_path: "vendor/file_0.py",
        status: "added",
        additions: 1,
        deletions: 0,
        is_binary: false,
        language: "python",
        is_truncated: false,
        is_empty: false,
        is_mode_only: false,
        is_rename_only: false,
        is_unavailable: false,
      },
      {
        kind: "hunk",
        file_index: 0,
        hunk_index: 0,
        header: "@@ -0,0 +1 @@",
        old_start: 0,
        old_count: 0,
        new_start: 1,
        new_count: 1,
        context_text: "",
      },
      {
        kind: "line",
        file_index: 0,
        hunk_index: 0,
        old_line: null,
        new_line: 1,
        content: "print('hello')",
        line_type: "addition",
      },
    ],
  };
}

test("an old-side discussion on a renamed file resolves to its deleted line", () => {
  const loaded = {
    layout: "unified",
    snapshotId: "snapshot-renamed",
    resource: "renamed",
    revision,
    partialError: null,
    rows: [
      {
        kind: "file",
        file_index: 0,
        old_path: "src/old_calc.py",
        new_path: "src/calc.py",
        status: "renamed",
        additions: 0,
        deletions: 1,
        is_binary: false,
        language: "python",
        is_truncated: false,
        is_empty: false,
        is_mode_only: false,
        is_rename_only: false,
        is_unavailable: false,
      },
      {
        kind: "hunk",
        file_index: 0,
        hunk_index: 0,
        header: "@@ -11 +10,0 @@",
        old_start: 11,
        old_count: 1,
        new_start: 10,
        new_count: 0,
        context_text: "",
      },
      {
        kind: "line",
        file_index: 0,
        hunk_index: 0,
        old_line: 11,
        new_line: null,
        content: "    return a / b",
        line_type: "deletion",
      },
    ],
  };
  // Both forges store the new path on an old-side comment; the old path is
  // what a comment written against the original file would carry.
  for (const path of ["src/calc.py", "src/old_calc.py"]) {
    const selection = resolveDiscussionTarget("renamed", loaded, {
      discussionId: "d-left",
      path,
      side: "old",
      line: 11,
    });
    assert.notEqual(selection, null, path);
    assert.deepEqual(
      [selection.side, selection.oldLine, selection.oldPath, selection.newPath],
      ["old", 11, "src/old_calc.py", "src/calc.py"],
    );
  }
  assert.equal(
    resolveDiscussionTarget("renamed", loaded, {
      discussionId: "d-elsewhere",
      path: "src/other.py",
      side: "old",
      line: 11,
    }),
    null,
  );
});

// #305: a layout switch is a view of the diff already read, and the diff is
// drawn from its first page while the rest is still arriving.

function fileRow(index, path) {
  return {
    kind: "file",
    file_index: index,
    old_path: path,
    new_path: path,
    status: "modified",
    additions: 1,
    deletions: 0,
    is_binary: false,
    language: "python",
    is_truncated: false,
    is_empty: false,
    is_mode_only: false,
    is_rename_only: false,
    is_unavailable: false,
  };
}

function hunkRow(fileIndex) {
  return {
    kind: "hunk",
    file_index: fileIndex,
    hunk_index: 0,
    header: "@@ -0,0 +1 @@",
    old_start: 0,
    old_count: 0,
    new_start: 1,
    new_count: 1,
    context_text: "",
  };
}

function sourceRow(fileIndex, layout, content) {
  const line = { old_line: null, new_line: 1, content, line_type: "addition" };
  return layout === "split"
    ? {
        kind: "split",
        file_index: fileIndex,
        hunk_index: 0,
        row_index: 0,
        old: null,
        new: { ...line, anchor_side: "new" },
      }
    : { kind: "line", file_index: fileIndex, hunk_index: 0, ...line };
}

function fileRows(fileIndex, layout) {
  const path = `src/file_${fileIndex}.py`;
  return [
    fileRow(fileIndex, path),
    hunkRow(fileIndex),
    sourceRow(fileIndex, layout, `value_${fileIndex} = ${fileIndex}`),
  ];
}

function diffBridge(review, { openDiff, pageDiff }) {
  return {
    cancelRead: async () => true,
    openExternal: async () => true,
    getReview: trackedDetail({}).getReview,
    openDiff,
    pageDiff,
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
}

function renderDiff(review, bridge) {
  const feature = createDiffFeature();
  const route = {
    kind: "review",
    item: { handle: review, repository: "repo", summary: overflowSummary() },
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

function filePaths(view) {
  return [
    ...view.container.querySelectorAll(
      ".file-list .file-item:not(.hunk-item) > span:first-child",
    ),
  ].map((node) => node.textContent);
}

function lineTexts(view, selector) {
  return [...view.container.querySelectorAll(selector)].map(
    (node) => node.textContent,
  );
}

function loadingTexts(view) {
  return lineTexts(view, ".notice-loading");
}

test("diff reconstruction reports the pages read so far before the next page", async () => {
  const order = [];
  const bridge = {
    cancelRead: async () => true,
    openDiff: (params) => {
      order.push(`open:${params.from_snapshot ?? "fresh"}`);
      return read("open", {
        snapshot_id: "s2",
        resource: "review",
        revision,
        cursor: 0,
        next_cursor: 3,
        entries: fileRows(0, "split"),
      });
    },
    pageDiff: () => {
      order.push("page");
      return read("page", {
        snapshot_id: "s2",
        resource: "review",
        revision,
        cursor: 3,
        next_cursor: null,
        entries: fileRows(1, "split"),
      });
    },
  };
  const result = await loadAllPages(
    "review",
    "split",
    bridge,
    new QueryCoordinator(bridge),
    {
      fromSnapshot: "s1",
      onProgress: (loaded) =>
        order.push(`progress:${loaded.rows.length}:${loaded.loadingMore}`),
    },
  );
  assert.deepEqual(order, ["open:s1", "progress:3:true", "page"]);
  assert.equal(result.rows.length, 6);
  assert.equal(result.loadingMore, false);
  assert.equal(result.snapshotId, "s2");
});

test("switching the layout re-projects the open snapshot and Refresh reads afresh", async () => {
  const review = "review-305";
  const opens = [];
  let snapshots = 0;
  const bridge = diffBridge(review, {
    openDiff: (params) => {
      opens.push({ ...params });
      snapshots += 1;
      return settledRead({
        snapshot_id: `snapshot-${snapshots}`,
        resource: review,
        revision,
        cursor: 0,
        next_cursor: null,
        entries: fileRows(0, params.layout),
      });
    },
    pageDiff: () => {
      throw new Error("the fixture diff has one page");
    },
  });
  const view = renderDiff(review, bridge);
  await waitFor(() =>
    assert.deepEqual(lineTexts(view, ".diff-unified .line-content"), [
      "+value_0 = 0",
    ]),
  );

  fireEvent.click(view.getByText("Split"));
  await waitFor(() =>
    assert.equal(
      view.container.querySelectorAll(".diff-split .split-cell .line-content")
        .length > 0,
      true,
    ),
  );
  fireEvent.click(view.getByText("Unified"));
  await waitFor(() => assert.equal(opens.length, 3));
  await waitFor(() =>
    assert.deepEqual(lineTexts(view, ".diff-unified .line-content"), [
      "+value_0 = 0",
    ]),
  );
  fireEvent.click(view.getByText("Refresh"));
  await waitFor(() => assert.equal(opens.length, 4));

  // The diff view always asks for gap rows (#383).
  assert.deepEqual(opens, [
    { review, layout: "unified", max_items: 1000, gaps: true },
    { review, layout: "split", max_items: 1000, from_snapshot: "snapshot-1", gaps: true },
    { review, layout: "unified", max_items: 1000, from_snapshot: "snapshot-2", gaps: true },
    { review, layout: "unified", max_items: 1000, gaps: true },
  ]);
  // Only the first open and the Refresh may read the forge.
  assert.equal(
    opens.filter((params) => params.from_snapshot === undefined).length,
    2,
  );
});

test("the diff is drawn from its first page while the rest is loading", async () => {
  const review = "review-progressive";
  let releasePage = () => {};
  let pages = 0;
  const bridge = diffBridge(review, {
    openDiff: () =>
      settledRead({
        snapshot_id: "snapshot-progressive",
        resource: review,
        revision,
        cursor: 0,
        next_cursor: 3,
        entries: fileRows(0, "unified"),
      }),
    pageDiff: () => {
      pages += 1;
      return {
        requestToken: crypto.randomUUID(),
        result: new Promise((resolve) => {
          releasePage = () =>
            resolve({
              snapshot_id: "snapshot-progressive",
              resource: review,
              revision,
              cursor: 3,
              next_cursor: null,
              entries: fileRows(1, "unified"),
            });
        }),
      };
    },
  });
  const view = renderDiff(review, bridge);

  await waitFor(() => assert.deepEqual(filePaths(view), ["src/file_0.py"]));
  assert.deepEqual(lineTexts(view, ".diff-unified .line-content"), [
    "+value_0 = 0",
  ]);
  assert.deepEqual(loadingTexts(view), ["Loading the rest of the diff…"]);
  await waitFor(() => assert.equal(pages, 1));

  releasePage();
  await waitFor(() =>
    assert.deepEqual(filePaths(view), ["src/file_0.py", "src/file_1.py"]),
  );
  assert.deepEqual(loadingTexts(view), []);
  // The reader stays on the file they were reading when the rest arrives.
  assert.deepEqual(lineTexts(view, ".diff-unified .line-content"), [
    "+value_0 = 0",
  ]);
});

test("a page that fails after the first keeps the drawn rows as a partial diff", async () => {
  const review = "review-partial";
  const bridge = diffBridge(review, {
    openDiff: () =>
      settledRead({
        snapshot_id: "snapshot-partial",
        resource: review,
        revision,
        cursor: 0,
        next_cursor: 3,
        entries: fileRows(0, "unified"),
      }),
    pageDiff: () => ({
      requestToken: crypto.randomUUID(),
      result: Promise.reject({
        code: "snapshot_expired",
        message: "The resource snapshot is invalid or expired; refetch it.",
        retryable: true,
      }),
    }),
  });
  const view = renderDiff(review, bridge);

  await waitFor(() =>
    assert.deepEqual(lineTexts(view, ".notice-error"), [
      "This diff snapshot expired. Reload the diff to continue.",
    ]),
  );
  assert.deepEqual(filePaths(view), ["src/file_0.py"]);
  assert.deepEqual(loadingTexts(view), []);
});

test("a layout switch that fails while the old layout pages freezes it as a partial diff", async () => {
  const review = "review-switch-partial";
  const opens = [];
  let pages = 0;
  const bridge = diffBridge(review, {
    openDiff: (params) => {
      opens.push(params.from_snapshot ?? "fresh");
      if (params.from_snapshot !== undefined)
        return {
          requestToken: crypto.randomUUID(),
          result: Promise.reject({
            code: "snapshot_expired",
            message: "The resource snapshot is invalid or expired; refetch it.",
            retryable: true,
          }),
        };
      return settledRead({
        snapshot_id: "snapshot-switch",
        resource: review,
        revision,
        cursor: 0,
        next_cursor: 3,
        entries: fileRows(0, "unified"),
      });
    },
    pageDiff: () => {
      pages += 1;
      // The rest of the unified diff never arrives before the switch.
      return { requestToken: crypto.randomUUID(), result: new Promise(() => {}) };
    },
  });
  const view = renderDiff(review, bridge);
  await waitFor(() =>
    assert.deepEqual(loadingTexts(view), ["Loading the rest of the diff…"]),
  );
  await waitFor(() => assert.equal(pages, 1));

  fireEvent.click(view.getByText("Split"));
  // The failure names why the rest is missing; a snapshot still marked as
  // loading would read as an older one instead ("Showing the previous diff
  // snapshot.") and keep waiting for pages that never come.
  await waitFor(() =>
    assert.deepEqual(lineTexts(view, ".notice-error"), [
      "This diff snapshot expired. Reload the diff to continue.",
    ]),
  );
  assert.deepEqual(opens, ["fresh", "snapshot-switch"]);
  // The drawn page stays as a finished partial diff, not one still loading.
  assert.deepEqual(loadingTexts(view), []);
  assert.deepEqual(filePaths(view), ["src/file_0.py"]);
  assert.deepEqual(lineTexts(view, ".diff-unified .line-content"), [
    "+value_0 = 0",
  ]);
  assert.equal(view.getByText("Refresh").hasAttribute("disabled"), false);
});

test("the no-newline marker row cannot be selected or commented on", async () => {
  const review = "review-no-newline";
  const bridge = diffBridge(review, {
    openDiff: () =>
      settledRead({
        snapshot_id: "snapshot-no-newline",
        resource: review,
        revision,
        cursor: 0,
        next_cursor: null,
        entries: [
          ...fileRows(0, "unified"),
          {
            kind: "line",
            file_index: 0,
            hunk_index: 0,
            old_line: null,
            new_line: null,
            content: "No newline at end of file",
            line_type: "no_newline",
          },
        ],
      }),
    pageDiff: () => {
      throw new Error("the fixture diff has one page");
    },
  });
  const view = renderDiff(review, bridge);
  const MARKER = ".diff-unified .diff-line.line-no_newline";
  const SOURCE = ".diff-unified .diff-line.line-addition";
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(MARKER).length, 1),
  );
  const attributes = (row) => {
    const code = view.container.querySelector(`${row} .line-content`);
    return JSON.stringify({
      className: code.className,
      role: code.getAttribute("role"),
      tabindex: code.getAttribute("tabindex"),
      label: code.getAttribute("aria-label"),
      buttons: view.container.querySelectorAll(`${row} button`).length,
    });
  };

  // The source line beside it is selectable and commentable, so the marker's
  // missing affordances are its own and not the fixture's.
  assert.equal(
    attributes(SOURCE),
    JSON.stringify({
      className: "line-content selectable-line",
      role: "button",
      tabindex: "0",
      label: "Select new line 1",
      buttons: 2,
    }),
  );
  assert.equal(
    attributes(MARKER),
    JSON.stringify({
      className: "line-content",
      role: null,
      tabindex: null,
      label: null,
      buttons: 0,
    }),
  );

  const marker = view.container.querySelector(`${MARKER} .line-content`);
  fireEvent.click(marker);
  fireEvent.keyDown(marker, { key: "c" });
  fireEvent.keyDown(marker, { key: "Enter" });
  assert.equal(view.container.querySelectorAll(".line-selected").length, 0);
  assert.equal(view.container.querySelectorAll(".inline-composer").length, 0);
  assert.equal(
    view.container.querySelectorAll(".line-number[aria-label]").length,
    1,
  );
});
