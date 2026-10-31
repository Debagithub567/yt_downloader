import assert from "node:assert/strict";
import test from "node:test";

import { ApiClient } from "../src/lib/api/client.ts";
import { ApiError } from "../src/lib/api/errors.ts";
import { MediaApi, saveDownloadedResult } from "../src/lib/api/media.ts";
import { MediaWorkflow, resultError } from "../src/lib/workflow.ts";

const ID = "a".repeat(32);
const SOURCE = { source_id: "b".repeat(32), source_token: "private_source_token",
  expires_at: "2099-01-01T00:00:00Z",
  media: { title: "Audio", duration_seconds: 2, file_size: 5, container: "m4a",
    video_streams: [], audio_streams: [{ index: 0, codec: "aac", sample_rate: 48000, channels: 1, bitrate: null }] },
  video_options: [], audio_options: ["m4a", "mp3", "original"] };
const JOB = { job_id: ID, access_token: "private_job_token", status: "queued", status_url: `/api/v1/jobs/${ID}` };
const COMPLETED = { job_id: ID, status: "completed", stage: "completed", progress: 100, attempt: 1,
  created_at: "2026-10-31T00:00:00Z", updated_at: "2026-10-31T00:00:00Z",
  expires_at: "2099-01-01T00:00:00Z", output_format: "m4a",
  output: { format: "m4a", size_bytes: 5, media: SOURCE.media }, download_url: `/api/v1/jobs/${ID}/download` };
const RESULT = { blob: new Blob(["audio"]), filename: "safe.m4a", contentType: "audio/mp4", sizeBytes: 5 };

function deferred() {
  let resolve;
  const promise = new Promise((yes) => { resolve = yes; });
  return { promise, resolve };
}

class Clock {
  task = null;
  set(callback) { this.task = callback; return 1; }
  clear() { this.task = null; }
  async tick() { const task = this.task; this.task = null; assert.ok(task); task(); await new Promise((done) => setImmediate(done)); }
}

function setup(overrides = {}) {
  const calls = [];
  const saved = [];
  const api = {
    inspectUrl: async () => SOURCE,
    inspectUpload: async () => SOURCE,
    createJob: async () => JOB,
    getJobStatus: async () => COMPLETED,
    cancelJob: async () => COMPLETED,
    downloadJob: async (...args) => { calls.push(args); return RESULT; },
    ...overrides,
  };
  const clock = new Clock();
  const workflow = new MediaWorkflow(api, clock, (result) => saved.push(result));
  return { workflow, clock, calls, saved };
}

async function submitted({ workflow, clock }) {
  workflow.setUrl("https://media.example/audio.m4a");
  await workflow.inspect();
  await workflow.submit();
  await clock.tick();
}

test("download is unavailable before backend completion", async () => {
  const { workflow, calls } = setup();
  await workflow.download();
  assert.equal(calls.length, 0);
  workflow.setUrl("https://media.example/audio.m4a");
  await workflow.inspect();
  await workflow.submit();
  await workflow.download();
  assert.equal(calls.length, 0);
});

test("completed metadata remains visible without inventing a filename", async () => {
  const harness = setup();
  await submitted(harness);
  assert.equal(harness.workflow.getSnapshot().status.output.format, "m4a");
  assert.equal(harness.workflow.getSnapshot().status.output.size_bytes, 5);
  assert.equal(harness.workflow.getSnapshot().status.expires_at, "2099-01-01T00:00:00Z");
  assert.equal(harness.workflow.getSnapshot().downloadedFilename, null);
});

test("authorized download uses the accepted capability and expected result size", async () => {
  const harness = setup();
  await submitted(harness);
  await harness.workflow.download();
  assert.equal(harness.calls[0][0].job_id, ID);
  assert.equal(harness.calls[0][0].access_token, JOB.access_token);
  assert.equal(harness.calls[0][1].expectedSizeBytes, 5);
  assert.equal(harness.saved.length, 1);
  assert.equal(harness.workflow.getSnapshot().downloadedFilename, "safe.m4a");
  assert.equal(harness.workflow.getSnapshot().downloading, false);
});

test("duplicate clicks cannot start a second download", async () => {
  const pending = deferred();
  let calls = 0;
  const harness = setup({ downloadJob: () => { calls++; return pending.promise; } });
  await submitted(harness);
  const first = harness.workflow.download();
  const second = harness.workflow.download();
  assert.equal(harness.workflow.getSnapshot().downloading, true);
  assert.equal(calls, 1);
  pending.resolve(RESULT);
  await Promise.all([first, second]);
  assert.equal(harness.saved.length, 1);
});

