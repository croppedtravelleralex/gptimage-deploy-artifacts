from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.openai_backend_api import ChatRequirements, OpenAIBackendAPI


class ChatRequirementsFinalizeShapeTests(unittest.TestCase):
    def _api(self) -> OpenAIBackendAPI:
        api = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
        api.access_token = "tok"
        api.base_url = "https://chatgpt.com"
        api.user_agent = "Mozilla/5.0"
        api.pow_script_sources = ["https://chatgpt.com/backend-api/sentinel/sdk.js"]
        api.pow_data_build = "prod-test"
        api.session = MagicMock()
        api.fp = {"sec-ch-ua": "", "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"'}
        api.account = {}
        api.device_id = "dev"
        api.session_id = "sess"
        api.client_version = "prod-test"
        api.client_build_number = "1"
        return api

    def test_finalize_uses_proofofwork_and_turnstile_keys(self) -> None:
        api = self._api()
        prepare_payload = {
            "prepare_token": "prep-abc",
            "proofofwork": {"required": True, "seed": "seed", "difficulty": "00"},
            "turnstile": {"required": True, "dx": "dx-blob"},
        }
        finalize_payload = {"token": "req-token", "so_token": ""}

        prepare_resp = MagicMock()
        prepare_resp.json.return_value = prepare_payload
        finalize_resp = MagicMock()
        finalize_resp.json.return_value = finalize_payload
        api.session.post.side_effect = [prepare_resp, finalize_resp]

        with (
            patch("services.openai_backend_api.build_legacy_requirements_token", return_value="p-token"),
            patch("services.openai_backend_api.build_proof_token", return_value="proof-xyz"),
            patch("services.openai_backend_api.solve_turnstile_token", return_value="ts-xyz"),
            patch("services.openai_backend_api.ensure_ok"),
            patch.object(api, "_headers", side_effect=lambda path, extra=None: dict(extra or {})),
        ):
            req = api._get_chat_requirements_once()

        self.assertIsInstance(req, ChatRequirements)
        self.assertEqual(req.token, "req-token")
        self.assertEqual(req.proof_token, "proof-xyz")
        self.assertEqual(req.turnstile_token, "ts-xyz")
        finalize_call = api.session.post.call_args_list[1]
        body = finalize_call.kwargs["json"]
        self.assertEqual(
            body,
            {
                "prepare_token": "prep-abc",
                "proofofwork": "proof-xyz",
                "turnstile": "ts-xyz",
            },
        )
        self.assertNotIn("proof_token", body)
        self.assertNotIn("turnstile_token", body)

    def test_turnstile_required_but_empty_hard_fails(self) -> None:
        api = self._api()
        prepare_payload = {
            "prepare_token": "prep-abc",
            "proofofwork": {"required": False},
            "turnstile": {"required": True, "dx": "dx-blob"},
        }
        prepare_resp = MagicMock()
        prepare_resp.json.return_value = prepare_payload
        api.session.post.side_effect = [prepare_resp]

        with (
            patch("services.openai_backend_api.build_legacy_requirements_token", return_value="p-token"),
            patch("services.openai_backend_api.solve_turnstile_token", return_value=""),
            patch("services.openai_backend_api.ensure_ok"),
            patch.object(api, "_headers", side_effect=lambda path, extra=None: dict(extra or {})),
        ):
            with self.assertRaisesRegex(RuntimeError, "turnstile_required_but_unsolved"):
                api._get_chat_requirements_once()

        self.assertEqual(api.session.post.call_count, 1)


class ImagePathPreTicketCacheTests(unittest.TestCase):
    """The image path must reuse the pre-ticket cache without gaining CF retries.

    It used to call the bare `_once` helper, paying prepare+finalize on every single
    generation even for a hot account.
    """

    def _api(self) -> OpenAIBackendAPI:
        api = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
        api.access_token = "tok-image"
        return api

    def test_cache_hit_skips_the_round_trip(self) -> None:
        api = self._api()
        cached = ChatRequirements(token="cached-token")
        with (
            patch.object(api, "_cached_chat_requirements", return_value=cached),
            patch.object(api, "_get_chat_requirements_once") as once,
            patch.object(api, "_store_chat_requirements") as store,
        ):
            got = api._get_chat_requirements_cached_once()

        self.assertIs(got, cached)
        once.assert_not_called()
        store.assert_not_called()

    def test_cache_miss_fetches_once_and_stores(self) -> None:
        api = self._api()
        fresh = ChatRequirements(token="fresh-token")
        with (
            patch.object(api, "_cached_chat_requirements", return_value=None),
            patch.object(api, "_get_chat_requirements_once", return_value=fresh) as once,
            patch.object(api, "_store_chat_requirements") as store,
        ):
            got = api._get_chat_requirements_cached_once()

        self.assertIs(got, fresh)
        self.assertEqual(once.call_count, 1)
        store.assert_called_once_with(fresh)

    def test_cf_403_still_propagates_without_retry(self) -> None:
        """Single-shot semantics are the whole reason the image path avoided the
        retrying helper; caching must not reintroduce hammering."""
        api = self._api()
        boom = RuntimeError("cf_edge_block 403")
        with (
            patch.object(api, "_cached_chat_requirements", return_value=None),
            patch.object(api, "_get_chat_requirements_once", side_effect=boom) as once,
            patch.object(api, "_store_chat_requirements") as store,
        ):
            with self.assertRaises(RuntimeError):
                api._get_chat_requirements_cached_once()

        self.assertEqual(once.call_count, 1)
        store.assert_not_called()


if __name__ == "__main__":
    unittest.main()
