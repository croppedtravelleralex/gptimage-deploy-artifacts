from __future__ import annotations

import base64
import unittest
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
        return {"proxy": "http://proxy.example:8080", "impersonate": kwargs["impersonate"]}

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

    def test_upload_put_and_resource_download_do_not_use_api_session(self) -> None:
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

        # PUT 时才惰性创建 resource session。
        with mock.patch.object(OpenAIBackendAPI, "_get_resource_session", wraps=api._get_resource_session) as get_resource:
            resource = api._get_resource_session()
            resource.put_responses.append(FakeResponse({}))
            resource.get_responses.append(FakeResponse(content=b"image-bytes"))
            api._upload_image(encoded)
            images = api.download_image_bytes(["https://cdn.example/image.png"])

        self.assertGreaterEqual(get_resource.call_count, 3)
        self.assertEqual(main.put_calls, [])
        self.assertEqual(main.get_calls, [])
        self.assertEqual(resource.put_calls[0][0], "https://blob.example/upload")
        self.assertEqual(resource.get_calls[0][0], "https://cdn.example/image.png")
        for call in (resource.put_calls[0][1], resource.get_calls[0][1]):
            headers = call.get("headers") or {}
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("OAI-Device-Id", headers)
            self.assertNotIn("OAI-Session-Id", headers)
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
        api._image_headers = lambda _path, _requirements: {}

        with self.assertRaisesRegex(RuntimeError, "image_prepare.*missing conduit_token"):
            api._prepare_image_conversation("cat", ChatRequirements(token="req"), "gpt-image-2")

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
