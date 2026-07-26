from unittest import mock

from services.image_pipeline.account_lease_pool import AccountLeasePool


def test_maintain_does_not_acquire_image_slots():
    pool = AccountLeasePool()
    with mock.patch.object(pool, "_pick_email", return_value="a@example.com"):
        with mock.patch("services.image_pipeline.account_lease_pool.account_service") as acct:
            pool.maintain(max_acquire=2)
            acct.get_available_access_token.assert_not_called()
    assert pool.pop_hint() == "a@example.com"


def test_pop_hint_prefers_requested_email():
    pool = AccountLeasePool()
    with pool._lock:
        from services.image_pipeline.account_lease_pool import _EmailHint
        import time

        pool._hints.append(_EmailHint(email="b@example.com", created_ts=time.time()))
        pool._hints.append(_EmailHint(email="a@example.com", created_ts=time.time()))
    assert pool.pop_hint("a@example.com") == "a@example.com"
    assert pool.pop_hint() == "b@example.com"
