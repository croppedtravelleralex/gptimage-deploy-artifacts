from __future__ import annotations

import base64
import unittest
from datetime import datetime
from io import BytesIO
from unittest import mock

from PIL import Image

from services import openai_backend_api
from services.openai_backend_api import ChatRequirements, InvalidAccessTokenError, OpenAIBackendAPI


def complete_fp() -> dict[str, str]:
    return {
        "user-agent": "Registered UA",
        "impersonate": "chrome120",
        "oai-device-id": "device-1",
        "oai-session-id": "session-1",
        "sec-ch-ua": '"Google Chrome";v="120"',
        "sec-ch-ua-arch": '"x86"',
        "sec-ch-ua-bitness": '"64"',
        "sec-ch-ua-full-version": '"120.0.0.0"',
        "sec-ch-ua-full-version-list": '"Google Chrome";v="120.0.0.0"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-platform-version": '"10.0.0"',
    }


class FakeResponse:
    def __init__(
        self,
        payload: dict | None = None,
        *,
        content: bytes = b"",
        status_code: int = 200,
        url: str = "https://resource.example/item",
    ) -> None:
        self._payload = payload or {}
        self.content = content
        self.status_code = status_code
        self.url = url
        self.headers: dict[str, str] = {}
        self.text = "response-body"

    def json(self) -> dict:
        return dict(self._payload)


class RecordingSession:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.verify = kwargs.get("verify", True)
        self.impersonate = kwargs.get("impersonate", "")
        proxy = kwargs.get("proxy")
        self.proxies = dict(kwargs.get("proxies") or ({"all": proxy} if proxy else {}))
        self.timeout = kwargs.get("timeout", 30)
        self.headers: dict[str, str] = {}
        self.get_calls: list[tuple[str, dict]] = []
        self.post_calls: list[tuple[str, dict]] = []
        self.put_calls: list[tuple[str, dict]] = []
        self.get_responses: list[FakeResponse] = []
        self.post_responses: list[FakeResponse] = []
        self.put_responses: list[FakeResponse] = []
        self.close_calls = 0

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.get_calls.append((url, kwargs))
        return self.get_responses.pop(0)

    def post(self, url: str, **kwargs) -> FakeResponse:
        self.post_calls.append((url, kwargs))
        return self.post_responses.pop(0)

    def put(self, url: str, **kwargs) -> FakeResponse:
        self.put_calls.append((url, kwargs))
        return self.put_responses.pop(0)

    def close(self) -> None:
        self.close_calls += 1


class SessionFactory:
    def __init__(self) -> None:
        self.sessions: list[RecordingSession] = []

    def __call__(self, **kwargs) -> RecordingSession:
        session = RecordingSession(**kwargs)
        self.sessions.append(session)
        return session


class FakeProxySettings:
    def __init__(self) -> None:
        self.session_calls: list[dict] = []
        self.header_calls: list[dict] = []

    def build_session_kwargs(self, **kwargs) -> dict:
        self.session_calls.append(dict(kwargs))
        return {
            "proxy": "http://proxy.example:8080",
            "impersonate": kwargs["impersonate"],
            "verify": kwargs.get("verify", True),
        }

    def build_headers(self, headers=None, target_url="", account=None, upstream=True, **kwargs) -> dict:
        self.header_calls.append({
            "headers": dict(headers or {}),
            "target_url": target_url,
            "account": dict(account or {}),
            "upstream": upstream,
            **kwargs,
        })
        result = dict(headers or {})
        result["Cookie"] = "cf_clearance=test"
        return result


class FakeAccountService:
    def __init__(self, fp: dict[str, str] | None = None, *, fail_update: bool = False) -> None:
        self.fp = dict(fp or complete_fp())
        self.fail_update = fail_update
        self.update_calls: list[tuple[str, dict, bool]] = []

    def get_account(self, token: str) -> dict:
        return {"access_token": token, "fp": dict(self.fp), "proxy": "http://proxy.example:8080"}

    def update_account(self, token: str, updates: dict, quiet: bool = False) -> dict:
        self.update_calls.append((token, updates, quiet))
        if self.fail_update:
            raise RuntimeError(f"secret persistence failure for {token}")
        return updates


