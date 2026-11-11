#!/usr/bin/env python3
"""تحديث ملف أحكام أكاديمية البيتكوين كل 6 ساعات — دمج فقط، بلا حذف.

المصدران:
  1. جدول العملات في صفحة «عملات رقمية حلال أم حرام؟» (766 صفًا / 77 صفحة)
     عبر r.jina.ai، لأن الجلب المباشر يتوقف بعد ~22 طلبًا.
  2. مقالات التصنيف 3181 عبر WordPress REST (814 مقالًا)، ومنها تُقرأ
     الجملة الختامية «فإن عملة X …» أو ما يعادلها.

القواعد:
  - 0 = حلال، 1 = حرام، 2 = مشبوهة.
  - «قيد المراجعة» و«اقرأ التقييم» ليستا حكمًا واضحًا ⇒ لا تغيير ولا اختراع حكم.
  - عند تعدد المقالات لنفس الرمز يفوز الأحدث؛ فإن اختلف الحكم مع مقال أقدم
    يُقبل الأحدث فقط إذا نصّ صراحة على «تحديث الحكم/عدّلنا حكم/أضفنا حكم»،
    وإلا يُترك الرمز بلا تغيير (ازدواج رمز لمشروعين مختلفين).
  - تُضاف العملات الجديدة فقط إذا كان الرمز معروفًا لدى النظام/بينانس.
  - لا يُحذف أي رمز موجود غاب عن الموقع: ``merged = dict(current); merged.update(site)``.
  - المخرجات الوحيدة: halal_academy.json و halal_academy_meta.json.

الاستخدام في GitHub Actions:
    python tools/update_halal_academy.py
الاستخدام المحلي للتجربة:
    python tools/update_halal_academy.py --dry-run \
        --directory-json /home/user/btcacademy_directory_full.json \
        --posts-json /home/user/btcacademy_posts.json
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sys
import time
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

REPO = os.environ.get("GITHUB_REPOSITORY", "turky4500/binance-spot-usdt-signal-bot")
BRANCH = "main"
PRIMARY_PATH = "data/daily_trend/halal_academy.json"
META_PATH = "data/daily_trend/halal_academy_meta.json"
EXTRA_PATH = "data/daily_trend/halal_extra.json"
PRICES_PATH = "data/prices.json"

SITE_ORIGIN = "https://btcacademy.online/"
DIRECTORY_PAGE = SITE_ORIGIN + urllib.parse.quote("عملات-رقمية-حلال-أم-حرام/")
WP_POSTS_URL = SITE_ORIGIN + "wp-json/wp/v2/posts"
WP_CATEGORY = 3181
JINA_PREFIX = "https://r.jina.ai/"
BINANCE_TICKERS = "https://data-api.binance.vision/api/v3/ticker/price"
AJAX_URL = SITE_ORIGIN + "wp-admin/admin-ajax.php"

DIRECT_LABEL = {"حلال": 0, "حرام": 1, "مشبوهة": 2, "مشبوه": 2}
WORD_TO_VERDICT = {"حلال": 0, "حرام": 1, "مشبوهة": 2, "مشبوه": 2, "قيد المراجعة": None}
FINAL_PHRASE = re.compile(
    r"فإن\s*(?:هل\s*)?(?:عملة\s*)?.{0,90}?(حلال|حرام|مشبوهة|مشبوه|قيد المراجعة)"
)
ALL_SERVICES_HARAM = "جميع الخدمات التي تقدمها حرام أو مشبوهة"
DOUBTFUL_CONCLUSION = re.compile(r"العملة\s+مشبوهة")
COIN_TITLE = re.compile(r"هل\s+عملة\s+([A-Za-z0-9]+)", re.IGNORECASE)
PAREN_TOKEN = re.compile(r"\(([A-Za-z0-9]{2,12})\)")
SLUG_SYMBOL = re.compile(r"هل-عملة-([a-z0-9]+)", re.IGNORECASE)
UPDATE_MARKERS = ("تحديث الحكم", "عدّلنا حكم", "أضفنا حكم", "عدلنا حكم")
FORMAT_CHARS = re.compile("[\u200e\u200f\u061c\u00a0]")
LABEL_WORDS = {"حلال", "حرام", "مشبوهة", "مشبوه", "قيد المراجعة", "اقرأ التقييم"}

UA = "Mozilla/5.0 (compatible; HalalAcademyRefresh/1.0; +GitHub Actions)"
JINA_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/plain",
    "X-No-Cache": "true",
    "Cache-Control": "no-cache",
}
SITE_HEADERS = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "ar,en;q=0.8"}


# ────────────────────────────────────────────────────────────── أدوات نصية
def clean_html(raw: Any) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    return re.sub(r"\s+", " ", FORMAT_CHARS.sub("", text)).strip()


def norm_link(url: Any) -> str:
    """تطبيع رابط المقال: بدون بروتوكول/نطاق/استعلام/شرطة مائلة نهائية."""
    if not url:
        return ""
    u = urllib.parse.unquote(html.unescape(str(url))).strip()
    u = re.sub(r"^https?://[^/]+/", "", u)
    return u.split("?", 1)[0].strip("/").rstrip("/")


def post_title(post: dict) -> str:
    return clean_html((post.get("title") or {}).get("rendered", ""))


def post_content(post: dict) -> str:
    return clean_html((post.get("content") or {}).get("rendered", ""))


def verdict_from_post(post: dict) -> int | None:
    """يقرأ الحكم الصريح من المقال، ولا يجتهد إن غابت الصيغة."""
    text = post_content(post)
    matches = list(FINAL_PHRASE.finditer(text))
    if matches:
        return WORD_TO_VERDICT[matches[-1].group(1)]
    if ALL_SERVICES_HARAM in text:
        return 1
    if DOUBTFUL_CONCLUSION.search(text):
        return 2
    return None


def resolve_symbol(post: dict, known: set[str]) -> str | None:
    """يربط المقال برمز عملة canonical؛ يرفض مقالات المنهجية/القوائم العامة."""
    title = post_title(post)
    m = COIN_TITLE.search(title)
    if not m:
        return None
    candidates = [m.group(1).upper()]
    candidates += [x.upper() for x in PAREN_TOKEN.findall(title)]
    slug = norm_link(post.get("slug", ""))
    sm = SLUG_SYMBOL.search(slug)
    if sm:
        candidates.append(sm.group(1).upper())
    for cand in candidates:
        if cand in known:
            return cand
    return None


def parse_jina_rows(markdown: str) -> list[dict]:
    """صفوف الجدول من ماركداون Jina: ## الاسم / الرمز / الحكم / رابط المقال."""
    rows: list[dict] = []
    for block in re.split(r"\n##\s+", "\n" + markdown)[1:]:
        lines = [x.strip() for x in block.splitlines() if x.strip()]
        if len(lines) < 3:
            continue
        symbol = label = None
        for line in lines[1:6]:
            if symbol is None and re.fullmatch(r"[A-Z0-9]{1,15}", line):
                symbol = line
            elif label is None and line in LABEL_WORDS:
                label = line
        if not (symbol and label):
            continue
        links = re.findall(r"\[\]\((https?://[^)]+)\)", block)
        article = ""
        for link in links:
            if "btcacademy.online" in link:
                article = html.unescape(link)
                break
        rows.append(
            {"symbol": symbol, "label": label, "name": lines[0], "article": article}
        )
    return rows


