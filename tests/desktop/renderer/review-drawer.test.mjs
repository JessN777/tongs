import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";

import { InlineAnchorStore } from "../../../desktop/dist/src/renderer/core/navigation.js";
import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import { createDiffFeature } from "../../../desktop/dist/src/renderer/features/diff/index.js";
import {
  ComposerRefusal,
  reviewMutationError,
} from "../../../desktop/dist/src/renderer/features/review/composer.js";
import {
  clearPendingEdit,
  draftContentTranscript,
  drawerFailureMessage,
} from "../../../desktop/dist/src/renderer/features/review/drawer.js";
import { ReviewWorkflowRefusal } from "../../../desktop/dist/src/renderer/features/review/state.js";
import {
  capabilitiesReady,
  waitForCount,
  waitForFlag,
  waitForList,
  waitForText,
} from "./wait.mjs";

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
const { cleanup, fireEvent, render } = desktopRequire(
  "@testing-library/react",
);
const React = desktopRequire("react");
afterEach(() => {
  cleanup();
  clearPendingEdit();
});

const REVISION = {
  head_sha: "f8bbf4877cd1a58f2a3a9d1a1a86bd2f4b7a1c33",
  base_sha: "df2bd3f",
  start_sha: null,
};
const DRAFT_ID = "11111111-1111-4111-8111-111111111111";

/** The drawer's verdict tiles, which render only once the capability read
 * behind them has answered. */
const VERDICT_TILES = ".review-drawer-verdict-tiles input";

/**
 * S54, end to end, on one review: start a review from the diff, add a second
 * entry as a suggestion, write the review's general comment as the drawer
 * Summary, choose the Comment verdict, and submit. Everything the store was
 * asked to hold is asserted as one string so the whole payload is the
 * assertion rather than a field of it.
 */
test("S54 runs from the in-diff composer through the drawer to a submitted comment verdict", async () => {
  const review = "review-drawer-s54";
  const saves = [];
  const submissions = [];
  const bridge = diffBridge(review, {
    saveReviewDraft: async (params) => {
      saves.push(params);
      return draft(review, params.expected_version + 1, params.content);
    },
    startReviewSubmission: async (params) => {
      submissions.push(params);
      return submission(review, "submitted", params.expected_version);
    },
  });
  const view = renderDiff(bridge, review, "gitlab");

  // Start a review from the line the reader clicked.
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  fireEvent.change(await view.findByLabelText("Inline review comment"), {
    target: { value: "Guard the zero divisor" },
  });
  fireEvent.click(view.getByRole("button", { name: "Start a review" }));
  await waitForCount(() => saves.length, 1);

  // A suggestion on the same line enters the same pending review.
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  const composer = await view.findByLabelText("Inline review comment");
  fireEvent.change(composer, { target: { value: "Raise the builtin" } });
  fireEvent.click(view.getByRole("button", { name: "Insert suggestion" }));
  await waitForFlag(
    () => view.getByLabelText("Inline review comment").value.includes("```suggestion"),
    true,
  );
  fireEvent.click(view.getByRole("button", { name: "Add to review" }));
  await waitForCount(() => saves.length, 2);

  // The review's general comment and its verdict are the drawer's.
  fireEvent.click(view.getByRole("button", { name: /Your review/ }));
  fireEvent.change(await view.findByLabelText("Summary"), {
    target: { value: "Two small changes, otherwise good." },
  });
  await capabilitiesReady(view.container, VERDICT_TILES);
  fireEvent.click(view.getByRole("radio", { name: "Comment" }));
  fireEvent.click(view.getByRole("button", { name: "Submit review" }));
  fireEvent.click(
    await view.findByRole("button", { name: "Confirm submit review" }),
  );
  await waitForCount(() => submissions.length, 1);

  // Submit saves the summary and the verdict first, so the submitted version
  // is the one the store confirmed rather than the one on screen.
  assert.equal(saves.length, 3);
  assert.equal(
    transcript(saves.at(-1).content),
    [
      "body=Two small changes, otherwise good.",
      "verdict=comment",
      "inline src/calc.py new:11 stale=false | Guard the zero divisor",
      "inline src/calc.py new:11 stale=false | Raise the builtin",
      "",
      "```suggestion:-0+0",
      "    if b == 0:",
      "```",
    ].join("\n"),
  );
  assert.equal(saves.at(-1).expected_version, 3);
  assert.equal(submissions[0].draft_id, DRAFT_ID);
  assert.equal(submissions[0].expected_version, 4);
  await view.findByText("Review submitted.");
});

/** S45: the identity, the captured revision and the version, in the UI. */
test("S45 the drawer footer names the draft, its revision and its version", async () => {
  const review = "review-drawer-footer";
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [draft(review, 7, content({ body: "Held" }))],
        }),
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  await view.findByText(`Draft ${DRAFT_ID}`);
  assert.ok(view.getByText("Revision f8bbf48"));
  assert.ok(view.getByText("Version 7"));
  assert.equal(
    view.container.querySelector(".review-drawer-footer [data-head-sha]")
      .getAttribute("data-head-sha"),
    REVISION.head_sha,
  );
});

/**
 * The drawer lists the whole review, including entries the diff never places:
 * a file outside the loaded diff, and a review-level general comment.
 */
test("the drawer groups every pending entry by file with its own count", async () => {
  const review = "review-drawer-groups";
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(
              review,
              2,
              content({
                comments: [
                  inlineEntry("a", "First", 11, "src/calc.py"),
                  inlineEntry("b", "Second", 12, "src/calc.py"),
                  inlineEntry("c", "Elsewhere", 3, "docs/guide.md"),
                  { id: "d", kind: "general", body: "Thanks for splitting this out." },
                ],
              }),
            ),
          ],
        }),
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  await view.findByLabelText("Pending comments");
  assert.deepEqual(
    [...view.container.querySelectorAll(".review-drawer-file-name")].map(
      (element) => element.textContent,
    ),
    ["src/calc.py2 pending", "docs/guide.md1 pending", "Review-level1 pending"],
  );
  assert.equal(
    view.container.querySelectorAll(".review-drawer-entry").length,
    4,
  );
  assert.equal(view.getByRole("button", { name: /Your review/ }).getAttribute("aria-label"), "Your review, 4 pending");

  // The file outside the loaded diff is listed and readable, and offers no
  // jump, because there is no loaded row to jump to.
  assert.ok(view.getByText("Elsewhere"));
  assert.ok(
    view.getByRole("button", {
      name: "Delete pending comment on docs/guide.md, new line 3",
    }),
  );
});

test("the summary and the verdict are saved on the draft's own version", async () => {
  const review = "review-drawer-save";
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 4, content())] }),
      saveReviewDraft: async (params) => {
        saves.push(params);
        return draft(review, params.expected_version + 1, params.content);
      },
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  fireEvent.change(await view.findByLabelText("Summary"), {
    target: { value: "Looks right to me." },
  });
  await capabilitiesReady(view.container, VERDICT_TILES);
  fireEvent.click(view.getByRole("radio", { name: "Approve" }));
  fireEvent.click(
    view.getByRole("button", { name: "Save summary and verdict" }),
  );
  await waitForCount(() => saves.length, 1);
  assert.equal(saves[0].expected_version, 4);
  assert.equal(saves[0].draft_id, DRAFT_ID);
  assert.equal(saves[0].content.body, "Looks right to me.");
  assert.equal(saves[0].content.verdict, "approve");
  // The version the store confirmed is the one the footer now names.
  await view.findByText("Version 5");
  assert.equal(view.getByRole("radio", { name: "Approve" }).checked, true);
});

