# Phase 4 Milestone B: media submission and live progress

The home page now connects the Milestone A typed API client to a browser workflow.
Users can inspect a direct HTTPS media URL or a local file, review returned media
details, choose an available output, submit a job, watch its status, and request
cancellation. Download controls are reserved for Milestone C.

The UI calls `MediaApi.inspectUrl`, `inspectUpload`, `createJob`, `getJobStatus`,
and `cancelJob`. Local uploads continue to use the backend's raw
`application/octet-stream` contract. URL inspection still depends on the
backend's direct-media host allowlist; website and watch-page URLs are not
supported. No capabilities are stored persistently or placed in URLs.

`apps/web/src/lib/workflow.ts` owns the browser state. It discards late
inspection responses after a source change, blocks duplicate submissions and
cancellation requests, and discards status responses superseded by a
cancellation response. Polling makes one status request at a time, waits 2.5
seconds after a successful active response, and backs off after errors. After
five consecutive errors it pauses until the user chooses **Retry status
updates**. Polling stops on a terminal response or component unmount.

Output formats come from the inspection response. MP4 resolutions come from
`video_options`. MP3 bitrate choices match the backend's fixed
`EngineConfig.mp3_bitrates_kbps` (128, 192, 320). Original audio is hidden
when the first audio stream codec is not in the backend's safe copy list.

The interface treats the backend's response as authoritative for cancellation:
it keeps observing processing after a nonterminal acknowledgement and also
accepts completion when it wins a cancellation race. Unknown progress is
shown as indeterminate; retries show the real attempt and previous progress.

Capabilities are held only in the current page's memory. Refreshing or closing
the page loses access to an active job, and an ambiguous submission disrupted
by navigation may continue on the server without a recoverable capability.
Account-backed recovery is outside this milestone. Browser-to-backend testing
through the Next.js proxy is deferred to Milestone E.
