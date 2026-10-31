import assert from "node:assert/strict";
import test from "node:test";

import { ApiError } from "../src/lib/api/errors.ts";
import { MediaWorkflow, availableFormats, buildSelection } from "../src/lib/workflow.ts";

const SOURCE_ID = "b".repeat(32);
const JOB_ID = "a".repeat(32);
const job = { job_id: JOB_ID, access_token: "private_job_token", status: "queued", status_url: `/api/v1/jobs/${JOB_ID}` };
const media = { title: "Sample", duration_seconds: 18, file_size: 2048, container: "mp4",
  video_streams: [{ index: 0, codec: "h264", width: 1280, height: 720, fps: 30, bitrate: null }],
  audio_streams: [{ index: 1, codec: "aac", sample_rate: 48000, channels: 2, bitrate: null }] };
const inspection = { source_id: SOURCE_ID, source_token: "private_source_token", expires_at: "2026-10-31T00:00:00Z",
  media, video_options: [{ resolution: 720, video_action: "copy", audio_action: "copy", output_format: "mp4" },
    { resolution: 480, video_action: "transcode", audio_action: "copy", output_format: "mp4" }],
  audio_options: ["m4a", "mp3", "original"] };
const status = { job_id: JOB_ID, status: "queued", stage: "queued", progress: 0, attempt: 0,
  created_at: "2026-10-31T00:00:00Z", updated_at: "2026-10-31T00:00:00Z",
  expires_at: "2026-11-01T00:00:00Z", output_format: "mp4", resolution: 720 };

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

class FakeClock {
  pending = new Map();
  next = 1;
  set(callback, delay) { const id = this.next++; this.pending.set(id, { callback, delay }); return id; }
  clear(id) { this.pending.delete(id); }
  async tick() {
    const [id, task] = this.pending.entries().next().value ?? [];
    assert.ok(task, "expected a scheduled poll");
    this.pending.delete(id);
    task.callback();
    await new Promise((resolve) => setImmediate(resolve));
  }
  delay() { return this.pending.values().next().value?.delay; }
}

function setup(overrides = {}) {
  const calls = { inspectUrl: [], inspectUpload: [], createJob: [], getJobStatus: [], cancelJob: [] };
  const api = {
    inspectUrl: async (...args) => { calls.inspectUrl.push(args); return inspection; },
    inspectUpload: async (...args) => { calls.inspectUpload.push(args); return inspection; },
    createJob: async (...args) => { calls.createJob.push(args); return job; },
    getJobStatus: async (...args) => { calls.getJobStatus.push(args); return status; },
    cancelJob: async (...args) => { calls.cancelJob.push(args); return { ...status, status: "cancelled", stage: "cancelled" }; },
    ...overrides,
  };
  const clock = new FakeClock();
  return { workflow: new MediaWorkflow(api, clock), clock, calls, api };
}

async function inspected(workflow) {
  workflow.setUrl("https://media.example/sample.mp4");
  await workflow.inspect();
  assert.ok(workflow.getSnapshot().inspection);
}

async function submitted(workflow) {
  await inspected(workflow);
  await workflow.submit();
  assert.ok(workflow.getSnapshot().job);
}

test("URL input is kept locally and sent to inspection with an abort signal", async () => {
  const { workflow, calls } = setup();
  await inspected(workflow);
  assert.equal(calls.inspectUrl[0][0], "https://media.example/sample.mp4");
  assert.ok(calls.inspectUrl[0][1].signal instanceof AbortSignal);
});

test("file selection sends the file and name to the raw-upload API helper", async () => {
  const { workflow, calls } = setup();
  const file = Object.assign(new Blob(["media"]), { name: "sample.mp4" });
  workflow.setMode("file");
  workflow.setFile(file);
  await workflow.inspect();
  assert.equal(calls.inspectUpload[0][0], file);
  assert.equal(calls.inspectUpload[0][1], "sample.mp4");
});

