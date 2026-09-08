# Error and retry semantics

Errors use the stable `ErrorCode` enum from `codito_protocol.errors`. The human
message is safe, concise, and non-oracular. Machine decisions use the code and
`retryable`, never message parsing.

| Class | Representative codes | Retry rule |
|---|---|---|
| Authentication/authorization | `unauthenticated`, `unauthorized`, `resource_mismatch`, `scope_missing` | Reauthorize/fix grant; never blind retry |
| Availability/admission | `device_offline`, `queue_full`, `rate_limited`, `deadline_exceeded` | Reads may retry with backoff/new deadline; mutations are new user actions |
| Filesystem/path | `path_invalid`, `path_outside_project`, `path_changed`, `reparse_point_forbidden`, `hardlink_mutation_forbidden` | Correct request or local registration; never weaken checks |
| Content/patch | `hash_mismatch`, `patch_invalid`, `patch_conflict`, `idempotency_conflict` | Re-read and create a new exact patch/key as appropriate |
| Approval/sandbox | `approval_required`, `approval_denied`, `approval_expired`, `sandbox_unavailable` | Requires local user/control recovery, not model assertion |
| Shell | `shell_not_found`, `output_limit_exceeded`, `cancelled`, `outcome_unknown` | `outcome_unknown` is never replayed automatically |
| Internal | `internal_error` | Log correlation ID, return no implementation secrets |

Authentication failures follow OAuth/MCP HTTP challenge semantics before tool-level
JSON. Foreign opaque IDs return the same tenant-scoped not-found class as nonexistent
IDs. `details` is allowlisted by code and must not contain raw exception text,
absolute paths, commands, content, tokens, or output.

## Local-to-wire mapping

Agent and relay modules may use precise internal reason names. Only the following
canonical codes cross MCP/tunnel/audit boundaries. Mapping occurs once at the
boundary; do not grow the public enum merely to expose implementation structure.

| Local/internal condition(s) | Canonical wire code | Notes |
|---|---|---|
| `invalid_config`, `invalid_ipc_request`, `invalid_cursor`, `invalid_glob`, `unknown_tool`, `protocol_error` | `invalid_request` | Protocol detail is logged locally after redaction |
| `authorization_context_missing`, `binding_mismatch` | `unauthorized` | Never reveal which foreign binding exists |
| `action_digest_mismatch` | `invalid_request` | Also emit a security audit event; never execute |
| `ticket_expired`, `login_required`, `not_enrolled`, `enrollment_required` | `unauthenticated` | Caller must refresh/login/enroll as applicable |
| `device_not_found`, `project_disabled`, `project_unavailable`, `invalid_project` | `project_not_found` or `device_offline` | Select without creating a cross-tenant existence oracle |
| `device_queue_full` | `queue_full` | Retryable only where operation replay policy permits |
| `operation_timeout` | `deadline_exceeded` | Shell start uncertainty still overrides to `outcome_unknown` |
| `operation_in_progress` | `idempotency_conflict` | Return the existing stable operation ID only to the same full binding |
| `invalid_path`, `unsafe_path`, `invalid_project_root` | `path_invalid` | Keep rejected absolute text out of remote error details |
| `path_not_found`, `not_a_file`, `not_a_directory` | `file_not_found` | Context-safe message distinguishes type only within authorized project |
| `path_race`, `project_identity_changed` | `path_changed` | Disable project/write as local policy requires |
| `unsafe_hardlink` | `hardlink_mutation_forbidden` | No approval override |
| `unsupported_encoding`, `binary_file` | `encoding_unsupported` | A bounded binary metadata result may be a future version |
| `invalid_patch` | `patch_invalid` | Syntax/precondition shape failure |
| `conflict` | `patch_conflict` | Include only bounded structured relative-path/hunk facts |
| `invalid_approval` | `approval_denied` | Audit replay/mismatch reason locally; do not expose signing detail |
| `job_not_found`, `operation_not_found` | `shell_not_found` | Tenant/project-bound lookup first |
| `result_limit` | `output_limit_exceeded` | Preserve truncation metadata when safe |
| `broker_unavailable`, `broker_failed`, `broker_protocol_error` | `sandbox_unavailable` | Isolated execution fails closed; native mode is never an implicit fallback |
| `read_failed`, `recovery_failed`, unexpected exception | `internal_error` | Non-retryable mutation until local reconciliation |
| `stale_connection` | `cancelled` | Relay records stale-epoch security reason; socket is closed |

Mapping cannot change `retryable` blindly. The operation class and acknowledgment
boundary determine retry safety: a read can often retry, a patch only under its
idempotency/hash proof, and an uncertain shell start never retries automatically.
