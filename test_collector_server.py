import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from x_collector_server import CollectionSession, CollectorServer, post_from_payload


class CollectorServerTests(unittest.TestCase):
    def test_validates_x_post(self):
        post = post_from_payload({
            "id": "123",
            "url": "https://x.com/example/status/123",
            "author": "Example",
            "handle": "@example",
            "text": "一条测试帖子",
            "created_at": "2026-09-16T01:00:00Z",
        })
        self.assertEqual(post.id, "123")

    def test_rejects_external_url(self):
        with self.assertRaises(ValueError):
            post_from_payload({
                "id": "123",
                "url": "https://example.com/status/123",
                "text": "bad",
            })

    def test_session_saves_and_summarizes(self):
        payload = {
            "id": "123",
            "url": "https://x.com/example/status/123",
            "author": "Example",
            "handle": "@example",
            "text": "一条测试帖子",
            "created_at": "2026-09-16T01:00:00Z",
        }
        with tempfile.TemporaryDirectory() as directory:
            session = CollectionSession(Path(directory), "local", "unused")
            try:
                self.assertEqual(session.add([payload, payload])["added"], 1)
                result = session.finish()
                self.assertEqual(result["total"], 1)
                self.assertTrue(Path(result["summary_path"]).exists())
                self.assertEqual(result["date_archive"]["added"], 1)
                self.assertTrue((Path(directory) / "output-by-date" / "20260916.json").exists())
            finally:
                session.close()

    def test_same_author_and_text_are_deduplicated_across_ids(self):
        first = {
            "id": "100",
            "url": "https://x.com/example/status/100",
            "author": "Example",
            "handle": "@Example",
            "text": "相同的内容\n[图片]",
        }
        second = {
            **first,
            "id": "101",
            "url": "https://x.com/example/status/101",
            "handle": "@example",
            "text": "  相同的内容  \n  [图片]  ",
        }
        different = {
            **first,
            "id": "102",
            "url": "https://x.com/example/status/102",
            "text": "同一作者发布的另一条内容\n[视频]",
        }
        with tempfile.TemporaryDirectory() as directory:
            session = CollectionSession(Path(directory), "local", "unused")
            try:
                result = session.add([first, second, different])
                self.assertEqual(result["added"], 2)
                self.assertEqual(result["total"], 2)
            finally:
                session.close()

    def test_server_accepts_a_new_batch_after_summary(self):
        first = {
            "id": "201",
            "url": "https://x.com/example/status/201",
            "author": "Example",
            "handle": "@example",
            "text": "第一批帖子",
        }
        second = {
            "id": "202",
            "url": "https://x.com/example/status/202",
            "author": "Example",
            "handle": "@example",
            "text": "第二批帖子",
        }
        with tempfile.TemporaryDirectory() as directory:
            session = CollectionSession(Path(directory), "local", "unused")
            try:
                self.assertEqual(session.add([first])["added"], 1)
                first_result = session.finish()
                self.assertTrue(first_result["ready_for_next"])
                self.assertEqual(len(session.posts), 0)

                # 旧帖子跨批次仍去重，新帖子则进入下一批。
                second_add = session.add([first, second])
                self.assertEqual(second_add["added"], 1)
                self.assertEqual(second_add["total"], 1)
                second_result = session.finish()
                self.assertEqual(second_result["completed_sessions"], 2)
                self.assertNotEqual(first_result["summary_path"], second_result["summary_path"])

                repeated_finish = session.finish()
                self.assertTrue(repeated_finish["already_finished"])
            finally:
                session.close()

    def test_history_deduplication_survives_server_restart(self):
        first = {
            "id": "301",
            "url": "https://x.com/example/status/301",
            "author": "Example",
            "handle": "@example",
            "text": "需要永久去重的帖子",
        }
        same_content_new_id = {
            **first,
            "id": "302",
            "url": "https://x.com/example/status/302",
        }
        new_post = {
            **first,
            "id": "303",
            "url": "https://x.com/example/status/303",
            "text": "服务重启后的新帖子",
        }
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            first_session = CollectionSession(output_dir, "local", "unused")
            try:
                self.assertEqual(first_session.add([first])["added"], 1)
                first_session.finish()
            finally:
                first_session.close()

            second_session = CollectionSession(output_dir, "local", "unused")
            try:
                result = second_session.add([first, same_content_new_id, new_post])
                self.assertEqual(result["added"], 1)
                self.assertEqual(result["historical_total"], 2)
                self.assertTrue((output_dir / "collector-history.sqlite3").exists())
            finally:
                second_session.close()

    def test_existing_json_history_is_imported_once(self):
        historical = {
            "id": "401",
            "url": "https://x.com/example/status/401",
            "author": "Example",
            "handle": "@example",
            "text": "旧输出文件中的帖子",
        }
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "posts-old.json").write_text(
                json.dumps([historical], ensure_ascii=False),
                encoding="utf-8",
            )
            session = CollectionSession(output_dir, "local", "unused")
            try:
                self.assertEqual(session.add([historical])["added"], 0)
                self.assertEqual(session.history.count(), 1)
            finally:
                session.close()

    def test_http_post_round_trip_with_token_and_cors(self):
        payload = {
            "posts": [{
                "id": "456",
                "url": "https://x.com/example/status/456",
                "author": "Example",
                "handle": "@example",
                "text": "HTTP 测试帖子",
            }]
        }
        with tempfile.TemporaryDirectory() as directory:
            session = CollectionSession(Path(directory), "local", "unused")
            server = CollectorServer(("127.0.0.1", 0), "test-token", session)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/posts",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "X-X-Collector-Token": "test-token",
                        "Origin": "chrome-extension://test-extension",
                    },
                    method="POST",
                )
                with urlopen(request, timeout=3) as response:
                    value = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(
                        response.headers["Access-Control-Allow-Origin"],
                        "chrome-extension://test-extension",
                    )
                self.assertTrue(value["ok"])
                self.assertEqual(value["total"], 1)
            finally:
                server.shutdown()
                server.server_close()
                session.close()


if __name__ == "__main__":
    unittest.main()