test("a verdict the forge or the permission withholds has no tile at all", async () => {
  const review = "review-drawer-verdict-caps";
  const view = renderDiff(
    diffBridge(review, {
      getReviewMutationCapabilities: () =>
        read({
          review,
          capabilities: capabilities({ approve: false, request_changes: false }),
        }),
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 1, content())] }),
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  await view.findByLabelText("Summary");
  await capabilitiesReady(view.container, VERDICT_TILES);
  assert.deepEqual(
    view.getAllByRole("radio").map((element) => element.value),
    ["comment"],
  );
  // Nothing selected is the comment outcome, and it says so rather than
  // leaving it to be discovered on submit.
  await view.findByText(
    "No verdict selected. Submitting posts this review as a comment.",
  );
});

test("a forge that records no verdict says what submitting will do instead", async () => {
  const review = "review-drawer-no-verdict";
  const view = renderDiff(
    diffBridge(review, {
      getReviewMutationCapabilities: () =>
        read({
          review,
          capabilities: capabilities({
            approve: false,
            request_changes: false,
            comment_verdict: false,
          }),
        }),
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 1, content())] }),
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  await view.findByText(
    "This review records no verdict on this forge or with this permission. Submitting posts the summary and the pending comments without one.",
  );
  assert.equal(view.container.querySelectorAll('input[type="radio"]').length, 0);
});

test("the drawer shows the engine's step list with each step's own standing", async () => {
  const review = "review-drawer-steps";
  const progress = {
    ...submission(review, "paused", 2),
    steps: [
      { id: "s1", kind: "inline_comment", comment_ids: ["a"] },
      { id: "s2", kind: "body", comment_ids: [] },
      { id: "s3", kind: "verdict", comment_ids: [] },
    ],
    completed_step_ids: ["s1"],
    unknown_step_ids: ["s2"],
  };
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 2, content())] }),
      startReviewSubmission: async () => progress,
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  fireEvent.click(await view.findByRole("button", { name: "Submit review" }));
  fireEvent.click(
    await view.findByRole("button", { name: "Confirm submit review" }),
  );
  await view.findByLabelText("Submission steps");
  assert.deepEqual(
    [...view.container.querySelectorAll(".review-drawer-steps li")].map(
      (element) => element.textContent,
    ),
    [
      "Inline comment: confirmed",
      "Review body: unknown",
      "Verdict: not started",
    ],
  );
  assert.ok(view.getByRole("button", { name: "Resume confirmed attempt" }));
});

test("a paused submission returns to editing beside Resume without sending more", async () => {
  const review = "review-drawer-paused-return";
  const kept = content({ body: "Kept summary", verdict: "approve" });
  const paused = {
    ...submission(review, "paused", 2),
    state: "submitting",
    failure: {
      code: "invalid_input",
      message: "The forge rejected this request as invalid.",
      retryable: false,
      step_id: "verdict",
    },
  };
  const reconciles = [];
  let resumes = 0;
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 2, kept)] }),
      startReviewSubmission: async () => paused,
      resumeReviewSubmission: async () => {
        resumes += 1;
        return submission(review, "submitted", 2);
      },
      reconcileReviewSubmission: async (params) => {
        reconciles.push(params);
        return { ...submission(review, "editable", 2), state: "editable" };
      },
      getReviewDraft: () => read(draft(review, 4, kept)),
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  fireEvent.click(await view.findByRole("button", { name: "Submit review" }));
  fireEvent.click(
    await view.findByRole("button", { name: "Confirm submit review" }),
  );
  await view.findByText("The forge rejected this request as invalid.");

  const resume = view.queryAllByRole("button", {
    name: "Resume confirmed attempt",
  });
  const returning = view.queryAllByRole("button", {
    name: "Return draft to editing",
  });
  assert.equal(resume.length, 1);
  assert.equal(returning.length, 1);
  assert.equal(returning[0].textContent, "Return draft to editing");
  assert.equal(returning[0].disabled, false);
  assert.equal(
    view.queryAllByRole("button", { name: "Mark submitted" }).length,
    0,
  );

  fireEvent.click(returning[0]);
  await waitForCount(
    () => view.queryAllByRole("button", { name: "Return draft to editing" }).length,
    0,
  );
  assert.equal(
    JSON.stringify(reconciles),
    JSON.stringify([
      {
        review,
        attempt_id: "22222222-2222-4222-8222-222222222222",
        resolution: "return_editable",
      },
    ]),
  );
  assert.equal(resumes, 0);
  assert.equal(view.queryAllByLabelText("Submission steps").length, 0);
  assert.equal(view.getByLabelText("Summary").value, "Kept summary");
  assert.equal(
    view.queryAllByRole("button", { name: "Submit review" }).length,
    1,
  );
  // The attempt was returned to editing, so it is no longer offered for
  // recovery.
  assert.equal(
    view.queryAllByRole("button", { name: /Recover paused attempt/ }).length,
    0,
  );
});

test("a listed paused attempt returned to editing leaves the recovery list", async () => {
  const review = "review-drawer-listed-paused-return";
  const kept = content({ body: "Kept summary", verdict: "approve" });
  const paused = {
    ...submission(review, "paused", 2),
    state: "submitting",
    failure: {
      code: "invalid_input",
      message: "The forge rejected this request as invalid.",
      retryable: false,
      step_id: "verdict",
    },
  };
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 2, kept)] }),
      listReviewSubmissions: () =>
        read({ cursor: 0, next_cursor: null, attempts: [paused] }),
      reconcileReviewSubmission: async () => ({
        ...submission(review, "editable", 2),
        state: "editable",
      }),
      getReviewDraft: () => read(draft(review, 4, kept)),
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  fireEvent.click(
    await view.findByRole("button", { name: /Recover paused attempt/ }),
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Return draft to editing" }),
  );
  await waitForCount(
    () => view.queryAllByRole("button", { name: "Return draft to editing" }).length,
    0,
  );
  assert.equal(view.queryAllByLabelText("Submission steps").length, 0);
  assert.equal(
    view.queryAllByRole("button", { name: /Recover paused attempt/ }).length,
    0,
  );
  assert.equal(
    view.queryAllByText("Durable submission recovery").length,
    0,
  );
});

test("a refused start adopts the stored draft version before the next save", async () => {
  const review = "review-drawer-start-refused";
  const kept = content({ body: "Kept summary", verdict: "approve" });
  const reads = [];
  const saves = [];
  let starts = 0;
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 2, kept)] }),
      startReviewSubmission: async () => {
        starts += 1;
        throw {
          code: "revision_changed",
          message: "The draft revision changed. Refresh its anchors before submitting.",
          retryable: true,
        };
      },
      // The refused start locked version 2 and released it, so the store now
      // holds version 4 of the same draft.
      getReviewDraft: (params) => {
        reads.push(params.draft_id);
        return read(draft(review, 4, kept));
      },
      saveReviewDraft: async (params) => {
        saves.push(params);
        return draft(review, params.expected_version + 1, params.content);
      },
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  await view.findByText("Version 2");
  fireEvent.click(await view.findByRole("button", { name: "Submit review" }));
  fireEvent.click(
    await view.findByRole("button", { name: "Confirm submit review" }),
  );
  await view.findByText("Version 4");
  await view.findByText(/The review revision changed/);

  assert.equal(starts, 1);
  assert.equal(reads.at(-1), DRAFT_ID);
  assert.equal(view.getByLabelText("Summary").value, "Kept summary");
  assert.equal(view.queryAllByText("Version 2").length, 0);

  fireEvent.change(view.getByLabelText("Summary"), {
    target: { value: "Kept summary, revised" },
  });
  fireEvent.click(
    view.getByRole("button", { name: "Save summary and verdict" }),
  );
  await waitForCount(() => saves.length, 1);
  assert.equal(saves[0].expected_version, 4);
  assert.equal(saves[0].content.body, "Kept summary, revised");
  await view.findByText("Version 5");
});

