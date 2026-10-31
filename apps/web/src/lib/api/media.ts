import { ApiClient, type ApiRequestOptions } from "./client.ts";
import { apiFailure } from "./errors.ts";
import type { AcceptedJob, DownloadedResult, InspectedSource, JobCapability, JobSelection, JobStatus, SourceCapability } from "./types.ts";
import { parseAcceptedJob, parseInspectedSource, parseJobStatus } from "./validation.ts";

export type RequestControl = Pick<ApiRequestOptions, "signal" | "timeoutMs">;
export type DownloadControl = RequestControl & { expectedSizeBytes?: number };

const IDENTIFIER = /^[0-9a-f]{32}$/;
const TOKEN = /^[A-Za-z0-9_-]+$/;
const MEDIA_TYPES: Record<string, string> = {
  "video/mp4": "mp4",
  "audio/mp4": "m4a",
  "audio/mpeg": "mp3",
  "audio/flac": "flac",
  "audio/ogg": "ogg",
  "audio/wav": "wav",
};

function jobPath(job: JobCapability, suffix = ""): string {
  if (!IDENTIFIER.test(job.job_id)) throw apiFailure.invalidRequest();
  if (!job.access_token || !TOKEN.test(job.access_token)) throw apiFailure.missingCapability();
  return `/v1/jobs/${job.job_id}${suffix}`;
}

function authorization(job: JobCapability): Record<string, string> {
  return { Authorization: `Bearer ${job.access_token}` };
}

function headerFilename(value: string): string {
  const basename = value.replaceAll("\\", "/").split("/").at(-1) ?? "";
  const safe = basename.replace(/[^A-Za-z0-9._-]/g, "_").replace(/^\.+/, "").slice(0, 120);
  return safe || "media.bin";
}

function downloadFilename(disposition: string | null, fallback: string): string {
  const match = disposition?.match(/(?:^|;)\s*filename\*\s*=\s*UTF-8''([^;]+)/i);
  if (!match) return fallback;
  try {
    const candidate = decodeURIComponent(match[1].trim().replace(/^"|"$/g, ""));
    const basename = candidate.replaceAll("\\", "/").split("/").at(-1) ?? "";
    const safe = basename.replace(/[\x00-\x1f\x7f<>:"|?*\u202a-\u202e\u2066-\u2069]/gi, "_")
      .replace(/^\.+/, "").trim().slice(0, 160);
    return safe || fallback;
  } catch {
    return fallback;
  }
}

/** Stateless API functions. Capability objects remain with the caller in memory. */
export class MediaApi {
  private readonly client: ApiClient;

  constructor(client = new ApiClient()) {
    this.client = client;
  }

  async inspectUpload(file: Blob, filename: string, control: RequestControl = {}): Promise<InspectedSource> {
    if (!(file instanceof Blob) || file.size === 0) throw apiFailure.invalidRequest();
    return this.client.json("/v1/media/inspect", {
      method: "POST", body: file, signal: control.signal, timeoutMs: control.timeoutMs,
      headers: { "Content-Type": "application/octet-stream", "X-Filename": headerFilename(filename) },
    }, parseInspectedSource);
  }

  async inspectUrl(url: string, control: RequestControl = {}): Promise<InspectedSource> {
    if (typeof url !== "string" || !url.trim()) throw apiFailure.invalidRequest();
    return this.client.json("/v1/media/inspect", {
      method: "POST", body: JSON.stringify({ url: url.trim() }),
      headers: { "Content-Type": "application/json" },
      signal: control.signal, timeoutMs: control.timeoutMs,
    }, parseInspectedSource);
  }

  async createJob(source: SourceCapability, selection: JobSelection,
            control: RequestControl = {}): Promise<AcceptedJob> {
    if (!IDENTIFIER.test(source.source_id) || typeof source.source_token !== "string" ||
        !TOKEN.test(source.source_token)) {
      throw apiFailure.invalidRequest();
    }
    return this.client.json("/v1/jobs", {
      method: "POST",
      body: JSON.stringify({ source_id: source.source_id, source_token: source.source_token, ...selection }),
      headers: { "Content-Type": "application/json" },
      signal: control.signal, timeoutMs: control.timeoutMs,
    }, parseAcceptedJob);
  }

  async getJobStatus(job: JobCapability, control: RequestControl = {}): Promise<JobStatus> {
    const result = await this.client.json(jobPath(job), {
      headers: authorization(job), signal: control.signal, timeoutMs: control.timeoutMs,
    }, (body) => parseJobStatus(body, [job.access_token]));
    if (result.job_id !== job.job_id) throw apiFailure.invalidResponse();
    return result;
  }

  async cancelJob(job: JobCapability, control: RequestControl = {}): Promise<JobStatus> {
    const result = await this.client.json(jobPath(job, "/cancel"), {
      method: "POST", headers: authorization(job),
      signal: control.signal, timeoutMs: control.timeoutMs,
    }, (body) => parseJobStatus(body, [job.access_token]));
    if (result.job_id !== job.job_id) throw apiFailure.invalidResponse();
    return result;
  }

  async downloadJob(job: JobCapability, control: DownloadControl = {}): Promise<DownloadedResult> {
    const expected = control.expectedSizeBytes;
    if (expected !== undefined && (!Number.isSafeInteger(expected) || expected <= 0)) {
      throw apiFailure.invalidRequest();
    }
    return this.client.response(jobPath(job, "/download"), {
      headers: authorization(job), signal: control.signal, timeoutMs: control.timeoutMs,
    }, async (response) => {
      const contentType = response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase();
      if (!contentType || !(contentType in MEDIA_TYPES)) throw apiFailure.invalidResponse();
      const length = response.headers.get("content-length");
      if (expected !== undefined && (length === null || !/^\d+$/.test(length) || Number(length) !== expected)) {
        throw apiFailure.invalidResponse();
      }
      const blob = await response.blob();
      if (blob.size === 0) throw apiFailure.invalidResponse();
      if (length !== null && (!/^\d+$/.test(length) || Number(length) !== blob.size)) {
        throw apiFailure.invalidResponse();
      }
      if (expected !== undefined && blob.size !== expected) throw apiFailure.invalidResponse();
      const fallback = `download-${job.job_id}.${MEDIA_TYPES[contentType]}`;
      return {
        blob, filename: downloadFilename(response.headers.get("content-disposition"), fallback),
        contentType, sizeBytes: blob.size,
      };
    });
  }
}

/** Call only after a user action in a browser client component. */
export function saveDownloadedResult(result: DownloadedResult): void {
  if (typeof document === "undefined" || typeof URL.createObjectURL !== "function") {
    throw apiFailure.invalidRequest();
  }
  const objectUrl = URL.createObjectURL(result.blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = result.filename;
  link.style.display = "none";
  try {
    document.body.appendChild(link);
    link.click();
  } finally {
    link.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1_000);
  }
}