test("switching source mode and clearing the file removes stale inspection data", async () => {
  const { workflow } = setup();
  await inspected(workflow);
  workflow.setMode("file");
  assert.equal(workflow.getSnapshot().inspection, null);
  workflow.setFile(Object.assign(new Blob(["x"]), { name: "x.mp3" }));
  workflow.clearSource();
  assert.equal(workflow.getSnapshot().file, null);
  assert.equal(workflow.getSnapshot().mode, "file");
});

test("invalid direct URL is rejected before an API request", async () => {
  const { workflow, calls } = setup();
  workflow.setUrl("http://example.test/watch");
  await workflow.inspect();
  assert.match(workflow.getSnapshot().error, /HTTPS/);
  workflow.setUrl("https://user:pass@example.test/media.mp4");
  await workflow.inspect();
  assert.equal(calls.inspectUrl.length, 0);
});

test("empty file is rejected before an API request", async () => {
  const { workflow, calls } = setup();
  workflow.setMode("file");
  workflow.setFile(Object.assign(new Blob([]), { name: "empty.mp4" }));
  await workflow.inspect();
  assert.match(workflow.getSnapshot().error, /non-empty/);
  assert.equal(calls.inspectUpload.length, 0);
});

test("inspection shows loading and then available options", async () => {
  const pending = deferred();
  const { workflow } = setup({ inspectUrl: () => pending.promise });
  workflow.setUrl("https://media.example/sample.mp4");
  const request = workflow.inspect();
  assert.equal(workflow.getSnapshot().inspecting, true);
  pending.resolve(inspection);
  await request;
  assert.equal(workflow.getSnapshot().inspecting, false);
  assert.deepEqual(availableFormats(workflow.getSnapshot().inspection), ["mp4", "m4a", "mp3", "original"]);
});

test("inspection failure keeps the source available for retry", async () => {
  const { workflow } = setup({ inspectUrl: async () => { throw new ApiError("network", "Connection failed."); } });
  workflow.setUrl("https://media.example/sample.mp4");
  await workflow.inspect();
  assert.equal(workflow.getSnapshot().error, "Connection failed.");
  assert.equal(workflow.getSnapshot().url, "https://media.example/sample.mp4");
});

test("changing URL aborts inspection and ignores a stale success response", async () => {
  const pending = deferred();
  let signal;
  const { workflow } = setup({ inspectUrl: (_url, control) => { signal = control.signal; return pending.promise; } });
  workflow.setUrl("https://media.example/first.mp4");
  const request = workflow.inspect();
  workflow.setUrl("https://media.example/second.mp4");
  assert.equal(signal.aborted, true);
  pending.resolve(inspection);
  await request;
  assert.equal(workflow.getSnapshot().inspection, null);
  assert.equal(workflow.getSnapshot().inspecting, false);
});

test("stop inspection reports cancellation without accepting its late result", async () => {
  const pending = deferred();
  const { workflow } = setup({ inspectUrl: () => pending.promise });
  workflow.setUrl("https://media.example/sample.mp4");
  const request = workflow.inspect();
  workflow.cancelInspection();
  pending.resolve(inspection);
  await request;
  assert.match(workflow.getSnapshot().error, /cancelled/);
  assert.equal(workflow.getSnapshot().inspection, null);
});

test("video resolution and MP3 bitrate choices produce supported payloads", async () => {
  const { workflow } = setup();
  await inspected(workflow);
  workflow.chooseResolution(480);
  assert.deepEqual(buildSelection(workflow.getSnapshot()), { output_type: "video", output_format: "mp4", resolution: 480 });
  workflow.chooseFormat("mp3");
  workflow.chooseBitrate(320);
  assert.deepEqual(buildSelection(workflow.getSnapshot()), { output_type: "audio", output_format: "mp3", audio_bitrate_kbps: 320 });
});

