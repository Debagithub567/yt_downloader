"use client";

import { useEffect, useMemo, useSyncExternalStore } from "react";
import { MediaApi } from "@/lib/api/media.ts";
import { MediaWorkflow, MP3_BITRATES, availableFormats, buildSelection, type OutputFormat } from "@/lib/workflow.ts";

const inputClass = "mt-2 w-full rounded-lg border border-stone-300 bg-white px-3 py-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700 disabled:bg-stone-100";
const buttonClass = "rounded-lg bg-teal-800 px-4 py-2 font-semibold text-white hover:bg-teal-900 disabled:cursor-not-allowed disabled:opacity-50";
const size = (bytes: number) => bytes < 1024 * 1024 ? `${Math.ceil(bytes / 1024)} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
const duration = (seconds: number | null) => seconds === null ? "Unknown" : `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;

export default function Home() {
  const workflow = useMemo(() => new MediaWorkflow(new MediaApi()), []);
  const state = useSyncExternalStore(workflow.subscribe, workflow.getSnapshot, workflow.getSnapshot);
  useEffect(() => { workflow.mount(); return () => workflow.dispose(); }, [workflow]);

  const locked = state.submitting || !!state.job;
  const formats = state.inspection ? availableFormats(state.inspection) : [];
  const selection = buildSelection(state);
  const lifecycle = state.status?.status ?? (state.job ? "queued" : null);
  const active = lifecycle === "queued" || lifecycle === "processing";

  return <main className="mx-auto max-w-3xl space-y-8 px-5 py-10 sm:px-8 sm:py-14">
    <header className="space-y-3">
      <p className="text-sm font-semibold uppercase tracking-widest text-teal-800">Media Downloader</p>
      <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">Prepare your media</h1>
      <p className="text-stone-600">Use a supported direct HTTPS media link or a local file you are allowed to process. Website and watch-page links are not supported.</p>
    </header>

    <section className="rounded-xl border border-stone-200 bg-white p-5 shadow-sm sm:p-7" aria-labelledby="source-heading">
      <h2 id="source-heading" className="text-xl font-semibold">1. Choose a source</h2>
      <fieldset className="mt-4 flex gap-6" disabled={locked}>
        <legend className="sr-only">Source type</legend>
        <label className="flex items-center gap-2"><input type="radio" name="source-mode" checked={state.mode === "url"} onChange={() => workflow.setMode("url")} />Direct URL</label>
        <label className="flex items-center gap-2"><input type="radio" name="source-mode" checked={state.mode === "file"} onChange={() => workflow.setMode("file")} />Local file</label>
      </fieldset>
      {state.mode === "url" ? <div className="mt-5">
        <label htmlFor="media-url" className="font-medium">Direct HTTPS media URL</label>
        <input id="media-url" type="url" inputMode="url" placeholder="https://media.example.com/file.mp4" className={inputClass}
          value={state.url} disabled={locked} onChange={(event) => workflow.setUrl(event.target.value)} />
      </div> : <div className="mt-5">
        <label htmlFor="media-file" className="font-medium">Media file</label>
        <input key={state.file ? "selected" : "empty"} id="media-file" type="file" accept="video/*,audio/*" className={inputClass} disabled={locked}
          onChange={(event) => workflow.setFile(event.target.files?.[0] ?? null)} />
        {state.file && <p className="mt-2 text-sm text-stone-600">{state.file.name} · {size(state.file.size)}</p>}
      </div>}
      <div className="mt-5 flex flex-wrap items-center gap-3">
        <button type="button" className={buttonClass} disabled={locked || state.inspecting} onClick={() => void workflow.inspect()}>{state.inspecting ? "Inspecting…" : "Inspect media"}</button>
        {state.inspecting && <button type="button" className="rounded-lg border px-4 py-2" onClick={() => workflow.cancelInspection()}>Stop inspection</button>}
        {!locked && (state.url || state.file) && <button type="button" className="rounded-lg border px-4 py-2" onClick={() => workflow.clearSource()}>Clear source</button>}
      </div>
      {state.inspecting && <p className="mt-3 text-sm text-stone-600" role="status">Inspecting media. This may take a moment.</p>}
    </section>

    {state.error && <p role="alert" className="rounded-lg border border-red-200 bg-red-50 p-4 text-red-900">{state.error}</p>}

    {state.inspection && <section className="rounded-xl border border-stone-200 bg-white p-5 shadow-sm sm:p-7" aria-labelledby="inspection-heading">
      <h2 id="inspection-heading" className="text-xl font-semibold">2. Media details</h2>
      <dl className="mt-4 grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
        <div><dt className="text-stone-500">Title</dt><dd className="break-words font-medium">{state.inspection.media.title}</dd></div>
        <div><dt className="text-stone-500">Duration</dt><dd className="font-medium">{duration(state.inspection.media.duration_seconds)}</dd></div>
        <div><dt className="text-stone-500">Container</dt><dd className="font-medium">{state.inspection.media.container ?? "Unknown"}</dd></div>
        <div><dt className="text-stone-500">Input size</dt><dd className="font-medium">{state.inspection.media.file_size === null ? "Unknown" : size(state.inspection.media.file_size)}</dd></div>
        <div><dt className="text-stone-500">Video streams</dt><dd className="font-medium">{state.inspection.media.video_streams.map((s) => `${s.codec}${s.width && s.height ? ` ${s.width}×${s.height}` : ""}`).join(", ") || "None"}</dd></div>
        <div><dt className="text-stone-500">Audio streams</dt><dd className="font-medium">{state.inspection.media.audio_streams.map((s) => s.codec).join(", ") || "None"}</dd></div>
      </dl>
      <div className="mt-6 border-t border-stone-200 pt-5">
        <h3 className="font-semibold">3. Choose output</h3>
        {formats.length === 0 ? <p className="mt-2 text-sm text-stone-600">This source has no supported output options.</p> : <>
          <label htmlFor="output-format" className="mt-4 block font-medium">Format</label>
          <select id="output-format" className={inputClass} value={state.format ?? ""} disabled={locked}
            onChange={(event) => workflow.chooseFormat(event.target.value as OutputFormat)}>
            {formats.map((format) => <option key={format} value={format}>{format === "original" ? "Original audio" : format.toUpperCase()}</option>)}
          </select>
          {state.format === "mp4" && <>
            <label htmlFor="resolution" className="mt-4 block font-medium">Video resolution</label>
            <select id="resolution" className={inputClass} value={state.resolution ?? ""} disabled={locked}
              onChange={(event) => workflow.chooseResolution(Number(event.target.value))}>
              {state.inspection.video_options.map((o) => <option key={o.resolution} value={o.resolution}>{o.resolution}p · {o.video_action}</option>)}
            </select>
          </>}
          {state.format === "mp3" && <>
            <label htmlFor="bitrate" className="mt-4 block font-medium">MP3 bitrate</label>
            <select id="bitrate" className={inputClass} value={state.bitrate} disabled={locked}
              onChange={(event) => workflow.chooseBitrate(Number(event.target.value))}>
              {MP3_BITRATES.map((bitrate) => <option key={bitrate} value={bitrate}>{bitrate} kbps</option>)}
            </select>
          </>}
          <button type="button" className={`${buttonClass} mt-5`} disabled={locked || !selection} onClick={() => void workflow.submit()}>{state.submitting ? "Submitting…" : "Convert media"}</button>
          {state.submitting && <p className="mt-2 text-sm text-stone-600" role="status">Submitting your job…</p>}
        </>}
      </div>
    </section>}

    {state.job && <section className="rounded-xl border border-stone-200 bg-white p-5 shadow-sm sm:p-7" aria-labelledby="job-heading">
      <h2 id="job-heading" className="text-xl font-semibold">Job progress</h2>
      <div className="mt-4 space-y-2" role="status" aria-live="polite">
        <p className="font-medium capitalize">{lifecycle}</p>
        <p className="text-sm text-stone-600">Stage: {(state.status?.stage ?? "queued").replaceAll("_", " ")} · Attempt {state.status?.attempt ?? 0}</p>
        {state.status && state.status.attempt > 1 && <p className="text-sm text-amber-800">Retry attempt {state.status.attempt}{state.status.last_attempt_progress !== undefined ? ` · previous attempt reached ${state.status.last_attempt_progress}%` : ""}</p>}
        {active && <>
          <progress className="h-3 w-full accent-teal-700" max={100} value={state.status?.progress ?? undefined} aria-label="Job progress" />
          <p className="text-sm text-stone-600">{state.status?.progress == null ? "Progress percentage unavailable" : `${state.status.progress}% complete`}</p>
        </>}
        {lifecycle === "completed" && <p className="text-teal-800">Conversion complete. Download controls will be added in the next milestone.</p>}
        {lifecycle === "failed" && <p className="text-red-800">{state.status?.error?.message ?? "Processing failed."}</p>}
        {lifecycle === "cancelled" && <p className="text-stone-700">The backend confirmed cancellation.</p>}
        {state.cancelling && <p className="text-amber-800">Cancellation requested. Waiting for the worker to stop…</p>}
      </div>
      {state.pollingError && <p role="alert" className="mt-4 text-sm text-red-800">{state.pollingError}</p>}
      <div className="mt-5 flex flex-wrap items-center gap-3">
        {active && <button type="button" className="rounded-lg border border-red-700 px-4 py-2 font-semibold text-red-800 disabled:opacity-50"
          disabled={state.cancelling} onClick={() => void workflow.cancel()}>{state.cancelling ? "Cancelling…" : "Cancel job"}</button>}
        {state.pollingStopped && active && <button type="button" className={buttonClass} onClick={() => workflow.retryStatus()}>Retry status updates</button>}
        {!active && <button type="button" className={buttonClass} onClick={() => workflow.startOver()}>Start another</button>}
      </div>
      {active && !state.cancelling && <p className="mt-2 text-xs text-stone-500">Cancel requests stop the active job; completion may win if it finishes first.</p>}
    </section>}
  </main>;
}