test("a zero-step interrupted attempt only offers returning to editing", async () => {
  const review = "review-drawer-zero-step-recovery";
  const attempt = {
    ...submission(review, "unknown", 3),
    unknown_step_ids: [],
  };
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 3, content())] }),
      listReviewSubmissions: () =>
        read({ cursor: 0, next_cursor: null, attempts: [attempt] }),
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  fireEvent.click(
    await view.findByRole("button", {
      name: "Recover unknown attempt with 0 confirmed step(s)",
    }),
  );
  await view.findByText(/interrupted before anything was sent/);
  assert.ok(view.getByRole("button", { name: "Return draft to editing" }));
  assert.equal(
    view.queryAllByRole("button", { name: "Retry only remaining steps" }).length,
    0,
  );
  assert.equal(
    view.queryAllByRole("button", { name: "Mark submitted" }).length,
    0,
  );
});

test("a partial interrupted attempt can retry remaining steps without repeat warning", async () => {
  const review = "review-drawer-partial-zero-unknown";
  const attempt = {
    ...submission(review, "unknown", 3),
    completed_step_ids: ["verdict"],
    unknown_step_ids: [],
  };
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 3, content())] }),
      listReviewSubmissions: () =>
        read({ cursor: 0, next_cursor: null, attempts: [attempt] }),
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  fireEvent.click(
    await view.findByRole("button", {
      name: "Recover unknown attempt with 1 confirmed step(s)",
    }),
  );

  await view.findByText(/interrupted after 1 confirmed step/);
  assert.ok(view.getByRole("button", { name: "Retry only remaining steps" }));
  assert.ok(view.getByRole("button", { name: "Return draft to editing" }));
  assert.equal(
    view.queryAllByText(/repeat an unconfirmed remote write/).length,
    0,
  );
  assert.equal(
    view.queryAllByRole("button", { name: "Mark submitted" }).length,
    0,
  );
});


test("a durable attempt is recovered inside the drawer without replaying start", async () => {
  const review = "review-drawer-recovery";
  let starts = 0;
  const attempt = submission(review, "unknown", 3);
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 3, content())] }),
      listReviewSubmissions: () =>
        read({ cursor: 0, next_cursor: null, attempts: [attempt] }),
      startReviewSubmission: async () => {
        starts += 1;
        return attempt;
      },
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  fireEvent.click(
    await view.findByRole("button", {
      name: "Recover unknown attempt with 0 confirmed step(s)",
    }),
  );
  await view.findByText(/Remote status is unknown/);
  assert.equal(starts, 0);
  assert.ok(view.getByRole("button", { name: "Retry only remaining steps" }));
  assert.ok(view.getByRole("button", { name: "Return draft to editing" }));
});

test("a conflict raised by a drawer save is resolved in the drawer", async () => {
  const review = "review-drawer-conflict";
  const saves = [];
  const stored = draft(review, 5, content({ body: "Written elsewhere" }));
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 3, content())] }),
      getReviewDraft: () => read(stored),
      saveReviewDraft: async (params) => {
        saves.push(params);
        if (params.expected_version === 3)
          throw {
            code: "conflict",
            message: "The stored draft moved on.",
            retryable: false,
          };
        return draft(review, params.expected_version + 1, params.content);
      },
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  fireEvent.change(await view.findByLabelText("Summary"), {
    target: { value: "My text" },
  });
  fireEvent.click(
    view.getByRole("button", { name: "Save summary and verdict" }),
  );
  await view.findByText(/The stored draft is now version 5/);
  assert.equal(
    view.getByLabelText("My unsaved draft text").value.includes("My text"),
    true,
  );
  assert.equal(
    view.getByLabelText("Stored draft text").value.includes("Written elsewhere"),
    true,
  );

  // Keeping my text re-saves it over the stored version rather than asserting
  // a version that a stale holder could never satisfy.
  fireEvent.click(
    view.getByRole("button", {
      name: "Keep my text and save over version 5",
    }),
  );
  await waitForCount(() => saves.length, 2);
  assert.equal(saves[1].expected_version, 5);
  assert.equal(saves[1].content.body, "My text");
  await view.findByText("Version 6");
  assert.equal(
    view.container.querySelectorAll(".review-workflow-conflict").length,
    0,
  );
});

test("Escape closes the drawer and hands focus back to the button", async () => {
  const review = "review-drawer-escape";
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 1, content())] }),
    }),
    review,
  );
  const toggle = await view.findByRole("button", { name: /Your review/ });
  fireEvent.click(toggle);
  await view.findByRole("dialog", { name: "Your review" });
  assert.equal(toggle.getAttribute("aria-expanded"), "true");
  fireEvent.keyDown(view.getByRole("dialog", { name: "Your review" }), {
    key: "Escape",
  });
  await waitForCount(
    () => view.container.querySelectorAll(".review-drawer").length,
    0,
  );
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  assert.equal(
    document.activeElement.getAttribute("aria-label"),
    "Your review, 0 pending",
  );
});

test("Edit in the drawer opens the in-diff composer in its edit shape", async () => {
  const review = "review-drawer-edit";
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(
              review,
              2,
              content({
                comments: [inlineEntry("a", "Guard this", 11, "src/calc.py")],
              }),
            ),
          ],
        }),
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  fireEvent.click(
    await view.findByRole("button", {
      name: "Edit pending comment on src/calc.py, new line 11",
    }),
  );
  // The drawer closes and the composer opens on the entry's own row, in the
  // edit shape rather than as a new comment on the same line.
  await view.findByLabelText("Edit pending comment composer");
  assert.equal(view.container.querySelectorAll(".review-drawer").length, 0);
  assert.equal(view.getByLabelText("Pending review comment").value, "Guard this");
  assert.ok(view.getByText("Editing pending comment"));
  assert.equal(
    view.container.querySelectorAll(".inline-composer").length,
    1,
  );
});

test("Delete in the drawer removes the entry from the store and from the diff", async () => {
  const review = "review-drawer-delete";
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(
              review,
              2,
              content({
                comments: [
                  inlineEntry("a", "Guard this", 11, "src/calc.py"),
                  inlineEntry("b", "And this", 12, "src/calc.py"),
                ],
              }),
            ),
          ],
        }),
      saveReviewDraft: async (params) => {
        saves.push(params);
        return draft(review, params.expected_version + 1, params.content);
      },
    }),
    review,
  );
  // The in-diff card and the drawer read one review, so what one removes the
  // other stops showing without a reload.
  await view.findByLabelText("Pending review comment on new line 11");
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  fireEvent.click(
    await view.findByRole("button", {
      name: "Delete pending comment on src/calc.py, new line 11",
    }),
  );
  await waitForCount(() => saves.length, 1);
  assert.equal(saves[0].expected_version, 2);
  assert.equal(
    transcript(saves[0].content),
    ["body=", "verdict=none", "inline src/calc.py new:12 stale=false | And this"].join(
      "\n",
    ),
  );
  await waitForCount(
    () => view.container.querySelectorAll(".review-drawer-entry").length,
    1,
  );
  assert.equal(
    view.container.querySelectorAll(
      '.pending-card[aria-label="Pending review comment on new line 11"]',
    ).length,
    0,
  );
});