ITEM_SPLIT = re.compile(r'<div class="crypto-item[^"\'<>]*"')


def parse_directory_html(markup: str) -> list[dict]:
    """قناتا البدل: صفحة GET أو رد admin-ajax.php.

    صنف العنصر قد يحمل كلاسات إضافية («crypto-item btca-added-article»)
    ورابط المقال قد يكون نسبيًا، لذلك الفتح متسامح معهما.
    """
    out: list[dict] = []
    for block in ITEM_SPLIT.split(markup)[1:]:
        block = block[:6000]
        sym = re.search(r'<p class="crypto-symbol"[^>]*>\s*([^<]+?)\s*</p>', block)
        ruling = re.search(r'<div class="crypto-ruling"[^>]*>\s*(.*?)\s*</div>', block, re.S)
        name = re.search(r'<h2 class="crypto-name"[^>]*>\s*(.*?)\s*</h2>', block, re.S)
        link = re.search(r'<a class="crypto-external-link[^>]*href=["\']([^"\']+)', block)
        if sym and ruling:
            article = html.unescape(link.group(1)) if link else ""
            if article.startswith("/"):
                article = "https://btcacademy.online" + article
            out.append(
                {
                    "symbol": sym.group(1).strip().upper(),
                    "label": clean_html(ruling.group(1)),
                    "name": clean_html(name.group(1)) if name else "",
                    "article": article,
                }
            )
    return out


