#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
指定した文庫レーベルの新刊情報を、国立国会図書館サーチ(NDLサーチ)のSRU APIから取得し、
openBD APIで書影・あらすじを補強したうえで docs/data/books.json を更新する。

処理の流れ:
  1. レーベルごとに、出版者 + キーワード + 出版年月の範囲でNDLサーチ(SRU)を検索
     - 通常検索に加えて、inprocess=true(NDL新着書誌情報=刊行前の作成中書誌)でも検索し、統合する
  2. 返ってきたレコード全体の文字列にレーベル名が含まれるものだけを残す
     (同一出版社内の別レーベルや無関係な本を除外。表記ゆれ(空白・ピリオド・セミコロン等)は無視して比較)
  3. 候補ISBNをopenBD APIへ問い合わせ、書影・あらすじを補強(取れなくても続行)
  4. 既知ISBN一覧(data/seen_isbns.json)と比較して新規分だけ追加
  5. docs/data/books.json (サイトが読み込む本体) を更新

NDLサーチAPIの仕様: https://ndlsearch.ndl.go.jp/help/api/specifications
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DATA_DIR = ROOT / "docs" / "data"
SEEN_FILE = DATA_DIR / "seen_isbns.json"
BOOKS_FILE = DOCS_DATA_DIR / "books.json"

USER_AGENT = "bunko-shinkan-checker/1.0 (personal use; https://github.com/)"
REQUEST_INTERVAL_SEC = 1.0  # 相手サーバへの配慮のための最低間隔
MAX_BOOKS_KEPT = 500        # books.json に保持する最大件数
LOOKBACK_DAYS = 30          # 何日前の月から検索するか
LOOKAHEAD_DAYS = 120        # 何日先の月まで検索するか(刊行予定の書誌も含めるため)
MAX_RECORDS = 500           # SRUの1回あたり最大取得件数(仕様上の上限)

NDL_SRU = "https://ndlsearch.ndl.go.jp/api/sru"

# 監視したいレーベル。
#   label     : 表示名 & 最終判定に使う名前(レコード全体の文字列に含まれるかで判定)
#   publisher : NDLサーチ上の出版者名(部分一致)
#   keyword   : 検索時のキーワード(表記ゆれに強いよう短めにする。最終判定は label で厳密に行う)
LABELS = [
    {"label": "岩波文庫", "publisher": "岩波書店", "keyword": "岩波文庫"},
    {"label": "講談社学術文庫", "publisher": "講談社", "keyword": "講談社学術文庫"},
    {"label": "ちくま学芸文庫", "publisher": "筑摩書房", "keyword": "ちくま学芸文庫"},
    {"label": "平凡社ライブラリー", "publisher": "平凡社", "keyword": "平凡社ライブラリー"},
    {"label": "ハヤカワ文庫NF", "publisher": "早川書房", "keyword": "ハヤカワ文庫"},
]


def log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", file=sys.stderr)


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


# ---------- ユーティリティ ----------

_NORM_RE = re.compile(r"[\s　.．。;；:：,，、・/／\-－ー_]+")


def norm(text: str) -> str:
    """比較用に、空白・句読点・記号を取り除く(「ハヤカワ文庫. NF ; 600」→「ハヤカワ文庫NF600」)"""
    return _NORM_RE.sub("", text or "")


ISBN_RE = re.compile(r"(?<![\d\-])(97[89][\d\-]{9,13}\d)(?![\d\-]?\d)")


def extract_isbn(text: str) -> str | None:
    """ハイフン付きでも拾い、ハイフンを除いた13桁ISBNを返す"""
    for m in ISBN_RE.finditer(text or ""):
        digits = m.group(1).replace("-", "")
        if len(digits) == 13:
            return digits
    return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# ---------- NDLサーチ(SRU) ----------

def month_str(d: date) -> str:
    return d.strftime("%Y-%m")  # from/until は同じ書式(YYYY-MM)で揃える必要がある


def build_sru_url(publisher: str, keyword: str, inprocess: bool) -> str:
    today = date.today()
    start = month_str(today - timedelta(days=LOOKBACK_DAYS))
    end = month_str(today + timedelta(days=LOOKAHEAD_DAYS))
    cql = (
        f'publisher="{publisher}" AND anywhere="{keyword}" '
        f'AND from="{start}" AND until="{end}"'
    )
    params = {
        "operation": "searchRetrieve",
        "version": "1.2",
        "recordSchema": "dcndl",
        "recordPacking": "xml",
        "maximumRecords": str(MAX_RECORDS),
        "query": cql,
    }
    if inprocess:
        params["inprocess"] = "true"
    return f"{NDL_SRU}?{urllib.parse.urlencode(params)}"


def parse_sru_records(raw: bytes) -> tuple[list[dict], str]:
    """SRUレスポンスから [{title, pubdate, isbn, text}] を返す。スキーマに依存しないよう全文字列から拾う。"""
    root = ET.fromstring(raw)
    number = ""
    diagnostics = []
    for el in root.iter():
        name = _local(el.tag)
        if name == "numberOfRecords":
            number = (el.text or "").strip()
        elif name == "message" and (el.text or "").strip():
            diagnostics.append(el.text.strip())

    records = []
    for rd in root.iter():
        if _local(rd.tag) != "recordData":
            continue
        texts = [t.strip() for t in rd.itertext() if t and t.strip()]
        # 属性値(rdf:resourceなど)にISBNやIDが入る場合もあるので、属性も連結して探す
        attrs = " ".join(v for e in rd.iter() for v in e.attrib.values())
        full_text = " ".join(texts)

        title = ""
        pubdate = ""
        for e in rd.iter():
            n = _local(e.tag)
            if n == "title" and not title and (e.text or "").strip():
                title = e.text.strip()
            elif n in ("issued", "date") and not pubdate and (e.text or "").strip():
                pubdate = e.text.strip()
        records.append(
            {
                "title": title or (texts[0] if texts else ""),
                "pubdate": pubdate,
                "isbn": extract_isbn(full_text) or extract_isbn(attrs),
                "text": full_text,
            }
        )
    info = f"numberOfRecords={number or '?'}"
    if diagnostics:
        info += f" diagnostics={diagnostics}"
    return records, info


