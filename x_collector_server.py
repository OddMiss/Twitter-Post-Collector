#!/usr/bin/env python3
"""接收 Edge 扩展采集的 X 帖子，实时保存并生成摘要。"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from organize_posts_by_date import archive_posts_by_date
from x_digest import IncrementalPostWriter, Post, ai_summary, local_summary, save_results


MAX_BODY_BYTES = 2 * 1024 * 1024
TOKEN_PATH = Path(".collector-token")


def post_from_payload(value: Any) -> Post:
    if not isinstance(value, dict):
        raise ValueError("帖子必须是 JSON 对象。")
    post_id = str(value.get("id", "")).strip()
    text = str(value.get("text", "")).strip()
    url = str(value.get("url", "")).strip()
    parsed = urlparse(url)
    if not post_id.isdigit() or len(post_id) > 30:
        raise ValueError("帖子 id 无效。")
    if not text or len(text) > 20_000:
        raise ValueError("帖子正文为空或过长。")
    if parsed.scheme != "https" or parsed.hostname not in {
        "x.com", "www.x.com", "twitter.com", "www.twitter.com"
    }:
        raise ValueError("帖子 URL 必须来自 x.com 或 twitter.com。")

    def optional(name: str, limit: int = 200) -> str | None:
        raw = value.get(name)
        if raw is None:
            return None
        result = str(raw).strip()
        return result[:limit] or None

    return Post(
        id=post_id,
        url=url[:1000],
        author=(optional("author") or "")[:200],
        handle=(optional("handle") or "")[:100],
        text=text,
        created_at=optional("created_at", 100),
        reply_count=optional("reply_count", 100),
        repost_count=optional("repost_count", 100),
        like_count=optional("like_count", 100),
    )


def load_or_create_token(path: Path) -> str:
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(24)
    path.write_text(token + "\n", encoding="utf-8")
    return token


def post_fingerprint(post: Post) -> str:
    """按作者和标准化正文去重，不依赖可能不同的帖子 ID。"""
    author = (post.handle or post.author).strip().casefold()
    text = " ".join(post.text.split()).casefold()
    return hashlib.sha256(f"{author}\0{text}".encode("utf-8")).hexdigest()


class HistoryIndex:
    """SQLite 永久去重索引，并负责首次导入已有输出文件。"""

    def __init__(self, path: Path, output_dir: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen_posts (
                post_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL UNIQUE,
                author_key TEXT NOT NULL,
                source_url TEXT NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_seen_posts_fingerprint
                ON seen_posts(fingerprint);
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self.connection.commit()
        imported = self._bootstrap_existing_files(output_dir)
        if imported:
            print(f"已从历史输出文件导入 {imported} 条永久去重记录。", flush=True)

    @staticmethod
    def _author_key(post: Post) -> str:
        return (post.handle or post.author).strip().casefold()

    def _insert(self, post: Post) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO seen_posts
                (post_id, fingerprint, author_key, source_url)
            VALUES (?, ?, ?, ?)
            """,
            (post.id, post_fingerprint(post), self._author_key(post), post.url),
        )
        return cursor.rowcount > 0

    def _bootstrap_existing_files(self, output_dir: Path) -> int:
        with self.lock:
            row = self.connection.execute(
                "SELECT value FROM metadata WHERE key = 'history_bootstrapped'"
            ).fetchone()
            if row:
                return 0

            imported = 0
            candidates: list[Any] = []
            for path in sorted(output_dir.glob("posts-*.json")):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(value, list):
                        candidates.extend(value)
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
            for path in sorted(output_dir.glob("browsed-*.jsonl")):
                try:
                    with path.open("r", encoding="utf-8") as handle:
                        for line in handle:
                            try:
                                candidates.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
                except (OSError, UnicodeError):
                    continue

            for value in candidates:
                try:
                    post = post_from_payload(value)
                except ValueError:
                    continue
                if self._insert(post):
                    imported += 1
            self.connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES ('history_bootstrapped', '1')"
            )
            self.connection.commit()
            return imported

    def contains(self, post: Post) -> bool:
        fingerprint = post_fingerprint(post)
        with self.lock:
            row = self.connection.execute(
                "SELECT 1 FROM seen_posts WHERE post_id = ? OR fingerprint = ? LIMIT 1",
                (post.id, fingerprint),
            ).fetchone()
            return row is not None

    def add(self, post: Post) -> bool:
        with self.lock:
            inserted = self._insert(post)
            self.connection.commit()
            return inserted

    def count(self) -> int:
        with self.lock:
            return int(self.connection.execute("SELECT COUNT(*) FROM seen_posts").fetchone()[0])

    def close(self) -> None:
        with self.lock:
            self.connection.close()