parse_ajax_rows = parse_directory_html  # توافق مع الاسم القديم


# ────────────────────────────────────────────────────────────── الجلب
def _get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    last: Exception | None = None
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=kwargs.pop("timeout", 45), **kwargs)
            if resp.status_code == 200:
                return resp
            last = RuntimeError(f"HTTP {resp.status_code} for {url[:110]}")
        except Exception as exc:  # network glitch / timeout
            last = exc
        time.sleep(2 + attempt * 3)
        kwargs.setdefault("timeout", 45)
    raise RuntimeError(f"fetch failed: {url[:110]}: {last}")


def fetch_direct_page(session: requests.Session, page: int) -> list[dict]:
    """قناة بديلة 1: GET للصفحة نفسها (صفحات معزولة تعمل بلا تحديد معدل)."""
    return parse_directory_html(_direct_html(session, page))


def fetch_ajax_pages(pages: list[int]) -> tuple[list[dict], list[int]]:
    """قناة بديلة 2: admin-ajax.php مع كوكي hc_js_gate.

    يُعيد (الصفوف، الصفحات التي ما زالت ناقصة) حتى لا يُعلَن الاكتمال زورًا.
    """
    s = requests.Session()
    s.headers.update(SITE_HEADERS)
    post_headers = {
        "Referer": DIRECTORY_PAGE,
        "Origin": "https://btcacademy.online",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    }
    try:
        warm = s.post(
            AJAX_URL,
            data={"action": "crypto_halal_search", "search": "", "page": "1"},
            headers=post_headers,
            timeout=30,
        )
        m = re.search(r"document\.cookie=['\"]([^=]+)=([^;]+)", warm.text)
        name, value = (m.group(1), m.group(2)) if m else ("hc_js_gate", "1")
        s.cookies.set(name, value, domain="btcacademy.online", path="/")
    except Exception as exc:
        print(f"  AJAX warm-up failed: {exc}", flush=True)
        return [], list(pages)
    out: list[dict] = []
    failed: list[int] = []
    for page in pages:
        time.sleep(1.2)
        try:
            resp = s.post(
                AJAX_URL,
                data={"action": "crypto_halal_search", "search": "", "page": str(page)},
                headers=post_headers,
                timeout=30,
            )
            got = parse_directory_html(resp.text)
            print(f"  AJAX fallback page {page}: {len(got)} rows", flush=True)
            if got:
                out += got
            else:
                failed.append(page)
        except Exception as exc:
            print(f"  AJAX fallback page {page} failed: {exc}", flush=True)
            failed.append(page)
    return out, failed


def _extract_max_page(text: str) -> int:
    nums = [int(x) for x in re.findall(r"cpage=(\d+)|data-page=\"(\d+)\"", text)
            for x in x if x]
    return max(nums) if nums else 0


def _direct_html(session: requests.Session, page: int) -> str:
    url = DIRECTORY_PAGE + f"?cpage={page}&btca_q=&_={time.time_ns()}"
    headers = {**SITE_HEADERS, "Accept": "text/html",
               "Cache-Control": "no-cache", "Pragma": "no-cache"}
    return _get(session, url, headers=headers, timeout=45).text


