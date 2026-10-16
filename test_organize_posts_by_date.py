import json
import tempfile
import unittest
from pathlib import Path

from organize_posts_by_date import archive_posts_by_date, parse_post_time


def make_post(post_id: str, created_at: str | None, text: str | None = None) -> dict[str, str | None]:
    return {
        "id": post_id,
        "url": f"https://x.com/example/status/{post_id}",
        "author": "Example",
        "handle": "@example",
        "text": text or f"帖子 {post_id}",
        "created_at": created_at,
        "reply_count": None,
        "repost_count": None,
        "like_count": None,
    }


class DateArchiveTests(unittest.TestCase):
    def test_converts_utc_to_beijing_time(self):
        parsed = parse_post_time("2026-09-15T18:30:00.000Z")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.isoformat(timespec="seconds"), "2026-09-16T02:30:00+08:00")

    def test_ignores_missing_or_ambiguous_time(self):
        self.assertIsNone(parse_post_time(None))
        self.assertIsNone(parse_post_time(""))
        self.assertIsNone(parse_post_time("2026-09-16 10:00:00"))

    def test_incremental_merge_sorts_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            first = archive_posts_by_date(
                [
                    make_post("2", "2026-09-15T03:00:00Z"),
                    make_post("3", None),
                    make_post("4", "2026-09-15T16:30:00Z"),
                ],
                output_dir,
            )
            self.assertEqual(first["added"], 2)
            self.assertEqual(first["ignored_without_time"], 1)
            self.assertTrue((output_dir / "20260915.json").exists())
            self.assertTrue((output_dir / "20260916.json").exists())

            # 下一轮插入当天更早的帖子，并同时提交 ID 重复和作者正文重复项。
            second = archive_posts_by_date(
                [
                    make_post("1", "2026-09-15T01:00:00Z"),
                    make_post("2", "2026-09-15T03:00:00Z"),
                    make_post("99", "2026-09-15T04:00:00Z", text="帖子 2"),
                ],
                output_dir,
            )
            self.assertEqual(second["added"], 1)
            self.assertEqual(second["duplicates"], 2)
            saved = json.loads((output_dir / "20260915.json").read_text(encoding="utf-8"))
            self.assertEqual([item["id"] for item in saved], ["1", "2"])
            self.assertEqual(
                [item["created_at"] for item in saved],
                sorted(item["created_at"] for item in saved),
            )

    def test_rerun_is_idempotent(self):
        post = make_post("10", "2026-09-16T01:00:00+00:00")
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            self.assertEqual(archive_posts_by_date([post], output_dir)["added"], 1)
            result = archive_posts_by_date([post], output_dir)
            self.assertEqual(result["added"], 0)
            self.assertEqual(result["duplicates"], 1)
            self.assertEqual(result["affected_dates"], 0)


if __name__ == "__main__":
    unittest.main()
