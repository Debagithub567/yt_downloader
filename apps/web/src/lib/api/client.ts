import { ApiError, apiFailure, httpError } from "./errors.ts";

export const DEFAULT_API_BASE_PATH = "/api";

export interface ApiClientOptions {
  /** Same-origin path handled by the Next.js rewrite. Never a backend URL. */
  basePath?: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

export interface ApiRequestOptions {
  method?: "GET" | "POST";
  body?: BodyInit;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  timeoutMs?: number;
}

function normalizeBasePath(value: string): string {
  if (!/^\/[A-Za-z0-9_-]+(?:\/[A-Za-z0-9_-]+)*\/?$/.test(value)) {
    throw apiFailure.invalidRequest();
  }
  const normalized = value.replace(/\/+$/, "");
  return normalized;
}

function endpointPath(value: string): string {
  if (!value.startsWith("/v1/") || value.includes("//") || value.includes("?") || value.includes("#") ||
      value.split("/").some((part) => part === "." || part === "..")) {
    throw apiFailure.invalidRequest();
  }
  return value;
}

function requestSecrets(options: ApiRequestOptions): string[] {
  const secrets: string[] = [];
  const authorization = options.headers?.Authorization;
  if (authorization?.startsWith("Bearer ")) secrets.push(authorization.slice(7));
  if (typeof options.body === "string") {
    try {
      const parsed: unknown = JSON.parse(options.body);
      if (parsed && typeof parsed === "object" && "source_token" in parsed &&
          typeof parsed.source_token === "string") secrets.push(parsed.source_token);
    } catch {
      // Only JSON submission bodies can carry a source capability.
    }
  }
  return secrets;
}

async function readHttpError(response: Response, secrets: readonly string[]): Promise<ApiError> {
  let body: unknown;
  const text = await response.text();
  try {
    body = JSON.parse(text);
  } catch {
    body = undefined;
  }
  return httpError(response.status, body, secrets);
}

export class ApiClient {
  readonly basePath: string;
  private readonly fetchImpl: typeof fetch;
  private readonly timeoutMs: number;

  constructor(options: ApiClientOptions = {}) {
    this.basePath = normalizeBasePath(options.basePath ?? DEFAULT_API_BASE_PATH);
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
    this.timeoutMs = options.timeoutMs ?? 120_000;
    if (!Number.isFinite(this.timeoutMs) || this.timeoutMs < 0) throw apiFailure.invalidRequest();
  }

  url(path: string): string {
    return this.basePath + endpointPath(path);
  }

  private async send<T>(path: string, options: ApiRequestOptions,
                        read: (response: Response) => Promise<T>): Promise<T> {
    const url = this.url(path);
    if (options.signal?.aborted) throw apiFailure.aborted();
    const timeoutMs = options.timeoutMs ?? this.timeoutMs;
    if (!Number.isFinite(timeoutMs) || timeoutMs < 0) throw apiFailure.invalidRequest();

    const controller = new AbortController();
    const forwardAbort = () => controller.abort();
    options.signal?.addEventListener("abort", forwardAbort, { once: true });
    let timedOut = false;
    const timer = timeoutMs > 0 ? setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs) : undefined;
    try {
      const response = await this.fetchImpl(url, {
        method: options.method ?? "GET",
        body: options.body,
        headers: options.headers,
        signal: controller.signal,
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) throw await readHttpError(response, requestSecrets(options));
      return await read(response);
    } catch (error) {
      if (timedOut) throw apiFailure.timeout();
      if (options.signal?.aborted) throw apiFailure.aborted();
      if (error instanceof ApiError) throw error;
      if (error instanceof Error && error.name === "AbortError") {
        throw apiFailure.aborted();
      }
      throw apiFailure.network();
    } finally {
      if (timer !== undefined) clearTimeout(timer);
      options.signal?.removeEventListener("abort", forwardAbort);
    }
  }

  async json<T>(path: string, options: ApiRequestOptions,
                parse: (body: unknown) => T): Promise<T> {
    return this.send(path, options, async (response) => {
      let body: unknown;
      try {
        body = JSON.parse(await response.text());
      } catch {
        throw apiFailure.invalidResponse();
      }
      return parse(body);
    });
  }

  async response<T>(path: string, options: ApiRequestOptions,
                    read: (response: Response) => Promise<T>): Promise<T> {
    return this.send(path, options, read);
  }
}
