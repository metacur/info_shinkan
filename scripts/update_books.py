#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
指定した文庫レーベルの新刊情報を版元ドットコムの検索RSSから取得し、
openBD APIで書影・あらすじを補強したうえで docs/data/books.json を更新する。

処理の流れ:
  1. 版元ドットコムの「新しい本」RSS(DISCOVERY_FEEDS)を取得
  2. タイトル/概要にレーベル名がそのまま含まれるものだけを候補として抽出
     (講談社や筑摩書房、早川書房は同一出版社内に複数の文庫レーベルがあるため、
      出版社名だけでなくレーベル名の文字列一致でも絞り込む)
  3. 候補ISBNをopenBD APIへまとめて問い合わせ、書影・あらすじを補強(取れなくても続行)
  4. 既知ISBN一覧(data/seen_isbns.json)と比較して新規分だけ追加
  5. docs/data/books.json (サイトが読み込む本体) を更新

失敗に強くする方針:
  - 1レーベルの取得に失敗しても他のレーベル処理は継続する
  - openBDで情報が取れなくても、RSSの情報だけでエントリを作る
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DATA_DIR = ROOT / "docs" / "data"
SEEN_FILE = DATA_DIR / "seen_isbns.json"
BOOKS_FILE = DOCS_DATA_DIR / "books.json"

USER_AGENT = "bunko-shinkan-checker/1.0 (+personal use; contact via GitHub repo)"
REQUEST_INTERVAL_SEC = 1.0  # 相手サーバへの配慮のための最低間隔
MAX_BOOKS_KEPT = 500  # books.json に保持する最大件数(古いものは切り捨て)

# 監視したいレーベルと、対応する出版社名(版元ドットコム上の表記)
LABELS = [
    {"label": "岩波文庫", "publisher": "岩波書店"},
    {"label": "講談社学術文庫", "publisher": "講談社"},
    {"label": "ちくま学芸文庫", "publisher": "筑摩書房"},
    {"label": "平凡社ライブラリー", "publisher": "平凡社"},
    {"label": "ハヤカワ文庫NF", "publisher": "早川書房"},
]

# 発見用のRSSフィード(複数指定可。全て取得して結果を統合する)。
# 版元ドットコムのトップページ「新しい本」に埋め込まれているRSSのURL(旧検索RSSは廃止されていたため差し替え)。
# レーベル名での絞り込みはこの後のスクリプト側(タイトル/概要の文字列一致)で行う。
# 別のRSSのURLが見つかったら、ここに追加すればよい。
DISCOVERY_FEEDS = [
    "https://www.hanmoto.com/ci/bd/search/hdt/none/pg/none/edate/today/jpo/0/hgroup/1/skipnoimg/1/order/desc/sdate/today/vw/rss20",
]


def log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", file=sys.stderr)


def http_get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


ISBN_RE = re.compile(r"(97[89]\d{10}|\d{9}[\dXx])")


def extract_isbn(text: str) -> str | None:
    m = ISBN_RE.search(text or "")
    return m.group(1) if m else None


