from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.account_service import AccountService


class ProxyBindingCapacityTests(unittest.TestCase):
    def _service(self, max_accounts: int = 5) -> tuple[AccountService, SimpleNamespace]:
        cfg = SimpleNamespace(proxy_binding_max_accounts=max_accounts)
        svc = AccountService.__new__(AccountService)
        svc._lock = threading.RLock()
        svc._accounts = {}
        return svc, cfg

    def test_duplicate_only_when_over_capacity(self) -> None:
        svc, cfg = self._service(5)
        binding = "same-binding-hash"
        proxy = "http://u:p@1.2.3.4:5800"
        with patch("services.account_service.config", cfg):
            for i in range(5):
                token = f"tok-{i}"
                svc._accounts[token] = {
                    "access_token": token,
                    "email": f"a{i}@x.com",
                    "status": "正常",
                    "panda_receive_state": "verified_ready",
                    "proxy": proxy,
                    "proxy_binding_hash": binding,
                    "quota": 20,
                }
            self.assertFalse(svc._active_proxy_binding_duplicate(svc._accounts["tok-0"]))
            svc._accounts["tok-5"] = {
                "access_token": "tok-5",
                "email": "a5@x.com",
                "status": "正常",
                "panda_receive_state": "verified_ready",
                "proxy": proxy,
                "proxy_binding_hash": binding,
                "quota": 20,
            }
            self.assertTrue(svc._active_proxy_binding_duplicate(svc._accounts["tok-0"]))

    def test_enforce_isolation_only_at_capacity(self) -> None:
        svc, cfg = self._service(2)
        binding = "bind-cap-2"
        proxy = "http://u:p@9.9.9.9:1"
        with patch("services.account_service.config", cfg):
            svc._accounts["tok-0"] = {
                "access_token": "tok-0",
                "email": "a0@x.com",
                "status": "正常",
                "panda_receive_state": "verified_ready",
                "proxy": proxy,
                "proxy_binding_hash": binding,
            }
            incoming = {
                "access_token": "tok-1",
                "email": "a1@x.com",
                "status": "正常",
                "panda_receive_state": "verified_ready",
                "proxy": proxy,
                "proxy_binding_hash": binding,
            }
            out = svc._enforce_shared_binding_isolation(incoming, "tok-1")
            self.assertEqual(out.get("panda_receive_state"), "verified_ready")
            svc._accounts["tok-1"] = dict(incoming)
            third = {
                "access_token": "tok-2",
                "email": "a2@x.com",
                "status": "正常",
                "panda_receive_state": "verified_ready",
                "proxy": proxy,
                "proxy_binding_hash": binding,
            }
            out2 = svc._enforce_shared_binding_isolation(third, "tok-2")
            self.assertEqual(out2.get("panda_receive_state"), "identity_isolated")


if __name__ == "__main__":
    unittest.main()
