import assert from "node:assert/strict";
import test from "node:test";

import { ApiClient } from "../src/lib/api/client.ts";
import { ApiError } from "../src/lib/api/errors.ts";
import { MediaApi, saveDownloadedResult } from "../src/lib/api/media.ts";

const JOB_ID = "a".repeat(32);
const OTHER_ID = "c".repeat(32);
const SOURCE_ID = "b".repeat(32);
const JOB = { job_id: JOB_ID, access_token: "private_job_token" };
const SOURCE = { source_id: SOURCE_ID, source_token: "private_source_token" };
const TIME = "2026-10-30T12:00:00+00:00";

const media = {
  title: "Tone", duration_seconds: 4, file_size: 6000, container: "m4a",
  video_streams: [], audio_streams: [{
    index: 0, codec: "aac", sample_rate: 48000, channels: 1, bitrate: null,
  }],
};
const inspection = {
  ...SOURCE, expires_at: TIME, media, video_options: [],
  audio_options: ["m4a", "mp3", "original"],
};
const accepted = {
  ...JOB, status: "queued", status_url: `/api/v1/jobs/${JOB_ID}`,
};
const queued = {
  job_id: JOB_ID, status: "queued", stage: "queued", progress: 0, attempt: 0,
  created_at: TIME, updated_at: TIME, expires_at: TIME, output_format: "m4a",
};

function json(body, status = 200) {
  return Response.json(body, { status });
}

function setup(response = json(queued), options = {}) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    return typeof response === "function" ? response(url, init) : response;
  };
  return { api: new MediaApi(new ApiClient({ fetchImpl, ...options })), calls };
}

function isKind(kind) {
  return (error) => error instanceof ApiError && error.kind === kind;
}

test("same-origin API base path is centralized and cannot be an external URL", () => {
  assert.equal(new ApiClient().url("/v1/jobs"), "/api/v1/jobs");
  assert.equal(new ApiClient({ basePath: "/proxy/api/" }).url("/v1/jobs"), "/proxy/api/v1/jobs");
  for (const basePath of ["https://example.test/api", "//example.test/api", "/api/../other", "/api?token=x"]) {
    assert.throws(() => new ApiClient({ basePath }), isKind("invalid_request"));
  }
});

test("URL inspection sends the real JSON request and parses capabilities", async () => {
  const { api, calls } = setup(json(inspection, 201));
  const result = await api.inspectUrl(" https://media.example/tone.m4a ");
  assert.equal(result.source_id, SOURCE_ID);
  assert.equal(result.media.audio_streams[0].codec, "aac");
  assert.deepEqual(result.audio_options, ["m4a", "mp3", "original"]);
  assert.equal(calls[0].url, "/api/v1/media/inspect");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(calls[0].init.body), { url: "https://media.example/tone.m4a" });
});

test("file inspection uses raw octet-stream, not multipart, and sanitizes its header", async () => {
  const { api, calls } = setup(json(inspection, 201));
  const file = new Blob(["media"], { type: "audio/mp4" });
  await api.inspectUpload(file, "../../unsafe\nname.m4a");
  assert.equal(calls[0].init.body, file);
  assert.equal(calls[0].init.headers["Content-Type"], "application/octet-stream");
  assert.equal(calls[0].init.headers["X-Filename"], "unsafe_name.m4a");
  assert.equal(calls[0].url, "/api/v1/media/inspect");
});

test("job submission sends JSON and keeps the returned capability paired with its ID", async () => {
  const { api, calls } = setup(json(accepted, 202));
  const result = await api.createJob(SOURCE, {
    output_type: "audio", output_format: "mp3", audio_bitrate_kbps: 128,
  });
  assert.deepEqual(result, accepted);
  assert.equal(calls[0].url, "/api/v1/jobs");
  assert.equal(calls[0].init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    ...SOURCE, output_type: "audio", output_format: "mp3", audio_bitrate_kbps: 128,
  });
});

test("status uses the bearer header and parses attempt and retry progress", async () => {
  const retrying = { ...queued, status: "processing", stage: "processing", attempt: 2,
    last_attempt_progress: 62 };
  delete retrying.progress; // FastAPI excludes a null progress field.
  const { api, calls } = setup(json(retrying));
  const result = await api.getJobStatus(JOB);
  assert.equal(result.progress, null);
  assert.equal(result.last_attempt_progress, 62);
  assert.equal(result.attempt, 2);
  assert.equal(calls[0].url, `/api/v1/jobs/${JOB_ID}`);
  assert.equal(calls[0].init.headers.Authorization, `Bearer ${JOB.access_token}`);
  assert.ok(!calls[0].url.includes(JOB.access_token));
  assert.ok(!calls[0].url.includes("?"));
});