def fetch_ndl_records(publisher: str, keyword: str, inprocess: bool) -> list[dict]:
    url = build_sru_url(publisher, keyword, inprocess)
    mode = "新着書誌(inprocess)" if inprocess else "通常"
    try:
        raw = http_get(url)
    except Exception as e:  # noqa: BLE001
        log(f"  [WARN] NDL取得失敗[{mode}]: {e}")
        return []
    try:
        records, info = parse_sru_records(raw)
    except ET.ParseError as e:
        log(f"  [WARN] NDLレスポンスのパース失敗[{mode}]: {e}")
        log(f"    先頭300文字: {raw[:300].decode('utf-8', errors='replace')!r}")
        return []
    log(f"  [{mode}] {info} / 解析できたレコード {len(records)}件")
    if not records:
        log(f"    URL: {url}")
        log(f"    本文(先頭600文字): {raw[:600].decode('utf-8', errors='replace')!r}")
    return records


def filter_by_label(records: list[dict], label: str) -> list[dict]:
    """レコード全体の文字列(題名・シリーズ名・注記など)にレーベル名が含まれるものだけ残す(表記ゆれ無視)"""
    key = norm(label)
    return [r for r in records if key in norm(r["text"])]


# ---------- openBD(補強) ----------

def enrich_with_openbd(isbns: list[str]) -> dict:
    """openBDへまとめて問い合わせ、ISBNごとの補強情報(書影・あらすじ・シリーズ名)を返す。取れないISBNは含めない。"""
    if not isbns:
        return {}
    result = {}
    chunk_size = 100
    for i in range(0, len(isbns), chunk_size):
        chunk = isbns[i : i + chunk_size]
        url = "https://api.openbd.jp/v1/get?isbn=" + ",".join(chunk)
        try:
            data = json.loads(http_get(url))
        except Exception as e:  # noqa: BLE001
            log(f"  [WARN] openBD取得失敗: ({e})")
            continue
        for entry in data or []:
            if not entry:
                continue
            summary = entry.get("summary", {}) or {}
            isbn = summary.get("isbn")
            if not isbn:
                continue
            result[isbn] = {
                "title": summary.get("title") or "",
                "series": summary.get("series"),
                "cover": summary.get("cover"),
                "description": summary.get("description") or "",
                "author": summary.get("author") or "",
                "pubdate": summary.get("pubdate") or "",
            }
        time.sleep(REQUEST_INTERVAL_SEC)
    return result


# ---------- 入出力 ----------

def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    seen_isbns: set[str] = set(load_json(SEEN_FILE, []))
    existing_books: list[dict] = load_json(BOOKS_FILE, [])
    new_books: list[dict] = []

    for entry in LABELS:
        label, publisher, keyword = entry["label"], entry["publisher"], entry["keyword"]
        log(f"[{label}] NDLサーチを検索中 (出版者={publisher}, キーワード={keyword})")

        records: list[dict] = []
        for inprocess in (False, True):
            records += fetch_ndl_records(publisher, keyword, inprocess)
            time.sleep(REQUEST_INTERVAL_SEC)

        # ISBNで重複排除(ISBN不明のレコードは扱わない=後で表示用リンクが作れないため)
        uniq: dict[str, dict] = {}
        for r in records:
            if r["isbn"] and r["isbn"] not in uniq:
                uniq[r["isbn"]] = r
        matched = filter_by_label(list(uniq.values()), label)
        log(f"[{label}] 取得 {len(records)}件 → ISBNあり{len(uniq)}件 → レーベル名一致 {len(matched)}件")

        openbd_info = enrich_with_openbd([m["isbn"] for m in matched])

        for m in matched:
            isbn = m["isbn"]
            if isbn in seen_isbns:
                continue
            info = openbd_info.get(isbn, {})

            # openBD側のシリーズ名が目的のレーベルと明らかに違う場合は除外(誤検出対策)
            series = norm(info.get("series") or "")
            if series and norm(label) not in series and series not in norm(label):
                log(f"  [SKIP] openBDのシリーズ名不一致: {m['title']} (series={series})")
                continue

            new_books.append(
                {
                    "label": label,
                    "publisher": publisher,
                    "title": info.get("title") or m["title"],
                    "isbn": isbn,
                    "link": f"https://www.hanmoto.com/bd/isbn/{isbn}",
                    "pubdate": info.get("pubdate") or m["pubdate"],
                    "cover": info.get("cover"),
                    "description": info.get("description") or "",
                    "author": info.get("author") or "",
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            seen_isbns.add(isbn)

    log(f"新着 {len(new_books)}件を追加します" if new_books else "新着はありませんでした")

    merged = new_books + existing_books
    merged.sort(key=lambda b: b.get("pubdate") or "", reverse=True)
    save_json(BOOKS_FILE, merged[:MAX_BOOKS_KEPT])
    save_json(SEEN_FILE, sorted(seen_isbns))
    log("完了")


if __name__ == "__main__":
    main()
