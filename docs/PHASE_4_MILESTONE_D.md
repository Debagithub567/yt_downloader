# Phase 4 Milestone D: interface and accessibility refinement

The home page now presents the existing workflow as four progressive steps:
choose media, review inspection, choose output, and follow the job. It keeps
the Milestone B controller and Milestone C bearer-protected download flow.
Only formats and quality choices returned or supported by the existing API
are displayed.

## Interface

The page uses a small set of shared colors, spacing, panel, field, and button
styles in `apps/web/src/app/globals.css`. Source modes and output formats show
their selected state. Loading, cancellation, retry, failure, expiry, missing
output, and completed result states have distinct messages and actions. The
Download action is primary only after backend-confirmed completion.

The layout stays in one column on narrow screens. At 960 px and above, an
active job appears beside the source and output steps. Below 640 px, actions
fill the available width. Below 390 px, mode cards, format cards, and metadata
use one column. Flexible grid tracks, wrapping metadata, and controls with
`min-width: 0` are intended to avoid horizontal overflow at 320, 375, 768,
1024, and 1440 px. These widths were reviewed statically; no browser viewport
or screenshot tool was available in this milestone.

## Accessibility

The page has a skip link, semantic headings and fieldsets, explicit labels,
visible selected states, native radio and file controls, 46 px minimum button
height, and focus indicators. Focus moves to newly revealed inspection or job
headings and back to the source heading when starting over. Validation errors
are connected to the source input when relevant. Busy actions use disabled
controls and visible loading text.

The native `<progress>` element receives only backend-reported percentages.
When no percentage is available it remains indeterminate and has explanatory
text. A dedicated live region announces job state, stage, and attempt; it does
not include the changing percentage, so ordinary polling does not repeat the
announcement. Error and download messages use their own alert or status
semantics. Transitions and the loading spinner honor `prefers-reduced-motion`.

The frontend tests verify announcement stability and readable media values.
This work is an implementation review, not a formal WCAG certification.
Browser visual and assistive-technology verification remain for Milestone E.
