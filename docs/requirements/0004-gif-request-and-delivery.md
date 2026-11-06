# 0004: GIF request controls and verified animated delivery

Status: implementing

- Created: 2026-11-06
- Owner: image protocol, ImageTaskService, ModelCatalogService, Studio composer
- Approval: user requested continued implementation after reviewing GIF API references.

## Outcome and evidence

Users can request GIF output and optional frame timing through synchronous and
asynchronous image interfaces without silently losing parameters. Studio exposes
the same controls and consumes the server's allowed-model projection. A static
image must never be presented as a successful animation.

Direct Images requests already accepted GIF options. Image Tasks previously dropped
frame_count and frame_duration_ms, and Studio lacked output-format controls. Real requests
to gpt-image-2.6, flare and sunburst have returned static output; accepting a GIF
request is not evidence that the upstream can produce it. Native file generation
has produced a real 12-frame animation as an explicit way to produce animated artifacts,
not an automatic retry after a consumed image generation.

## Scope and ownership

- The image protocol owns format constraints, optional prompt hints and result
  validation; ImageTaskService retains queue admission and lifecycle ownership.
- ModelCatalogService projects configured GIF permission, not a claim of proven
  upstream animation capability. Studio owns drafts, selection and rendering.
- Carry optional parameters through generation/edit task submission and queue
  spooling. Preserve omission rather than inventing frame defaults.
- Preserve genuine GIF bytes; reject static files and do not switch accounts
  because an upstream result has the wrong format.
- Existing repositories, authorization, image storage and task recovery remain
  authoritative. No new persistence owner or database migration is required.
- Generic Studio files reuse chat file submission and existing task polling;
  filenames and all published assets remain visible. File attempts update account
  last-use on failure too; a new request may rotate, an accepted task never replays.
- The bot routes explicit GIF production directly to native files, validates
  distinct decoded frames and timing, then delivers original bytes as an image.
  Persona/reference ownership and existing platform receipt rules remain in force.
- SVG, Gemini protocol expansion and unrelated integrations are outside this task.

## Acceptance

- [x] Public synchronous/asynchronous generation and edit contracts preserve GIF
  options; omitted options remain absent, and static formats ignore frame hints.
- [x] Studio exposes format/frame controls and consumes allowed models from the
  catalog; switching away from GIF does not send stale frame hints.
- [x] Desktop/mobile controls are usable and no page-level overflow is added.
- [ ] Real animated output is verified by decoded frames and distinct content;
  bot delivery requires actual platform receipts. Record the generation method.
- [x] Static-output failures remain explicit and do not trigger paid replay.
- [ ] Documentation, production build, deployment and rollback evidence agree.

## Verification and rollback

Use isolated HTTP contract tests, a bounded real upstream probe, frontend build,
browser checks and the authorized QQ test group. Record user acceptance separately.
Deploy only after candidate checks, with the existing service-scoped Compose and
source backups. Restoring the previous image/source does not overwrite user data.

## Unresolved

The native image upstream has not produced a real animation in current samples.
Verify a viable generation path before declaring GIF delivery complete.

Agent checks: 42 focused Python cases, image/file frontend runtime cases,
production frontend build, eight-route desktop smoke and 390px Studio checks.
Bot candidate: 52 SDK compatibility checks plus 13 GIF worker checks. Full Python
collection is blocked by five existing tests requiring absent fixture paths or
`PRODUCTION_PORT_ISOLATED_ROOT`; the temporary UI application copy is excluded.
Production deployment and QQ delivery are pending. User acceptance is separate.
