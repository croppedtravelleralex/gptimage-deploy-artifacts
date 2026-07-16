from __future__ import annotations

import unittest

from services.request_shape import body_shape, header_shape


class RequestShapeTests(unittest.TestCase):
    def test_header_shape_redacts_secrets_and_is_stable(self) -> None:
        shape = header_shape(
            {
                "Authorization": "Bearer secret-token",
                "Accept": "application/json",
                "Cookie": "a=b",
            }
        )
        self.assertEqual(shape["header_count"], 3)
        self.assertIn("authorization", shape["header_names"])
        again = header_shape(
            {
                "Cookie": "a=b",
                "Accept": "application/json",
                "Authorization": "Bearer other",
            }
        )
        self.assertEqual(shape["shape_hash"], again["shape_hash"])

    def test_body_shape_uses_keys_only(self) -> None:
        shape = body_shape({"prompt": "secret text", "n": 1})
        self.assertEqual(shape["keys"], ["n", "prompt"])
        self.assertNotIn("secret", shape["shape_hash"])


if __name__ == "__main__":
    unittest.main()