test("completed status parses output and rejects a token-bearing download URL", async () => {
  const completed = { ...queued, status: "completed", stage: "completed", attempt: 1,
    progress: 100, output: { format: "m4a", size_bytes: 6000, media },
    download_url: `/api/v1/jobs/${JOB_ID}/download` };
  const { api } = setup(json(completed));
  const result = await api.getJobStatus(JOB);
  assert.equal(result.output.size_bytes, 6000);
  assert.equal(result.download_url, completed.download_url);
  const unsafe = setup(json({ ...completed, download_url: `${completed.download_url}?token=secret` }));
  await assert.rejects(unsafe.api.getJobStatus(JOB), isKind("invalid_response"));
});

test("cancellation posts with authorization and accepts a terminal response", async () => {
  const cancelled = { ...queued, status: "cancelled", stage: "cancelled", attempt: 1,
    error: { code: "cancelled", message: "Processing was cancelled" } };
  const { api, calls } = setup(json(cancelled));
  assert.equal((await api.cancelJob(JOB)).status, "cancelled");
  assert.equal(calls[0].url, `/api/v1/jobs/${JOB_ID}/cancel`);
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers.Authorization, `Bearer ${JOB.access_token}`);
  assert.equal(calls[0].init.body, undefined);
});

test("terminal job error messages cannot expose paths or the capability", async () => {
  for (const message of ["File /data/private/output.mp3 failed", `Rejected ${JOB.access_token}`]) {
    const failed = { ...queued, status: "failed", stage: "failed", attempt: 1,
      error: { code: "internal_error", message } };
    const { api } = setup(json(failed));
    const result = await api.getJobStatus(JOB);
    assert.equal(result.error.message, "Media processing failed.");
  }
});

test("download uses the bearer header, validates metadata, and sanitizes the filename", async () => {
  const data = new Blob(["sound"], { type: "audio/mpeg" });
  const response = new Response(data, { status: 200, headers: {
    "Content-Type": "audio/mpeg", "Content-Length": String(data.size),
    "Content-Disposition": "attachment; filename*=UTF-8''..%2Fsafe.mp3",
  } });
  const { api, calls } = setup(response);
  const result = await api.downloadJob(JOB);
  assert.equal(result.filename, "safe.mp3");
  assert.equal(result.contentType, "audio/mpeg");
  assert.equal(result.sizeBytes, 5);
  assert.equal(await result.blob.text(), "sound");
  assert.equal(calls[0].url, `/api/v1/jobs/${JOB_ID}/download`);
  assert.equal(calls[0].init.headers.Authorization, `Bearer ${JOB.access_token}`);
  assert.ok(!calls[0].url.includes(JOB.access_token));
});

test("download rejects wrong file size and unexpected content type", async () => {
  const mismatch = setup(new Response("abc", { headers: {
    "Content-Type": "audio/mpeg", "Content-Length": "9",
  } }));
  await assert.rejects(mismatch.api.downloadJob(JOB), isKind("invalid_response"));
  const html = setup(new Response("<html/>", { headers: { "Content-Type": "text/html" } }));
  await assert.rejects(html.api.downloadJob(JOB), isKind("invalid_response"));
});

test("browser save helper uses a temporary object URL and attachment filename", () => {
  const previousDocument = globalThis.document;
  const link = { href: "", download: "", style: {}, clicked: false, removed: false,
    click() { this.clicked = true; }, remove() { this.removed = true; } };
  globalThis.document = {
    createElement: (tag) => { assert.equal(tag, "a"); return link; },
    body: { appendChild: (element) => assert.equal(element, link) },
  };
  try {
    saveDownloadedResult({ blob: new Blob(["audio"]), filename: "safe.mp3",
      contentType: "audio/mpeg", sizeBytes: 5 });
    assert.ok(link.href.startsWith("blob:"));
    assert.equal(link.download, "safe.mp3");
    assert.ok(link.clicked && link.removed);
  } finally {
    globalThis.document = previousDocument;
  }
});