test("invalid format, resolution, and bitrate cannot be submitted", async () => {
  const { workflow } = setup();
  await inspected(workflow);
  workflow.chooseFormat("flac");
  workflow.chooseResolution(2160);
  assert.equal(workflow.getSnapshot().format, "mp4");
  assert.equal(workflow.getSnapshot().resolution, 720);
  workflow.chooseFormat("mp3");
  workflow.chooseBitrate(64);
  assert.equal(workflow.getSnapshot().bitrate, 192);
  assert.equal(buildSelection({ inspection, format: "mp4", resolution: 2160, bitrate: 192 }), null);
});

test("original audio is hidden when backend cannot safely copy its codec", () => {
  const unsupported = { ...inspection, media: { ...media, audio_streams: [{ ...media.audio_streams[0], codec: "ac3" }] } };
  assert.deepEqual(availableFormats(unsupported), ["mp4", "m4a", "mp3"]);
});

test("submission sends the source capability and selection, then enters queued state", async () => {
  const { workflow, calls, clock } = setup();
  await submitted(workflow);
  assert.equal(calls.createJob[0][0].source_token, inspection.source_token);
  assert.deepEqual(calls.createJob[0][1], { output_type: "video", output_format: "mp4", resolution: 720 });
  assert.equal(workflow.getSnapshot().job.access_token, job.access_token);
  assert.equal(clock.delay(), 0);
});

test("double-click submission creates only one job", async () => {
  const pending = deferred();
  let count = 0;
  const { workflow } = setup({ createJob: () => { count++; return pending.promise; } });
  await inspected(workflow);
  const first = workflow.submit();
  const second = workflow.submit();
  assert.equal(workflow.getSnapshot().submitting, true);
  assert.equal(count, 1);
  pending.resolve(job);
  await Promise.all([first, second]);
  assert.equal(workflow.getSnapshot().job.job_id, JOB_ID);
});

test("submission error preserves source and output configuration", async () => {
  const { workflow } = setup({ createJob: async () => { throw new ApiError("http", "Queue full."); } });
  await inspected(workflow);
  workflow.chooseResolution(480);
  await workflow.submit();
  assert.equal(workflow.getSnapshot().error, "Queue full.");
  assert.equal(workflow.getSnapshot().resolution, 480);
  assert.ok(workflow.getSnapshot().inspection);
});

test("queued poll uses credentials and schedules a bounded interval", async () => {
  const { workflow, clock, calls } = setup();
  await submitted(workflow);
  await clock.tick();
  assert.equal(workflow.getSnapshot().status.status, "queued");
  assert.equal(calls.getJobStatus[0][0].access_token, job.access_token);
  assert.equal(clock.delay(), 2500);
});

test("processing progress is updated without overlapping requests", async () => {
  const pending = deferred();
  let count = 0;
  const { workflow, clock } = setup({ getJobStatus: () => { count++; return pending.promise; } });
  await submitted(workflow);
  await clock.tick();
  void workflow.refreshStatus();
  assert.equal(count, 1);
  pending.resolve({ ...status, status: "processing", stage: "processing", progress: 48, attempt: 1 });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(workflow.getSnapshot().status.progress, 48);
});

test("unknown-duration progress remains null and retry metadata is preserved", async () => {
  const { workflow, clock } = setup({ getJobStatus: async () => ({ ...status, status: "processing", stage: "retrying",
    progress: null, attempt: 2, last_attempt_progress: 63 }) });
  await submitted(workflow);
  await clock.tick();
  assert.equal(workflow.getSnapshot().status.progress, null);
  assert.equal(workflow.getSnapshot().status.attempt, 2);
  assert.equal(workflow.getSnapshot().status.last_attempt_progress, 63);
});

test("completed and failed states stop polling and allow a new source", async () => {
  for (const terminal of ["completed", "failed"]) {
    const { workflow, clock } = setup({ getJobStatus: async () => ({ ...status, status: terminal, stage: terminal }) });
    await submitted(workflow);
    await clock.tick();
    assert.equal(workflow.getSnapshot().status.status, terminal);
    assert.equal(clock.pending.size, 0);
    workflow.startOver();
    assert.equal(workflow.getSnapshot().job, null);
  }
});

