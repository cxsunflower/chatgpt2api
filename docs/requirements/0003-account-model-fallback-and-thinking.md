# 0003: Account model capability, fallback chains, and chat thinking

Status: pending-verification

- Created: 2026-11-06
- Owner: `AccountService`, `ModelCatalogService`, `model_selection_service`,
  `EditableFileTaskService`, Studio conversation composer
- User approval: explicit request to add per-account models, ordered model
  fallback, and a chat thinking switch.

## Outcome

The model catalog reflects models observed from each upstream account. Chat,
image, and generic file requests may move to the next configured model only
when the previous model is explicitly unavailable. A chat switch allows users
to follow the model default, enable a specific effort, or disable the thinking
parameter; the robot bridge consumes the same contract.

## Unique ownership

- `AccountService` owns each account's current model snapshot and its sync
  failure state. Account records remain JSON-backed, so the change is additive.
- `ModelCatalogService` owns the public catalog projection and account list.
- `model_selection_service` owns chain ordering and the decision that a model
  is explicitly unavailable.
- `EditableFileTaskService` continues to own file task lifecycle, ownership,
  idempotency, publication, and GET-only recovery.
- Studio owns the browser's follow/enable/disable interaction state and sends
  the existing `reasoning_effort` transport field.

## Contract

- `/api/model-catalog` schema 2 adds `account_models` and `fallback_chains`.
  Account error values are classifications, not raw exception text.
- `POST /v1/chat/completions` also accepts `output_type=file`,
  `client_task_id`, `output_filename`, `output_bundle`, and optional
  `thinking_enabled`; file responses include a `file_task` receipt.
- `POST /v1/editable-file-tasks/{id}/recover` only performs GET recovery.

## Boundaries and failures

No fallback is attempted after any streamed content or image output was
emitted. Auth, policy refusal, 429, 5xx, timeout, transport, and ordinary
invalid-input failures do not trigger model fallback. A missing model snapshot
does not exclude an existing account from scheduling. File text without a real
artifact is not a successful chat file task. No model capability is inferred
from `/v1/models` fallback names.

## Acceptance

- [x] Self-contained unit contracts for model chains, no-output boundaries,
  thinking follow/on/off, sandbox-link cleanup, quote repair, and catalog
  schema 2.
- [x] Frontend production TypeScript/Vite build succeeds.
- [x] Isolated real HTTP file generation with and without input files, thinking
  on/off, repeated client task IDs, and downloaded-byte verification.
- [x] Contract checks cover model fallback exhaustion, accepted-conversation
  boundaries, original-account recovery, and authenticated download isolation.
- [x] Production deployment, native file byte verification, and image/file QQ
  delivery receipts in the authorized test group.
- [x] Browser checks cover eight console pages, desktop/mobile layouts, and
  persisted thinking follow/on/off state using isolated data.
- [ ] Real model fallback and user acceptance remain to be verified.