test("missing capability and denied access produce an access state", async () => {
  for (const error of [new ApiError("missing_capability", "Missing"),
    new ApiError("http", "Bearer job capability required", { status: 401 }),
    new ApiError("http", "Access denied", { status: 403 })]) {
    const harness = setup({ downloadJob: async () => { throw error; } });
    await submitted(harness);
    await harness.workflow.download();
    assert.equal(harness.workflow.getSnapshot().resultIssue, "unauthorized");
    assert.match(harness.workflow.getSnapshot().downloadError, /denied/);
  }
});

test("invalid capability response remains ambiguous and never asserts job existence", async () => {
  const harness = setup({ downloadJob: async () => { throw new ApiError("http", "Job was not found or has expired", { status: 404 }); } });
  await submitted(harness);
  await harness.workflow.download();
  assert.equal(harness.workflow.getSnapshot().resultIssue, "unavailable");
  assert.match(harness.workflow.getSnapshot().downloadError, /access key is invalid/);
});

test("expired result is refused locally before a network request", async () => {
  const harness = setup({ getJobStatus: async () => ({ ...COMPLETED, expires_at: "2000-01-01T00:00:00Z" }) });
  await submitted(harness);
  await harness.workflow.download();
  assert.equal(harness.calls.length, 0);
  assert.equal(harness.workflow.getSnapshot().resultIssue, "expired");
});

test("backend expiry and missing output have distinct messages", async () => {
  for (const [detail, issue] of [["Job has expired", "expired"], ["Completed file is unavailable", "missing_output"]]) {
    const harness = setup({ downloadJob: async () => { throw new ApiError("http", detail, { status: 410 }); } });
    await submitted(harness);
    await harness.workflow.download();
    assert.equal(harness.workflow.getSnapshot().resultIssue, issue);
    assert.equal(harness.saved.length, 0);
  }
});

test("network interruption and backend outage leave the result retryable", async () => {
  for (const error of [new ApiError("network", "Offline"), new ApiError("http", "Unavailable", { status: 503 })]) {
    const harness = setup({ downloadJob: async () => { throw error; } });
    await submitted(harness);
    await harness.workflow.download();
    assert.equal(harness.workflow.getSnapshot().resultIssue, null);
    assert.match(harness.workflow.getSnapshot().downloadError, /again/i);
  }
});

test("save failure is shown and a later download can succeed", async () => {
  let fail = true;
  const api = { inspectUrl: async () => SOURCE, inspectUpload: async () => SOURCE,
    createJob: async () => JOB, getJobStatus: async () => COMPLETED, cancelJob: async () => COMPLETED,
    downloadJob: async () => RESULT };
  const clock = new Clock();
  const workflow = new MediaWorkflow(api, clock, () => { if (fail) throw new Error("blocked"); });
  await submitted({ workflow, clock });
  await workflow.download();
  assert.match(workflow.getSnapshot().downloadError, /could not be completed/);
  fail = false;
  await workflow.download();
  assert.equal(workflow.getSnapshot().downloadError, null);
});

test("starting another job aborts an old download and discards its late result", async () => {
  const pending = deferred();
  let signal;
  const harness = setup({ downloadJob: (_job, control) => { signal = control.signal; return pending.promise; } });
  await submitted(harness);
  const request = harness.workflow.download();
  harness.workflow.startOver();
  assert.equal(signal.aborted, true);
  pending.resolve(RESULT);
  await request;
  assert.equal(harness.saved.length, 0);
  assert.equal(harness.workflow.getSnapshot().job, null);
});

test("status expiry stops polling and lets the user start over", async () => {
  const harness = setup({ getJobStatus: async () => { throw new ApiError("http", "Job has expired", { status: 410 }); } });
  await submitted(harness);
  assert.equal(harness.workflow.getSnapshot().resultIssue, "expired");
  assert.equal(harness.clock.task, null);
  harness.workflow.startOver();
  assert.equal(harness.workflow.getSnapshot().job, null);
});