/**
 * The #190 carry over, F3 and F7: the diff places a stale card on whatever
 * current row now happens to carry the stored line number, and it renders no
 * card at all for an entry whose row is not loaded. The drawer is the listing
 * that neither implies a row nor omits an entry, so it lists the stale one by
 * its stored line and offers no jump to a row it cannot vouch for.
 *
 * S46 also asks that the old draft stay usable, so the body is editable here.
 * Editing it must not move the anchor: the whole point of a stale entry is
 * that nothing is silently retargeted.
 */
test("a stale entry is listed by its stored line, editable, with no jump", async () => {
  const review = "review-drawer-stale";
  const stale = inlineEntry("a", "Was on the old line", 11, "src/calc.py");
  const staleAnchor = { ...stale.anchor, stale: true };
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(
              review,
              3,
              content({
                comments: [
                  { ...stale, anchor: staleAnchor },
                  inlineEntry("b", "Still current", 12, "src/calc.py"),
                ],
              }),
            ),
          ],
        }),
      saveReviewDraft: async (params) => {
        saves.push(params);
        return draft(review, params.expected_version + 1, params.content);
      },
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  await view.findByLabelText("Pending comments");
  await view.findByText(
    "Stale, was line 11 at revision f8bbf48. It is listed here by the line it was stored against, and offers no jump, because no row of the current diff is known to be that line. Its text can still be edited here, and its anchor is never retargeted.",
  );

  // The diff still shows its own stale card, so the text is on screen twice:
  // once where the diff guesses a row, once where the drawer states the line.
  assert.equal(view.getAllByText("Was on the old line").length, 2);
  assert.equal(
    view.container.querySelectorAll(".review-drawer-entry-stale").length,
    1,
  );
  // No jump, because no current row is known to be that line.
  assert.equal(
    view.container.querySelectorAll(
      '.review-drawer-entry-stale button[aria-label^="Jump"]',
    ).length,
    0,
  );

  // Edit is offered, and it edits in place rather than opening a composer on
  // a current row that is not the line the entry was stored against.
  fireEvent.click(
    view.getByRole("button", {
      name: "Edit pending comment on src/calc.py, new line 11",
    }),
  );
  const editor = await view.findByLabelText(
    "Edit pending stale comment on src/calc.py, new line 11",
  );
  assert.equal(editor.value, "Was on the old line");
  assert.equal(view.container.querySelectorAll(".inline-composer").length, 0);
  fireEvent.change(editor, { target: { value: "Reworded, same target" } });
  fireEvent.click(
    view.getByRole("button", {
      name: "Save pending comment on src/calc.py, new line 11",
    }),
  );
  await waitForCount(() => saves.length, 1);

  // The body changed and nothing else did. The anchor is compared field by
  // field against the one the store held, so a retarget cannot hide in it.
  const written = saves[0].content.comments[0];
  assert.equal(written.id, "a");
  assert.equal(written.body, "Reworded, same target");
  assert.equal(
    JSON.stringify(written.anchor),
    JSON.stringify({
      revision: staleAnchor.revision,
      old_path: staleAnchor.old_path,
      new_path: staleAnchor.new_path,
      old_line: staleAnchor.old_line,
      new_line: staleAnchor.new_line,
      side: staleAnchor.side,
      context_fingerprint: staleAnchor.context_fingerprint,
      start_line: staleAnchor.start_line,
      start_side: staleAnchor.start_side,
    }),
  );
  assert.equal(saves[0].content.comments[1].body, "Still current");
});

/**
 * F1 from the #205 review: the workflow state refuses a change with a sentence
 * written for the reader, and every one of them used to be reported as a
 * failed read. The refusal the two surfaces make ordinary is a write held in
 * flight on one of them, so the save is started from the diff and the refused
 * press is made in the drawer, which keeps its own controls enabled because
 * its own instance is not the busy one.
 */
test("a drawer write refused by the workflow state shows the refusal, not a read failure", async () => {
  const review = "review-drawer-refusal";
  let release = () => {};
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(
              review,
              2,
              content({
                comments: [
                  inlineEntry("a", "First", 11, "src/calc.py"),
                  inlineEntry("b", "Second", 12, "src/calc.py"),
                ],
              }),
            ),
          ],
        }),
      saveReviewDraft: async (params) => {
        saves.push(params);
        await gate;
        return draft(review, params.expected_version + 1, params.content);
      },
    }),
    review,
  );

  // A diff-side write is put in flight and held there.
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  fireEvent.change(await view.findByLabelText("Inline review comment"), {
    target: { value: "Another note" },
  });
  fireEvent.click(view.getByRole("button", { name: "Add to review" }));
  await waitForCount(() => saves.length, 1);

  // The drawer's own instance is not busy, so its Delete is pressable and the
  // state is what refuses it.
  fireEvent.click(view.getByRole("button", { name: /Your review/ }));
  fireEvent.click(
    await view.findByRole("button", {
      name: "Delete pending comment on src/calc.py, new line 12",
    }),
  );
  await view.findByText("A draft save is already pending");
  assert.equal(saves.length, 1);
  assert.equal(
    view.container.querySelectorAll(".review-drawer .notice-error").length,
    1,
  );
  release();
});

/**
 * The other half of F1: Discard is refused by the same state, and because a
 * disabled control cannot carry a notice its sentence is on the control.
 */
test("a discard refused while a save is in flight names the save on the button", async () => {
  const review = "review-drawer-discard-refusal";
  let release = () => {};
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const discards = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 2, content())] }),
      saveReviewDraft: async (params) => {
        await gate;
        return draft(review, params.expected_version + 1, params.content);
      },
      discardReviewDraft: async (params) => {
        discards.push(params);
        return { discarded: draft(review, 2, content()) };
      },
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  fireEvent.change(await view.findByLabelText("Summary"), {
    target: { value: "Held" },
  });
  fireEvent.click(
    view.getByRole("button", { name: "Save summary and verdict" }),
  );
  const discard = await view.findByRole("button", { name: "Discard review" });
  await waitForFlag(() => discard.disabled, true);
  assert.equal(
    discard.getAttribute("title"),
    "A draft save is in flight. Let it settle before discarding the review.",
  );
  assert.equal(discards.length, 0);
  release();
  await waitForCount(() => view.getAllByText("Version 3").length, 1);
  await waitForFlag(
    () => view.getByRole("button", { name: "Discard review" }).disabled,
    false,
  );
});

/**
 * F2 from the #205 review: a press on an in-diff pending card is refused by
 * the same shared state, and the composer that used to be the only thing
 * rendering that refusal is not open.
 */
test("an in-diff write refused while the drawer saves is explained on the diff", async () => {
  const review = "review-diff-refusal";
  let release = () => {};
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(
              review,
              2,
              content({ comments: [inlineEntry("a", "First", 11, "src/calc.py")] }),
            ),
          ],
        }),
      saveReviewDraft: async (params) => {
        await gate;
        return draft(review, params.expected_version + 1, params.content);
      },
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  fireEvent.change(await view.findByLabelText("Summary"), {
    target: { value: "Held" },
  });
  fireEvent.click(
    view.getByRole("button", { name: "Save summary and verdict" }),
  );
  fireEvent.click(view.getByRole("button", { name: "Close your review" }));

  // No composer is open, so before this fix the press did nothing visible.
  assert.equal(view.container.querySelectorAll(".inline-composer").length, 0);
  fireEvent.click(
    await view.findByRole("button", {
      name: "Delete pending comment on new line 11",
    }),
  );
  await view.findByText("A draft save is already pending");
  assert.equal(
    view.container.querySelectorAll(".diff-view .notice-error").length,
    1,
  );
  fireEvent.click(view.getByRole("button", { name: "Dismiss" }));
  await waitForCount(
    () => view.container.querySelectorAll(".diff-view .notice-error").length,
    0,
  );
  release();
});

