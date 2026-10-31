"use client";

import { useEffect, useMemo, useRef, useSyncExternalStore, type ReactNode } from "react";
import { MediaApi } from "@/lib/api/media.ts";
import { formatDuration, formatFileSize, jobAnnouncement, jobStateLabel } from "@/lib/ui.ts";
import { MediaWorkflow, MP3_BITRATES, availableFormats, buildSelection, type OutputFormat } from "@/lib/workflow.ts";

const formatHelp: Record<OutputFormat, string> = {
  mp4: "Video with audio when available",
  m4a: "AAC audio",
  mp3: "Widely supported audio",
  original: "Copy the source audio",
};

function Detail({ term, children }: { term: string; children: ReactNode }) {
  return <div className="detail-item"><dt>{term}</dt><dd>{children}</dd></div>;
}

function StepMark({ number }: { number: string }) {
  return <span className="step-mark" aria-hidden="true">{number}</span>;
}

export default function Home() {
  const workflow = useMemo(() => new MediaWorkflow(new MediaApi()), []);
  const state = useSyncExternalStore(workflow.subscribe, workflow.getSnapshot, workflow.getSnapshot);
  const sourceHeading = useRef<HTMLHeadingElement>(null);
  const inspectionHeading = useRef<HTMLHeadingElement>(null);
  const jobHeading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { workflow.mount(); return () => workflow.dispose(); }, [workflow]);
  useEffect(() => { if (state.inspection) inspectionHeading.current?.focus(); }, [state.inspection]);
  useEffect(() => { if (state.job) jobHeading.current?.focus(); }, [state.job]);

  const locked = state.submitting || !!state.job;
  const formats = state.inspection ? availableFormats(state.inspection) : [];
  const selection = buildSelection(state);
  const lifecycle = state.status?.status ?? (state.job ? "queued" : null);
  const active = !state.resultIssue && (lifecycle === "queued" || lifecycle === "processing");
  const downloadReady = lifecycle === "completed" && !!state.status?.output && !state.resultIssue;
  const sourceError = !!state.error && !state.inspection && !state.job;
  const sourceInputInvalid = state.error?.startsWith("Enter an HTTPS") || state.error?.startsWith("Choose a non-empty");
  const tone = state.resultIssue || lifecycle === "failed" ? "error" : lifecycle === "completed" ? "success" : "neutral";

  return <div className="site-shell">
    <a className="skip-link" href="#main-content">Skip to main content</a>
    <header className="site-header">
      <div className="site-header-inner">
        <div className="brand"><span className="brand-mark" aria-hidden="true">M</span><span>Media Downloader</span></div>
        <span className="header-context">Direct links &amp; local files</span>
      </div>
    </header>

    <main id="main-content" className="page-main">
      <div className="hero">
        <p className="eyebrow">Media conversion workspace</p>
        <h1>Make media work in the format you need.</h1>
        <p className="hero-lead">Inspect a direct HTTPS media file or upload a local file, choose an available output, and follow the conversion as it runs.</p>
        <div className="privacy-note"><span className="note-dot" aria-hidden="true" />Job access stays in this page. Refreshing or closing it loses access to an active job; you can start again here.</div>
      </div>

      <div className={`workflow-layout${state.job ? " has-job" : ""}`}>
        <div className="workflow-column">
          <section className="panel" aria-labelledby="source-heading" aria-busy={state.inspecting}>
            <div className="section-heading"><StepMark number="01" /><div>
              <p className="section-kicker">Start here</p>
              <h2 id="source-heading" ref={sourceHeading} tabIndex={-1} className="focus-heading">Choose your media</h2>
              <p>Use a supported direct media URL or a file on your device.</p>
            </div></div>

            <fieldset className="source-modes" disabled={locked}>
              <legend>Source type</legend>
              <div className="mode-grid">
                <label className="mode-option" data-selected={state.mode === "url"}>
                  <input type="radio" name="source-mode" checked={state.mode === "url"} onChange={() => workflow.setMode("url")} />
                  <span className="mode-title">Direct URL</span><span className="mode-description">HTTPS media file</span>
                </label>
                <label className="mode-option" data-selected={state.mode === "file"}>
                  <input type="radio" name="source-mode" checked={state.mode === "file"} onChange={() => workflow.setMode("file")} />
                  <span className="mode-title">Local file</span><span className="mode-description">Choose from your device</span>
                </label>
              </div>
            </fieldset>

            {state.mode === "url" ? <div className="field-block">
              <label htmlFor="media-url" className="field-label">Direct HTTPS media URL</label>
              <input id="media-url" className="text-input" type="url" inputMode="url" autoComplete="url"
                placeholder="https://media.example.com/file.mp4" value={state.url} disabled={locked}
                aria-invalid={!!sourceInputInvalid} aria-describedby={sourceError ? "source-help source-error" : "source-help"}
                onChange={(event) => workflow.setUrl(event.target.value)} />
              <p id="source-help" className="field-help">Link directly to a supported media file. Website and watch-page URLs are not supported.</p>
            </div> : <div className="field-block">
              <label htmlFor="media-file" className="field-label">Media file</label>
              <input key={state.file ? "selected" : "empty"} id="media-file" className="file-input" type="file"
                accept="video/*,audio/*" disabled={locked} aria-invalid={!!sourceInputInvalid}
                aria-describedby={sourceError ? "file-help source-error" : "file-help"}
                onChange={(event) => workflow.setFile(event.target.files?.[0] ?? null)} />
              <p id="file-help" className="field-help">Choose an audio or video file. You can replace it before submitting a job.</p>
              {state.file && <div className="selected-file"><span className="file-icon" aria-hidden="true">▧</span>
                <span className="file-summary"><strong>{state.file.name}</strong><span>{formatFileSize(state.file.size)}</span></span>
              </div>}
            </div>}

            <div className="action-row">
              <button type="button" className="button button-primary" disabled={locked || state.inspecting}
                onClick={() => void workflow.inspect()}>
                {state.inspecting && <span className="button-spinner" aria-hidden="true" />}{state.inspecting ? "Inspecting media…" : "Inspect media"}
              </button>
              {state.inspecting && <button type="button" className="button button-secondary" onClick={() => workflow.cancelInspection()}>Stop inspection</button>}
              {!locked && (state.url || state.file) && <button type="button" className="button button-quiet" onClick={() => workflow.clearSource()}>Clear source</button>}
            </div>
            {state.inspecting && <p className="inline-status" role="status">Checking this media and its available outputs…</p>}
            {sourceError && <p id="source-error" className="notice notice-error" role="alert">{state.error}</p>}
          </section>

          {state.error && !sourceError && <p className="notice notice-error" role="alert">{state.error}</p>}

          {state.inspection && <section className="panel" aria-labelledby="inspection-heading">
            <div className="section-heading"><StepMark number="02" /><div>
              <p className="section-kicker">Inspection complete</p>
              <h2 id="inspection-heading" ref={inspectionHeading} tabIndex={-1} className="focus-heading">Media details</h2>
              <p>These details come from the inspected source.</p>
            </div></div>
            <dl className="details-grid">
              <Detail term="Title">{state.inspection.media.title}</Detail>
              <Detail term="Duration">{formatDuration(state.inspection.media.duration_seconds)}</Detail>
              <Detail term="Container">{state.inspection.media.container ?? "Unknown"}</Detail>
              <Detail term="Input size">{state.inspection.media.file_size === null ? "Unknown" : formatFileSize(state.inspection.media.file_size)}</Detail>
              <Detail term="Video streams">{state.inspection.media.video_streams.map((stream) => `${stream.codec}${stream.width && stream.height ? ` ${stream.width}×${stream.height}` : ""}`).join(", ") || "None"}</Detail>
              <Detail term="Audio streams">{state.inspection.media.audio_streams.map((stream) => stream.codec).join(", ") || "None"}</Detail>
            </dl>
          </section>}

          {state.inspection && <section className="panel" aria-labelledby="output-heading" aria-busy={state.submitting}>
            <div className="section-heading"><StepMark number="03" /><div>
              <p className="section-kicker">Your output</p>
              <h2 id="output-heading">Choose a format</h2>
              <p>Only outputs available for this source are shown.</p>
            </div></div>
            {formats.length === 0 ? <p className="notice notice-muted">No supported output is available for this source. Choose another file or link.</p> : <>
              <fieldset className="format-fieldset" disabled={locked}>
                <legend>Output format</legend>
                <div className="format-grid">
                  {formats.map((format) => <label key={format} className="format-option" data-selected={state.format === format}>
                    <input type="radio" name="output-format" value={format} checked={state.format === format}
                      onChange={() => workflow.chooseFormat(format)} />
                    <span className="format-name">{format === "original" ? "Original" : format.toUpperCase()}</span>
                    <span className="format-help">{formatHelp[format]}</span>
                  </label>)}
                </div>
              </fieldset>
              {state.format === "mp4" && <div className="field-block quality-field">
                <label htmlFor="resolution" className="field-label">Video resolution</label>
                <select id="resolution" className="select-input" value={state.resolution ?? ""} disabled={locked}
                  onChange={(event) => workflow.chooseResolution(Number(event.target.value))}>
                  {state.inspection.video_options.map((option) => <option key={option.resolution} value={option.resolution}>{option.resolution}p · {option.video_action}</option>)}
                </select>
                <p className="field-help">Available resolutions come from this media inspection.</p>
              </div>}
              {state.format === "mp3" && <div className="field-block quality-field">
                <label htmlFor="bitrate" className="field-label">MP3 bitrate</label>
                <select id="bitrate" className="select-input" value={state.bitrate} disabled={locked}
                  onChange={(event) => workflow.chooseBitrate(Number(event.target.value))}>
                  {MP3_BITRATES.map((bitrate) => <option key={bitrate} value={bitrate}>{bitrate} kbps</option>)}
                </select>
                <p className="field-help">A higher bitrate makes a larger MP3 file.</p>
              </div>}
              {(state.format === "m4a" || state.format === "original") && <p className="field-help quality-note">No additional quality setting is needed for this output.</p>}
              <div className="output-action"><button type="button" className="button button-primary" disabled={locked || !selection}
                onClick={() => void workflow.submit()}>
                {state.submitting && <span className="button-spinner" aria-hidden="true" />}{state.submitting ? "Submitting job…" : "Convert media"}
              </button><p>Conversion begins after the job is accepted.</p></div>
              {state.submitting && <p className="inline-status" role="status">Submitting your job…</p>}
            </>}
          </section>}
        </div>

        {state.job && <div className="job-column"><section className="panel job-panel" aria-labelledby="job-heading" aria-busy={state.downloading}>
          <div className="section-heading"><StepMark number="04" /><div>
            <p className="section-kicker">Your job</p>
            <h2 id="job-heading" ref={jobHeading} tabIndex={-1} className="focus-heading">Progress &amp; result</h2>
            <p>Follow the job until the backend confirms its outcome.</p>
          </div></div>
          <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">{jobAnnouncement(state)}</p>
          <div className="job-summary">
            <span className="state-badge" data-tone={tone}>{jobStateLabel(state)}</span>
            <div className="job-stage"><span>Current stage</span><strong>{(state.status?.stage ?? "queued").replaceAll("_", " ")}</strong></div>
            <div className="job-stage"><span>Attempt</span><strong>{state.status?.attempt ?? 0}</strong></div>
          </div>
          {state.status && state.status.attempt > 1 && <p className="retry-note">Retry attempt {state.status.attempt}{state.status.last_attempt_progress !== undefined ? ` · previous attempt reached ${state.status.last_attempt_progress}%` : ""}</p>}
          {active && <div className="progress-block">
            <div className="progress-label"><span>Conversion progress</span><strong>{state.status?.progress == null ? "In progress" : `${state.status.progress}%`}</strong></div>
            <progress max={100} value={state.status?.progress ?? undefined} aria-label="Conversion progress"
              aria-valuetext={state.status?.progress == null ? "Progress percentage unavailable" : undefined} />
            {state.status?.progress == null && <p className="field-help">The backend has not reported a percentage for this stage.</p>}
          </div>}
          {lifecycle === "completed" && !state.resultIssue && <p className="notice notice-success">Conversion complete. Your result is ready.</p>}
          {lifecycle === "failed" && <p className="notice notice-error" role="alert">{state.status?.error?.message ?? "Processing failed."}</p>}
          {lifecycle === "cancelled" && <p className="notice notice-muted">The backend confirmed cancellation.</p>}
          {state.cancelling && <p className="notice notice-warning">Cancellation requested. Waiting for the worker to stop…</p>}
          {state.status?.status === "completed" && state.status.output && <div className="result-card">
            <h3>Completed result</h3>
            <dl className="details-grid result-details">
              <Detail term="Format">{state.status.output.format.toUpperCase()}</Detail>
              <Detail term="File size">{formatFileSize(state.status.output.size_bytes)}</Detail>
              <Detail term="Filename">{state.downloadedFilename ?? "Provided when downloaded"}</Detail>
              <Detail term="Available until">{new Date(state.status.expires_at).toLocaleString()}</Detail>
            </dl>
          </div>}
          {state.resultIssue === "expired" && !state.downloadError && <p className="notice notice-error" role="alert">This result has expired. Start a new job.</p>}
          {state.downloadError && <p className="notice notice-error" role="alert">{state.downloadError}</p>}
          {state.pollingError && <p className="notice notice-error" role="alert">{state.pollingError}</p>}
          {state.downloading && <p className="inline-status" role="status">Downloading your result…</p>}
          {state.downloadedFilename && !state.downloadError && <p className="inline-status" role="status">Download started: {state.downloadedFilename}</p>}
          <div className="job-actions">
            {downloadReady && <button type="button" className="button button-primary" disabled={state.downloading}
              onClick={() => void workflow.download()}>
              {state.downloading && <span className="button-spinner" aria-hidden="true" />}{state.downloading ? "Downloading…" : "Download result"}
            </button>}
            {active && <button type="button" className="button button-danger" disabled={state.cancelling}
              onClick={() => void workflow.cancel()}>{state.cancelling ? "Cancelling…" : "Cancel job"}</button>}
            {state.pollingStopped && active && <button type="button" className="button button-secondary" onClick={() => workflow.retryStatus()}>Retry status updates</button>}
            {!active && state.inspection && (lifecycle === "failed" || lifecycle === "cancelled" || state.resultIssue) &&
              <button type="button" className="button button-secondary" onClick={() => { workflow.reuseSource(); sourceHeading.current?.focus(); }}>Use same source</button>}
            {!active && <button type="button" className="button button-quiet" onClick={() => { workflow.startOver(); sourceHeading.current?.focus(); }}>Start another</button>}
          </div>
          {active && !state.cancelling && <p className="job-footnote">Cancel requests stop the active job; completion may win if it finishes first.</p>}
        </section></div>}
      </div>
    </main>
    <footer className="site-footer">Process media you own or have permission to use.</footer>
  </div>;
}
