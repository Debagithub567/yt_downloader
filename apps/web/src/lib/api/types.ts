/** Wire types from the Phase 3 FastAPI responses. */

export interface VideoStream {
  index: number;
  codec: string;
  width: number | null;
  height: number | null;
  fps: number | null;
  bitrate: number | null;
}

export interface AudioStream {
  index: number;
  codec: string;
  sample_rate: number | null;
  channels: number | null;
  bitrate: number | null;
}

export interface MediaInfo {
  title: string;
  duration_seconds: number | null;
  file_size: number | null;
  container: string | null;
  video_streams: VideoStream[];
  audio_streams: AudioStream[];
}

export interface VideoOption {
  resolution: number;
  video_action: string;
  audio_action: string | null;
  output_format: "mp4";
}

export interface InspectedSource {
  source_id: string;
  source_token: string;
  expires_at: string;
  media: MediaInfo;
  video_options: VideoOption[];
  audio_options: Array<"m4a" | "mp3" | "original">;
}

export type SourceCapability = Pick<InspectedSource, "source_id" | "source_token">;

export type JobSelection =
  | { output_type: "video"; output_format: "mp4"; resolution: number; audio_bitrate_kbps?: never }
  | { output_type: "audio"; output_format: "m4a" | "original"; resolution?: never; audio_bitrate_kbps?: never }
  | { output_type: "audio"; output_format: "mp3"; resolution?: never; audio_bitrate_kbps: number };

export type CreateJobRequest = SourceCapability & JobSelection;

export interface AcceptedJob {
  job_id: string;
  access_token: string;
  status: "queued";
  status_url: string;
}

/** Keep this object in memory and pass it to status, cancellation, and download. */
export type JobCapability = Pick<AcceptedJob, "job_id" | "access_token">;

export type JobLifecycle = "queued" | "processing" | "completed" | "failed" | "cancelled";
export type PublishedFormat = "mp4" | "m4a" | "mp3" | "flac" | "ogg" | "opus" | "wav";

export interface JobOutput {
  format: PublishedFormat;
  size_bytes: number;
  media: MediaInfo;
}

export interface JobFailure {
  code: string;
  message: string;
}

export interface JobStatus {
  job_id: string;
  status: JobLifecycle;
  stage: string;
  progress: number | null;
  attempt: number;
  last_attempt_progress?: number;
  created_at: string;
  updated_at: string;
  expires_at: string;
  output_format: JobSelection["output_format"];
  resolution?: number;
  audio_bitrate_kbps?: number;
  output?: JobOutput;
  download_url?: string;
  error?: JobFailure;
}

export interface DownloadedResult {
  blob: Blob;
  filename: string;
  contentType: string;
  sizeBytes: number;
}
