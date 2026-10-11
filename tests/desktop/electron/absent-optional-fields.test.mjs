import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import {
  decodeReadFailure,
  encodeReadFailure,
} from "../../../desktop/dist/src/shared/bridge.js";
import {
  assertParams,
  assertResult,
} from "../../../desktop/dist/src/main/security.js";

const fixture = JSON.parse(
  readFileSync(
    path.join(import.meta.dirname, "../fixtures/diff-shapes.json"),
    "utf8",
  ),
);

function comment(overrides) {
  return {
    id: "note-1",
    author: { username: "reviewer", display_name: "" },
    body: "looks good",
    created_at: "2026-09-08T12:00:00+00:00",
    file_path: null,
    old_line: null,
    new_line: null,
    is_resolved: false,
    replies: [],
    ...overrides,
  };
}

function discussions(root) {
  return {
    snapshot_id: "discussions-snapshot",
    resource: "review-handle",
    revision: { discussion_count: 1 },
    cursor: 0,
    next_cursor: null,
    discussions: [
      {
        id: "53f505e47aca",
        is_inline: false,
        root_comment: comment(root),
        is_resolved: false,
        resolvable: true,
      },
    ],
  };
}

test("every sandbox diff shape passes the read boundary on both forges", () => {
  for (const key of ["github_unified_page", "gitlab_unified_page"]) {
    const page = fixture[key];
    const files = page.entries.filter((entry) => entry.kind === "file");
    assert.equal(files.length, 8, `${key} must carry all eight shapes`);
    assert.equal(
      files.filter((file) => file.language === null).length,
      1,
      `${key} must carry the file with no detected language`,
    );
    assertResult("diff.open", page);
    assertResult("diff.page", page);
  }
});

test("a file row naming an empty language is rejected with its cause", () => {
  const page = structuredClone(fixture.github_unified_page);
  page.entries[0].language = "";

  assert.throws(
    () => assertResult("diff.open", page),
    (error) => {
      assert.match(error.message, /Invalid diff\.open result/);
      assert.match(error.message, /"language" must be null or non-empty text/);
      return true;
    },
  );
});

test("a review-level note reads with no inline position", () => {
  assertResult("discussions.list", discussions({}));
  assertResult("discussions.list", discussions({ body: "" }));
  assertResult(
    "discussions.list",
    discussions({
      file_path: "src/calc.py",
      new_line: 26,
      replies: [comment({ id: "note-2", file_path: "src/calc.py", new_line: 26 })],
    }),
  );

  assert.throws(
    () => assertResult("discussions.list", discussions({ file_path: "" })),
    (error) => {
      assert.match(error.message, /Invalid discussions\.list result/);
      assert.match(error.message, /"file_path" must be null or non-empty text/);
      return true;
    },
  );
});

test("a discussions page carries its snapshot cursor across the read boundary", () => {
  const page = { ...discussions({}), revision: { discussion_count: 1200 } };
  assertResult("discussions.list", { ...page, next_cursor: 400 });
  assertResult("discussions.page", { ...page, cursor: 800, next_cursor: null });
  assertParams("discussions.page", {
    snapshot: "discussions-snapshot",
    resource: "review-handle",
    cursor: 400,
  });

  // The unpaged 1.0.2 shape no longer passes: every answer is a page.
  assert.throws(() =>
    assertResult("discussions.list", { discussions: page.discussions }),
  );
  assert.throws(
    () => assertResult("discussions.page", { ...page, cursor: 400, next_cursor: 400 }),
    (error) => {
      assert.match(error.message, /Invalid discussions\.page result/);
      assert.match(error.message, /"next_cursor" must advance past the cursor/);
      return true;
    },
  );
  assert.throws(() =>
    assertResult("discussions.page", { ...page, revision: { discussion_count: -1 } }),
  );
  assert.throws(() =>
    assertParams("discussions.page", { snapshot: "s", resource: "r" }),
  );
});

test("an empty job log reads as one empty row", () => {
  assertResult("logs.open", {
    snapshot_id: "snapshot",
    resource: "job",
    revision: {
      sha256:
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      byte_count: 0,
    },
    cursor: 0,
    next_cursor: null,
    entries: [{ text: "" }],
  });
});

