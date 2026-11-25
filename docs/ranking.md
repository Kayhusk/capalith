# Explicit search ranking

`discover` uses AI ranking when the user opts into the built-in TypeSafe transport with their own key, or the host supplies a managed transport through `CAPALITH_RANKING_CONFIG`. With no configuration/key, an unavailable transport, a bad response, an exhausted deadline, or an unavailable view cache, it returns the existing local ranking. The result's `ranking.path` is `ai` or `free`; a free result includes a non-sensitive `reason`. An AI abstention returns no candidates and no recommended skill.

There are no automatic prompt hints or background provider calls. A configured search sends the query, the scoped catalog's skill names and descriptions, and shortlisted instruction excerpts to the host's provider. It does not send source paths, source IDs, artifact IDs, resource files, activation state, or host transport context. Skill text is evidence for ranking, never an instruction Capalith executes.

## Bring your own TypeSafe key

1. Create a dedicated, revocable key for Capalith in the [TypeSafe console](https://console.typesafe.ai/keys). No key is shipped with Capalith and no other person's key is used. Validate it before your first search with the [free curl models check](../README.md#bring-your-own-typesafe-key), in your own trusted terminal outside the agent's shell. HTTP 200 confirms model-list access; HTTP 401 means authentication failed. No paid inference request is made.
2. Set `CAPALITH_RANKING=typesafe` and `TYPESAFE_API_KEY` only in the MCP server's own secret or environment entry. Setting only the key does not enable paid search. Reconnect after changing that entry. Never export the key into an interactive shell shared with the agent or put it in a catalog, search, JSON config or chat.
3. Run an explicit search and check `ranking.path` is `ai`. For `free`, inspect `ranking.reason`. With no explicit host-managed config, no opt-in or no key means free ranking, without a provider call or ranking cache creation. Unset `CAPALITH_RANKING` to turn this route off; a separate host-managed config still takes precedence.

The built-in transport reads the user's key from the worker environment and sends it only in the authorization header to `https://api.typesafe.ai/v1/systemone`, the [documented endpoint](https://docs.typesafe.ai/api/system-one). It uses verified HTTPS, follows no redirects, reads bounded responses and makes no automatic retries. The endpoint shown and the pinned model `jev-1.14.0` are the shipped defaults. A trusted user settings file can change them (`ranking.endpoint`, `ranking.model`: see [settings](settings.md)); a query, a project file or an environment proxy setting never can. The ranking result reports the configured model: `typesafe-ai/jev-1.14.0` with the shipped default, whichever route made the call. A returned model that differs from the configured one is rejected. Provider bodies, exception text and arbitrary response metadata are not emitted to results or logs.

A key in a shell or server environment can be read by the agent's own tools, and a malicious skill could try. Server-specific settings limit inheritance, not access by tools that can inspect the server. Monitor the dedicated key's usage and revoke it to stop further spending.

Costs come from your own TypeSafe account. A successful uncached search normally makes two requests; [the current tariff](https://docs.typesafe.ai/models) is $0.042 per million input tokens, with output tokens free. Add the input tokens for both requests: 20,000 combined tokens cost $0.00084. The [shipped policy](../capalith/ranking-policy.json) caps each request at 240,000 bytes (240 KB). At roughly 60,000 input tokens, that is about $0.0025 per request or $0.005 per two-request search. Tokenization varies: this byte cap is not an exact token or dollar cap. There is no limit on the number of searches an agent makes. Only identical queries on an unchanged catalog, scope and configuration reuse an unexpired cached view; different queries can each cost money. A timeout or failed cache write may still leave a charge. Cached continuation makes no provider request.

Spending draws on your organization's credits, and any organization member can see its key list. See [TypeSafe's organization key guidance](https://help.typesafe.ai/articles/8310295754-who-can-create-and-manage-api-keys).

Usage draws down [prepaid organization credits](https://help.typesafe.ai/articles/5187202169-how-much-does-jev-cost); an empty balance returns HTTP 402. The balance is a spending ceiling unless optional auto-recharge or arranged invoice billing applies. Invoice billing is available on request.

The built-in transport provides no money cap or usage journal. [TypeSafe documents no per-key spend limit](https://help.typesafe.ai/articles/8310295754-who-can-create-and-manage-api-keys); check Settings → Usage, and revoke the dedicated key when needed, or use a host-managed route with its own enforced budget. A valid-key search through the built-in route was not tested live before release; transport checks used a local stub.

## Host-managed configuration

Set the absolute configuration path in the environment that starts the CLI or MCP server. This explicit setting takes precedence over the built-in route, including when the built-in variables are present. An invalid explicit config falls back to free ranking; it does not switch providers. Clients which filter subprocess environments must forward the pointer, together with the host adapter's module location. This is an operator setting; a search cannot select an adapter or destination.

The configuration file and its containing directory must belong to the calling user, with neither group nor world write permission. Use a private directory and file (0700 and 0600 respectively). Other ownership or writable modes return `configuration_error` before any transport import or provider call.

```json
{
  "transport": "my_managed_transport:call",
  "context": {"key_pointer_name": "my-team"},
  "cache_dir": "/absolute/profile-owned/cache/capalith-ranking"
}
```

The transport is a Python callable importable by the same interpreter as Capalith:

```python
def call(body: dict, *, timeout: float, **options) -> dict:
    # Delegate to the host's existing managed provider client.
    # Resolve pointers inside that client; never return credentials.
    return managed_client.call(body, timeout=timeout, **options)
```

It returns the native System One JSON response. `context` supplies keyword arguments directly to the callable, so an existing managed client's function can be selected without a wrapper. It cannot override `body` or `timeout`. The host owns endpoint allowlisting, credential custody, permission and budget enforcement, and attempt/usage accounting. Configuration holds pointers, never keys. Capalith contains no host-specific credential paths, and a managed transport takes no endpoint from Capalith (the built-in transport's endpoint can be changed only in the trusted user settings file). Its worker retains the host's ownership and accounting environment, starts with Python's safe-path flag in a neutral working directory, and uses the same trusted Capalith package as the caller. Only explicit absolute host PYTHONPATH entries are retained; an empty or relative entry cannot add the caller's checkout to module lookup.

The shipped policy is [ranking-policy.json](../capalith/ranking-policy.json). It owns the pinned model, built-in TypeSafe endpoint/environment names and response bound, request limits, shortlist, excerpt length, deadline, worker grace, abstention threshold, cache count and maximum age. A ranking configuration file named by `CAPALITH_RANKING_CONFIG` may set only `transport`, `context`, `cache_dir`, `timeout_seconds` and `none_threshold`; the last two are validated, and every other key in that file is rejected. The trusted user settings file's `[ranking]` table can override more of the policy: see [settings](settings.md). Changing the threshold changes measured relevance and abstention. If a config explicitly selects `capalith.typesafe:call`, its context must be empty; credentials and endpoint options cannot be supplied that way.

The first Choice compares the descriptions of the skills it is given: every searched skill when they fit, otherwise the free search's top N for that query. N is `ranking.wide_candidates` (default 200, at most 254); the list is also trimmed from the bottom of that order until it fits the byte limits in the next paragraph, never with an error. The answer reports this under `ranking.roster`: `total` (skills searched), `sent`, and `reason`, which is null when nothing was trimmed and otherwise `wide_candidates`, `request_bytes` or both (written "wide_candidates, request_bytes"). `ranking.reason = "empty_roster"` means there was nothing to send: either the catalog is larger than `wide_candidates` and the free search listed no skill to send for that query, or not even one skill fits the request limit. The free search answers instead, and nothing is sent to the provider or charged. When only the nearest skills were sent, a no-skill answer says so: "None of the N nearest skills fits this task", not "no skill in the catalog". A second Choice reads shortlisted instructions and includes a no-match option. Its no-match probability decides abstention; the first comparison retains the ranking order. Verification reordering reduced relevance in the frozen evaluation, so it does not replace that order. Scope, duplicate-name priority, stored version identities, dependency recommendations and evidence remain the catalog's responsibility.

Before the first provider call the list is cut until all three of these fit: the first request and the second request (counted as if the `shortlist` largest skills in the list, by description plus instruction excerpt, were verified) each under `max_request_bytes`, and the worker's own payload under `max_cache_bytes`. So no paid call is spent on a search whose second request could not be sent.

When the meaning side of the free search is not in use (the meaning index is off, not built yet or out of date, or the model is unavailable), the free order holds only the skills the word search found, and a search with no word hit sends nothing and the free search answers (`empty_roster`) if the catalog is larger than `wide_candidates`; a catalog that fits is still sent whole. The word search reads each skill's name, description and instructions in `SKILL.md` and the file paths and text of its other stored files, so on a large catalog a task that matches a skill only by meaning can be missed while the meaning side is off.

## Deadline, fallback and continuation

One child process owns both provider requests under an absolute deadline set before worker startup. Each request receives only the remaining time. The parent allows the policy's short grace after that deadline so a cooperating transport can time out and finish usage accounting. A transport that ignores its deadline is killed and reaped at the end of the grace. The parent also stops and reaps a worker whose output exceeds the policy's `max_worker_output_bytes`, returning free results with `provider_error`; input and output share the same deadline. No paid request retries automatically. Local catalog retrieval and cache persistence are additional time. A timeout can still be billed by the remote provider; missing usage remains unknown spend.

Ranking views live in a separate private SQLite cache under `cache_dir`, defaulting to the current profile's XDG cache location. The catalog remains read-only during ranking. The cache binds the raw query, exact catalog state, ordered source scope, transport configuration and policy to the accepted result. It stores discovery results in clear, including the raw query; credentials and raw provider responses are excluded. Views expire at the maximum age in policy and are never served after expiry. Expired rows are removed on the next cache access with SQLite secure deletion enabled; an idle cache file remains until that access. Age expiry or bounded oldest-view eviction can make a continuation stale.

CLI invocations and MCP reconnects continue the stored order without another provider request. A changed query, source state, catalog or ranking configuration invalidates the old view. A missing, corrupt or evicted AI continuation returns `stale_view`; it never silently changes ranking. A free continuation stays free even if the provider recovers. Start a new search to request a new view.

AI search requires a writable profile-owned ranking cache even when `--db` selects a read-only catalog. With an unwritable or invalid cache, search falls back to the local ranking and reports `cache_unavailable`.

If a provider answers but persisting its accepted view fails, that answer may still be charged even though the local fallback is returned. Capalith does not retry it automatically or retain an uncommitted result; a later new search can call the provider and pay again. The host's usage ledger remains the authority for charges.

`discover` is annotated as writing and non-destructive in every connection mode because it can maintain that cache. `inspect`, `traverse` and `config_show` remain read-only.