/**
 * The half of the "opening the drawer clears nothing" fix that nothing
 * covered: a write refused while the drawer is shut is exactly what the reader
 * opens it to find, so it has to survive the reopen.
 */
test("a failure raised while the drawer is shut is still there on reopen", async () => {
  const review = "review-drawer-closed-failure";
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 2, content())] }),
      saveReviewDraft: async () => {
        throw {
          code: "mutation_failed",
          message: "The mutation failed.",
          retryable: false,
        };
      },
    }),
    review,
  );
  const toggle = await view.findByRole("button", { name: /Your review/ });
  fireEvent.click(toggle);
  fireEvent.change(await view.findByLabelText("Summary"), {
    target: { value: "Doomed" },
  });
  fireEvent.click(
    view.getByRole("button", { name: "Save summary and verdict" }),
  );
  // Shut before the write settles, so the failure lands on a closed drawer.
  fireEvent.click(view.getByRole("button", { name: "Close your review" }));
  await waitForCount(
    () => view.container.querySelectorAll(".review-drawer").length,
    0,
  );
  await waitForText(
    () => toggle.getAttribute("aria-label"),
    "Your review, 0 pending, needs attention",
  );
  assert.equal(toggle.classList.contains("review-drawer-toggle-error"), true);
  fireEvent.click(toggle);
  assert.equal(
    view.container.querySelectorAll(".review-drawer .notice-error").length,
    1,
  );

  // Closing deliberately drops it, so the next open is not greeted by an
  // answer to a question nobody asked.
  fireEvent.click(view.getByRole("button", { name: "Close your review" }));
  fireEvent.click(toggle);
  await view.findByLabelText("Summary");
  assert.equal(
    view.container.querySelectorAll(".review-drawer .notice-error").length,
    0,
  );
  assert.equal(toggle.getAttribute("aria-label"), "Your review, 0 pending");
  assert.equal(toggle.classList.contains("review-drawer-toggle-error"), false);
});

/**
 * `review.discard_draft` already exists in the protocol and the TUI already
 * binds it, so the drawer offers it here rather than leaving the review with
 * no way out. It is confirmed inline, and the confirmation says exactly what
 * is being destroyed.
 */
test("Discard review is confirmed inline and then drops the pending review", async () => {
  const review = "review-drawer-discard";
  const discards = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(
              review,
              4,
              content({
                body: "Nearly there",
                comments: [inlineEntry("a", "Guard this", 11, "src/calc.py")],
              }),
            ),
          ],
        }),
      discardReviewDraft: async (params) => {
        discards.push(params);
        return { discarded: draft(review, 4, content()) };
      },
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  await view.findByLabelText("Summary");

  // One press asks, it does not act.
  fireEvent.click(view.getByRole("button", { name: "Discard review" }));
  await view.findByText(
    "Discard 1 pending comment and the summary? This cannot be undone.",
  );
  assert.equal(discards.length, 0);
  fireEvent.click(
    view.getByRole("button", {
      name: "Confirm discard of 1 pending comment and the summary",
    }),
  );
  await waitForCount(() => discards.length, 1);
  assert.equal(discards[0].draft_id, DRAFT_ID);
  assert.equal(discards[0].expected_version, 4);

  // The review returns to its no-draft state on both surfaces at once.
  await view.findByText(
    "No pending review is open. Start one from a diff line, or choose a preserved draft below.",
  );
  assert.equal(
    view.getByRole("button", { name: /Your review/ }).getAttribute("aria-label"),
    "Your review, 0 pending",
  );
  assert.equal(view.container.querySelectorAll(".pending-card").length, 0);
});

/**
 * The two facts inside the confirmation are read from the draft, not assumed.
 * A review with no summary must not be told it is about to lose one, and the
 * count is the same number the badge shows, singular or plural.
 */
test("the discard confirmation counts what is pending and names a summary only when there is one", async () => {
  const review = "review-drawer-discard-count";
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(
              review,
              7,
              content({
                body: "",
                comments: [
                  inlineEntry("a", "Guard this", 11, "src/calc.py"),
                  inlineEntry("b", "And this", 12, "src/calc.py"),
                ],
              }),
            ),
          ],
        }),
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  await view.findByLabelText("Summary");
  fireEvent.click(view.getByRole("button", { name: "Discard review" }));
  await view.findByText("Discard 2 pending comments? This cannot be undone.");
  assert.equal(
    view.container.querySelectorAll(".review-drawer-confirm").length,
    1,
  );
  await view.findByRole("button", {
    name: "Confirm discard of 2 pending comments",
  });
});

/**
 * Escape answers the armed question, not the drawer. Backing out of a
 * destructive confirmation must leave the pending review, the drawer and the
 * in-diff cards exactly as they were, and must send nothing.
 */
test("Escape cancels an armed discard and leaves the pending review untouched", async () => {
  const review = "review-drawer-discard-escape";
  const discards = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(
              review,
              4,
              content({
                body: "Nearly there",
                comments: [inlineEntry("a", "Guard this", 11, "src/calc.py")],
              }),
            ),
          ],
        }),
      discardReviewDraft: async (params) => {
        discards.push(params);
        return { discarded: draft(review, 4, content()) };
      },
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  await view.findByLabelText("Summary");
  fireEvent.click(view.getByRole("button", { name: "Discard review" }));
  await view.findByText(
    "Discard 1 pending comment and the summary? This cannot be undone.",
  );

  fireEvent.keyDown(view.getByRole("dialog", { name: "Your review" }), {
    key: "Escape",
  });
  await waitForCount(
    () => view.container.querySelectorAll(".review-drawer-confirm").length,
    0,
  );
  assert.equal(discards.length, 0);

  // The drawer is still open on the same pending review, and the button has
  // gone back to asking rather than acting.
  await view.findByLabelText("Summary");
  assert.equal(
    view.getByRole("button", { name: /Your review/ }).getAttribute("aria-label"),
    "Your review, 1 pending",
  );
  assert.equal(view.container.querySelectorAll(".pending-card").length, 1);
  await view.findByRole("button", { name: "Discard review" });

  // A second Escape, with nothing armed, closes the drawer as it always did.
  fireEvent.keyDown(view.getByRole("dialog", { name: "Your review" }), {
    key: "Escape",
  });
  await waitForCount(
    () => view.container.querySelectorAll(".review-drawer").length,
    0,
  );
  assert.equal(discards.length, 0);
});

/**
 * RFE 3, the other surface. The composer overflow carries the same Discard,
 * offered only while there is a review to discard, and a discard taken there
 * returns the composer to Start a review and empties the diff and the badge
 * without the drawer being opened at all.
 */