test("a read failure keeps its code, retryability and cause across the boundary", () => {
  const encoded = encodeReadFailure({
    code: "snapshot_expired",
    message: "The resource snapshot is invalid or expired; refetch it.",
    retryable: true,
  });

  assert.deepEqual(decodeReadFailure(encoded), {
    code: "snapshot_expired",
    message: "The resource snapshot is invalid or expired; refetch it.",
    retryable: true,
  });
  // Electron rebuilds the rejection as a fresh Error around the message text.
  assert.deepEqual(
    decodeReadFailure(
      new Error(
        `Error invoking remote method 'tongs:diff.open': Error: ${encoded.message}`,
      ),
    ),
    {
      code: "snapshot_expired",
      message: "The resource snapshot is invalid or expired; refetch it.",
      retryable: true,
    },
  );
});

test("an untyped failure becomes a non-retryable invalid response", () => {
  const failure = decodeReadFailure(
    encodeReadFailure(new Error('Invalid diff.open result: field "language" must be null or non-empty text')),
  );

  assert.deepEqual(failure, {
    code: "invalid_response",
    message:
      'Invalid diff.open result: field "language" must be null or non-empty text',
    retryable: false,
  });
  assert.equal(decodeReadFailure(new Error("plain failure")), null);
  assert.equal(decodeReadFailure(null), null);
  assert.equal(
    decodeReadFailure(new Error("tongs-read-failure:%7Bnot-json")),
    null,
  );
  // A well-formed marker still cannot smuggle in a code outside the typed shape.
  const forged = encodeURIComponent(
    JSON.stringify({ code: "Not A Code", message: "forged", retryable: false }),
  );
  assert.equal(decodeReadFailure(new Error(`tongs-read-failure:${forged}`)), null);
});

function reviewSnapshot(detailOverrides = {}) {
  return {
    handle: "review-handle",
    repository: "repository-handle",
    detail: {
      number: 42,
      title: "Vendor a large dependency",
      author: { username: "alice", display_name: "Alice Dev" },
      state: "open",
      is_draft: false,
      source_branch: "feature/vendor",
      target_branch: "main",
      ci_status: "success",
      created_at: "2026-06-01T09:00:00+00:00",
      updated_at: "2026-06-02T14:30:00+00:00",
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
      changes_count: 1000,
      detailed_merge_status: "mergeable",
      draft_notes_count: null,
      status_check_rollup: null,
      ...detailOverrides,
    },
    capabilities: {
      batched_review: true,
      thread_resolution: true,
      draft_notes: true,
      unapprove: true,
      job_cancel: true,
    },
    revision: { head_sha: "ccc333", base_sha: "aaa111", start_sha: "bbb222" },
    revision_error: null,
  };
}

test("a review detail reads with or without the truncated file list flag", () => {
  // The current core states the flag either way.
  assertResult("reviews.get", reviewSnapshot({ changes_overflow: true }));
  assertResult("reviews.get", reviewSnapshot({ changes_overflow: false }));
  // A core that predates the flag omits it, and the detail still reads.
  const older = reviewSnapshot();
  assert.equal(Object.hasOwn(older.detail, "changes_overflow"), false);
  assertResult("reviews.get", older);
  assert.equal(older.detail.changes_overflow === true, false);
});

test("a review detail rejects a non-boolean flag and any unknown key", () => {
  assert.throws(
    () => assertResult("reviews.get", reviewSnapshot({ changes_overflow: "yes" })),
    (error) => {
      assert.match(error.message, /Invalid reviews\.get result/);
      assert.match(error.message, /"changes_overflow" must be a boolean/);
      return true;
    },
  );
  assert.throws(
    () => assertResult("reviews.get", reviewSnapshot({ changes_overflow: null })),
    /"changes_overflow" must be a boolean/,
  );
  assert.throws(
    () => assertResult("reviews.get", reviewSnapshot({ files_overflow: true })),
    /Invalid reviews\.get result/,
  );
  assert.throws(
    () =>
      assertResult(
        "reviews.get",
        reviewSnapshot({ changes_overflow: true, files_overflow: true }),
      ),
    /Invalid reviews\.get result/,
  );
});

