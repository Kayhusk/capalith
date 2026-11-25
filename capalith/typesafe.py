"""Direct TypeSafe HTTP transport. Credentials come only from the user's environment."""
from __future__ import annotations

import contextlib
import http.client
import json
import os
from urllib.parse import urlsplit

from capalith._ranking_errors import RankingUnavailable
from capalith.ranking import load_policy


def call(body: dict, *, timeout: float) -> dict:
    policy = load_policy()
    config = policy["typesafe"]
    key = os.environ.get(config["key_env"], "").strip()
    if not key:
        raise RankingUnavailable("not_configured")
    if any(not 33 <= ord(c) <= 126 for c in key):
        raise RankingUnavailable("configuration_error")
    endpoint = urlsplit(config["endpoint"])
    # Only the packaged policy or trusted user file owns the endpoint; no
    # search/context argument can change it. No redirects or ambient proxies.
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise RankingUnavailable("configuration_error")
    if body.get("model") != policy["model"]:
        raise RankingUnavailable("invalid_response")
    model = policy["model"].removeprefix("typesafe-ai/")
    request = {**body, "model": model}
    try:
        with contextlib.closing(http.client.HTTPSConnection(endpoint.hostname, port=endpoint.port, timeout=timeout)) as connection:
            connection.request("POST", endpoint.path, body=json.dumps(request).encode(),
                               headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            with connection.getresponse() as response:
                if response.status != 200:
                    raise RankingUnavailable("provider_error")
                raw = response.read(config["max_response_bytes"] + 1)
            if len(raw) > config["max_response_bytes"]:
                raise RankingUnavailable("invalid_response")
            try:
                answer = json.loads(raw)
            except (ValueError, UnicodeError):
                raise RankingUnavailable("invalid_response") from None
            if type(answer) is not dict or answer.get("model") != model:
                raise RankingUnavailable("invalid_response")
            # Keep the existing common ranking model identity for both routes.
            # The worker validates the closed Choice distribution and returns
            # only names/scores; arbitrary provider metadata never leaves here.
            return {"model": policy["model"], "answers": answer.get("answers")}
    except RankingUnavailable:
        raise
    except TimeoutError:
        raise RankingUnavailable("provider_timeout") from None
    except Exception:
        # HTTP/TLS errors and provider bodies may contain credentials. Never
        # include their text in public errors or a subprocess traceback.
        raise RankingUnavailable("provider_error") from None