test("cancellation locks duplicate requests and waits for backend acknowledgement", async () => {
  const pending = deferred();
  let count = 0;
  const { workflow, clock } = setup({ cancelJob: () => { count++; return pending.promise; } });
  await submitted(workflow);
  const first = workflow.cancel();
  const second = workflow.cancel();
  assert.equal(count, 1);
  assert.equal(workflow.getSnapshot().cancelling, true);
  assert.notEqual(workflow.getSnapshot().status?.status, "cancelled");
  pending.resolve({ ...status, status: "cancelled", stage: "cancelled" });
  await Promise.all([first, second]);
  assert.equal(workflow.getSnapshot().status.status, "cancelled");
  assert.equal(clock.pending.size, 0);
});

test("nonterminal cancellation acknowledgement keeps polling until terminal", async () => {
  let count = 0;
  const { workflow, clock } = setup({ cancelJob: async () => ({ ...status, status: "processing", stage: "cancelling" }),
    getJobStatus: async () => { count++; return { ...status, status: "cancelled", stage: "cancelled" }; } });
  await submitted(workflow);
  await workflow.cancel();
  assert.equal(workflow.getSnapshot().cancelling, true);
  await clock.tick();
  assert.equal(count, 1);
  assert.equal(workflow.getSnapshot().status.status, "cancelled");
  assert.equal(workflow.getSnapshot().cancelling, false);
});

test("completion wins a cancellation race when backend returns completed", async () => {
  const { workflow, clock } = setup({ cancelJob: async () => ({ ...status, status: "completed", stage: "completed", progress: 100 }) });
  await submitted(workflow);
  await workflow.cancel();
  assert.equal(workflow.getSnapshot().status.status, "completed");
  assert.equal(clock.pending.size, 0);
});

test("cancel failure keeps status polling and allows another cancel attempt", async () => {
  let count = 0;
  const { workflow, clock } = setup({ cancelJob: async () => { count++; throw new ApiError("network", "Offline."); } });
  await submitted(workflow);
  await workflow.cancel();
  assert.equal(workflow.getSnapshot().cancelling, false);
  assert.equal(workflow.getSnapshot().error, "Offline.");
  assert.equal(clock.delay(), 0);
  await workflow.cancel();
  assert.equal(count, 2);
});

test("repeated status failures pause polling until user retries", async () => {
  const { workflow, clock } = setup({ getJobStatus: async () => { throw new ApiError("network", "Offline."); } });
  await submitted(workflow);
  for (let i = 0; i < 5; i++) await clock.tick();
  assert.equal(workflow.getSnapshot().pollingStopped, true);
  assert.equal(clock.pending.size, 0);
  workflow.retryStatus();
  assert.equal(clock.delay(), 0);
});

test("unmount aborts in-flight inspection and clears scheduled status polls", async () => {
  const pending = deferred();
  let signal;
  const { workflow } = setup({ inspectUrl: (_url, control) => { signal = control.signal; return pending.promise; } });
  workflow.setUrl("https://media.example/sample.mp4");
  const request = workflow.inspect();
  workflow.dispose();
  assert.equal(signal.aborted, true);
  pending.resolve(inspection);
  await request;
  assert.equal(workflow.getSnapshot().inspection, null);
  const next = setup();
  await submitted(next.workflow);
  assert.equal(next.clock.pending.size, 1);
  next.workflow.dispose();
  assert.equal(next.clock.pending.size, 0);
});

test("stale poll result cannot overwrite cancellation response", async () => {
  const pending = deferred();
  const { workflow, clock } = setup({ getJobStatus: () => pending.promise,
    cancelJob: async () => ({ ...status, status: "cancelled", stage: "cancelled" }) });
  await submitted(workflow);
  await clock.tick();
  await workflow.cancel();
  pending.resolve({ ...status, status: "processing", stage: "processing" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(workflow.getSnapshot().status.status, "cancelled");
});
