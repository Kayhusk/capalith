# Capalith operation reference

The connected Capalith server's schemas define the accepted arguments. This reference describes the fields in this release. If the live schema differs, follow it and report the difference only when it blocks the task.

## `discover`

Use `discover` to find skills for a natural-language task.

Required input:

- `query`: a non-empty task description.

Optional input:

- `source_ids`: unique source IDs in the selected priority order.
- `limit`: 1 through 50. The default is 5.
- `offset`: a non-negative page offset. The default is 0.
- `view_id`: the `d1:` identifier returned by the first page when continuing that result set.

Read only the fields needed for the task:

- `candidates`: matches with `name`, `source_id`, `artifact_id`, `content_digest`, `bundle_path`, evidence, and declared relationships.
- `recommendation.status`: `ready`, `blocked`, or `not_found`.
- `recommendation.skills`: the best match and any unambiguous required skills, with requirements first. Each item includes `name`, `source_id`, `artifact_id`, `content_digest`, and `skill_path`.
- `recommendation.uncertainty`: skills missing from the search index, invalid metadata or files, unresolved requirements, conflicts, or cycles.
- `retrieval.has_more`, `retrieval.next_offset`, and `retrieval.view_id`: pagination fields.

Request another page only when needed. Send the same `query` and `source_ids` with the returned `next_offset` and `view_id`. Changing the query, selected sources, catalog, retrieval method, model, or ranking rules can make the view stale. On `stale_view`, start again at offset 0 without the old view ID.

An empty candidate page is valid. Do not add sources unless the user or task permits it.

## `inspect`

Use `inspect` when the task needs exact stored metadata, a resource list, or one stored resource.

Required input:

- `artifact_id`: use an exact ID from Capalith output or explicit task input.
- `content_digest`: use the exact digest paired with that ID.

Optional input:

- `resource_path`: use an exact canonical relative POSIX path from Capalith output or explicit task input. Omit it to inspect the artifact summary and resource list.

Do not shorten or rebuild these values. Do not guess a resource path. Inspection reads the stored copy; it does not reopen the source directory.

## `traverse`

Use `traverse` when declared relationships affect the task.

Required input:

- `artifact_id` and `content_digest`: use one exact pair from a current `ready` result.

Optional input:

- `source_ids`: unique enabled source IDs in the caller's priority order. If supplied, include the source that contains the starting artifact.
- `relationship_types`: any of `requires`, `complements`, `alternatives`, `conflicts`, or `supersedes`.
- `direction`: `outbound`, `inbound`, or `both`. The default is `outbound`.
- `depth`: 1 through 3. The default is 1.
- `limit`: 1 through 100. The default is 10.
- `offset`: a non-negative page offset.
- `view_id`: the `t1:` identifier returned by the first traversal page.

Request another relationship page only when needed. Keep the starting `artifact_id`, `content_digest`, selected sources, relationship filters, direction, and depth unchanged. Send the returned `next_offset` and `view_id`. Keep any missing or ambiguous relationship information. Do not infer an artifact ID from a target name.

## `config_show`

`config_show` takes no input. Use it when the task needs source IDs, source status, or available Capalith features. It reports Capalith state, not host configuration or permission to change a source.

## Pass a result to the host

Do this only when the host exposes a skill reader or loader and the task requires selected content.

- Use only `ready` recommendations, and review `recommendation.uncertainty` before choosing one.
- Keep each recommended skill's `name`, `source_id`, `artifact_id`, `content_digest`, and `skill_path` together.
- Follow the host tool's current schema. Pass a returned name or relative path only when that schema accepts it.
- If the host cannot use the returned name or path without guessing, stop. Do not substitute a machine path or an unrelated skill with the same name.

Capalith returns IDs and stored data. The agent decides whether to use Capalith and which tools to call. The host decides whether selected content can be read, loaded, or run.

## Errors

- `invalid_request`: reread the live operation schema and correct the input. Do not retry unchanged.
- `stale_view`: restart the search or traversal without the old pagination fields if the task still needs the result set.
- `stale_artifact`: the `artifact_id` and `content_digest` are no longer current. Use `discover` if the task needs the current version.
- `artifact_not_found`: no current artifact matches the ID. Use `discover` with the same selected sources if the task needs a replacement. Do not invent an ID.
- `resource_not_found`: inspect the artifact summary and choose a returned path, or stop.
- `source_unavailable`: report the unavailable source. Do not enable or scan a source from this skill.
- `database_unavailable`: report that the configured catalog cannot be read. Do not create or migrate it.
- `internal_error`: report `internal_error`. Do not expose or infer database details.
