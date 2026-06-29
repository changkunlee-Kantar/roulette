#!/usr/bin/env python3
"""Scrape Gangnam Unni community posts and comments into CSV files."""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import os
import warnings

import requests
import urllib3

DEFAULT_URLS = [
    "https://www.gangnamunni.com/community/4278803",
    "https://www.gangnamunni.com/community/4281832",
]

BASE_URL = "https://www.gangnamunni.com"
API_BASE = f"{BASE_URL}/api/solar"
OUTPUT_DIR = Path(__file__).parent / "output"

CSV_COLUMNS = [
    "type",
    "post_id",
    "comment_id",
    "parent_comment_id",
    "category",
    "author_nickname",
    "author_id",
    "created_at",
    "content",
    "like_count",
    "comment_count",
    "url",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REQUEST_DELAY_SEC = 0.75
SSL_VERIFY = os.environ.get("GU_SSL_VERIFY", "0").lower() in ("1", "true", "yes")

if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def extract_post_id(url: str) -> int:
    path = urlparse(url).path.rstrip("/")
    match = re.search(r"/community/(\d+)$", path)
    if not match:
        raise ValueError(f"Could not extract post ID from URL: {url}")
    return int(match.group(1))


def fetch_page_data(post_id: int, session: requests.Session) -> tuple[str, dict[str, Any]]:
    url = f"{BASE_URL}/community/{post_id}"
    response = session.get(url, timeout=30)
    response.raise_for_status()

    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        response.text,
        re.DOTALL,
    )
    if not match:
        raise RuntimeError(f"__NEXT_DATA__ not found for post {post_id}")

    data = json.loads(match.group(1))
    page_props = data["props"]["pageProps"]
    auth = page_props["headers"]["Authorization"]
    post_detail = page_props["communityDocumentDetail"]
    return auth, post_detail