test("the composer overflow discards the pending review and returns to Start a review", async () => {
  const review = "review-composer-discard";
  const discards = [];
  let stored = null;
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: stored ? [stored] : [] }),
      createReviewDraft: () => {
        stored = draft(review, 1, content());
        return Promise.resolve(stored);
      },
      saveReviewDraft: async (params) => {
        stored = draft(review, params.expected_version + 1, params.content);
        return stored;
      },
      discardReviewDraft: async (params) => {
        discards.push(params);
        const discarded = stored;
        stored = null;
        return { discarded };
      },
    }),
    review,
  );

  // With no pending review there is nothing to overflow into.
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  await view.findByLabelText("Inline review comment");
  assert.equal(
    view.container.querySelectorAll('[aria-label="More review actions"]').length,
    0,
  );

  fireEvent.change(view.getByLabelText("Inline review comment"), {
    target: { value: "Guard the zero divisor" },
  });
  fireEvent.click(view.getByRole("button", { name: "Start a review" }));
  await waitForCount(
    () => view.container.querySelectorAll(".pending-card").length,
    1,
  );

  // Reopened on the same line, the overflow is now offered.
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  await view.findByLabelText("Inline review comment");
  fireEvent.click(view.getByRole("button", { name: "More review actions" }));

  // One press asks, it does not act.
  fireEvent.click(view.getByRole("button", { name: "Discard review" }));
  await view.findByText("Discard 1 pending comment? This cannot be undone.");
  assert.equal(discards.length, 0);
  fireEvent.click(
    view.getByRole("button", { name: "Confirm discard of 1 pending comment" }),
  );
  await waitForCount(() => discards.length, 1);
  assert.equal(discards[0].draft_id, DRAFT_ID);
  assert.equal(discards[0].expected_version, 2);

  // The diff drops its pending cards, the badge returns to zero, and the
  // composer that is still open offers to start a review again.
  await waitForCount(
    () => view.container.querySelectorAll(".pending-card").length,
    0,
  );
  assert.equal(
    view.getByRole("button", { name: /Your review/ }).getAttribute("aria-label"),
    "Your review, 0 pending",
  );
  await view.findByRole("button", { name: "Start a review" });
  assert.equal(
    view.container.querySelectorAll('[aria-label="More review actions"]').length,
    0,
  );
});

/**
 * Escape on the composer surface, in the three stages it has to answer. The
 * diff view above the composer closes it on any Escape while an anchor is
 * open, so an Escape the composer consumed has to stop there; otherwise
 * backing out of a destructive confirmation takes the composer with it.
 */
test("Escape in the composer cancels the armed discard, then the overflow, then closes", async () => {
  const review = "review-composer-discard-escape";
  const discards = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(
              review,
              4,
              content({
                body: "Nearly there",
                comments: [inlineEntry("a", "Guard this", 11, "src/calc.py")],
              }),
            ),
          ],
        }),
      discardReviewDraft: async (params) => {
        discards.push(params);
        return { discarded: draft(review, 4, content()) };
      },
    }),
    review,
  );
  const composers = () =>
    view.container.querySelectorAll(
      'textarea[aria-label="Inline review comment"]',
    ).length;
  const overflows = () =>
    view.container.querySelectorAll('[role="group"][aria-label="Review actions"]')
      .length;
  const prompts = () =>
    view.container.querySelectorAll('.inline-composer [role="status"]').length;
  const escape = () =>
    fireEvent.keyDown(view.container.querySelector(".inline-composer"), {
      key: "Escape",
    });

  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  await view.findByLabelText("Inline review comment");
  fireEvent.click(view.getByRole("button", { name: "More review actions" }));
  fireEvent.click(view.getByRole("button", { name: "Discard review" }));
  await view.findByText(
    "Discard 1 pending comment and the summary? This cannot be undone.",
  );
  assert.equal(composers(), 1);
  assert.equal(overflows(), 1);
  assert.equal(prompts(), 1);

  // Stage one: the armed confirmation is cancelled and nothing else moves.
  escape();
  await waitForCount(() => prompts(), 0);
  assert.equal(composers(), 1);
  assert.equal(overflows(), 1);
  assert.equal(discards.length, 0);

  // Stage two: the overflow closes, the composer stays.
  escape();
  await waitForCount(() => overflows(), 0);
  assert.equal(composers(), 1);

  // Stage three: with nothing left to answer, the composer closes as always.
  escape();
  await waitForCount(() => composers(), 0);
  assert.equal(discards.length, 0);
  assert.equal(view.container.querySelectorAll(".pending-card").length, 1);
});

/**
 * A destructive action that reports nothing when it is refused is the wrong
 * failure mode. The drawer's own message slot wins over the composer's, so it
 * has to be cleared as the discard starts: the refusal that renders must be
 * the discard's own, and a discard that succeeds must not leave an older,
 * unrelated failure standing beside an emptied review.
 */
test("a refused discard reports its own reason and a successful one clears the slot", async () => {
  const review = "review-drawer-discard-message";
  let refuseDiscard = true;
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(
              review,
              4,
              content({
                body: "Nearly there",
                comments: [inlineEntry("a", "Guard this", 11, "src/calc.py")],
              }),
            ),
          ],
        }),
      saveReviewDraft: async () => {
        throw {
          code: "network_unavailable",
          message: "The forge was unreachable.",
          retryable: true,
        };
      },
      discardReviewDraft: async () => {
        if (refuseDiscard) {
          throw {
            code: "conflict",
            message: "The stored draft moved on.",
            retryable: false,
          };
        }
        return { discarded: draft(review, 4, content()) };
      },
    }),
    review,
  );
  const notices = () =>
    [
      ...view.container.querySelectorAll(".review-drawer .notice-error"),
    ].map((element) => element.textContent);

  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  fireEvent.change(await view.findByLabelText("Summary"), {
    target: { value: "My text" },
  });
  fireEvent.click(
    view.getByRole("button", { name: "Save summary and verdict" }),
  );
  await waitForCount(() => notices().length, 1);
  assert.deepEqual(notices(), [
    "The forge could not be reached. Refresh remote state before deciding whether to retry.",
  ]);

  // The refused discard replaces that sentence with its own rather than
  // leaving the reader with an unrelated one beside an untouched review.
  fireEvent.click(view.getByRole("button", { name: "Discard review" }));
  fireEvent.click(
    view.getByRole("button", {
      name: "Confirm discard of 1 pending comment and the summary",
    }),
  );
  await waitForList(() => notices(), [
    "The forge refused this action: the review changed remotely or its branch conflicts with the target. Refresh, and check for merge conflicts.",
  ]);
  assert.equal(view.container.querySelectorAll(".pending-card").length, 1);

  // The discard that lands leaves no error behind it.
  refuseDiscard = false;
  fireEvent.click(view.getByRole("button", { name: "Discard review" }));
  fireEvent.click(
    view.getByRole("button", {
      name: "Confirm discard of 1 pending comment and the summary",
    }),
  );
  await waitForCount(
    () => view.container.querySelectorAll(".pending-card").length,
    0,
  );
  assert.deepEqual(notices(), []);
});

/**
 * Both surfaces have to leave identical state, and the drawer's list of
 * recoverable drafts is part of that state. A draft discarded from the in-diff
 * overflow, with the drawer never opened for the discard, must not still be
 * offered for recovery afterwards.
 */
