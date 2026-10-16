import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from x_digest import IncrementalPostWriter, Post, XBrowser, _post_from_article, canonical_x_url, local_summary


class XDigestTests(unittest.TestCase):
    def test_canonical_x_url(self):
        self.assertEqual(
            canonical_x_url("https://twitter.com/example/status/12345?s=20"),
            "https://x.com/example/status/12345",
        )

    def test_rejects_non_x_url(self):
        with self.assertRaises(ValueError):
            canonical_x_url("https://example.com/user/status/12345")

    def test_local_summary_contains_source_link(self):
        post = Post("1", "https://x.com/a/status/1", "A", "@a", "这是一个用于测试的帖子内容")
        summary = local_summary([post])
        self.assertIn("共读取 1 条帖子", summary)
        self.assertIn("https://x.com/a/status/1", summary)

    def test_extract_post_from_x_like_dom(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")

        markup = """
        <article data-testid="tweet">
          <div data-testid="tweetText">测试帖子</div>
          <div data-testid="User-Name">测试用户\n@test</div>
          <a href="/test/status/123">
            <time datetime="2026-09-12T00:00:00Z"></time>
          </a>
          <button data-testid="like" aria-label="12 Likes"></button>
        </article>
        """
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                self.skipTest(f"Chromium is not installed: {exc}")
            try:
                page = browser.new_page()
                page.set_content(markup)
                post = _post_from_article(page.query_selector("article"))
                self.assertIsNotNone(post)
                self.assertEqual(post.id, "123")
                self.assertEqual(post.handle, "@test")
                self.assertEqual(post.like_count, "12")
            finally:
                browser.close()

    def test_incremental_writer_flushes_each_unique_post(self):
        post = Post(
            "1",
            "https://x.com/a/status/1",
            "A",
            "@a",
            "第一行\n第二行",
            created_at="2026-09-15T18:30:00Z",
        )
        console = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with redirect_stdout(console):
                with IncrementalPostWriter(Path(directory)) as writer:
                    writer.write(post)
                    writer.write(post)
                    saved = writer.jsonl_path.read_text(encoding="utf-8").splitlines()
                    markdown = writer.md_path.read_text(encoding="utf-8")
            self.assertEqual(len(saved), 1)
            self.assertEqual(json.loads(saved[0])["text"], post.text)
            self.assertIn("> 第一行", markdown)
            self.assertIn("https://x.com/a/status/1", markdown)
            self.assertIn(
                "已保存 [1] @a，发布时间：260916-023000: 第一行 第二行",
                console.getvalue(),
            )

    def test_login_waits_for_manual_enter_before_navigation(self):
        class FakePage:
            def __init__(self):
                self.visited = []

            def goto(self, url, **kwargs):
                self.visited.append(url)

        browser = XBrowser(Path("unused-profile"), headless=False)
        browser.page = FakePage()
        browser._looks_logged_out = lambda: True
        with patch("builtins.input", return_value="") as wait_for_enter, patch("x_digest.notify_login"):
            browser.ensure_login("https://x.com/home")
        wait_for_enter.assert_called_once()
        self.assertEqual(browser.page.visited, ["https://x.com/home"])

    def test_post_wait_reloads_once_then_reports_page(self):
        class FakePage:
            url = "https://x.com/home"

            def __init__(self):
                self.reload_count = 0

            def query_selector_all(self, selector):
                return []

            def locator(self, selector):
                class EmptyLocator:
                    def count(self):
                        return 0
                return EmptyLocator()

            def reload(self, **kwargs):
                self.reload_count += 1

            def wait_for_timeout(self, milliseconds):
                pass

            def title(self):
                return "Home / X"

        browser = XBrowser(Path("unused-profile"), headless=False, page_timeout=0.001)
        browser.page = FakePage()
        with self.assertRaisesRegex(RuntimeError, "页面仍没有可读取内容"):
            browser.wait_for_posts("https://x.com/home")
        self.assertEqual(browser.page.reload_count, 1)

    def test_can_launch_installed_edge_with_isolated_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            try:
                with XBrowser(
                    Path(directory),
                    headless=True,
                    browser_name="edge",
                ) as browser:
                    browser.page.set_content("<title>Edge smoke test</title>")
                    self.assertEqual(browser.page.title(), "Edge smoke test")
            except RuntimeError as exc:
                self.skipTest(f"Microsoft Edge is unavailable: {exc}")


if __name__ == "__main__":
    unittest.main()