def fetch_directory(session: requests.Session) -> list[dict]:
    """كل صفحات الجدول عبر Jina، مع قناتي بدل، ورفض أي نقص قبل الكتابة."""
    def jina(page: int) -> tuple[str, list[dict]]:
        url = (
            JINA_PREFIX
            + DIRECTORY_PAGE
            + f"?cpage={page}&btca_q&_={time.time_ns()}"
        )
        text = _get(session, url, headers=JINA_HEADERS, timeout=60).text
        return text, parse_jina_rows(text)

    first_text, first_rows = jina(1)
    max_page = _extract_max_page(first_text)
    # Jina قد يُعيد نسخة page 1 بلا روابط ترقيم؛ صفحة GET الأصلية موثوقة لذلك
    if not (1 < max_page <= 200) or not first_rows:
        try:
            raw = _direct_html(session, 1)
            max_page = _extract_max_page(raw) or max_page
            if not first_rows:
                first_rows = parse_directory_html(raw)
                print("  Jina page 1 فارغة — استُخدم GET", flush=True)
        except Exception as exc:
            print(f"  GET page 1 failed: {exc}", flush=True)
    if not (1 < max_page <= 200):
        raise RuntimeError(f"تعذّر تحديد عدد الصفحات: {max_page}")
    rows = list(first_rows)
    missing: list[int] = []
    for page in range(2, max_page + 1):
        time.sleep(0.6)
        if page % 20 == 0:
            time.sleep(8)  # فاصل يمنع تحديد معدل Jina بعد عشرات الطلبات
        page_rows: list[dict] = []
        for attempt in range(2):
            try:
                _, page_rows = jina(page)
            except Exception as exc:
                print(f"  Jina page {page} attempt {attempt}: {exc}", flush=True)
                page_rows = []
            if page_rows:
                break
            if attempt == 0:
                time.sleep(5)
        if not page_rows:
            missing.append(page)
            continue
        rows += page_rows
        if page % 10 == 0 or page == max_page:
            print(f"  directory page {page}/{max_page} — rows {len(rows)}", flush=True)

    still_missing: list[int] = []
    if missing:
        print(f"  Jina missed pages {missing} — قنوات البدل", flush=True)
        for page in missing:
            time.sleep(1.5)
            try:
                got = fetch_direct_page(session, page)
            except Exception as exc:
                print(f"  GET fallback page {page}: {exc}", flush=True)
                got = []
            if got:
                print(f"  GET fallback page {page}: {len(got)} rows", flush=True)
                rows += got
            else:
                still_missing.append(page)
        if still_missing:
            ajax_rows, ajax_failed = fetch_ajax_pages(still_missing)
            rows += ajax_rows
            still_missing = ajax_failed

    # لا كتابة على الإطلاق إن بقيت صفحة ناقصة: الأرقام الوصفية يجب أن تكون صادقة،
    # والدمج وحده لا يكفي لأن meta قد يوحي بجلب 766 بينما جُلب أقل.
    if still_missing or len(rows) < 10 * (max_page - 1) + 1:
        raise RuntimeError(
            f"directory incomplete: rows={len(rows)} max_page={max_page} "
            f"missing={still_missing} — إلغاء التحديث بلا كتابة"
        )
    print(f"  ✅ directory complete: {len(rows)} rows / {max_page} pages", flush=True)
    return rows


def fetch_posts(session: requests.Session) -> list[dict]:
    posts: list[dict] = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        resp = _get(
            session,
            WP_POSTS_URL,
            params={
                "categories": WP_CATEGORY,
                "per_page": 100,
                "page": page,
                "_fields": "id,date,modified,link,slug,title,content",
            },
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=60,
        )
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break
        posts += batch
        tp = int(resp.headers.get("X-WP-TotalPages") or 1)
        total_pages = max(total_pages, tp)
        page += 1
    if len(posts) < 700:
        raise RuntimeError(f"REST posts={len(posts)} < 700 — إلغاء التحديث")
    print(f"  REST posts {len(posts)}", flush=True)
    return posts


