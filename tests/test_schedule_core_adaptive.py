from services.image_pipeline.schedule_core import SedimentParser, dispatch_should_apply_interval


def test_dispatch_adaptive_under_cap():
    assert dispatch_should_apply_interval(interval_ms=1500, inflight=3, cap=10, queued=2) is False


def test_dispatch_adaptive_over_cap():
    assert dispatch_should_apply_interval(interval_ms=1500, inflight=10, cap=10, queued=3) is True


def test_sediment_parser_finds_ids():
    p = SedimentParser()
    try:
        assert p.feed('chunk sediment://file_abc123 rest') is True
        ids = p.ids()
        assert "file_abc123" in ids
    finally:
        p.close()