class CollectionSession:
    def __init__(
        self,
        output_dir: Path,
        summarizer: str,
        model: str,
        history_db: Path | None = None,
        date_output_dir: Path | None = None,
    ) -> None:
        self.writer: IncrementalPostWriter | None = None
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.summarizer = summarizer
        self.model = model
        self.date_output_dir = date_output_dir or (self.output_dir / "output-by-date")
        self.history = HistoryIndex(
            history_db or (self.output_dir / "collector-history.sqlite3"),
            self.output_dir,
        )
        self.posts: dict[str, Post] = {}
        # 服务进程存活期间持续保留全局去重索引，跨摘要批次仍然有效。
        self.all_post_ids: set[str] = set()
        self.all_fingerprints: set[str] = set()
        self.lock = threading.RLock()
        self.last_finished: dict[str, Any] | None = None
        self.completed_sessions = 0

    def _ensure_writer(self) -> IncrementalPostWriter:
        if self.writer is None:
            self.writer = IncrementalPostWriter(self.output_dir)
            self.writer.__enter__()
        return self.writer

    def add(self, values: list[Any]) -> dict[str, int]:
        added = 0
        rejected = 0
        with self.lock:
            for value in values:
                try:
                    post = post_from_payload(value)
                except ValueError:
                    rejected += 1
                    continue
                fingerprint = post_fingerprint(post)
                if (
                    post.id in self.all_post_ids
                    or fingerprint in self.all_fingerprints
                    or self.history.contains(post)
                ):
                    continue
                self._ensure_writer().write(post)
                if not self.history.add(post):
                    # 理论上只会在另一个服务进程同时使用同一数据库时发生。
                    continue
                self.posts[post.id] = post
                self.all_post_ids.add(post.id)
                self.all_fingerprints.add(fingerprint)
                added += 1
        return {
            "added": added,
            "rejected": rejected,
            "total": len(self.posts),
            "completed_sessions": self.completed_sessions,
            "historical_total": self.history.count(),
        }

    def finish(self) -> dict[str, Any]:
        with self.lock:
            posts = list(self.posts.values())
            if not posts:
                if self.last_finished:
                    return {**self.last_finished, "already_finished": True, "ready_for_next": True}
                raise RuntimeError("还没有收到任何帖子。")
            writer = self._ensure_writer()
            use_openai = self.summarizer == "openai" or (
                self.summarizer == "auto" and bool(os.getenv("OPENAI_API_KEY"))
            )
            if self.summarizer == "openai" and not os.getenv("OPENAI_API_KEY"):
                raise RuntimeError("使用 OpenAI 摘要需要设置 OPENAI_API_KEY。")
            summary = ai_summary(posts, self.model) if use_openai else local_summary(posts)
            json_path, summary_path = save_results(
                posts,
                summary,
                self.output_dir,
                stamp=writer.stamp,
            )
            date_archive = archive_posts_by_date(posts, self.date_output_dir)
            result = {
                "total": len(posts),
                "json_path": str(json_path),
                "summary_path": str(summary_path),
                "browsed_jsonl_path": str(writer.jsonl_path),
                "browsed_markdown_path": str(writer.md_path),
                "date_archive": date_archive,
                "ready_for_next": True,
            }
            print(f"\n摘要已生成：{summary_path}", flush=True)
            print(
                "按北京时间归档完成："
                f"新增 {date_archive['added']} 条，"
                f"忽略无发布时间 {date_archive['ignored_without_time']} 条，"
                f"更新 {date_archive['affected_dates']} 个日期文件。",
                flush=True,
            )
            writer.__exit__(None, None, None)
            self.writer = None
            self.posts.clear()
            self.completed_sessions += 1
            result["completed_sessions"] = self.completed_sessions
            self.last_finished = result
            print("服务端已准备好，可直接开始下一批采集。", flush=True)
            return result

    def close(self) -> None:
        if self.writer is not None:
            self.writer.__exit__(None, None, None)
            self.writer = None
        self.history.close()


class CollectorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], token: str, session: CollectionSession):
        super().__init__(address, CollectorHandler)
        self.token = token
        self.session = session


class CollectorHandler(BaseHTTPRequestHandler):
    server: CollectorServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _origin(self) -> str | None:
        origin = self.headers.get("Origin")
        if origin and origin.startswith("chrome-extension://"):
            return origin
        return None

    def end_headers(self) -> None:
        origin = self._origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-X-Collector-Token")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-X-Collector-Token", "")
        return hmac.compare_digest(supplied, self.server.token)

    def _body(self) -> dict[str, Any]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效。") from exc
        if size < 1 or size > MAX_BODY_BYTES:
            raise ValueError("请求内容为空或过大。")
        value = json.loads(self.rfile.read(size).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求必须是 JSON 对象。")
        return value

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:
        if self.path != "/health":
            self._json(404, {"ok": False, "error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"ok": False, "error": "令牌不正确"})
            return
        self._json(200, {
            "ok": True,
            "total": len(self.server.session.posts),
            "completed_sessions": self.server.session.completed_sessions,
            "historical_total": self.server.session.history.count(),
            "ready": True,
        })

    def do_POST(self) -> None:
        if not self._authorized():
            self._json(401, {"ok": False, "error": "令牌不正确"})
            return
        try:
            payload = self._body()
            if self.path == "/posts":
                posts = payload.get("posts")
                if not isinstance(posts, list) or len(posts) > 100:
                    raise ValueError("posts 必须是最多 100 项的数组。")
                result = self.server.session.add(posts)
                self._json(200, {"ok": True, **result})
            elif self.path == "/finish":
                result = self.server.session.finish()
                self._json(200, {"ok": True, **result})
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            print(f"处理请求失败：{exc}", flush=True)
            self._json(500, {"ok": False, "error": "服务器内部错误"})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="X Edge 扩展的本机采集服务")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument(
        "--date-output-dir",
        type=Path,
        default=Path("output-by-date"),
        help="按北京时间归档的每日 JSON 目录（默认 output-by-date）",
    )
    parser.add_argument("--history-db", type=Path, help="永久去重 SQLite 路径（默认 output/collector-history.sqlite3）")
    parser.add_argument("--summarizer", choices=("auto", "openai", "local"), default="auto")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-5-mini"))
    return parser.parse_args()


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    args = parse_args()
    if not 1 <= args.port <= 65535:
        print("错误：端口必须在 1-65535 之间。", flush=True)
        return 2

    token = load_or_create_token(TOKEN_PATH)
    session = CollectionSession(
        args.output_dir,
        args.summarizer,
        args.model,
        args.history_db,
        args.date_output_dir,
    )
    try:
        server = CollectorServer(("127.0.0.1", args.port), token, session)
    except OSError as exc:
        session.close()
        print(f"错误：无法监听 127.0.0.1:{args.port}，端口可能已被占用：{exc}", flush=True)
        return 1
    print("X 帖子采集服务已启动。", flush=True)
    print(f"地址：http://127.0.0.1:{args.port}", flush=True)
    print(f"扩展连接令牌：{token}", flush=True)
    print("首次使用时把令牌粘贴到 Edge 扩展中。按 Ctrl+C 可停止服务。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止……", flush=True)
        if session.posts:
            try:
                session.finish()
            except Exception as exc:
                print(f"生成摘要失败：{exc}", flush=True)
    finally:
        server.server_close()
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
