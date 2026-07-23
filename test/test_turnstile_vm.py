import base64
import json
import unittest
from pathlib import Path

from utils.turnstile import solve_turnstile_token


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "turnstile_dx_20260721.json"


class TestTurnstileVM(unittest.TestCase):
    def test_current_spa_fixture_yields_non_empty_token(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

        token = solve_turnstile_token(fixture["dx"], fixture["p"])

        self.assertIsNotNone(token)
        self.assertGreater(len(token or ""), 1000)
        decoded = base64.b64decode(token or "").decode("utf-8")
        self.assertGreater(len(decoded), 750)

    def test_invalid_payload_fails_closed(self) -> None:
        self.assertIsNone(solve_turnstile_token("not-base64", "fixture-p"))


if __name__ == "__main__":
    unittest.main()
