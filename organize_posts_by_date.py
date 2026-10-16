#!/usr/bin/env python3
"""把采集到的 X 帖子按北京时间增量归档到每日 JSON 文件。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def parse_post_time(value: Any) -> datetime | None:
    """解析带时区的 ISO 时间并转换到北京时间；模糊或无时区时间不归档。"""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(BEIJING_TZ)


def _post_dict(value: Any) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("帖子必须是字典或 dataclass 实例。")


def _fingerprint(post: Mapping[str, Any]) -> str:
    author = str(post.get("handle") or post.get("author") or "").strip().casefold()
    text = " ".join(str(post.get("text") or "").split()).casefold()
    return hashlib.sha256(f"{author}\0{text}".encode("utf-8")).hexdigest()


def _normalized_post(value: Any) -> tuple[str, dict[str, Any]] | None:
    post = _post_dict(value)
    published = parse_post_time(post.get("created_at"))
    if published is None:
        return None
    post["id"] = str(post.get("id") or "").strip()
    post["created_at"] = published.isoformat(timespec="seconds")
    return published.strftime("%Y%m%d"), post


def _load_day(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取已有日期文件 {path}：{exc}") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise RuntimeError(f"已有日期文件格式无效：{path}")
    return value


def _write_day_atomic(path: Path, posts: list[dict[str, Any]]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(posts, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def archive_posts_by_date(posts: Iterable[Any], output_dir: Path) -> dict[str, Any]:
    """增量合并帖子，只读取和重写本批次涉及的日期文件。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[dict[str, Any]]] = {}
    input_count = 0
    ignored_without_time = 0

    for value in posts:
        input_count += 1
        normalized = _normalized_post(value)
        if normalized is None:
            ignored_without_time += 1
            continue
        day, post = normalized
        grouped.setdefault(day, []).append(post)

    added = 0
    duplicates = 0
    affected_files: list[str] = []
    for day, incoming in sorted(grouped.items()):
        path = output_dir / f"{day}.json"
        existing = _load_day(path)
        merged: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_fingerprints: set[str] = set()
        day_added = 0

        for value in existing:
            normalized = _normalized_post(value)
            if normalized is None or normalized[0] != day:
                raise RuntimeError(f"日期文件 {path} 中存在无时间或日期不匹配的帖子。")
            post = normalized[1]
            post_id = str(post.get("id") or "")
            fingerprint = _fingerprint(post)
            if (post_id and post_id in seen_ids) or fingerprint in seen_fingerprints:
                continue
            if post_id:
                seen_ids.add(post_id)
            seen_fingerprints.add(fingerprint)
            merged.append(post)

        for post in incoming:
            post_id = str(post.get("id") or "")
            fingerprint = _fingerprint(post)
            if (post_id and post_id in seen_ids) or fingerprint in seen_fingerprints:
                duplicates += 1
                continue
            if post_id:
                seen_ids.add(post_id)
            seen_fingerprints.add(fingerprint)
            merged.append(post)
            added += 1
            day_added += 1

        if day_added or not path.exists():
            merged.sort(key=lambda item: (str(item["created_at"]), str(item.get("id") or "")))
            _write_day_atomic(path, merged)
            affected_files.append(str(path.resolve()))

    return {
        "input": input_count,
        "added": added,
        "duplicates": duplicates,
        "ignored_without_time": ignored_without_time,
        "affected_dates": len(affected_files),
        "files": affected_files,
    }


def load_finished_batches(input_dir: Path) -> list[dict[str, Any]]:
    """读取所有已完成批次；实时 JSONL 不参与，避免重复扫描未完成批次。"""
    posts: list[dict[str, Any]] = []
    for path in sorted(input_dir.glob("posts-*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取 {path}：{exc}") from exc
        if not isinstance(value, list):
            raise RuntimeError(f"批次文件不是帖子数组：{path}")
        posts.extend(item for item in value if isinstance(item, dict))
    return posts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按北京时间把 X 帖子增量归档到每日 JSON")
    parser.add_argument("--input-dir", type=Path, default=Path("output"))
    parser.add_argument("--output-dir", type=Path, default=Path("output-by-date"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        posts = load_finished_batches(args.input_dir)
        result = archive_posts_by_date(posts, args.output_dir)
    except (OSError, RuntimeError, TypeError) as exc:
        print(f"错误：{exc}")
        return 1
    print(
        "按日期归档完成："
        f"新增 {result['added']} 条，忽略无发布时间 {result['ignored_without_time']} 条，"
        f"更新 {result['affected_dates']} 个日期文件。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