class OpenAIBackendTransportIsolationTests(unittest.TestCase):
    def _make_api(
        self,
        *,
        account_service: FakeAccountService | None = None,
    ) -> tuple[OpenAIBackendAPI, SessionFactory, FakeProxySettings, FakeAccountService]:
        factory = SessionFactory()
        proxy = FakeProxySettings()
        accounts = account_service or FakeAccountService()
        patches = (
            mock.patch.object(openai_backend_api, "proxy_settings", proxy),
            mock.patch.object(openai_backend_api, "account_service", accounts),
            mock.patch.object(openai_backend_api.requests, "Session", side_effect=factory),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return OpenAIBackendAPI("secret-token"), factory, proxy, accounts

    def test_api_headers_are_explicit_and_default_session_is_transport_neutral(self) -> None:
        api, factory, _proxy, _accounts = self._make_api()

        self.assertEqual(len(factory.sessions), 1)
        for name in (
            "Authorization",
            "Origin",
            "Referer",
            "OAI-Device-Id",
            "OAI-Session-Id",
            "OAI-Language",
            "OAI-Client-Version",
        ):
            self.assertNotIn(name, api.session.headers)

        headers = api._headers("/backend-api/me")
        self.assertEqual(headers["Authorization"], "Bearer secret-token")
        self.assertEqual(headers["Origin"], "https://chatgpt.com")
        self.assertEqual(headers["Referer"], "https://chatgpt.com/")
        self.assertEqual(headers["OAI-Device-Id"], "device-1")
        self.assertEqual(headers["OAI-Session-Id"], "session-1")
        self.assertEqual(headers["X-OpenAI-Target-Path"], "/backend-api/me")
        self.assertEqual(headers["Cookie"], "cf_clearance=test")

    def test_resource_session_is_lazy_proxy_bound_and_closed_with_api_session(self) -> None:
        api, factory, proxy, _accounts = self._make_api()
        self.assertEqual(len(factory.sessions), 1)

        resource = api._get_resource_session()
        self.assertIs(resource, api._get_resource_session())
        self.assertEqual(len(factory.sessions), 2)
        self.assertEqual(len(proxy.session_calls), 2)
        self.assertEqual(proxy.session_calls[1]["account"]["access_token"], "secret-token")
        self.assertTrue(proxy.session_calls[1]["resource"])
        self.assertTrue(proxy.session_calls[1]["upstream"])
        for name in (
            "Authorization",
            "Cookie",
            "Origin",
            "Referer",
            "OAI-Device-Id",
            "OAI-Session-Id",
            "X-OpenAI-Target-Path",
        ):
            self.assertNotIn(name, resource.headers)

        api.close()
        api.close()
        self.assertEqual(api.session.close_calls, 1)
        self.assertEqual(resource.close_calls, 1)

    def test_upload_put_uses_resource_session_without_api_auth(self) -> None:
        api, factory, _proxy, _accounts = self._make_api()
        main = factory.sessions[0]
        main.post_responses.extend([
            FakeResponse({"upload_url": "https://blob.example/upload", "file_id": "file-1"}),
            FakeResponse({}),
        ])

        image = Image.new("RGB", (1, 1), "white")
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

        with mock.patch.object(OpenAIBackendAPI, "_get_resource_session", wraps=api._get_resource_session) as get_resource:
            resource = api._get_resource_session()
            resource.put_responses.append(FakeResponse({}))
            api._upload_image(encoded)

        self.assertGreaterEqual(get_resource.call_count, 2)
        self.assertEqual(main.put_calls, [])
        self.assertEqual(resource.put_calls[0][0], "https://blob.example/upload")
        headers = resource.put_calls[0][1].get("headers") or {}
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("OAI-Device-Id", headers)
        self.assertNotIn("OAI-Session-Id", headers)

    def test_image_result_download_uses_authenticated_api_session(self) -> None:
        """estuary/chatgpt.com 生图结果下载必须走主会话（带 Bearer）；resource 无鉴权会 403。"""
        api, factory, _proxy, _accounts = self._make_api()
        main = factory.sessions[0]
        main.get_responses.append(FakeResponse(content=b"image-bytes"))
        resource = api._get_resource_session()

        images = api.download_image_bytes(["https://chatgpt.com/backend-api/estuary/content?id=file-1"])

        self.assertEqual(main.get_calls[0][0], "https://chatgpt.com/backend-api/estuary/content?id=file-1")
        download_headers = main.get_calls[0][1].get("headers") or {}
        self.assertTrue(str(download_headers.get("Authorization") or "").startswith("Bearer "))
        self.assertEqual(resource.get_calls, [])
        self.assertEqual(images, [b"image-bytes"])

    def test_image_headers_include_all_requirement_tokens(self) -> None:
        api, _factory, _proxy, _accounts = self._make_api()
        requirements = ChatRequirements(
            token="requirements",
            proof_token="proof",
            turnstile_token="turnstile",
            so_token="so-token",
        )

        headers = api._image_headers("/backend-api/f/conversation", requirements)

        self.assertEqual(headers["OpenAI-Sentinel-Proof-Token"], "proof")
        self.assertEqual(headers["OpenAI-Sentinel-Turnstile-Token"], "turnstile")
        self.assertEqual(headers["OpenAI-Sentinel-SO-Token"], "so-token")

    def test_spa_image_headers_use_proven_legacy_client_and_minimal_shape(self) -> None:
        api, _factory, _proxy, _accounts = self._make_api()
        requirements = ChatRequirements(token="requirements", proof_token="proof")

        headers = api._image_headers(
            "/backend-api/f/conversation",
            requirements,
            accept="text/event-stream",
            spa_tool_path=True,
        )

        self.assertEqual(
            headers["OAI-Client-Version"],
            "prod-a194cd50d4416d3c0b47c740f206b12ce60f5887",
        )
        self.assertEqual(headers["OAI-Client-Build-Number"], "6708908")
        self.assertEqual(headers["OAI-Language"], "en-US")
        self.assertNotIn("X-Conduit-Token", headers)
        self.assertNotIn("X-Oai-Turn-Trace-Id", headers)
        self.assertNotIn("Sec-Ch-Ua", headers)
        self.assertNotIn("X-OpenAI-Target-Path", headers)

    def test_image_transport_retry_classifier_excludes_cf_blocks(self) -> None:
        self.assertTrue(
            OpenAIBackendAPI._is_transient_transport_error(
                RuntimeError("curl: (35) TLS connect error")
            )
        )
        self.assertTrue(
            OpenAIBackendAPI._is_transient_transport_error(
                RuntimeError("curl: (56) Recv failure: Connection was reset")
            )
        )
        self.assertFalse(
            OpenAIBackendAPI._is_transient_transport_error(
                RuntimeError("cf_edge_block: /backend-api/f/conversation HTTP 403")
            )
        )

    def test_transport_rebuild_preserves_proxy_tls_and_impersonation(self) -> None:
        api, factory, _proxy, _accounts = self._make_api()
        old_session = api.session
        old_session.verify = False
        old_session.proxies = {"all": "http://127.0.0.1:7897"}
        old_session.impersonate = "chrome131"
        old_session.timeout = 90

        api._rebuild_api_session_after_transport_error()

        self.assertEqual(len(factory.sessions), 2)
        rebuilt = api.session
        self.assertIsNot(rebuilt, old_session)
        self.assertFalse(rebuilt.kwargs["verify"])
        self.assertEqual(rebuilt.kwargs["proxies"], {"all": "http://127.0.0.1:7897"})
        self.assertNotIn("proxy", rebuilt.kwargs)
        self.assertEqual(rebuilt.kwargs["impersonate"], "chrome131")
        self.assertEqual(rebuilt.kwargs["timeout"], 90)
        self.assertEqual(old_session.close_calls, 1)

    def test_image_requirements_cf_path_is_single_shot(self) -> None:
        api, _factory, _proxy, _accounts = self._make_api()
        requirements = ChatRequirements(token="requirements")
        response = FakeResponse({})

        with (
            mock.patch.object(api, "_report_progress"),
            mock.patch.object(api, "_ensure_bootstrap"),
            mock.patch.object(api, "_get_chat_requirements_once", return_value=requirements) as get_once,
            mock.patch.object(api, "_get_chat_requirements") as get_with_cf_retry,
            mock.patch.object(api, "_prepare_image_conversation", return_value="") as prepare,
            mock.patch.object(api, "_start_image_generation", return_value=response),
        ):
            actual = api._open_image_sse_with_cf_retry("cat", "gpt-image-2", [])

        self.assertIs(actual, response)
        get_once.assert_called_once_with()
        get_with_cf_retry.assert_not_called()
        prepare.assert_called_once_with("cat", requirements, "gpt-image-2")

    def test_get_user_info_stops_after_me_authentication_failure(self) -> None:
        api = object.__new__(OpenAIBackendAPI)
        api.access_token = "secret-token"

        with mock.patch.object(api, "_get_me", side_effect=InvalidAccessTokenError("expired")) as get_me, mock.patch.object(
            api, "_get_conversation_init"
        ) as get_init, mock.patch.object(api, "_get_default_account") as get_account:
            with self.assertRaises(InvalidAccessTokenError):
                api.get_user_info()

        get_me.assert_called_once_with()
        get_init.assert_not_called()
        get_account.assert_not_called()

    def test_prepare_image_conversation_rejects_missing_conduit_token_at_prepare_stage(self) -> None:
        api = object.__new__(OpenAIBackendAPI)
        api.base_url = "https://chatgpt.com"
        api.session = RecordingSession()
        api.session.post_responses.append(FakeResponse({}))
        api._image_headers = lambda _path, _requirements, **_kwargs: {}

        with mock.patch(
            "services.protocol.chatgpt_web_request.image_spa_tool_path_enabled",
            return_value=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "missing conduit_token"):
                api._prepare_image_conversation("cat", ChatRequirements(token="req"), "gpt-image-2")

    def test_find_conversation_by_prompt_accepts_iso_updated_at(self) -> None:
        api = object.__new__(OpenAIBackendAPI)
        updated_at = "2026-07-22T02:03:24.804876Z"
        api._list_recent_conversations = lambda **_kwargs: [
            {
                "id": "conversation-1",
                "title": "A simple flat blue circle icon",
                "updated_at": updated_at,
            }
        ]

        started_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00")).timestamp() - 5

        self.assertEqual(
            api.find_conversation_by_prompt("a simple flat blue circle icon, no text", started_at),
            "conversation-1",
        )

    def test_web_image_model_uses_spa_auto_slug(self) -> None:
        api = object.__new__(OpenAIBackendAPI)

        self.assertEqual(api._image_model_slug("gpt-image-2"), "auto")

    def test_complete_ensured_fingerprint_is_persisted_when_any_field_differs(self) -> None:
        incomplete = complete_fp()
        incomplete.pop("sec-ch-ua-arch")
        accounts = FakeAccountService(incomplete)
        api, _factory, _proxy, _accounts = self._make_api(account_service=accounts)

        self.assertEqual(len(accounts.update_calls), 1)
        token, updates, quiet = accounts.update_calls[0]
        self.assertEqual(token, "secret-token")
        self.assertTrue(quiet)
        self.assertEqual(updates["fp"], api.fp)
        self.assertEqual(updates["fp"]["sec-ch-ua-arch"], '"x86"')

    def test_fingerprint_persist_failure_logs_only_structured_non_secret_fields(self) -> None:
        incomplete = complete_fp()
        incomplete.pop("sec-ch-ua-arch")
        accounts = FakeAccountService(incomplete, fail_update=True)

        with mock.patch.object(openai_backend_api.logger, "warning") as warning:
            self._make_api(account_service=accounts)

        warning.assert_called_once()
        payload = warning.call_args.args[0]
        self.assertEqual(payload["event"], "account_fp_persist_failed")
        self.assertEqual(payload["error_type"], "RuntimeError")
        self.assertGreater(payload["field_count"], 0)
        self.assertNotIn("secret-token", repr(payload))
        self.assertNotIn("persistence failure", repr(payload))


if __name__ == "__main__":
    unittest.main()
