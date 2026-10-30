import { apiFailure, safePublicMessage } from "./errors.ts";
import type { AcceptedJob, AudioStream, InspectedSource, JobOutput, JobStatus, MediaInfo, VideoOption, VideoStream } from "./types.ts";

const IDENTIFIER = /^[0-9a-f]{32}$/;
const LIFECYCLES = new Set(["queued", "processing", "completed", "failed", "cancelled"]);
const REQUEST_FORMATS = new Set(["mp4", "m4a", "mp3", "original"]);
const PUBLISHED_FORMATS = new Set(["mp4", "m4a", "mp3", "flac", "ogg", "opus", "wav"]);
const AUDIO_OPTIONS = new Set(["m4a", "mp3", "original"]);

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw apiFailure.invalidResponse();
  return value as Record<string, unknown>;
}

function text(value: unknown): string {
  if (typeof value !== "string" || !value) throw apiFailure.invalidResponse();
  return value;
}

function identifier(value: unknown): string {
  const result = text(value);
  if (!IDENTIFIER.test(result)) throw apiFailure.invalidResponse();
  return result;
}

function number(value: unknown, minimum = 0): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum) {
    throw apiFailure.invalidResponse();
  }
  return value;
}

function integer(value: unknown, minimum = 0): number {
  const result = number(value, minimum);
  if (!Number.isInteger(result)) throw apiFailure.invalidResponse();
  return result;
}

function nullable<T>(value: unknown, parse: (value: unknown) => T): T | null {
  return value === null ? null : parse(value);
}

function nullableOmitted<T>(value: unknown, parse: (value: unknown) => T): T | null {
  return value === null || value === undefined ? null : parse(value);
}

function optional<T>(value: unknown, parse: (value: unknown) => T): T | undefined {
  return value === undefined ? undefined : parse(value);
}

function videoStream(value: unknown): VideoStream {
  const item = record(value);
  return {
    index: integer(item.index), codec: text(item.codec),
    width: nullableOmitted(item.width, integer), height: nullableOmitted(item.height, integer),
    fps: nullableOmitted(item.fps, number), bitrate: nullableOmitted(item.bitrate, integer),
  };
}

function audioStream(value: unknown): AudioStream {
  const item = record(value);
  return {
    index: integer(item.index), codec: text(item.codec),
    sample_rate: nullableOmitted(item.sample_rate, integer), channels: nullableOmitted(item.channels, integer),
    bitrate: nullableOmitted(item.bitrate, integer),
  };
}

function mediaInfo(value: unknown): MediaInfo {
  const item = record(value);
  if (!Array.isArray(item.video_streams) || !Array.isArray(item.audio_streams)) {
    throw apiFailure.invalidResponse();
  }
  return {
    title: text(item.title),
    duration_seconds: nullableOmitted(item.duration_seconds, number),
    file_size: nullableOmitted(item.file_size, integer),
    container: nullableOmitted(item.container, text),
    video_streams: item.video_streams.map(videoStream),
    audio_streams: item.audio_streams.map(audioStream),
  };
}

function videoOption(value: unknown): VideoOption {
  const item = record(value);
  if (item.output_format !== "mp4") throw apiFailure.invalidResponse();
  return {
    resolution: integer(item.resolution, 1),
    video_action: text(item.video_action),
    audio_action: nullable(item.audio_action, text),
    output_format: "mp4",
  };
}

export function parseInspectedSource(value: unknown): InspectedSource {
  const item = record(value);
  if (!Array.isArray(item.video_options) || !Array.isArray(item.audio_options)) {
    throw apiFailure.invalidResponse();
  }
  const audioOptions = item.audio_options.map((option) => {
    if (typeof option !== "string" || !AUDIO_OPTIONS.has(option)) throw apiFailure.invalidResponse();
    return option as InspectedSource["audio_options"][number];
  });
  return {
    source_id: identifier(item.source_id),
    source_token: text(item.source_token),
    expires_at: text(item.expires_at),
    media: mediaInfo(item.media),
    video_options: item.video_options.map(videoOption),
    audio_options: audioOptions,
  };
}

export function parseAcceptedJob(value: unknown): AcceptedJob {
  const item = record(value);
  const jobId = identifier(item.job_id);
  if (item.status !== "queued" || item.status_url !== `/api/v1/jobs/${jobId}`) {
    throw apiFailure.invalidResponse();
  }
  return {
    job_id: jobId, access_token: text(item.access_token),
    status: "queued", status_url: item.status_url,
  };
}

function jobOutput(value: unknown): JobOutput {
  const item = record(value);
  if (typeof item.format !== "string" || !PUBLISHED_FORMATS.has(item.format)) {
    throw apiFailure.invalidResponse();
  }
  return {
    format: item.format as JobOutput["format"],
    size_bytes: integer(item.size_bytes, 1), media: mediaInfo(item.media),
  };
}

export function parseJobStatus(value: unknown, secrets: readonly string[] = []): JobStatus {
  const item = record(value);
  const jobId = identifier(item.job_id);
  if (typeof item.status !== "string" || !LIFECYCLES.has(item.status) ||
      typeof item.output_format !== "string" || !REQUEST_FORMATS.has(item.output_format)) {
    throw apiFailure.invalidResponse();
  }
  const progress = nullableOmitted(item.progress, (value) => integer(value));
  if (progress !== null && progress > 100) throw apiFailure.invalidResponse();
  const lastProgress = optional(item.last_attempt_progress, (value) => integer(value));
  if (lastProgress !== undefined && lastProgress > 99) throw apiFailure.invalidResponse();
  const downloadUrl = optional(item.download_url, text);
  if (downloadUrl !== undefined && downloadUrl !== `/api/v1/jobs/${jobId}/download`) {
    throw apiFailure.invalidResponse();
  }
  const output = optional(item.output, jobOutput);
  if (item.status === "completed" && (!output || !downloadUrl)) throw apiFailure.invalidResponse();
  const failure = optional(item.error, (value) => {
    const error = record(value);
    const code = text(error.code);
    if (!/^[a-z][a-z0-9_]{0,63}$/.test(code)) throw apiFailure.invalidResponse();
    return { code, message: safePublicMessage(error.message, secrets) ?? "Media processing failed." };
  });
  return {
    job_id: jobId,
    status: item.status as JobStatus["status"],
    stage: text(item.stage),
    progress,
    attempt: integer(item.attempt),
    ...(lastProgress !== undefined ? { last_attempt_progress: lastProgress } : {}),
    created_at: text(item.created_at),
    updated_at: text(item.updated_at),
    expires_at: text(item.expires_at),
    output_format: item.output_format as JobStatus["output_format"],
    ...(item.resolution !== undefined ? { resolution: integer(item.resolution, 1) } : {}),
    ...(item.audio_bitrate_kbps !== undefined ? { audio_bitrate_kbps: integer(item.audio_bitrate_kbps, 1) } : {}),
    ...(output ? { output } : {}),
    ...(downloadUrl ? { download_url: downloadUrl } : {}),
    ...(failure ? { error: failure } : {}),
  };
}