test("failed and cancelled jobs remain ineligible for download", async () => {
  for (const state of ["failed", "cancelled"]) {
    const harness = setup({ getJobStatus: async () => ({ ...COMPLETED, status: state, stage: state, output: undefined }) });
    await submitted(harness);
    await harness.workflow.download();
    assert.equal(harness.calls.length, 0);
    assert.equal(harness.workflow.getSnapshot().status.status, state);
  }
});

test("failed job can reuse a valid source and keep its output choice", async () => {
  const harness = setup({ getJobStatus: async () => ({ ...COMPLETED, status: "failed", stage: "failed", output: undefined }) });
  await submitted(harness);
  harness.workflow.reuseSource();
  assert.equal(harness.workflow.getSnapshot().job, null);
  assert.equal(harness.workflow.getSnapshot().inspection.source_id, SOURCE.source_id);
  assert.equal(harness.workflow.getSnapshot().format, "m4a");
});

test("expired source keeps the input but requires inspection again", async () => {
  const harness = setup({ inspectUrl: async () => ({ ...SOURCE, expires_at: "2000-01-01T00:00:00Z" }) });
  await submitted(harness);
  harness.workflow.reuseSource();
  assert.equal(harness.workflow.getSnapshot().inspection, null);
  assert.equal(harness.workflow.getSnapshot().url, "https://media.example/audio.m4a");
  assert.match(harness.workflow.getSnapshot().error, /Inspect the media again/);
});

test("response size is checked before buffering and rejects missing or mismatched headers", async () => {
  for (const headers of [{ "Content-Type": "audio/mp4", "Content-Length": "9" }, { "Content-Type": "audio/mp4" }]) {
    const response = new Response("audio", { headers });
    response.blob = () => { throw new Error("must not buffer"); };
    const api = new MediaApi(new ApiClient({ fetchImpl: async () => response }));
    await assert.rejects(api.downloadJob(JOB, { expectedSizeBytes: 5 }),
      (error) => error instanceof ApiError && error.kind === "invalid_response");
  }
});

test("authorized URL has no token and safe filename is used for the browser object URL", async () => {
  const urls = [];
  const api = new MediaApi(new ApiClient({ fetchImpl: async (url, init) => {
    urls.push({ url, init });
    return new Response("audio", { headers: { "Content-Type": "audio/mp4", "Content-Length": "5",
      "Content-Disposition": "attachment; filename*=UTF-8''..%2Fsafe.m4a" } });
  } }));
  const result = await api.downloadJob(JOB, { expectedSizeBytes: 5 });
  assert.equal(result.filename, "safe.m4a");
  assert.equal(urls[0].url, `/api/v1/jobs/${ID}/download`);
  assert.equal(urls[0].init.headers.Authorization, `Bearer ${JOB.access_token}`);
  assert.ok(!urls[0].url.includes(JOB.access_token));

  const originalDocument = globalThis.document;
  const originalCreate = URL.createObjectURL;
  const originalRevoke = URL.revokeObjectURL;
  const revoked = [];
  const link = { style: {}, click() {}, remove() {} };
  globalThis.document = { createElement: () => link, body: { appendChild() {} } };
  URL.createObjectURL = () => "blob:temporary";
  URL.revokeObjectURL = (url) => revoked.push(url);
  try {
    saveDownloadedResult(result);
    assert.equal(link.download, "safe.m4a");
    assert.equal(link.href, "blob:temporary");
    await new Promise((done) => setTimeout(done, 1010));
    assert.deepEqual(revoked, ["blob:temporary"]);
  } finally {
    globalThis.document = originalDocument;
    URL.createObjectURL = originalCreate;
    URL.revokeObjectURL = originalRevoke;
  }
});

test("workflow has no credential persistence side effect", async () => {
  const originalStorage = globalThis.localStorage;
  const originalSession = globalThis.sessionStorage;
  globalThis.localStorage = { setItem() { throw new Error("localStorage must not be used"); } };
  globalThis.sessionStorage = { setItem() { throw new Error("sessionStorage must not be used"); } };
  try {
    const harness = setup();
    await submitted(harness);
    await harness.workflow.download();
    assert.equal(harness.workflow.getSnapshot().job.access_token, JOB.access_token);
  } finally {
    globalThis.localStorage = originalStorage;
    globalThis.sessionStorage = originalSession;
  }
});

test("result classification never exposes arbitrary internal error strings", () => {
  assert.deepEqual(resultError(new Error("private /data/path")),
    { issue: null, message: "The download could not be completed. Try again." });
});
