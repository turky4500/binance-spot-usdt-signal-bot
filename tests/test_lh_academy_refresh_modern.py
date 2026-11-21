"""حراسة تحديث الأكاديمية بعد تغيّر تصميم الدليل؛ لا اتصالات أو رفع في الاختبار."""
from datetime import datetime, timezone

import pytest
import requests

from tools import update_halal_academy as U


def row(sym, label="حلال", href="/article/"):
    return (f'<li class="bhd-row" data-k="halal"><a href="{href}">'
            f'<span class="bhd-nm"><strong>{sym}</strong><small>Name {sym}</small></span>'
            f'<span class="bhd-badge bhd-halal">{label}</span></a></li>')


def test_modern_row_extracts_symbol_label_and_absolute_article():
    rows = U.parse_directory_html(row("METIS"))
    assert rows == [{"symbol": "METIS", "label": "حلال", "name": "Name METIS",
                     "article": "https://btcacademy.online/article/"}]


def test_modern_single_page_is_accepted_only_if_complete(monkeypatch):
    monkeypatch.setattr(U, "_get", lambda *a, **k: type("R", (), {"text": ""})())
    full = ''.join(row(f"C{i}") for i in range(800))
    monkeypatch.setattr(U, "_direct_html", lambda *a: full)
    assert len(U.fetch_directory(requests.Session(), previous_rows=777)) == 800

    # A new layout with 1 missing row / unknown badge cannot be marked fresh.
    monkeypatch.setattr(U, "_direct_html", lambda *a: full + row("ERR", "غير معروف"))
    with pytest.raises(RuntimeError, match="directory incomplete"):
        U.fetch_directory(requests.Session(), previous_rows=777)
    monkeypatch.setattr(U, "_direct_html", lambda *a: ''.join(row(f"C{i}") for i in range(699)))
    with pytest.raises(RuntimeError, match="directory incomplete"):
        U.fetch_directory(requests.Session(), previous_rows=777)
    monkeypatch.setattr(U, "_direct_html", lambda *a: full + row("C1"))
    with pytest.raises(RuntimeError, match="directory incomplete"):
        U.fetch_directory(requests.Session(), previous_rows=777)


def test_modern_refuses_offsite_article(monkeypatch):
    full = ''.join(row(f"C{i}") for i in range(800)) + row("EVIL", href="https://elsewhere.example/x")
    monkeypatch.setattr(U, "_get", lambda *a, **k: type("R", (), {"text": ""})())
    monkeypatch.setattr(U, "_direct_html", lambda *a: full)
    with pytest.raises(RuntimeError, match="invalid=1"):
        U.fetch_directory(requests.Session(), previous_rows=777)


def test_articles_fail_closed_when_page_empty_even_if_prior_count_is_700(monkeypatch):
    class Fake:
        def __init__(self, page):
            self.headers = {"X-WP-TotalPages": "8", "X-WP-Total": "750"}
            self.page = page
        def json(self):
            return [{"id": (self.page - 1) * 100 + i} for i in range(100)] if self.page < 8 else []
    monkeypatch.setattr(U, "_get", lambda s, url, **kw: Fake(kw["params"]["page"]))
    with pytest.raises(RuntimeError, match="empty"):
        U.fetch_posts(requests.Session())


def test_articles_require_pagination_headers(monkeypatch):
    class Fake:
        headers = {}
        def json(self):
            return [{"id": 1}]
    monkeypatch.setattr(U, "_get", lambda *a, **k: Fake())
    with pytest.raises(RuntimeError, match="pagination headers"):
        U.fetch_posts(requests.Session())


def test_btc_parser_only_accepts_explicit_academy_section():
    prefix = '<p>يرى بعض العلماء أن البيتكوين حرام</p><h2 id="btca-verdict">حكم الأكاديمية</h2>'
    assert U.parse_bitcoin_verdict(prefix + '<p>فإن عملة البيتكوين حلال، والله أعلم</p><h2>آراء أخرى</h2>') == 0
    assert U.parse_bitcoin_verdict(prefix + '<p>فإن عملة البيتكوين مشبوهة</p>') == 2
    assert U.parse_bitcoin_verdict('<p>فإن عملة البيتكوين حلال</p>') is None
    assert U.parse_bitcoin_verdict(prefix + '<p>فإن عملة البيتكوين حلال ثم فإن عملة البيتكوين حرام</p>') is None
    assert U.parse_bitcoin_verdict(prefix + '<p>لا حكم واضح</p>') is None


def test_btc_supplemental_verification_preserves_but_blocks_conflicts():
    args = ({'BTC': 0}, {'source': 'btcacademy.online'}, {}, {'prices': {}}, [], [], {'BTC'},
            datetime(2026, 11, 5, tzinfo=timezone.utc))
    merged, meta, _ = U.build_merge(*args, supplemental={'BTC': 0})
    assert merged['BTC'] == 0 and 'BTC' not in meta['unverified_symbols']
    assert meta['supplemental_verified'] == ['BTC']
    from src.lh1m_github import primary_digest
    assert meta['primary_sha256'] == U.primary_digest(merged) == primary_digest(merged)
    conflicting_rows = [{'symbol': 'BTC', 'label': 'حرام', 'name': 'Bitcoin', 'article': ''}]
    args_conflict = ({'BTC': 0}, {'source': 'btcacademy.online'}, {}, {'prices': {}},
                     conflicting_rows, [], {'BTC'}, datetime(2026, 11, 5, tzinfo=timezone.utc))
    merged, meta, _ = U.build_merge(*args_conflict, supplemental={'BTC': 0})
    assert merged['BTC'] == 0  # no deletion, no invented reclassification
    assert 'BTC' in meta['ambiguous'] and 'BTC' in meta['unverified_symbols']