function pipelinesResult(overrides = {}) {
  return {
    pipelines: [
      {
        handle: "pipeline-handle",
        value: {
          id: 1,
          status: "success",
          ref: "main",
          sha: "abc123",
          web_url: "https://github.com/acme/repo/actions/runs/1",
          source: "push",
          created_at: null,
          finished_at: null,
          duration_seconds: null,
          ...overrides,
        },
      },
    ],
  };
}

function jobsResult(overrides = {}) {
  return {
    jobs: [
      {
        handle: "job-handle",
        value: {
          id: 2,
          name: "build",
          stage: "CI",
          status: "queued",
          web_url: "https://github.com/acme/repo/actions/runs/1/job/2",
          started_at: null,
          finished_at: null,
          duration_seconds: null,
          allow_failure: false,
          ...overrides,
        },
      },
    ],
  };
}

function commitsResult(overrides = {}) {
  return {
    commits: [
      {
        sha: "abc123def456",
        short_sha: "abc123de",
        title: "Fix the build",
        message: "Fix the build",
        author: { username: "dev", display_name: "" },
        created_at: null,
        web_url: "https://github.com/acme/repo/commit/abc123def456",
        ...overrides,
      },
    ],
  };
}

// Each read and the fields a forge may leave out, which the sidecar sends as
// null (#184; a workflow run's branch, #443). Every other text field of these
// rows stays required.
const OPTIONAL_TEXT = [
  ["review_pipelines.list", pipelinesResult, ["ref", "source", "web_url"]],
  ["pipelines.list", pipelinesResult, ["ref", "source", "web_url"]],
  ["jobs.list", jobsResult, ["stage", "web_url"]],
  ["commits.list", commitsResult, ["web_url"]],
];
const REQUIRED_TEXT = [
  ["pipelines.list", pipelinesResult, ["status", "sha"]],
  ["jobs.list", jobsResult, ["name", "status"]],
  ["commits.list", commitsResult, ["sha", "short_sha", "title", "message"]],
];

test("a pipeline with no branch or source and a job with no stage pass the read boundary", () => {
  for (const [method, result, fields] of OPTIONAL_TEXT) {
    assertResult(method, result());
    assertResult(
      method,
      result(Object.fromEntries(fields.map((field) => [field, null]))),
    );
  }
  assertResult("reviews.get", reviewSnapshot({ merge_status: null }));
});

test("an absent optional field must be null, never empty or another type", () => {
  for (const [method, result, fields] of OPTIONAL_TEXT) {
    for (const field of fields) {
      for (const bad of ["", 7, false, {}]) {
        assert.throws(
          () => assertResult(method, result({ [field]: bad })),
          (error) => {
            assert.match(error.message, new RegExp(`Invalid ${method.replace(".", "\\.")} result`));
            assert.match(error.message, new RegExp(`"${field}" must be`));
            return true;
          },
          `${method} ${field}=${JSON.stringify(bad)}`,
        );
      }
    }
  }
  for (const bad of ["", 7]) {
    assert.throws(
      () => assertResult("reviews.get", reviewSnapshot({ merge_status: bad })),
      /"merge_status" must be null or non-empty text/,
    );
  }
});

test("required pipeline, job and commit text still rejects null and empty text", () => {
  for (const [method, result, fields] of REQUIRED_TEXT) {
    for (const field of fields) {
      for (const bad of [null, "", 7]) {
        assert.throws(
          () => assertResult(method, result({ [field]: bad })),
          new RegExp(`Invalid ${method.replace(".", "\\.")} result`),
          `${method} ${field}=${JSON.stringify(bad)}`,
        );
      }
    }
  }
  // A missing key is not an absent value: every row keeps its exact shape.
  const { stage: _stage, ...withoutStage } = jobsResult().jobs[0].value;
  assert.throws(
    () =>
      assertResult("jobs.list", {
        jobs: [{ handle: "job-handle", value: withoutStage }],
      }),
    /Invalid jobs\.list result/,
  );
});