test("a discard taken from the composer prunes the drawer recovery list", async () => {
  const review = "review-drawer-discard-candidates";
  const first = "44444444-4444-4444-8444-444444444444";
  const third = "55555555-5555-4555-8555-555555555555";
  const candidates = [
    { ...draft(review, 2, content({ body: "First" })), id: first },
    draft(review, 3, content({ body: "Second" })),
    { ...draft(review, 4, content({ body: "Third" })), id: third },
  ];
  const discards = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: candidates }),
      discardReviewDraft: async (params) => {
        discards.push(params);
        return { discarded: draft(review, 3, content()) };
      },
    }),
    review,
  );
  const recoveries = () =>
    view.container.querySelectorAll(".review-workflow-recovery button").length;

  const toggle = await view.findByRole("button", { name: /Your review/ });
  fireEvent.click(toggle);
  await waitForCount(() => recoveries(), 3);

  // Adopt the middle one, then leave the drawer entirely.
  fireEvent.click(
    view.getByRole("button", { name: new RegExp(`Draft ${DRAFT_ID}`) }),
  );
  await view.findByLabelText("Summary");
  fireEvent.click(view.getByRole("button", { name: "Close your review" }));
  await waitForCount(
    () => view.container.querySelectorAll(".review-drawer").length,
    0,
  );

  // Discard it from the in-diff overflow, without the drawer being open.
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  await view.findByLabelText("Inline review comment");
  fireEvent.click(view.getByRole("button", { name: "More review actions" }));
  fireEvent.click(view.getByRole("button", { name: "Discard review" }));
  fireEvent.click(
    view.getByRole("button", { name: "Confirm discard of the summary" }),
  );
  await waitForCount(() => discards.length, 1);
  assert.equal(discards[0].draft_id, DRAFT_ID);

  // The drawer offers the two that are left, and not the one that is gone.
  fireEvent.click(view.getByRole("button", { name: /Your review/ }));
  await waitForCount(() => recoveries(), 2);
  assert.equal(
    [...view.container.querySelectorAll(".review-workflow-recovery button")]
      .map((element) => element.textContent)
      .filter((label) => label.includes(DRAFT_ID)).length,
    0,
  );
});

/**
 * The carry over from #190: mount adoption and the first bind used to ask the
 * sidecar the same question twice. One shared read now answers both, and the
 * drawer's own candidate list.
 */
test("one active-draft read answers mount adoption, the drawer and the first bind", async () => {
  const review = "review-drawer-one-read";
  let reads = 0;
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () => {
        reads += 1;
        return read({ cursor: 0, next_cursor: null, drafts: [] });
      },
    }),
    review,
  );
  await view.findByRole("button", { name: "Comment on new line 11" });
  await view.findByRole("button", { name: /Your review/ });
  await waitForFlag(() => reads >= 1, true);
  assert.equal(reads, 1);
});

/**
 * F4 from the #205 review: the transcript is both sides of the S46 conflict
 * and the preserved old draft list, so it has to stay legible when two entries
 * share one anchor and when a stale anchor is on the old side. A label built
 * from the path and one line number cannot tell those apart.
 */
test("the draft transcript distinguishes two entries on one anchor and marks a stale old-side one", () => {
  const shared = {
    revision: REVISION,
    old_path: "/dev/null",
    new_path: "/dev/null",
    old_line: 42,
    new_line: null,
    side: "old",
    context_fingerprint: "f".repeat(64),
    start_line: null,
    start_side: null,
  };
  const text = draftContentTranscript({
    body: "b",
    verdict: null,
    comments: [
      {
        id: "c1",
        kind: "inline",
        body: "removed line note",
        anchor: { ...shared, stale: true },
      },
      {
        id: "c2",
        kind: "inline",
        body: "second note on same line",
        anchor: { ...shared },
      },
      { id: "c3", kind: "reply", body: "a reply", thread_id: "thread-9" },
      { id: "c4", kind: "general", body: "a general note" },
    ],
  });
  assert.equal(
    text,
    [
      "b",
      "",
      "Verdict: none",
      "",
      "Inline c1 \u00b7 /dev/null \u00b7 old line 42 \u00b7 stale anchor",
      "removed line note",
      "",
      "Inline c2 \u00b7 /dev/null \u00b7 old line 42",
      "second note on same line",
      "",
      "Reply c3 to discussion thread-9",
      "a reply",
      "",
      "General comment c4",
      "a general note",
    ].join("\n"),
  );
});

/**
 * `v` records a verdict, so it stands down while a running submission holds
 * the draft, for the same reason the tiles are disabled. The state alone does
 * not refuse the edit while the start is only pending, so the drawer's lock is
 * what keeps the verdict the store is submitting.
 */
test("v leaves the verdict alone while a running submission locks the drawer", async () => {
  const review = "review-drawer-locked-verdict";
  let release = () => {};
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const starts = [];
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [draft(review, 2, content({ body: "Ready", verdict: "approve" }))],
        }),
      saveReviewDraft: async (params) => {
        saves.push(params);
        return draft(review, params.expected_version + 1, params.content);
      },
      startReviewSubmission: async (params) => {
        starts.push(params);
        await gate;
        return submission(review, "submitted", params.expected_version);
      },
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  await capabilitiesReady(view.container, VERDICT_TILES);
  assert.equal(checkedVerdict(view), "approve");

  fireEvent.click(view.getByRole("button", { name: "Submit review" }));
  fireEvent.click(
    await view.findByRole("button", { name: "Confirm submit review" }),
  );
  await waitForCount(() => starts.length, 1);
  await waitForCount(
    () => view.container.querySelectorAll(`${VERDICT_TILES}:disabled`).length,
    3,
  );

  fireEvent.keyDown(document.body, { key: "v" });
  fireEvent.keyDown(document.body, { key: "v" });
  assert.equal(checkedVerdict(view), "approve");
  assert.equal(saves.length, 0);
  assert.equal(
    view.container.querySelectorAll(".review-drawer .notice-error").length,
    0,
  );

  release();
  await view.findByText("Review submitted.");
  assert.equal(checkedVerdict(view), "approve");
  assert.equal(starts[0].expected_version, 2);
  assert.equal(saves.length, 0);
});

/**
 * Several active drafts are a choice the drawer owns. Once one is adopted it
 * is the pending review, so the list of the others is no longer offered beside
 * it.
 */
test("the preserved draft list goes away once one of its drafts is adopted", async () => {
  const review = "review-drawer-adopt-candidate";
  const other = "66666666-6666-4666-8666-666666666666";
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(review, 2, content({ body: "First" })),
            { ...draft(review, 5, content({ body: "Second" })), id: other },
          ],
        }),
    }),
    review,
  );
  const preserved = () =>
    view.queryAllByRole("button", { name: /^Draft .* version \d+$/ }).length;

  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  await waitForCount(() => preserved(), 2);
  assert.equal(view.queryAllByText("Choose a preserved draft").length, 1);
  assert.equal(view.queryAllByLabelText("Summary").length, 0);

  fireEvent.click(view.getByRole("button", { name: new RegExp(`Draft ${other}`) }));
  await view.findByLabelText("Summary");
  assert.equal(view.getByLabelText("Summary").value, "Second");
  await view.findByText("Version 5");
  assert.equal(preserved(), 0);
  assert.equal(view.queryAllByText("Choose a preserved draft").length, 0);
  assert.equal(view.queryAllByText(/No pending review is open/).length, 0);
});

/**
 * A refusal raised by the workflow state or the composer is already a sentence
 * for the reader, so the drawer shows it as written. Anything else keeps the
 * generic reporting boundary, so an internal message never reaches the reader.
 */
test("a drawer refusal is shown in its own words and other failures stay generic", () => {
  assert.equal(
    drawerFailureMessage(
      new ReviewWorkflowRefusal("Submission operation is already pending"),
    ),
    "Submission operation is already pending",
  );
  assert.equal(
    drawerFailureMessage(
      new ComposerRefusal("Several pending reviews were recovered."),
    ),
    "Several pending reviews were recovered.",
  );

  const internal = new Error("assertion: pending.kind was undefined");
  const generic = drawerFailureMessage(internal);
  assert.equal(generic, reviewMutationError(internal));
  assert.equal(generic.includes("pending.kind"), false);

  const typed = {
    code: "revision_changed",
    message: "raw sidecar text",
    retryable: true,
  };
  assert.equal(drawerFailureMessage(typed), reviewMutationError(typed));
  assert.equal(drawerFailureMessage(typed).includes("raw sidecar text"), false);
});

