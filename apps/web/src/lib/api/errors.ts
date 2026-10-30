export type ApiErrorKind =
  | "http"
  | "network"
  | "aborted"
  | "timeout"
  | "invalid_response"
  | "missing_capability"
  | "invalid_request";

const FALLBACK_MESSAGES: Record<number, string> = {
  400: "Check the media or conversion options and try again.",
  401: "A job capability is required.",
  403: "Access to this media or job was denied.",
  404: "The media or job was not found or has expired.",
  409: "This job is not ready for download.",
  410: "This media or job has expired.",
  413: "The media exceeds the allowed size.",
  415: "This upload type is not supported.",
  422: "Check the submitted fields and try again.",
  429: "The processing queue is full. Try again later.",
  500: "The media service encountered an error.",
  503: "The media service is temporarily unavailable.",
  504: "Media processing timed out. Try again.",
};

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status?: number;
  readonly code?: string;

  constructor(kind: ApiErrorKind, message: string, options: { status?: number; code?: string } = {}) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = options.status;
    this.code = options.code;
  }
}

export function safePublicMessage(value: unknown, secrets: readonly string[]): string | undefined {
  if (typeof value !== "string") return undefined;
  const message = value.trim();
  if (!message || message.length > 200 || /[\r\n\x00-\x1f]/.test(message)) return undefined;
  if (secrets.some((secret) => secret.length >= 4 && message.includes(secret))) return undefined;
  if (/traceback|stack trace|file\s+"|\/(?:app|data|users|private|tmp)\/|[a-z]:\\/i.test(message)) {
    return undefined;
  }
  return message;
}

export function httpError(status: number, body: unknown, secrets: readonly string[] = []): ApiError {
  const fallback = FALLBACK_MESSAGES[status] ??
    (status >= 500 ? "The media service is temporarily unavailable." : "The request could not be completed.");
  if (status >= 500) return new ApiError("http", fallback, { status });

  let message: string | undefined;
  let code: string | undefined;
  if (body && typeof body === "object" && "detail" in body) {
    const detail = body.detail;
    if (typeof detail === "string") {
      message = safePublicMessage(detail, secrets);
    } else if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      if ("message" in detail) message = safePublicMessage(detail.message, secrets);
      if ("code" in detail && typeof detail.code === "string" && /^[a-z][a-z0-9_]{0,63}$/.test(detail.code)) {
        code = detail.code;
      }
    }
  }
  return new ApiError("http", message ?? fallback, { status, code });
}

export const apiFailure = {
  network: () => new ApiError("network", "Could not reach the media service. Check your connection."),
  aborted: () => new ApiError("aborted", "The request was cancelled."),
  timeout: () => new ApiError("timeout", "The request timed out. Try again."),
  invalidResponse: () => new ApiError("invalid_response", "The media service returned an invalid response."),
  missingCapability: () => new ApiError("missing_capability", "The job capability is missing. Submit the job again."),
  invalidRequest: () => new ApiError("invalid_request", "The request contains an invalid identifier or value."),
};
