import { ApiError } from "./api/errors.ts";
import type { AcceptedJob, InspectedSource, JobSelection, JobStatus } from "./api/types.ts";
import type { MediaApi } from "./api/media.ts";

export type SourceMode = "url" | "file";
export type OutputFormat = JobSelection["output_format"];
export const MP3_BITRATES = [128, 192, 320] as const; // media_core.engine.EngineConfig
const COPYABLE_AUDIO = new Set(["aac", "mp3", "flac", "vorbis", "opus", "pcm_s16le"]);

export interface WorkflowState {
  mode: SourceMode;
  url: string;
  file: File | null;
  inspecting: boolean;
  submitting: boolean;
  cancelling: boolean;
  inspection: InspectedSource | null;
  format: OutputFormat | null;
  resolution: number | null;
  bitrate: number;
  job: AcceptedJob | null;
  status: JobStatus | null;
  error: string | null;
  pollingError: string | null;
  pollingStopped: boolean;
}

export interface WorkflowApi {
  inspectUrl: MediaApi["inspectUrl"];
  inspectUpload: MediaApi["inspectUpload"];
  createJob: MediaApi["createJob"];
  getJobStatus: MediaApi["getJobStatus"];
  cancelJob: MediaApi["cancelJob"];
}

export interface PollClock {
  set(callback: () => void, delay: number): ReturnType<typeof setTimeout>;
  clear(id: ReturnType<typeof setTimeout>): void;
}

const realClock: PollClock = { set: (callback, delay) => setTimeout(callback, delay), clear: clearTimeout };
const terminal = (status: JobStatus["status"]) =>
  status === "completed" || status === "failed" || status === "cancelled";

export function availableFormats(source: InspectedSource): OutputFormat[] {
  return [
    ...(source.video_options.length ? ["mp4" as const] : []),
    ...source.audio_options.filter((format) => format !== "original" ||
      COPYABLE_AUDIO.has(source.media.audio_streams[0]?.codec)),
  ];
}

export function buildSelection(state: Pick<WorkflowState, "inspection" | "format" | "resolution" | "bitrate">): JobSelection | null {
  const { inspection, format, resolution, bitrate } = state;
  if (!inspection || !format || !availableFormats(inspection).includes(format)) return null;
  if (format === "mp4") {
    return resolution !== null && inspection.video_options.some((option) => option.resolution === resolution)
      ? { output_type: "video", output_format: "mp4", resolution } : null;
  }
  if (format === "mp3") {
    return MP3_BITRATES.some((option) => option === bitrate)
      ? { output_type: "audio", output_format: "mp3", audio_bitrate_kbps: bitrate } : null;
  }
  return { output_type: "audio", output_format: format };
}

function message(error: unknown): string {
  return error instanceof ApiError ? error.message : "The request could not be completed. Try again.";
}

function validDirectUrl(value: string): boolean {
  try {
    const url = new URL(value.trim());
    return url.protocol === "https:" && !!url.hostname && !url.username && !url.password;
  } catch {
    return false;
  }
}

export class MediaWorkflow {
  private state: WorkflowState = {
    mode: "url", url: "", file: null, inspecting: false, submitting: false,
    cancelling: false, inspection: null, format: null, resolution: null,
    bitrate: 192, job: null, status: null, error: null, pollingError: null,
    pollingStopped: false,
  };
  private readonly listeners = new Set<() => void>();
  private sourceVersion = 0;
  private jobVersion = 0;
  private inspectionController: AbortController | null = null;
  private statusController: AbortController | null = null;
  private cancelController: AbortController | null = null;
  private pollTimer: ReturnType<typeof setTimeout> | null = null;
  private polling = false;
  private pollFailures = 0;
  private active = true;
  private readonly api: WorkflowApi;
  private readonly clock: PollClock;

  constructor(api: WorkflowApi, clock: PollClock = realClock) {
    this.api = api;
    this.clock = clock;
  }

