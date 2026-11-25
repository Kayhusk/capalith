from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from capalith.intake import scan_local_source
from capalith.store import Store
from tests.test_catalog import make_catalog_skill
from tests.test_ranking import ADAPTER


# Only the test child imports this stub. Verify its trusted endpoint choice
# before redirecting the socket to the local synthetic provider.
STUB = '''
import http.client, os
class EndpointStub(http.client.HTTPConnection):
    def __init__(self, host, port=None, timeout=None, **kwargs):
        assert host == os.environ.get("CAPALITH_TEST_HOST", "api.typesafe.ai") and port in (None, 443)
        if os.environ.get("CAPALITH_TEST_ERROR"):
            raise OSError(os.environ["TYPESAFE_API_KEY"])
        super().__init__("127.0.0.1", int(os.environ["CAPALITH_TEST_PORT"]), timeout=timeout)
http.client.HTTPSConnection = EndpointStub
'''


class TypeSafeTests(unittest.TestCase):
    def test_trusted_user_endpoint_and_key_name_reach_only_the_configured_connection(self):
        with self.search() as (root, store, seen, token):
            config = root / "settings.toml"
            config.write_text('[ranking]\nendpoint = "https://gateway.example.invalid/custom"\nkey_env = "FIXTURE_PROVIDER_KEY"\n')
            config.chmod(0o600)
            with patch.dict(os.environ, {"CAPALITH_CONFIG":str(config),"CAPALITH_TEST_HOST":"gateway.example.invalid","FIXTURE_PROVIDER_KEY":token}):
                result = self.cli(store)
            self.assertEqual(0,result.returncode,result.stderr)
            self.assertEqual("ai",json.loads(result.stdout)["ranking"]["path"])
            self.assertEqual(2,len(seen))
            self.assertTrue(all(r["authenticated"] and r["path"]=="/custom" for r in seen))
            self.assertNotIn(token,result.stdout+result.stderr)

    @contextlib.contextmanager
    def search(self, mode="normal"):
        token = "synthetic-typesafe-fixture-token"
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append({"path": self.path, "authenticated": self.headers.get("Authorization") == "Bearer " + token, "body": body})
                if mode in ("http_error", "redirect"):
                    self.send_response(401 if mode == "http_error" else 302)
                    if mode == "redirect":
                        self.send_header("Location", "https://untrusted.invalid/steal")
                    payload = json.dumps({"error": token}).encode()
                elif mode == "oversized":
                    self.send_response(200)
                    payload = b"x" * 2_000_001
                elif mode == "invalid_json":
                    self.send_response(200)
                    payload = token.encode()
                else:
                    self.send_response(200)
                    values = {name: .01 for name in body["questions"]["which"]["criteria"]}
                    if "__none_applies__" in values:
                        values.update({"alpha": .8, "__none_applies__": .1})
                    else:
                        values.update({"zeta": .9, "beta": .07, "alpha": .03})
                    total = sum(values.values())
                    probabilities = {n: v / total for n, v in values.items()}
                    payload = json.dumps({"model": token if mode == "invalid_model" else "jev-1.14.0",
                        "answers": {"which": {"type": "choice", "choice": max(values, key=values.get), "probabilities": probabilities}},
                        "debug": token, "usage": {"input_tokens": 200, "output_tokens": 0}}).encode()
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        with tempfile.TemporaryDirectory() as temporary, ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            root = Path(temporary)
            for name in ("alpha", "beta", "zeta"):
                make_catalog_skill(root / "skills", name, description="Common " + name)
            store = Store(root / "catalog.sqlite3")
            store.initialize()
            source = store.add_local_source(root / "skills")
            scan_local_source(store, source.id)
            (root / "sitecustomize.py").write_text(STUB)
            (root / "ranking_fixture.py").write_text(ADAPTER)
            thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
            thread.start()
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": token, "CAPALITH_RANKING": "typesafe", "CAPALITH_RANKING_CONFIG": "",
                    "CAPALITH_SEMANTIC_MODEL": "/missing-catalog-test-model", "CAPALITH_TEST_PORT": str(server.server_port),
                    "CAPALITH_TEST_ERROR": "yes" if mode == "exception" else "",
                    "XDG_CACHE_HOME": str(root / "cache"), "PYTHONPATH": os.pathsep.join((str(root), str(Path(__file__).resolve().parents[1])))}):
                try:
                    yield root, Store(store.path, read_only=True), seen, token
                finally:
                    server.shutdown()
                    thread.join(timeout=2)

    def cli(self, store):
        return subprocess.run([sys.executable, "-m", "capalith", "--db", str(store.path), "--limit", "1", "discover", "common"],
                              cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)

    def test_user_key_reaches_only_the_typesafe_header_through_the_real_worker(self):
        with self.search() as (root, store, seen, token):
            result = self.cli(store)
            self.assertEqual(0, result.returncode, result.stderr)
            first = json.loads(result.stdout)
            self.assertEqual("ai", first["ranking"]["path"])
            self.assertEqual("zeta", first["candidates"][0]["name"])
            self.assertEqual(2, len(seen))
            self.assertTrue(all(r["authenticated"] and r["path"] == "/v1/systemone" for r in seen))
            self.assertTrue(all(r["body"]["model"] == "jev-1.14.0" for r in seen))
            self.assertNotIn(token, json.dumps([r["body"] for r in seen]))
            second = store.discover("common", limit=1, offset=1, view_id=first["retrieval"]["view_id"])
            self.assertEqual("beta", second["candidates"][0]["name"])
            self.assertEqual(2, len(seen), "continuation must not buy another answer")
            self.assertNotIn(token, result.stdout + result.stderr + json.dumps(second))
            for file in (root / "cache").rglob("*"):
                if file.is_file():
                    self.assertNotIn(token.encode(), file.read_bytes())

    def test_provider_errors_redirects_and_invalid_data_never_echo_the_key(self):
        for mode, reason in (("http_error", "provider_error"), ("redirect", "provider_error"),
                             ("invalid_model", "invalid_response"), ("invalid_json", "invalid_response"),
                             ("oversized", "invalid_response"), ("exception", "provider_error")):
            with self.subTest(mode=mode), self.search(mode) as (root, store, seen, token):
                result = self.cli(store)
                self.assertEqual(0, result.returncode)
                self.assertEqual({"path": "free", "reason": reason}, json.loads(result.stdout)["ranking"])
                self.assertEqual(0 if mode == "exception" else 1, len(seen), "failure must neither follow a redirect nor retry")
                self.assertNotIn(token, result.stdout + result.stderr)
                for file in (root / "cache").rglob("*"):
                    if file.is_file():
                        self.assertNotIn(token.encode(), file.read_bytes())

    def test_header_injection_is_rejected_without_a_request_or_key_in_output(self):
        with self.search() as (_, store, seen, token):
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": token + "\r\nInjected: value"}):
                result = self.cli(store)
            self.assertEqual({"path": "free", "reason": "configuration_error"}, json.loads(result.stdout)["ranking"])
            self.assertNotIn(token, result.stdout + result.stderr)
            self.assertEqual([], seen)

    def test_no_key_or_no_explicit_opt_in_stays_free_without_a_call_or_cache(self):
        for enabled, key in (("typesafe", ""), ("", "synthetic-key"), ("typesafe", "   ")):
            with self.subTest(enabled=enabled, hasKey=bool(key.strip())), self.search() as (root, store, seen, _):
                with patch.dict(os.environ, {"TYPESAFE_API_KEY": key, "CAPALITH_RANKING": enabled}):
                    result = store.discover("common")
                self.assertEqual({"path": "free", "reason": "missing_key" if enabled == "typesafe" else "not_configured"}, result["ranking"])
                self.assertEqual([], seen)
                self.assertFalse((root / "cache").exists())

    def test_host_managed_config_keeps_precedence_over_the_builtin(self):
        with self.search() as (root, store, seen, _):
            pointer = root / "ranking.json"
            pointer.write_text(json.dumps({"transport": "ranking_fixture:call", "cache_dir": str(root / "managed-cache")}))
            pointer.chmod(0o600)
            with patch.dict(os.environ, {"CAPALITH_RANKING_CONFIG": str(pointer)}):
                self.assertEqual("ai", store.discover("common")["ranking"]["path"])
            self.assertEqual([], seen)
