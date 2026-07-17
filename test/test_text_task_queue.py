from __future__ import annotations

import unittest

from services.text_task_queue import TextTaskQueue


class TextTaskQueueTests(unittest.TestCase):
    def test_enqueue_dequeue_depth(self) -> None:
        queue = TextTaskQueue()
        self.assertEqual(queue.depth(), 0)
        item = queue.enqueue({"x": 1})
        self.assertEqual(queue.depth(), 1)
        self.assertEqual(queue.snapshot()["depth"], 1)
        got = queue.dequeue()
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got.item_id, item.item_id)
        self.assertEqual(queue.depth(), 0)
        self.assertIsNone(queue.dequeue())


if __name__ == "__main__":
    unittest.main()
