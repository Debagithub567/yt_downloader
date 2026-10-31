import assert from "node:assert/strict";
import test from "node:test";

import { formatDuration, formatFileSize, jobAnnouncement, jobStateLabel } from "../src/lib/ui.ts";

const state = {
  job: { job_id: "a".repeat(32), access_token: "secret" },
  status: { status: "processing", stage: "processing", attempt: 2, progress: 20 },
  resultIssue: null,
  cancelling: false,
};

test("readable media values handle zero, hours, and large files", () => {
  assert.equal(formatFileSize(0), "0 B");
  assert.equal(formatFileSize(1_048_576), "1.0 MB");
  assert.equal(formatFileSize(1_073_741_824), "1.00 GB");
  assert.equal(formatDuration(null), "Unknown duration");
  assert.equal(formatDuration(3_661), "1:01:01");
});

test("live job announcement changes on stage or attempt, not percentage-only polling", () => {
  const first = jobAnnouncement(state);
  assert.equal(first, "Processing. Stage processing. Attempt 2.");
  assert.equal(jobAnnouncement({ ...state, status: { ...state.status, progress: 83 } }), first);
  assert.notEqual(jobAnnouncement({ ...state, status: { ...state.status, stage: "finalizing" } }), first);
  assert.notEqual(jobAnnouncement({ ...state, status: { ...state.status, attempt: 3 } }), first);
  assert.ok(!first.includes("secret"));
});

test("announcements distinguish cancellation, completion, failure, and unavailable results", () => {
  assert.match(jobAnnouncement({ ...state, cancelling: true }), /Cancellation requested/);
  assert.equal(jobAnnouncement({ ...state, status: { ...state.status, status: "completed" } }),
    "Conversion complete. Result ready to download.");
  assert.equal(jobAnnouncement({ ...state, status: { ...state.status, status: "failed" } }),
    "Conversion failed.");
  assert.equal(jobStateLabel({ ...state, resultIssue: "missing_output" }), "File unavailable");
  assert.equal(jobAnnouncement({ ...state, resultIssue: "expired" }),
    "Expired. The result cannot be downloaded.");
});
