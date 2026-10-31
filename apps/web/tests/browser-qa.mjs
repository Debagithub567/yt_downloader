// Run against the local frontend and a Chromium browser with remote debugging:
// node apps/web/tests/browser-qa.mjs
// BROWSER_CDP_URL may override the default http://127.0.0.1:9223 endpoint.
import assert from "node:assert/strict";

const endpoint = process.env.BROWSER_CDP_URL ?? "http://127.0.0.1:9223";
const pages = await fetch(`${endpoint}/json`).then((response) => response.json());
const page = pages.find((item) => item.type === "page");
assert.ok(page, "a browser page must be open");
const socket = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.onopen = resolve;
  socket.onerror = reject;
});

let nextId = 1;
const pending = new Map();
socket.onmessage = ({ data }) => {
  const message = JSON.parse(data);
  if (!message.id) return;
  const request = pending.get(message.id);
  if (!request) return;
  pending.delete(message.id);
  if (message.error) request.reject(new Error(JSON.stringify(message.error)));
  else request.resolve(message.result);
};

function send(method, params = {}) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`Browser command timed out: ${method}`));
    }, 10_000);
    pending.set(id, {
      resolve: (result) => { clearTimeout(timer); resolve(result); },
      reject: (error) => { clearTimeout(timer); reject(error); },
    });
    socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await send("Runtime.evaluate", {
    expression, awaitPromise: true, returnByValue: true,
  });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
  return result.result.value;
}

async function until(expression) {
  for (let attempt = 0; attempt < 40; attempt++) {
    const result = await evaluate(expression);
    if (result) return result;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`Browser state did not appear: ${expression}`);
}

const mockedApi = `(() => {
  const originalFetch = window.fetch;
  const jobId = "a".repeat(32);
  const sourceId = "b".repeat(32);
  const media = {
    title: "QA sample", duration_seconds: 18, file_size: 10, container: "m4a",
    video_streams: [], audio_streams: [{
      index: 0, codec: "aac", sample_rate: 48000, channels: 2, bitrate: null,
    }],
  };
  const expiry = () => new Date(Date.now() + 86_400_000).toISOString();
  const json = (body, status = 200) => new Response(JSON.stringify(body), {
    status, headers: { "content-type": "application/json" },
  });
  window.__qaStage = "processing";
  window.fetch = async (resource, options = {}) => {
    const url = String(resource);
    if (url.endsWith("/v1/media/inspect")) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      return json({
        source_id: sourceId, source_token: "qa_source_token", expires_at: expiry(),
        media, video_options: [], audio_options: ["m4a", "mp3"],
      });
    }
    if (url.endsWith("/v1/jobs")) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      return json({
        job_id: jobId, access_token: "qa_job_token", status: "queued",
        status_url: "/api/v1/jobs/" + jobId,
      }, 202);
    }
    if (url.endsWith("/download")) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      return new Response(new Blob(["fake-media"], { type: "audio/mpeg" }), {
        headers: {
          "content-type": "audio/mpeg", "content-length": "10",
          "content-disposition": "attachment; filename*=UTF-8''qa-result.mp3",
        },
      });
    }
    if (url.endsWith("/cancel")) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      window.__qaStage = "cancelled";
    }
    if (url.includes("/v1/jobs/")) {
      const completed = window.__qaStage === "completed";
      return json({
        job_id: jobId, status: window.__qaStage, stage: window.__qaStage,
        progress: completed ? 100 : 0, attempt: 1,
        created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
        expires_at: expiry(), output_format: "mp3",
        ...(completed ? {
          output: { format: "mp3", size_bytes: 10, media },
          download_url: "/api/v1/jobs/" + jobId + "/download",
        } : {}),
      });
    }
    return originalFetch(resource, options);
  };
})();`;

try {
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Page.addScriptToEvaluateOnNewDocument", { source: mockedApi });
  await send("Page.reload", { ignoreCache: true });
  await until('document.querySelector("#media-url") !== null');
  await new Promise((resolve) => setTimeout(resolve, 500));

  await evaluate('document.querySelector("#media-url").focus()');
  await send("Input.insertText", { text: "https://example.com/sample.m4a" });
  await evaluate('document.querySelector(".action-row button").click()');
  const inspecting = await until('document.querySelector(".inline-status[role=status]")?.textContent');
  assert.match(inspecting, /Checking this media/);
  assert.equal(await evaluate('!!document.querySelector(".inline-status[role=status]")?.closest("[aria-busy]")'), false);
  await until('document.querySelectorAll(".format-option").length === 2');

  await evaluate('document.querySelector("input[value=mp3]").click()');
  await evaluate('document.querySelector(".output-action button").click()');
  const submitting = await until('document.querySelector(".output-action [role=status]")?.textContent || document.querySelector(".inline-status[role=status]")?.textContent?.includes("Submitting")');
  assert.ok(submitting);
  assert.equal(await evaluate('!!document.querySelector(".inline-status[role=status]")?.closest("[aria-busy]")'), false);
  await until('document.querySelector(".state-badge")?.textContent?.trim() === "Processing"');
  await evaluate('[...document.querySelectorAll("button")].find((button) => button.textContent.includes("Cancel job")).click()');
  await until('[...document.querySelectorAll(".notice-warning")].some((item) => item.textContent.includes("Cancellation requested"))');
  await until('document.querySelector(".state-badge")?.textContent?.trim() === "Cancelled"');
  await evaluate('[...document.querySelectorAll("button")].find((button) => button.textContent.includes("Use same source")).click()');
  const focused = await until('document.activeElement?.getAttribute("name") === "output-format" && document.activeElement?.value');
  assert.equal(focused, "mp3");
  assert.equal(await evaluate('getComputedStyle(document.activeElement.closest("label")).outlineStyle'), "solid");

  await evaluate('window.__qaStage = "completed"');
  await evaluate('document.querySelector(".output-action button").click()');
  await until('document.querySelector(".state-badge")?.textContent?.trim() === "Completed"');
  await evaluate('[...document.querySelectorAll("button")].find((button) => button.textContent.includes("Download result")).click()');
  const downloading = await until('[...document.querySelectorAll(".inline-status[role=status]")].some((item) => item.textContent.includes("Downloading"))');
  assert.equal(downloading, true);
  assert.equal(await evaluate('!![...document.querySelectorAll(".inline-status[role=status]")].find((item) => item.textContent.includes("Downloading"))?.closest("[aria-busy]")'), false);
  await until('[...document.querySelectorAll(".inline-status[role=status]")].some((item) => item.textContent.includes("Download started"))');
  console.log("Browser accessibility regression checks passed.");
} finally {
  socket.close();
}
