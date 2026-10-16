#!/usr/bin/env python3
"""使用真实浏览器读取 X 帖子，并输出 JSON 与 Markdown 摘要。

仅用于读取当前账号正常可见的内容；遇到登录、验证码或风控时交给用户处理。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, TextIO
from urllib.parse import quote, urlparse

from organize_posts_by_date import parse_post_time


STATUS_RE = re.compile(r"/(?:i/web/)?[^/]+/status/(\d+)")
ARTICLE_SELECTOR = 'article[data-testid="tweet"], article[role="article"]'
WORD_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_'-]{2,}")
STOP_WORDS = {
    "about", "after", "again", "also", "and", "are", "but", "can", "for",
    "from", "has", "have", "https", "into", "just", "more", "not", "that",
    "the", "their", "this", "was", "were", "what", "when", "where", "which",
    "will", "with", "you", "your", "一个", "一些", "不是", "什么", "他们", "以及",
    "但是", "可以", "因为", "如果", "已经", "我们", "这个", "这些", "进行", "还有",
}


@dataclass(slots=True)
class Post:
    id: str
    url: str
    author: str
    handle: str
    text: str
    created_at: str | None = None
    reply_count: str | None = None
    repost_count: str | None = None
    like_count: str | None = None


class IncrementalPostWriter:
    """发现帖子时立即落盘，避免浏览中断后丢失已采集内容。"""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        # 微秒部分确保连续批次在同一秒内开始时也不会复用文件名。
        self.stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.jsonl_path = (output_dir / f"browsed-{self.stamp}.jsonl").resolve()
        self.md_path = (output_dir / f"browsed-{self.stamp}.md").resolve()
        self._jsonl: TextIO | None = None
        self._markdown: TextIO | None = None
        self._seen: set[str] = set()

    def __enter__(self) -> "IncrementalPostWriter":
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._jsonl = self.jsonl_path.open("a", encoding="utf-8")
        self._markdown = self.md_path.open("a", encoding="utf-8")
        if self.md_path.stat().st_size == 0:
            self._markdown.write(f"# 浏览中保存的 X 帖子\n\n开始时间：{datetime.now().isoformat(timespec='seconds')}\n\n")
            self._markdown.flush()
        return self

    def write(self, post: Post) -> None:
        if post.id in self._seen:
            return
        if not self._jsonl or not self._markdown:
            raise RuntimeError("增量保存器尚未打开。")
        self._seen.add(post.id)
        self._jsonl.write(json.dumps(asdict(post), ensure_ascii=False) + "\n")
        self._jsonl.flush()

        author = " ".join(part for part in (post.author, post.handle) if part)
        self._markdown.write(f"## {author or '未知作者'}\n\n")
        if post.created_at:
            self._markdown.write(f"时间：{post.created_at}\n\n")
        quoted_text = "\n".join(f"> {line}" if line else ">" for line in post.text.splitlines())
        self._markdown.write(f"{quoted_text}\n\n[查看原帖]({post.url})\n\n---\n\n")
        self._markdown.flush()
        published = parse_post_time(post.created_at)
        published_text = published.strftime("%y%m%d-%H%M%S") if published else "未知"
        preview = post.text[:60].replace(chr(10), " ")
        print(
            f"已保存 [{len(self._seen)}] {post.handle or post.author}，"
            f"发布时间：{published_text}: {preview}",
            flush=True,
        )

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._jsonl:
            self._jsonl.close()
        if self._markdown:
            self._markdown.close()


def canonical_x_url(url: str) -> str:
    """只接受 x.com/twitter.com 的帖子 URL，并去掉查询参数。"""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        raise ValueError(f"不是 X 帖子地址：{url}")
    match = STATUS_RE.search(parsed.path)
    if not match:
        raise ValueError(f"地址中没有 /status/<id>：{url}")
    parts = [part for part in parsed.path.split("/") if part]
    try:
        status_index = parts.index("status")
        handle = parts[status_index - 1]
    except (ValueError, IndexError):
        handle = "i"
    return f"https://x.com/{handle}/status/{match.group(1)}"


def notify_login() -> None:
    message = "\n需要登录 X：请切换到刚打开的浏览器完成登录。\n"
    print("\a" + message, flush=True)
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except (ImportError, RuntimeError):
        pass


def _metric(article: Any, test_ids: Iterable[str]) -> str | None:
    for test_id in test_ids:
        node = article.query_selector(f'[data-testid="{test_id}"]')
        if not node:
            continue
        label = node.get_attribute("aria-label")
        if label:
            number = re.search(r"[\d,.万亿千百]+", label)
            return number.group(0) if number else label
    return None


def _post_from_article(article: Any) -> Post | None:
    text_node = article.query_selector('[data-testid="tweetText"]')
    if not text_node:
        return None
    text = text_node.inner_text().strip()
    if not text:
        return None

    status_link = article.query_selector('a[href*="/status/"]')
    href = status_link.get_attribute("href") if status_link else None
    match = STATUS_RE.search(href or "")
    if not match:
        return None

    url = f"https://x.com{href.split('?')[0]}" if href and href.startswith("/") else (href or "")
    user_node = article.query_selector('[data-testid="User-Name"]')
    user_text = user_node.inner_text().strip() if user_node else ""
    handle_match = re.search(r"@[A-Za-z0-9_]{1,15}", user_text)
    handle = handle_match.group(0) if handle_match else ""
    user_lines = [line.strip() for line in user_text.splitlines() if line.strip()]
    author = next((line for line in user_lines if handle not in line), "")
    if not author and handle:
        author = re.sub(re.escape(handle) + r".*$", "", user_text).strip()
    author = author or handle
    time_node = article.query_selector("time")
    created_at = time_node.get_attribute("datetime") if time_node else None

    return Post(
        id=match.group(1),
        url=url,
        author=author,
        handle=handle,
        text=text,
        created_at=created_at,
        reply_count=_metric(article, ("reply",)),
        repost_count=_metric(article, ("retweet", "unretweet")),
        like_count=_metric(article, ("like", "unlike")),
    )


class XBrowser:
    def __init__(
        self,
        profile: Path,
        headless: bool,
        page_timeout: int = 30,
        browser_name: str = "edge",
        cdp_url: str | None = None,
    ) -> None:
        self.profile = profile
        self.headless = headless
        self.page_timeout = page_timeout
        self.browser_name = browser_name
        self.cdp_url = cdp_url
        self._playwright: Any = None
        self._connected_browser: Any = None
        self._owns_context = False
        self.context: Any = None
        self.page: Any = None

    def __enter__(self) -> "XBrowser":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "缺少 Playwright。请先运行：pip install -r requirements.txt，"
                "然后运行：python -m playwright install chromium"
            ) from exc

        self._playwright = sync_playwright().start()
        if self.cdp_url:
            try:
                self._connected_browser = self._playwright.chromium.connect_over_cdp(
                    self.cdp_url,
                    timeout=10_000,
                )
            except Exception as exc:
                self._playwright.stop()
                self._playwright = None
                raise RuntimeError(
                    f"无法连接 Edge 调试地址 {self.cdp_url}。"
                    "请确认 Edge 使用了独立 user-data-dir 并已开启远程调试。"
                ) from exc
            if not self._connected_browser.contexts:
                raise RuntimeError("已连接 Edge，但没有可用的浏览器上下文。")
            self.context = self._connected_browser.contexts[0]
            x_pages = [page for page in self.context.pages if "x.com" in page.url]
            self.page = x_pages[0] if x_pages else self.context.new_page()
        else:
            self.profile.mkdir(parents=True, exist_ok=True)
            channel = "msedge" if self.browser_name == "edge" else None
            try:
                self.context = self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile),
                    channel=channel,
                    headless=self.headless,
                    viewport={"width": 1365, "height": 900},
                    locale="zh-CN",
                )
            except Exception as exc:
                self._playwright.stop()
                self._playwright = None
                browser_label = "Microsoft Edge" if channel else "Chromium"
                raise RuntimeError(
                    f"无法启动 {browser_label} 自动化 profile：{self.profile}。"
                    "请确认没有另一个脚本实例正在使用该目录。"
                ) from exc
            self._owns_context = True
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.context and self._owns_context:
            self.context.close()
        # CDP 模式只断开连接，不关闭用户已经打开的 Edge。
        self._connected_browser = None
        if self._playwright:
            self._playwright.stop()

    def _looks_logged_out(self) -> bool:
        url = self.page.url.lower()
        if "/i/flow/login" in url or "/login" in url:
            return True
        login_fields = self.page.locator(
            'input[autocomplete="username"], input[autocomplete="current-password"]'
        )
        if login_fields.count() > 0 and login_fields.first.is_visible():
            return True
        # X 有时先显示登录按钮，稍后才跳转到登录流程。
        login_links = self.page.locator('a[href="/login"], [data-testid="loginButton"]')
        return not self._article_nodes() and login_links.count() > 0 and login_links.first.is_visible()

    def _looks_like_verification(self) -> bool:
        url = self.page.url.lower()
        return any(part in url for part in ("/account/access", "/i/flow/consent", "/challenge"))

    def _article_nodes(self) -> list[Any]:
        try:
            return [
                article
                for article in self.page.query_selector_all(ARTICLE_SELECTOR)
                if article.query_selector('[data-testid="tweetText"]')
                and article.query_selector('a[href*="/status/"]')
            ]
        except Exception:
            # 页面正处于跳转或重绘时，下一轮会重新读取 DOM。
            return []

    def ensure_login(self, return_url: str) -> None:
        if not self._looks_logged_out():
            return
        if self.headless:
            raise RuntimeError("检测到需要登录；请去掉 --headless 后重试，以便手动登录。")

        notify_login()
        print("脚本正在等待，不会自动继续或超时退出。", flush=True)
        try:
            input("完成登录后，请回到此终端并按 Enter 继续……")
        except EOFError as exc:
            raise RuntimeError(
                "当前运行环境无法接收 Enter 输入。请在交互式终端中运行脚本。"
            ) from exc
        print("已收到确认，继续抓取。", flush=True)
        self.page.goto(return_url, wait_until="domcontentloaded", timeout=60_000)

    def ensure_verification(self, return_url: str) -> None:
        if not self._looks_like_verification():
            return
        if self.headless:
            raise RuntimeError("X 要求人工验证；请去掉 --headless 后重试。")
        print("\a\nX 要求人工验证：请在浏览器中完成验证。", flush=True)
        try:
            input("完成验证后，请回到此终端并按 Enter 继续……")
        except EOFError as exc:
            raise RuntimeError("当前运行环境无法接收 Enter 输入。") from exc
        self.page.goto(return_url, wait_until="domcontentloaded", timeout=60_000)

    def wait_for_posts(self, return_url: str) -> list[Any]:
        """等待帖子，同时处理延迟出现的登录跳转和一次加载失败。"""
        reloaded = False
        deadline = time.monotonic() + self.page_timeout
        while True:
            articles = self._article_nodes()
            if articles:
                return articles

            if self._looks_logged_out():
                self.ensure_login(return_url)
                deadline = time.monotonic() + self.page_timeout
                continue
            if self._looks_like_verification():
                self.ensure_verification(return_url)
                deadline = time.monotonic() + self.page_timeout
                continue

            if time.monotonic() >= deadline:
                if not reloaded:
                    print("页面暂未出现帖子，正在重新加载一次……", flush=True)
                    self.page.reload(wait_until="domcontentloaded", timeout=60_000)
                    reloaded = True
                    deadline = time.monotonic() + self.page_timeout
                    continue
                try:
                    title = self.page.title()
                except Exception:
                    title = "未知"
                raise RuntimeError(
                    f"等待帖子超过 {self.page_timeout * 2} 秒，页面仍没有可读取内容。"
                    f"当前页面：{self.page.url}（标题：{title}）。"
                    "请查看浏览器中是否显示空时间线、网络错误、账号限制或尚未完成的验证。"
                )
            self.page.wait_for_timeout(500)

    def visit(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        self.page.wait_for_timeout(800)
        self.ensure_login(url)
        self.ensure_verification(url)

    def collect_feed(
        self,
        url: str,
        limit: int,
        max_scrolls: int,
        on_post: Callable[[Post], None] | None = None,
    ) -> list[Post]:
        self.visit(url)
        posts: dict[str, Post] = {}
        no_growth = 0
        for _ in range(max_scrolls + 1):
            count_before_scan = len(posts)
            for article in self.wait_for_posts(url):
                try:
                    post = _post_from_article(article)
                except Exception:
                    # 时间线会在滚动时回收 DOM；单条失效不应中断整批任务。
                    continue
                if post and post.id not in posts:
                    posts[post.id] = post
                    if on_post:
                        on_post(post)
                if len(posts) >= limit:
                    return list(posts.values())[:limit]

            no_growth = no_growth + 1 if len(posts) == count_before_scan else 0
            if no_growth >= 4:
                break
            self.page.mouse.wheel(0, 1800)
            self.page.wait_for_timeout(1400)
        return list(posts.values())[:limit]

    def collect_urls(
        self,
        urls: list[str],
        on_post: Callable[[Post], None] | None = None,
    ) -> list[Post]:
        results: list[Post] = []
        for raw_url in urls:
            url = canonical_x_url(raw_url)
            wanted_id = STATUS_RE.search(url).group(1)  # type: ignore[union-attr]
            print(f"读取：{url}", flush=True)
            self.visit(url)
            found: Post | None = None
            for article in self.wait_for_posts(url):
                candidate = _post_from_article(article)
                if candidate and candidate.id == wanted_id:
                    found = candidate
                    break
            if found:
                results.append(found)
                if on_post:
                    on_post(found)
            else:
                print(f"警告：未能读取帖子 {wanted_id}（可能已删除、不可见或页面结构改变）。", file=sys.stderr)
        return results


def build_source_url(source: str, query: str | None) -> str:
    if source == "home":
        return "https://x.com/home"
    if source == "bookmarks":
        return "https://x.com/i/bookmarks"
    if source == "search":
        if not query:
            raise ValueError("--source search 时必须提供 --query。")
        return f"https://x.com/search?q={quote(query)}&src=typed_query&f=live"
    raise ValueError(f"不支持的来源：{source}")


def local_summary(posts: list[Post]) -> str:
    authors = Counter(post.handle or post.author for post in posts)
    tokens = Counter(
        token.lower()
        for post in posts
        for token in WORD_RE.findall(post.text)
        if token.lower() not in STOP_WORDS and not token.startswith("http")
    )
    top_terms = "、".join(term for term, _ in tokens.most_common(10)) or "未提取到"
    top_authors = "、".join(f"{name}（{count}）" for name, count in authors.most_common(5))
    representative = sorted(posts, key=lambda post: len(post.text), reverse=True)[:5]
    lines = [
        "# X 帖子摘要（本地模式）",
        "",
        f"共读取 {len(posts)} 条帖子，涉及 {len(authors)} 位作者。",
        f"高频词：{top_terms}",
        f"活跃作者：{top_authors or '未提取到'}",
        "",
        "## 代表性内容",
        "",
    ]
    for post in representative:
        preview = re.sub(r"\s+", " ", post.text).strip()
        if len(preview) > 240:
            preview = preview[:237] + "…"
        lines.append(f"- {post.handle or post.author}：{preview} ([原帖]({post.url}))")
    lines.extend(["", "> 未配置 OPENAI_API_KEY，因此这是统计与摘录，不是模型生成的语义摘要。"])
    return "\n".join(lines)


def ai_summary(posts: list[Post], model: str) -> str:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("已设置 OPENAI_API_KEY，但缺少 openai 包。请安装 requirements.txt。") from exc

    compact_posts = [
        {
            "author": post.author,
            "handle": post.handle,
            "text": post.text,
            "created_at": post.created_at,
            "url": post.url,
        }
        for post in posts
    ]
    response = OpenAI().responses.create(
        model=model,
        instructions=(
            "你是严谨的中文信息分析助手。仅依据给定的 X 帖子总结，不补充未知事实。"
            "先给出 3-6 条核心主题，再写值得关注的观点、分歧或趋势，最后列出 3-8 条"
            "代表性帖子并保留其原帖链接。区分事实陈述与作者观点；重复内容合并。使用 Markdown。"
        ),
        input=json.dumps(compact_posts, ensure_ascii=False),
        max_output_tokens=2200,
        store=False,
    )
    if not response.output_text:
        raise RuntimeError("OpenAI API 没有返回可用文本。")
    return response.output_text.strip()


def save_results(
    posts: list[Post],
    summary: str,
    output_dir: Path,
    stamp: str | None = None,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = output_dir / f"posts-{stamp}.json"
    md_path = output_dir / f"summary-{stamp}.md"
    json_path.write_text(json.dumps([asdict(post) for post in posts], ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(summary + "\n", encoding="utf-8")
    return json_path.resolve(), md_path.resolve()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="浏览 X 帖子并生成中文摘要")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--url", action="append", help="指定帖子 URL，可重复使用")
    target.add_argument("--source", choices=("home", "search", "bookmarks"), default="home")
    parser.add_argument("--query", help="搜索关键词（配合 --source search）")
    parser.add_argument("--limit", type=int, default=20, help="最多读取帖子数（默认 20）")
    parser.add_argument("--max-scrolls", type=int, default=20, help="最多滚动次数（默认 20）")
    parser.add_argument("--profile", type=Path, help="自动化浏览器资料目录（默认按浏览器选择）")
    parser.add_argument("--output-dir", type=Path, default=Path("output"), help="输出目录")
    parser.add_argument("--browser", choices=("edge", "chromium"), default="edge", help="浏览器（默认 edge）")
    parser.add_argument("--cdp-url", help="附加到已开启远程调试的 Edge，例如 http://127.0.0.1:9222")
    parser.add_argument("--headless", action="store_true", help="无界面运行；首次登录不要使用")
    parser.add_argument("--page-timeout", type=int, default=30, help="每次等待页面帖子的秒数（默认 30，会自动重载一次）")
    parser.add_argument("--summarizer", choices=("auto", "openai", "local"), default="auto")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-5-mini"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    args = parse_args(argv)
    if args.profile is None:
        args.profile = Path(".x-edge-profile" if args.browser == "edge" else ".x-profile")
    if args.limit < 1 or args.max_scrolls < 0 or args.page_timeout < 1:
        print("错误：limit/page-timeout 必须为正数，max-scrolls 不能为负数。", file=sys.stderr)
        return 2

    try:
        with IncrementalPostWriter(args.output_dir) as live_writer:
            print(f"浏览内容将实时保存到：{live_writer.jsonl_path}", flush=True)
            with XBrowser(
                args.profile.resolve(),
                args.headless,
                args.page_timeout,
                browser_name=args.browser,
                cdp_url=args.cdp_url,
            ) as browser:
                if args.url:
                    posts = browser.collect_urls(args.url, on_post=live_writer.write)
                else:
                    posts = browser.collect_feed(
                        build_source_url(args.source, args.query),
                        args.limit,
                        args.max_scrolls,
                        on_post=live_writer.write,
                    )
            if not posts:
                raise RuntimeError("没有读取到帖子。请确认账号权限、目标内容和网络状态。")

            use_openai = args.summarizer == "openai" or (
                args.summarizer == "auto" and bool(os.getenv("OPENAI_API_KEY"))
            )
            if args.summarizer == "openai" and not os.getenv("OPENAI_API_KEY"):
                raise RuntimeError("--summarizer openai 需要设置 OPENAI_API_KEY。")
            summary = ai_summary(posts, args.model) if use_openai else local_summary(posts)
            json_path, md_path = save_results(posts, summary, args.output_dir, stamp=live_writer.stamp)
        print(f"\n完成：读取 {len(posts)} 条帖子")
        print(f"实时记录：{live_writer.jsonl_path}")
        print(f"阅读版记录：{live_writer.md_path}")
        print(f"原始数据：{json_path}")
        print(f"摘要：{md_path}\n")
        print(summary)
        return 0
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