def api_post(
    session: requests.Session,
    auth: str,
    post_id: int,
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    headers = {
        "User-Agent": USER_AGENT,
        "Authorization": auth,
        "Accept-Language": "ko-KR",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/community/{post_id}",
    }
    response = session.post(f"{API_BASE}{path}", json=payload, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()


def normalize_post_row(post_id: int, detail: dict[str, Any]) -> dict[str, str]:
    writer = detail.get("writer") or {}
    return {
        "type": "post",
        "post_id": str(post_id),
        "comment_id": "",
        "parent_comment_id": "",
        "category": detail.get("categoryName") or "",
        "author_nickname": writer.get("nickname") or writer.get("nickName") or "",
        "author_id": str(writer.get("id") or ""),
        "created_at": detail.get("createTime") or "",
        "content": detail.get("contents") or detail.get("title") or "",
        "like_count": str(detail.get("thumbUpCount") or 0),
        "comment_count": str(detail.get("commentCount") or 0),
        "url": f"{BASE_URL}/community/{post_id}",
    }


def normalize_api_comment(comment: dict[str, Any], post_id: int) -> dict[str, str]:
    author = comment.get("author") or {}
    content = comment.get("content") or {}
    metrics = comment.get("metrics") or {}
    parent_id = comment.get("replyCommentId")

    return {
        "type": "comment",
        "post_id": str(post_id),
        "comment_id": str(comment.get("commentId") or ""),
        "parent_comment_id": str(parent_id) if parent_id else "",
        "category": "",
        "author_nickname": author.get("nickName") or author.get("nickname") or "",
        "author_id": str(author.get("id") or ""),
        "created_at": comment.get("writtenAt") or comment.get("createTime") or "",
        "content": content.get("text") if isinstance(content, dict) else str(content or ""),
        "like_count": str(metrics.get("likeCount") or comment.get("thumbUpCount") or 0),
        "comment_count": "",
        "url": "",
    }


def normalize_ssr_comment(comment: dict[str, Any], post_id: int) -> dict[str, str]:
    writer = comment.get("writer") or {}
    return {
        "type": "comment",
        "post_id": str(post_id),
        "comment_id": str(comment.get("id") or ""),
        "parent_comment_id": "",
        "category": "",
        "author_nickname": writer.get("nickname") or writer.get("nickName") or "",
        "author_id": str(writer.get("id") or ""),
        "created_at": comment.get("createTime") or "",
        "content": comment.get("contents") or "",
        "like_count": str(comment.get("thumbUpCount") or 0),
        "comment_count": "",
        "url": "",
    }


def fetch_all_replies(
    session: requests.Session,
    auth: str,
    post_id: int,
    comment_id: int,
    initial_replies: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    replies_block = initial_replies or {}
    items = list(replies_block.get("items") or [])
    cursor = replies_block.get("nextCursor")
    has_next = replies_block.get("hasNext", False)

    while has_next:
        time.sleep(REQUEST_DELAY_SEC)
        result = api_post(
            session,
            auth,
            post_id,
            "/community/query/user/v1/get-comment-replies/process",
            {"id": comment_id, "cursor": {"pageSize": 20, "cursor": cursor}},
        )
        items.extend(result.get("items") or [])
        cursor = result.get("nextCursor")
        has_next = result.get("hasNext", False)

    return items


def flatten_comment_tree(
    session: requests.Session,
    auth: str,
    post_id: int,
    comment: dict[str, Any],
    rows: list[dict[str, str]],
    seen_ids: set[str],
) -> None:
    if "commentId" in comment:
        row = normalize_api_comment(comment, post_id)
    else:
        row = normalize_ssr_comment(comment, post_id)

    comment_id = row["comment_id"]
    if not comment_id or comment_id in seen_ids:
        return

    seen_ids.add(comment_id)
    rows.append(row)

    replies_block = comment.get("replies")
    if isinstance(replies_block, dict):
        reply_items = fetch_all_replies(
            session,
            auth,
            post_id,
            int(comment_id),
            replies_block,
        )
    elif isinstance(replies_block, list):
        reply_items = replies_block
    else:
        reply_items = []

    for reply in reply_items:
        flatten_comment_tree(session, auth, post_id, reply, rows, seen_ids)


def fetch_all_comments(
    session: requests.Session,
    auth: str,
    post_id: int,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    cursor = None

    while True:
        result = api_post(
            session,
            auth,
            post_id,
            "/community/query/user/v1/get-comments/process",
            {
                "postId": post_id,
                "sort": "latest",
                "cursor": {"pageSize": 20, "cursor": cursor},
            },
        )

        for comment in result.get("items") or []:
            flatten_comment_tree(session, auth, post_id, comment, rows, seen_ids)

        if not result.get("hasNext"):
            break

        cursor = result.get("nextCursor")
        time.sleep(REQUEST_DELAY_SEC)

    return rows


def scrape_post(post_id: int, session: requests.Session) -> list[dict[str, str]]:
    auth, post_detail = fetch_page_data(post_id, session)
    time.sleep(REQUEST_DELAY_SEC)

    post_row = normalize_post_row(post_id, post_detail)
    comment_rows = fetch_all_comments(session, auth, post_id)
    return [post_row, *comment_rows]


def write_csv(rows: list[dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape Gangnam Unni community posts to CSV")
    parser.add_argument(
        "--url",
        action="append",
        dest="urls",
        help="Community post URL (can be repeated)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help="Directory for output CSV files",
    )
    args = parser.parse_args()

    urls = args.urls or DEFAULT_URLS
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    session.verify = SSL_VERIFY
    if not SSL_VERIFY:
        warnings.warn(
            "SSL verification disabled (set GU_SSL_VERIFY=1 to enable).",
            stacklevel=1,
        )

    for url in urls:
        post_id = extract_post_id(url)
        print(f"Scraping post {post_id}...")
        rows = scrape_post(post_id, session)
        output_path = args.output_dir / f"community_{post_id}.csv"
        write_csv(rows, output_path)

        post_count = sum(1 for r in rows if r["type"] == "post")
        comment_count = sum(1 for r in rows if r["type"] == "comment")
        reply_count = sum(1 for r in rows if r["type"] == "comment" and r["parent_comment_id"])
        print(
            f"  Saved {output_path}: {len(rows)} rows "
            f"(posts={post_count}, comments={comment_count}, replies={reply_count})"
        )


if __name__ == "__main__":
    main()