def fetch_feed_items(url: str) -> list[dict]:
    """RSSを取得し、[{title, link, pubdate, description, isbn}, ...] を返す。失敗時は原因を診断ログに出す。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as res:
            raw = res.read()
            status = res.status
            ctype = res.headers.get("Content-Type", "")
            final_url = res.geturl()
    except Exception as e:  # noqa: BLE001
        log(f"  [WARN] RSS取得失敗: {url} ({e})")
        return []

    log(f"  取得: status={status} content-type={ctype} bytes={len(raw)}")
    if final_url != url:
        log(f"  リダイレクト先: {final_url}")

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        log(f"  [WARN] RSSパース失敗 ({e})")
        log(f"    先頭300文字: {raw[:300].decode('utf-8', errors='replace')!r}")
        return []

    items = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pubdate = (item.findtext("pubDate") or "").strip()
        description = (item.findtext("description") or "").strip()
        isbn = extract_isbn(link) or extract_isbn(description)
        items.append(
            {
                "title": title,
                "link": link,
                "pubdate": pubdate,
                "description": description,
                "isbn": isbn,
            }
        )
    return items


def filter_by_label(items: list[dict], label: str) -> list[dict]:
    """タイトルまたは概要にレーベル名がそのまま含まれるものだけ残す"""
    matched = []
    for it in items:
        haystack = f"{it['title']} {it['description']}"
        if label in haystack:
            matched.append(it)
    return matched


def enrich_with_openbd(isbns: list[str]) -> dict:
    """
    openBD APIへまとめて問い合わせ、ISBNごとの補強情報(書影・あらすじ・シリーズ名)を返す。
    取得できなかったISBNは結果に含めない(呼び出し側で存在チェックすること)。
    """
    if not isbns:
        return {}
    result = {}
    # openBDは1リクエストで多数のISBNを受け付けるが、安全のため小分けにする
    chunk_size = 100
    for i in range(0, len(isbns), chunk_size):
        chunk = isbns[i : i + chunk_size]
        url = "https://api.openbd.jp/v1/get?isbn=" + ",".join(chunk)
        try:
            raw = http_get(url)
            data = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            log(f"  [WARN] openBD取得失敗: ({e})")
            continue
        for entry in data:
            if not entry:
                continue
            summary = entry.get("summary", {}) or {}
            isbn = summary.get("isbn")
            if not isbn:
                continue
            result[isbn] = {
                "series": summary.get("series"),
                "cover": summary.get("cover"),
                "description": summary.get("description") or "",
                "author": summary.get("author") or "",
                "publisher": summary.get("publisher") or "",
                "pubdate": summary.get("pubdate") or "",
            }
        time.sleep(REQUEST_INTERVAL_SEC)
    return result


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> None:
    seen_isbns: set[str] = set(load_json(SEEN_FILE, []))
    existing_books: list[dict] = load_json(BOOKS_FILE, [])

    new_books: list[dict] = []

    # 発見用フィードを取得(ISBNで重複排除)
    all_items: list[dict] = []
    seen_in_feed: set[str] = set()
    for feed_url in DISCOVERY_FEEDS:
        log(f"RSS取得中: {feed_url}")
        for it in fetch_feed_items(feed_url):
            key = it["isbn"] or it["link"]
            if key in seen_in_feed:
                continue
            seen_in_feed.add(key)
            all_items.append(it)
        time.sleep(REQUEST_INTERVAL_SEC)
    log(f"RSS合計 {len(all_items)}件")

    for entry in LABELS:
        label = entry["label"]
        publisher = entry["publisher"]
        matched = filter_by_label(all_items, label)
        log(f"[{label}] RSS {len(all_items)}件中 {len(matched)}件がレーベル名一致")

        candidate_isbns = [m["isbn"] for m in matched if m["isbn"]]
        openbd_info = enrich_with_openbd(candidate_isbns)

        for m in matched:
            isbn = m["isbn"]
            if not isbn or isbn in seen_isbns:
                continue

            info = openbd_info.get(isbn, {})
            # openBD側にもシリーズ名があり、かつ目的のレーベルと明らかに異なる場合は除外
            # (レーベル名の偶然の文字列一致による誤検出を防ぐ簡易チェック)
            openbd_series = (info.get("series") or "").replace(" ", "")
            if openbd_series and label.replace(" ", "") not in openbd_series and openbd_series not in label:
                log(f"  [SKIP] openBDのシリーズ名不一致のため除外: {m['title']} (series={openbd_series})")
                continue

            new_books.append(
                {
                    "label": label,
                    "publisher": publisher,
                    "title": m["title"],
                    "isbn": isbn,
                    "link": m["link"] or f"https://www.hanmoto.com/bd/isbn/{isbn}",
                    "pubdate": info.get("pubdate") or m["pubdate"],
                    "cover": info.get("cover"),
                    "description": info.get("description") or m["description"],
                    "author": info.get("author") or "",
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            seen_isbns.add(isbn)

    if new_books:
        log(f"新着 {len(new_books)}件を追加します")
    else:
        log("新着はありませんでした")

    merged = new_books + existing_books
    # 発売日の新しい順に並べ替え(取得できない場合は末尾扱い)
    merged.sort(key=lambda b: b.get("pubdate") or "", reverse=True)
    merged = merged[:MAX_BOOKS_KEPT]

    save_json(BOOKS_FILE, merged)
    save_json(SEEN_FILE, sorted(seen_isbns))

    log("完了")


if __name__ == "__main__":
    main()