  getSnapshot = (): WorkflowState => this.state;
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };

  mount(): void { this.active = true; }

  dispose(): void {
    this.active = false;
    this.sourceVersion++;
    this.jobVersion++;
    this.inspectionController?.abort();
    this.statusController?.abort();
    this.cancelController?.abort();
    this.clearPoll();
  }

  private set(patch: Partial<WorkflowState>): void {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) listener();
  }

  private canEditSource(): boolean {
    return !this.state.submitting && !this.state.job;
  }

  private clearInspection(): void {
    this.sourceVersion++;
    this.inspectionController?.abort();
    this.inspectionController = null;
    this.set({ inspecting: false, inspection: null, format: null, resolution: null, error: null });
  }

  setMode(mode: SourceMode): void {
    if (!this.canEditSource() || mode === this.state.mode) return;
    this.clearInspection();
    this.set({ mode, url: "", file: null });
  }

  setUrl(url: string): void {
    if (!this.canEditSource() || this.state.url === url) return;
    this.clearInspection();
    this.set({ url });
  }

  setFile(file: File | null): void {
    if (!this.canEditSource()) return;
    this.clearInspection();
    this.set({ file });
  }

  clearSource(): void {
    if (!this.canEditSource()) return;
    this.clearInspection();
    this.set({ url: "", file: null });
  }

  cancelInspection(): void {
    if (!this.state.inspecting) return;
    this.clearInspection();
    this.set({ error: "Inspection was cancelled." });
  }

  async inspect(): Promise<void> {
    if (this.state.inspecting || !this.canEditSource()) return;
    const { mode, url, file } = this.state;
    if (mode === "url" ? !validDirectUrl(url) : !file || file.size === 0) {
      this.set({ error: mode === "url" ? "Enter an HTTPS direct media URL without credentials." : "Choose a non-empty media file." });
      return;
    }
    const version = ++this.sourceVersion;
    const controller = new AbortController();
    this.inspectionController = controller;
    this.set({ inspecting: true, error: null, inspection: null, format: null, resolution: null });
    try {
      const result = mode === "url"
        ? await this.api.inspectUrl(url.trim(), { signal: controller.signal })
        : await this.api.inspectUpload(file!, file!.name, { signal: controller.signal });
      if (!this.active || version !== this.sourceVersion) return;
      const format = availableFormats(result)[0] ?? null;
      this.set({ inspection: result, format, resolution: format === "mp4" ? result.video_options[0].resolution : null });
    } catch (error) {
      if (this.active && version === this.sourceVersion) this.set({ error: message(error) });
    } finally {
      if (this.active && version === this.sourceVersion) this.set({ inspecting: false });
      if (this.inspectionController === controller) this.inspectionController = null;
    }
  }

  chooseFormat(format: OutputFormat): void {
    const inspection = this.state.inspection;
    if (!inspection || !availableFormats(inspection).includes(format) || this.state.job || this.state.submitting) return;
    this.set({ format, resolution: format === "mp4" ? inspection.video_options[0].resolution : null, error: null });
  }

  chooseResolution(resolution: number): void {
    if (this.state.format !== "mp4" || this.state.job || this.state.submitting ||
        !this.state.inspection?.video_options.some((option) => option.resolution === resolution)) return;
    this.set({ resolution, error: null });
  }

  chooseBitrate(bitrate: number): void {
    if (this.state.format !== "mp3" || this.state.job || this.state.submitting ||
        !MP3_BITRATES.some((option) => option === bitrate)) return;
    this.set({ bitrate, error: null });
  }

  async submit(): Promise<void> {
    if (this.state.submitting || this.state.job || this.state.inspecting) return;
    const selection = buildSelection(this.state);
    if (!selection) { this.set({ error: "Choose an available output format and quality." }); return; }
    const source = this.state.inspection!;
    const version = ++this.jobVersion;
    this.set({ submitting: true, error: null, pollingError: null });
    try {
      const job = await this.api.createJob(source, selection);
      if (!this.active || version !== this.jobVersion) return;
      this.set({ job, status: null, pollingStopped: false });
      this.schedulePoll(0);
    } catch (error) {
      if (this.active && version === this.jobVersion) this.set({ error: message(error) });
    } finally {
      if (this.active && version === this.jobVersion) this.set({ submitting: false });
    }
  }

  private clearPoll(): void {
    if (this.pollTimer !== null) this.clock.clear(this.pollTimer);
    this.pollTimer = null;
  }

  private schedulePoll(delay: number): void {
    this.clearPoll();
    if (!this.active || !this.state.job || this.state.pollingStopped ||
        (this.state.status && terminal(this.state.status.status))) return;
    this.pollTimer = this.clock.set(() => {
      this.pollTimer = null;
      void this.refreshStatus();
    }, delay);
  }

  async refreshStatus(): Promise<void> {
    if (this.polling) { this.schedulePoll(250); return; }
    if (!this.active || !this.state.job || this.state.pollingStopped ||
        (this.state.status && terminal(this.state.status.status))) return;
    this.clearPoll();
    this.polling = true;
    const job = this.state.job;
    const version = this.jobVersion;
    const controller = new AbortController();
    this.statusController = controller;
    try {
      const status = await this.api.getJobStatus(job, { signal: controller.signal });
      if (!this.active || version !== this.jobVersion) return;
      this.pollFailures = 0;
      this.set({ status, pollingError: null, cancelling: terminal(status.status) ? false : this.state.cancelling });
    } catch (error) {
      if (!this.active || version !== this.jobVersion || controller.signal.aborted) return;
      this.pollFailures++;
      if (this.pollFailures >= 5) {
        this.set({ pollingError: "Status updates paused after repeated errors. Retry to continue.", pollingStopped: true });
      } else {
        this.set({ pollingError: message(error) });
      }
    } finally {
      this.polling = false;
      if (this.statusController === controller) this.statusController = null;
      if (this.active && version === this.jobVersion && !this.state.pollingStopped) {
        this.schedulePoll(this.pollFailures ? Math.min(15_000, 2_500 * 2 ** (this.pollFailures - 1)) : 2_500);
      }
    }
  }

  retryStatus(): void {
    if (!this.state.job || !this.state.pollingStopped) return;
    this.pollFailures = 0;
    this.set({ pollingStopped: false, pollingError: null });
    this.schedulePoll(0);
  }

  async cancel(): Promise<void> {
    const job = this.state.job;
    if (!job || this.state.cancelling || (this.state.status && terminal(this.state.status.status))) return;
    this.set({ cancelling: true, error: null });
    this.clearPoll();
    this.statusController?.abort();
    const version = ++this.jobVersion;
    const controller = new AbortController();
    this.cancelController = controller;
    try {
      const status = await this.api.cancelJob(job, { signal: controller.signal });
      if (!this.active || version !== this.jobVersion) return;
      this.pollFailures = 0;
      this.set({ status, cancelling: !terminal(status.status), pollingStopped: false, pollingError: null });
    } catch (error) {
      if (this.active && version === this.jobVersion) {
        this.set({ cancelling: false, error: message(error), pollingStopped: false });
      }
    } finally {
      if (this.cancelController === controller) this.cancelController = null;
      if (this.active && version === this.jobVersion) this.schedulePoll(0);
    }
  }

  startOver(): void {
    if (this.state.submitting || this.state.cancelling ||
        (this.state.job && (!this.state.status || !terminal(this.state.status.status)))) return;
    this.jobVersion++;
    this.sourceVersion++;
    this.inspectionController?.abort();
    this.statusController?.abort();
    this.clearPoll();
    this.pollFailures = 0;
    this.set({ mode: "url", url: "", file: null, inspecting: false, submitting: false,
      cancelling: false, inspection: null, format: null, resolution: null, bitrate: 192,
      job: null, status: null, error: null, pollingError: null, pollingStopped: false });
  }
}