test("missing capability fails before fetch", async () => {
  const { api, calls } = setup();
  await assert.rejects(api.getJobStatus({ job_id: JOB_ID, access_token: "" }), isKind("missing_capability"));
  await assert.rejects(api.cancelJob({ job_id: JOB_ID, access_token: "" }), isKind("missing_capability"));
  await assert.rejects(api.downloadJob({ job_id: JOB_ID, access_token: "" }), isKind("missing_capability"));
  assert.equal(calls.length, 0);
});

test("mismatched job IDs and malformed success responses are rejected", async () => {
  await assert.rejects(setup(json({ ...queued, job_id: OTHER_ID })).api.getJobStatus(JOB),
    isKind("invalid_response"));
  await assert.rejects(setup(new Response("{", { status: 200 })).api.getJobStatus(JOB),
    isKind("invalid_response"));
  await assert.rejects(setup(json({ ...queued, status: "unknown" })).api.getJobStatus(JOB),
    isKind("invalid_response"));
});

test("HTTP errors retain safe details and normalize the supported status codes", async () => {
  const structured = setup(json({ detail: { code: "invalid_source", message: "Unsupported target resolution" } }, 400));
  await assert.rejects(structured.api.getJobStatus(JOB), (error) =>
    error instanceof ApiError && error.status === 400 && error.code === "invalid_source" &&
    error.message === "Unsupported target resolution");

  for (const status of [401, 403, 404, 409, 410, 413, 415, 422, 429, 500, 503, 504]) {
    const response = status === 422 ? { detail: [{ loc: ["body", "source_id"], msg: "Field required" }] }
      : { detail: "Request failed" };
    const { api } = setup(json(response, status));
    await assert.rejects(api.getJobStatus(JOB), (error) =>
      error instanceof ApiError && error.kind === "http" && error.status === status && !!error.message);
  }
});

test("server paths and stack traces are not shown as frontend errors", async () => {
  for (const status of [400, 500, 503]) {
    const { api } = setup(json({ detail: { code: "internal_error",
      message: 'Traceback File "/app/media_api/routes.py" /data/private' } }, status));
    await assert.rejects(api.getJobStatus(JOB), (error) =>
      error instanceof ApiError && !error.message.includes("/app/") && !error.message.includes("/data/"));
  }
});

test("backend error details cannot echo job or source capability tokens", async () => {
  const jobEcho = setup(json({ detail: `Invalid ${JOB.access_token}` }, 400));
  await assert.rejects(jobEcho.api.getJobStatus(JOB), (error) =>
    error instanceof ApiError && !error.message.includes(JOB.access_token));
  const sourceEcho = setup(json({ detail: { message: `Invalid ${SOURCE.source_token}` } }, 403));
  await assert.rejects(sourceEcho.api.createJob(SOURCE,
    { output_type: "audio", output_format: "m4a" }), (error) =>
    error instanceof ApiError && !error.message.includes(SOURCE.source_token));
});

test("network failures do not expose raw errors", async () => {
  const { api } = setup(() => { throw new Error("private internal host and token"); });
  await assert.rejects(api.getJobStatus(JOB), (error) =>
    error instanceof ApiError && error.kind === "network" && !error.message.includes("token"));
});

test("external request abort propagates through fetch", async () => {
  const controller = new AbortController();
  const { api } = setup((_url, init) => new Promise((_resolve, reject) => {
    init.signal.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
  }));
  const waiting = api.getJobStatus(JOB, { signal: controller.signal });
  controller.abort();
  await assert.rejects(waiting, isKind("aborted"));
});

test("request timeout is distinct from user abort", async () => {
  const { api } = setup((_url, init) => new Promise((_resolve, reject) => {
    init.signal.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
  }), { timeoutMs: 5 });
  await assert.rejects(api.getJobStatus(JOB), isKind("timeout"));
});

test("invalid media and source capability are rejected locally", async () => {
  const { api, calls } = setup();
  await assert.rejects(api.inspectUpload(new Blob([]), "empty.m4a"), isKind("invalid_request"));
  await assert.rejects(api.createJob({ source_id: SOURCE_ID, source_token: "" },
    { output_type: "audio", output_format: "m4a" }), isKind("invalid_request"));
  assert.equal(calls.length, 0);
});