def fetch_binance_symbols(session: requests.Session) -> set[str]:
    try:
        resp = _get(
            session,
            BINANCE_TICKERS,
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=30,
        )
        return {
            item["symbol"][:-4]
            for item in resp.json()
            if item.get("symbol", "").endswith("USDT")
        }
    except Exception as exc:
        print(f"  Binance universe unavailable ({exc}) — استخدام رموز المستودع فقط", flush=True)
        return set()


# ────────────────────────────────────────────────────────────── المنطق النقي
def price_symbols(prices: dict) -> set[str]:
    blob = prices.get("prices", prices) if isinstance(prices, dict) else {}
    return {str(k).upper()[:-4] for k in blob if str(k).upper().endswith("USDT")}


def build_site_verdicts(
    rows: list[dict],
    posts: list[dict],
    known: set[str],
    validated: set[str],
) -> tuple[dict[str, int], dict]:
    """يُنتج خريطةsite symbol→verdict والإحصاءات. خالص بلا I/O."""
    dir_symbols = {r["symbol"].upper() for r in rows}

    link_to_dir: dict[str, str] = {}
    for row in rows:
        key = norm_link(row.get("article"))
        if key:
            link_to_dir.setdefault(key, row["symbol"].upper())

    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for post in posts:
        symbol = resolve_symbol(post, known) or link_to_dir.get(norm_link(post.get("link")))
        if symbol:
            by_symbol[symbol].append(post)

    site: dict[str, int] = {}
    ambiguous: set[str] = set()
    unclear: set[str] = set()
    from_article = 0
    from_label = 0

    for symbol, group in by_symbol.items():
        clear = []
        for post in group:
            value = verdict_from_post(post)
            if value in (0, 1, 2):
                clear.append((str(post.get("modified") or post.get("date") or ""), value, post))
        if clear:
            newest = max(clear, key=lambda c: (c[0], c[2].get("id", 0)))
            conflicts = [c for c in clear if c[1] != newest[1]]
            if conflicts and not any(
                marker in post_content(newest[2]) for marker in UPDATE_MARKERS
            ):
                ambiguous.add(symbol)
                continue
            site[symbol] = newest[1]
            from_article += 1
        else:
            unclear.add(symbol)

    # صفوف الجدول: المقالات أسبق، ثم الوسم الصريح في الجدول إن غاب الحكم
    for row in rows:
        symbol = row["symbol"].upper()
        if symbol in site or symbol in ambiguous:
            continue
        value = DIRECT_LABEL.get(row.get("label"))
        if value is not None:
            site[symbol] = value
            from_label += 1
        else:
            unclear.add(symbol)

    # عملات جديدة من المقالات: فقط الرموز المُتحقَّق منها
    for symbol in sorted(set(by_symbol) - dir_symbols):
        if symbol in site or symbol in ambiguous or symbol not in validated:
            continue
        # site[symbol] قد يكون قد ضُبط من الجدول؛ نعيد التحقق من المقالات
        clear = [verdict_from_post(p) for p in by_symbol[symbol]]
        clear = [v for v in clear if v in (0, 1, 2)]
        if clear:
            site[symbol] = clear[-1]
            from_article += 1
        else:
            unclear.add(symbol)

    stats = {
        "directory_rows": len(rows),
        "directory_unique_symbols": len(dir_symbols),
        "articles_scanned": len(posts),
        "articles_mapped": sum(len(v) for v in by_symbol.values()),
        "verdicts_from_articles": from_article,
        "verdicts_from_labels": from_label,
        "site_verdicts": len(site),
        "unknown": sorted(dir_symbols - set(site) - ambiguous),
        "ambiguous": sorted(ambiguous),
        "unclear_articles": sorted(unclear & (set(by_symbol) - dir_symbols)),
    }
    return site, stats


