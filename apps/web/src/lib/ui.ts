import type { WorkflowState } from "./workflow.ts";

export function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "Unknown";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export function formatDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return "Unknown duration";
  const whole = Math.floor(seconds);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const remainder = String(whole % 60).padStart(2, "0");
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${remainder}` : `${minutes}:${remainder}`;
}

export function jobStateLabel(state: WorkflowState): string {
  if (state.resultIssue === "missing_output") return "File unavailable";
  if (state.resultIssue === "unauthorized") return "Access denied";
  if (state.resultIssue === "unavailable") return "Unavailable";
  if (state.resultIssue === "expired") return "Expired";
  if (state.status?.status === "completed") return "Completed";
  if (state.status?.status === "failed") return "Failed";
  if (state.status?.status === "cancelled") return "Cancelled";
  if (state.status?.status === "processing") return "Processing";
  return "Queued";
}

/** This text changes with state, stage, or attempt, but never on percentage-only polls. */
export function jobAnnouncement(state: WorkflowState): string {
  if (!state.job) return "";
  if (state.resultIssue) return `${jobStateLabel(state)}. The result cannot be downloaded.`;
  if (state.status?.status === "completed") return "Conversion complete. Result ready to download.";
  if (state.status?.status === "failed") return "Conversion failed.";
  if (state.status?.status === "cancelled") return "Job cancelled.";
  if (state.cancelling) return "Cancellation requested. Waiting for the worker to stop.";
  const stage = (state.status?.stage ?? "queued").replaceAll("_", " ");
  const attempt = state.status?.attempt ?? 0;
  return `${jobStateLabel(state)}. Stage ${stage}. Attempt ${attempt}.`;
}
