import unittest

from director_v2.legacy_history import legacy_history


class LegacyHistoryTests(unittest.TestCase):
    def test_owner_filter_order_pagination_and_no_fabricated_status(self):
        records = {
            "old-admin": {"title": "无归属旧记录", "prompt": "A"},
            "mine": {"user_id": 2, "title": "我的记录", "prompt": "B", "seed": 99, "duration": 5},
            "other": {"user_id": 3, "title": "他人记录", "prompt": "C"},
            "new": {"user_id": 2, "title": "新版映射", "v2_task_id": "task_x"},
        }
        mine = legacy_history(records, {"id": 2, "role": "user"})
        self.assertEqual([x["title"] for x in mine["items"]], ["我的记录"])
        self.assertEqual(mine["items"][0]["status"], "unknown")
        self.assertNotIn("seed", mine["items"][0]["parameters"])
        admin = legacy_history(records, {"id": 1, "role": "admin"})
        self.assertEqual([x["title"] for x in admin["items"]], ["无归属旧记录"])
        self.assertEqual(legacy_history(records, {"id": 2, "role": "user"}, query="不存在")["total"], 0)


if __name__ == "__main__":
    unittest.main()
