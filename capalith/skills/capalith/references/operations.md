# Capalith operation reference

Use the connected Capalith server's schemas as the source of truth for accepted arguments. The fields below describe this package. Follow the live schema, and report a mismatch only when it blocks the task.

## `discover`

Use `discover` when you need candidates for a natural-language task.

Required input:

- `query`: a non-empty task description.

Optional input:

- `source_ids`: unique source IDs in caller-selected priority order.
- `limit`: 1 through 50. The default is 5.
- `offset`: a non-negative page offset. The default is 0.
- `view_id`: the `d1:` identifier returned by the first page when continuing that result set.

Read the fields needed for the choice:

- `candidates`: matches with `name`, `source_id`, `artifact_id`, `content_digest`, `bundle_path`, evidence, and declared relationships.
- `recommendation.status`: `ready`, `blocked`, or `not_found`.
- `recommendation.skills`: the primary match and uniquely resolved requirements in dependency-first order. Each item includes `name`, `source_id`, `artifact_id`, `content_digest`, and `skill_path`.
- `recommendation.uncertainty`: missing coverage, unresolved requirements, conflicts, or cycles.
- `retrieval.has_more`, `retrieval.next_offset`, and `retrieval.view_id`: continuation state.

Continue only when another page is useful. Send the same `query` and `source_ids` with the returned `next_offset` and `view_id`. A changed query, source scope, catalog state, retrieval method, model identity, or ranking rule can make the view stale. On `stale_view`, start again at offset 0 without the old view ID.

An empty candidate page is a valid result. Do not widen the source scope unless the user or task permits it.

## `inspect`

Use `inspect` when the task needs exact stored metadata, a resource list, or one stored resource.

Required input:

- `artifact_id`: use an exact ID from Capalith output or explicit task input.
- `content_digest`: use the exact digest paired with that ID.

Optional input:

- `resource_path`: use an exact canonical relative POSIX path from Capalith output or explicit task input. Omit it to inspect the artifact summary and resource list.

Do not shorten, normalize, or reconstruct either identity. Do not guess a resource path. Inspection reads the stored snapshot; it does not reopen the source directory.

## `traverse`

Use `traverse` when declared relationships affect the task.

Required input:

- `artifact_id` and `content_digest`: one exact current pair for a ready catalog artifact.

Optional input:

- `source_ids`: unique enabled source IDs in caller-selected priority order. If supplied, include the root artifact's source.
- `relationship_types`: any of `requires`, `complements`, `alternatives`, `conflicts`, or `supersedes`.
- `direction`: `outbound`, `inbound`, or `both`. The default is `outbound`.
- `depth`: 1 through 3. The default is 1.
- `limit`: 1 through 100. The default is 10.
- `offset`: a non-negative page offset.
- `view_id`: the `t1:` identifier returned by the first traversal page.

Continue only when another relationship page is useful. Keep the root identity, source scope, relationship filters, direction, and depth unchanged. Send the returned `next_offset` and `view_id`. Preserve missing and ambiguous relationship evidence. Do not infer an artifact identity from a target name.

## `config_show`

`config_show` takes no input. Use it when configured source IDs, source states, or Capalith's read-only capability report affect the task. It reports Capalith state, not host configuration or permission to change a source.

## Optional host handoff

Use a host handoff only when the host exposes a native skill reader or loader and the task requires selected content.

- Review `recommendation.status` and `recommendation.uncertainty` before choosing a recommendation.
- Keep each recommended skill's `name`, `source_id`, `artifact_id`, `content_digest`, and `skill_path` together.
- Use `inspect` first only when the host needs a returned resource path or the task needs exact stored evidence.
- Follow the host operation's live schema. Pass a returned name or relative path only when that schema accepts it.
- If the host cannot map the identity safely, stop. Do not guess a machine path or read an unrelated skill with the same name.

Capalith returns catalog identities. The agent decides whether the task needs Capalith and which operations to call. The host decides whether selected content is visible, loadable, or runnable.

## Errors

- `invalid_request`: reread the live operation schema and correct the input. Do not retry unchanged.
- `stale_view`: if the task still needs the result set, restart the search or traversal without the old continuation state.
- `stale_artifact`: the identity pair is no longer current. Use `discover` only if the task still needs a fresh identity.
- `artifact_not_found`: no current artifact matches the ID. Use `discover` in the existing scope only if the task still needs a replacement. Do not invent an ID.
- `resource_not_found`: inspect the artifact summary and choose a returned path, or stop.
- `source_unavailable`: report the unavailable source. Do not enable, rescan, or replace it from this skill.
- `database_unavailable`: report that the configured catalog cannot be read. Do not create or migrate it.
- `internal_error`: report `internal_error`. Do not expose or infer database details.