def build_merge(
    current: dict[str, int],
    meta: dict,
    extra: dict[str, int],
    prices: dict,
    rows: list[dict],
    posts: list[dict],
    binance_symbols: set[str],
    now: datetime,
) -> tuple[dict[str, int], dict, dict]:
    """الدمج النهائي: لا حذف، لا اختراع، لا تعديل ملفات أخرى."""
    current = {str(k).upper(): int(v) for k, v in current.items() if isinstance(v, (int, float))}
    known = set(current) | set(extra) | price_symbols(prices) | {r["symbol"].upper() for r in rows}
    known |= binance_symbols
    validated = set(current) | set(extra) | price_symbols(prices) | binance_symbols

    site, stats = build_site_verdicts(rows, posts, known, validated)
    # الرموز الخارجة عن الجدول لا تُقبل إلا إذا تحقّقنا أنها عملة متداولة/معروفة
    dir_symbols = {r["symbol"].upper() for r in rows}
    site = {
        symbol: value
        for symbol, value in site.items()
        if symbol in dir_symbols or symbol in validated
    }

    merged = dict(current)  # ← الدمج فقط: كل رمز موجود يبقى ولو غاب عن الموقع
    added: list[dict] = []
    changed: list[dict] = []
    for symbol in sorted(site):
        value = site[symbol]
        if symbol not in merged:
            merged[symbol] = value
            added.append({"symbol": symbol, "value": value})
        elif merged[symbol] != value:
            changed.append({"symbol": symbol, "from": merged[symbol], "to": value})
            merged[symbol] = value

    if any(v not in (0, 1, 2) for v in merged.values()):
        raise RuntimeError("قيمة حكم غير صالحة — إلغاء التحديث")

    counts = Counter(merged.values())
    now_iso = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_meta = dict(meta)
    new_meta.update(
        {
            "fetched_at": now_iso[:10],
            "last_check_utc": now_iso,
            "refresh_hours": 6,
            "merge_only": True,
            "total": len(merged),
            "halal": counts[0],
            "haram": counts[1],
            "doubtful": counts[2],
            "unknown": stats["unknown"],
            "site_directory_rows": stats["directory_rows"],
            "site_unique_symbols": stats["directory_unique_symbols"],
            "articles_scanned": stats["articles_scanned"],
            "ambiguous": stats["ambiguous"],
            "unclear_articles": stats["unclear_articles"],
            "added_last_run": [x["symbol"] for x in added],
            "changed_last_run": changed,
            "retained_not_on_site": len(
                set(merged) - {r["symbol"].upper() for r in rows}
            ),
        }
    )
    report = {
        "checked_at_utc": now_iso,
        "added": added,
        "changed": changed,
        "site": stats,
        "total_before": len(current),
        "total_after": len(merged),
        "halal": counts[0],
        "haram": counts[1],
        "doubtful": counts[2],
    }
    return merged, new_meta, report


# ────────────────────────────────────────────────────────────── GitHub I/O
def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": UA,
    }


def gh_get(path: str, token: str) -> tuple[Any, str]:
    resp = requests.get(
        f"https://api.github.com/repos/{REPO}/contents/{path}",
        params={"ref": BRANCH},
        headers=_gh_headers(token),
        timeout=40,
    )
    resp.raise_for_status()
    payload = resp.json()
    raw = base64.b64decode(payload["content"]).decode("utf-8")
    return json.loads(raw), payload["sha"]


def gh_put(path: str, data: Any, sha: str, token: str) -> requests.Response:
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    return requests.put(
        f"https://api.github.com/repos/{REPO}/contents/{path}",
        headers=_gh_headers(token),
        json={
            "message": f"chore(halal): refresh Academy rulings ({Path(path).name}) [skip ci]",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "sha": sha,
            "branch": BRANCH,
        },
        timeout=60,
    )


def load_state_from_raw(session: requests.Session) -> tuple[dict, dict, dict, dict]:
    def get(path: str) -> Any:
        return _get(
            session,
            f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{path}",
            headers={"User-Agent": UA, "Cache-Control": "no-cache"},
            timeout=40,
        ).json()

    return get(PRIMARY_PATH), get(META_PATH), get(EXTRA_PATH), get(PRICES_PATH)