function checkedVerdict(view) {
  const checked = view.container.querySelector(`${VERDICT_TILES}:checked`);
  return checked === null ? null : checked.value;
}

function transcript(content) {
  return [
    `body=${content.body}`,
    `verdict=${content.verdict ?? "none"}`,
    ...content.comments.map((comment) =>
      comment.kind === "inline"
        ? `inline ${comment.anchor.new_path} ${comment.anchor.side}:${comment.anchor.new_line} stale=${comment.anchor.stale === true} | ${comment.body}`
        : `${comment.kind} | ${comment.body}`,
    ),
  ].join("\n");
}

function renderDiff(bridge, review, forge = "github") {
  const feature = createDiffFeature();
  const route = { kind: "review", item: reviewItem(review), panel: "diff" };
  function Harness() {
    const inlineAnchors = React.useMemo(() => new InlineAnchorStore(), []);
    const [current, setCurrent] = React.useState(route);
    const queries = React.useMemo(() => new QueryCoordinator(bridge), []);
    return feature.render(
      {
        bridge,
        queries,
        repositories: [
          { handle: "repo", display_name: "example/repo", forge_type: forge },
        ],
        repositoriesReady: true,
        repositoryGeneration: 1,
        reviewPanels: [{ id: "diff", label: "Files changed", order: 20 }],
        inlineAnchors,
        navigate: setCurrent,
      },
      current,
    );
  }
  return render(React.createElement(Harness));
}

function diffBridge(review, changes = {}) {
  return {
    cancelRead: async () => true,
    openExternal: async () => true,
    openDiff: (params) => read(diffPage(review, params.layout ?? "unified")),
    pageDiff: () => {
      throw new Error("the fixture diff has one page");
    },
    getReviewMutationCapabilities: () =>
      read({ review, capabilities: capabilities() }),
    listDiscussions: () => read(discussionsPage([])),
    listReviewDrafts: () => read({ cursor: 0, next_cursor: null, drafts: [] }),
    listReviewSubmissions: () =>
      read({ cursor: 0, next_cursor: null, attempts: [] }),
    getReviewSubmission: () => read(submission(review, "unknown", 1)),
    getReviewDraft: () => read(draft(review, 1, content())),
    createReviewDraft: async () => draft(review, 1, content()),
    saveReviewDraft: async (params) =>
      draft(review, params.expected_version + 1, params.content),
    discardReviewDraft: async () => ({ discarded: draft(review, 1, content()) }),
    startReviewSubmission: async (params) =>
      submission(review, "submitted", params.expected_version),
    resumeReviewSubmission: async () => submission(review, "submitted", 1),
    reconcileReviewSubmission: async () => submission(review, "submitted", 1),
    postInlineReviewComment: async (params) => mutation(params.operation_id),
    ...changes,
  };
}

function read(value) {
  return { requestToken: crypto.randomUUID(), result: Promise.resolve(value) };
}

function capabilities(changes = {}) {
  return {
    general_comment: true,
    inline_comment: true,
    multiline_comment: true,
    reply: true,
    resolve: true,
    approve: true,
    request_changes: true,
    comment_verdict: true,
    atomic_review_batch: false,
    ...changes,
  };
}

function content(changes = {}) {
  return { body: "", verdict: null, comments: [], ...changes };
}

function inlineEntry(id, body, newLine, path) {
  return {
    id,
    kind: "inline",
    body,
    anchor: {
      revision: REVISION,
      old_path: path,
      new_path: path,
      old_line: null,
      new_line: newLine,
      side: "new",
      context_fingerprint: "f".repeat(64),
      start_line: null,
      start_side: null,
      stale: false,
    },
  };
}

function draft(review, version, body) {
  return {
    id: DRAFT_ID,
    review,
    revision: REVISION,
    version,
    body: body.body ?? "",
    verdict: body.verdict ?? null,
    comments: body.comments ?? [],
    state: "editable",
    created_at: "2026-09-09T00:00:00+00:00",
    updated_at: "2026-09-09T00:00:00+00:00",
  };
}

function mutation(operationId) {
  return {
    operation_id: operationId,
    outcome: "known",
    receipt: { remote_id: "remote", comment_id: "comment", discussion_id: null },
    reason: null,
    resync_required: false,
  };
}

function submission(review, outcome, frozenVersion) {
  return {
    attempt_id: "22222222-2222-4222-8222-222222222222",
    draft_id: DRAFT_ID,
    review,
    frozen_version: frozenVersion,
    state: outcome === "submitted" ? "submitted" : outcome,
    outcome,
    steps: [{ id: "verdict", kind: "verdict", comment_ids: [] }],
    receipts: [],
    completed_step_ids: [],
    unknown_step_ids: outcome === "unknown" ? ["verdict"] : [],
    atomic: false,
    resync_required: outcome === "unknown",
    failure: null,
    plan_available: true,
  };
}

function reviewItem(handle) {
  return {
    handle,
    repository: "repo",
    summary: {
      number: 46,
      title: "Desktop review",
      author: { username: "author", display_name: "Author" },
      state: "open",
      is_draft: false,
      source_branch: "feature",
      target_branch: "main",
      ci_status: "success",
      created_at: "2026-09-09T00:00:00Z",
      updated_at: "2026-09-09T00:00:00Z",
      web_url: "https://github.com/example/repo/pull/46",
      comment_count: 0,
      has_conflicts: false,
      labels: [],
      review_decision: null,
      additions: 1,
      deletions: 1,
      description: "",
      merge_status: "can_be_merged",
      approvals: [],
      reviewers: [],
      assignees: [],
      changes_count: 2,
      detailed_merge_status: null,
      draft_notes_count: 0,
      status_check_rollup: "success",
    },
  };
}

function diffPage(review, layout) {
  const file = {
    kind: "file",
    file_index: 0,
    old_path: "src/calc.py",
    new_path: "src/calc.py",
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
  };
  const hunk = {
    kind: "hunk",
    file_index: 0,
    hunk_index: 0,
    header: "@@ -10,3 +10,3 @@",
    old_start: 10,
    old_count: 3,
    new_start: 10,
    new_count: 3,
    context_text: "def divide(a, b):",
  };
  const sources = [
    { old_line: 10, new_line: 10, content: "def divide(a, b):", line_type: "context" },
    { old_line: null, new_line: 11, content: "    if b == 0:", line_type: "addition" },
    { old_line: null, new_line: 12, content: "        raise ValueError", line_type: "addition" },
  ];
  const entries =
    layout === "split"
      ? sources.map((source, index) => ({
          kind: "split",
          file_index: 0,
          hunk_index: 0,
          row_index: index,
          old: source.old_line === null ? null : { ...source, anchor_side: "old" },
          new: source.new_line === null ? null : { ...source, anchor_side: "new" },
        }))
      : sources.map((source) => ({
          kind: "line",
          file_index: 0,
          hunk_index: 0,
          ...source,
        }));
  return {
    snapshot_id: `snapshot-${layout}`,
    resource: review,
    revision: REVISION,
    cursor: 0,
    next_cursor: null,
    entries: [file, hunk, ...entries],
  };
}

/** A first discussions page that holds every thread, as the sidecar sends it. */
function discussionsPage(discussions) {
  return { discussions, revision: { discussion_count: discussions.length } };
}