def load_state_from_repo(root: Path) -> tuple[dict, dict, dict, dict]:
    def get(path: str) -> Any:
        return json.loads((root / path).read_text(encoding="utf-8"))

    return get(PRIMARY_PATH), get(META_PATH), get(EXTRA_PATH), get(PRICES_PATH)


def read_fixtures(directory_json: Path, posts_json: Path) -> tuple[list[dict], list[dict]]:
    rows = json.loads(directory_json.read_text(encoding="utf-8"))
    posts = json.loads(posts_json.read_text(encoding="utf-8"))
    return rows, posts


def publish(token: str, compute) -> dict:
    """رفع الملفين مع إعادة المحاولة عند تعارض sha (كاتب آخر في نفس اللحظة).

    ``compute(current, meta)`` يُعاد استدعاؤه مقابل أحدث نسخة عند كل تعارض،
    فلا تضيع إضافات كاتب آخر ولا تُكتب أحكام قديمة فوق أحكام أحدث.
    """
    for attempt in range(1, 6):
        current, sha = gh_get(PRIMARY_PATH, token)
        meta, meta_sha = gh_get(META_PATH, token)
        merged, new_meta, report = compute(current, meta)

        if merged == current:
            print("  ✅ لا تغيير في الأحكام — لا كومِت لملف الأحكام", flush=True)
        else:
            resp = gh_put(PRIMARY_PATH, merged, sha, token)
            if resp.status_code == 409:
                print(f"  sha conflict on primary (attempt {attempt}) — إعادة الحساب", flush=True)
                time.sleep(2)
                continue
            resp.raise_for_status()
            print(f"  ✅ {PRIMARY_PATH} → {resp.json()['commit']['sha'][:8]}", flush=True)

        meta_resp = gh_put(META_PATH, new_meta, meta_sha, token)
        for _ in range(3):
            if meta_resp.status_code != 409:
                break
            _, meta_sha = gh_get(META_PATH, token)
            time.sleep(2)
            meta_resp = gh_put(META_PATH, new_meta, meta_sha, token)
        meta_resp.raise_for_status()
        print(f"  ✅ {META_PATH} → {meta_resp.json()['commit']['sha'][:8]}", flush=True)
        return report
    raise RuntimeError("تعذّر الرفع بعد 5 محاولات sha conflict")


# ────────────────────────────────────────────────────────────── main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="حساب وعرض فقط بلا رفع")
    ap.add_argument("--directory-json", type=Path, help="صفوف الجدول المحفوظة (للاختبار)")
    ap.add_argument("--posts-json", type=Path, help="مقالات REST المحفوظة (للاختبار)")
    ap.add_argument("--repo-root", type=Path, help="قراءة ملفات البيانات من نسخة محلية")
    ap.add_argument("--report-json", type=Path, help="حفظ تقرير الدمج")
    args = ap.parse_args(argv)

    session = requests.Session()
    if args.repo_root:
        current, meta, extra, prices = load_state_from_repo(args.repo_root)
    else:
        current, meta, extra, prices = load_state_from_raw(session)

    if args.directory_json and args.posts_json:
        rows, posts = read_fixtures(args.directory_json, args.posts_json)
        print(f"  fixtures: {len(rows)} rows / {len(posts)} posts", flush=True)
    else:
        print("  fetching directory (77 pages) …", flush=True)
        rows = fetch_directory(session)
        print("  fetching REST articles …", flush=True)
        posts = fetch_posts(session)

    binance_symbols = fetch_binance_symbols(session)
    now = datetime.now(timezone.utc)

    def compute(fresh_current: dict, fresh_meta: dict):
        return build_merge(
            fresh_current, fresh_meta, extra, prices, rows, posts,
            binance_symbols, now,
        )

    merged, new_meta, report = compute(current, meta)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if args.report_json:
        args.report_json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    if args.dry_run:
        print("\n--dry-run: لا رفع. merged entries =", len(merged), flush=True)
        return 0

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GITHUB_TOKEN غير موجود — استخدم --dry-run أو شغّل داخل GitHub Actions", file=sys.stderr)
        return 2
    publish(token, compute)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
