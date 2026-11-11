"""اختبارات مُحدِّث أحكام أكاديمية البيتكوين (تحديث كل 6 ساعات).

القواعد المحمية هنا — كلها من طلب المستخدم:
  - «يحدث ما يحتاج إلى تحديث» ⇒ تغيير الأحكام وإضافة الجديد،
  - و«بعضها كانت بلا حكم وأضيف لها حكم» ⇒ AKRO/GPS تُضاف،
  - مع «لا حذف» ⇒ أي رمز موجود يبقى ولو لم يعد في الموقع،
  - و«لا اجتهاد» ⇒ لا حكم يُخترع من «قيد المراجعة» أو «اقرأ التقييم»
    أو من مقالتين متعارضتين على نفس الرمز.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from datetime import datetime, timezone

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import update_halal_academy as U  # noqa: E402
from src.halal import load_primary_verdicts  # noqa: E402

NOW = datetime(2026, 11, 11, 6, 0, tzinfo=timezone.utc)


def post(symbol: str, word: str, modified: str = "2026-01-01T00:00:00",
         extra_text: str = "", title: str | None = None, pid: int = 1) -> dict:
    """مقال بالصيغة المعتمدة في الموقع: «فإن عملة X …»."""
    slug = f"هل-عملة-{symbol.lower()}-حلال-أم-حرام؟"
    content = (
        "بناء على المعطيات السابقة ومشروع عملة "
        f"{symbol} فإن عملة {symbol} {word}، والله أعلم. {extra_text}"
    )
    return {
        "id": pid,
        "date": modified,
        "modified": modified,
        "slug": slug,
        "link": f"https://btcacademy.online/{slug}/",
        "title": {"rendered": title or f"هل عملة {symbol} حلال أم حرام؟"},
        "content": {"rendered": content},
    }


def row(symbol: str, label: str = "اقرأ التقييم", slug: str | None = None) -> dict:
    slug = slug or f"هل-عملة-{symbol.lower()}-حلال-أم-حرام؟"
    return {
        "symbol": symbol,
        "label": label,
        "name": symbol,
        "article": f"https://btcacademy.online/{slug}/",
    }


def merge(current, rows, posts, meta=None, extra=None, prices=None, binance=None):
    return U.build_merge(
        current or {}, meta or {"source": "btcacademy.online"},
        extra or {}, prices or {"prices": {}}, rows, posts,
        set(binance or ()), NOW,
    )


# ─────────────────────────────────────────────────────────── الدمج بلا حذف
def test_existing_symbol_absent_from_site_is_retained():
    current = {"AAA": 0, "OLDCOIN": 1}
    merged, meta, report = merge(current, [row("AAA")], [post("AAA", "حلال")])
    assert merged["OLDCOIN"] == 1, "غياب الرمز عن الموقع ليس سببًا لحذفه"
    assert meta["retained_not_on_site"] >= 1
    assert report["total_before"] == 2 and report["total_after"] == 2


def test_changed_ruling_is_updated_not_appended():
    merged, _, report = merge({"AAA": 0}, [row("AAA")], [post("AAA", "حرام")])
    assert merged["AAA"] == 1
    assert report["changed"] == [{"symbol": "AAA", "from": 0, "to": 1}]
    assert report["added"] == []


def test_new_coin_is_added():
    merged, _, report = merge({}, [row("NEW", "حلال")], [], binance=["NEW"])
    assert merged["NEW"] == 0
    assert report["added"] == [{"symbol": "NEW", "value": 0}]


def test_previously_unclear_coins_get_their_new_rulings():
    """AKRO وGPS كانتا في meta.unknown والآن لهما جملة حكم صريحة."""
    merged, meta, report = merge(
        {},
        [row("AKRO"), row("GPS")],
        [post("AKRO", "حرام"), post("GPS", "حلال")],
    )
    assert merged["AKRO"] == 1 and merged["GPS"] == 0
    assert meta["unknown"] == []
    assert {x["symbol"] for x in report["added"]} == {"AKRO", "GPS"}


def test_second_run_is_idempotent():
    current, rows, posts = {"AAA": 0}, [row("AAA")], [post("AAA", "حلال")]
    merged, meta, _ = merge(current, rows, posts)
    again, _, report = merge(merged, rows, posts, meta=meta)
    assert again == merged
    assert report["added"] == [] and report["changed"] == []


# ─────────────────────────────────────────────────────────── لا اختراع حكم
def test_review_labels_are_not_rulings():
    merged, meta, _ = merge(
        {"PENDING": 1},
        [row("PENDING", "قيد المراجعة"), row("READ", "اقرأ التقييم")],
        [],
    )
    assert merged == {"PENDING": 1}, "قيد المراجعة/اقرأ التقييم لا تُنشئ ولا تُغيّر حكمًا"
    assert "READ" not in merged
    assert "READ" in meta["unknown"]


def test_conditional_article_does_not_invent_a_ruling():
    pi = post("PI", "حلال")
    pi["title"] = {"rendered": "هل عملة Pi Network حلال أم حرام؟"}
    pi["content"] = {"rendered": "حكم مشروط، ولا يمكن الجزم بحلال أو حرام."}
    merged, meta, report = merge({"PI": 0}, [], [pi])
    assert merged["PI"] == 0
    assert report["added"] == [] and report["changed"] == []
    assert "PI" in meta["unclear_articles"]


def test_conflicting_articles_on_one_symbol_are_skipped():
    """STRK: مقالتان لمشروعين مختلفين بلا «تحديث حكم» صريح ⇒ لا نخترع."""
    a = post("STRK", "حرام", "2026-11-06T22:29:24", pid=1)
    b = post("STRK", "حلال", "2026-11-06T22:29:25", pid=2)
    merged, meta, _ = merge({}, [], [a, b], binance=["STRK"])
    assert "STRK" not in merged
    assert "STRK" in meta["ambiguous"]


def test_newest_explicit_update_wins():
    """WLD: مقال قديم «حلال» وتحديث 7 نوفمبر «مشبوهة» ⇒ الأحدث."""
    old = post("WLD", "حلال", "2026-10-20T05:42:59", pid=1)
    new = post("WLD", "مشبوهة", "2026-11-06T22:46:12",
               extra_text="🔄 تحديث الحكم (7 نوفمبر 2026): أضفنا حكم الأكاديمية.",
               title="هل عملة Worldcoin (WLD) حلال أم حرام؟", pid=2)
    new["slug"] = "هل-عملة-worldcoin-wld-حلال-أم-حرام؟"
    new["link"] = "https://btcacademy.online/هل-عملة-worldcoin-wld-حلال-أم-حرام؟/"
    merged, _, report = merge({"WLD": 0}, [row("WLD")], [old, new])
    assert merged["WLD"] == 2
    assert report["changed"] == [{"symbol": "WLD", "from": 0, "to": 2}]


def test_doubtful_conclusion_is_read_without_inventing():
    """AUDIO: «العملة مشبوهة ولا يمكن تأكيد أنها حلال أو حرام» ⇒ 2."""
    audio = post("AUDIO", "حلال")
    audio["content"] = {"rendered": "هل عملة AUDIO حلال؟ العملة مشبوهة ولا يمكن تأكيد أنها حلال أو حرام."}
    merged, _, _ = merge({"AUDIO": 2}, [row("AUDIO")], [audio])
    assert merged["AUDIO"] == 2


def test_unvalidated_rest_only_symbol_is_not_added():
    """اسم مشروع ليس رمزًا متداولًا ⇒ لا يُضاف (DLICOM/GO/SD)."""
    p = post("GO", "حلال")
    merged, _, _ = merge({}, [], [p])
    assert "GO" not in merged, "الرمز غير المتحقّق منه لا يُضاف"


# ─────────────────────────────────────────────────────────── الملف والـmeta
def test_meta_counts_and_original_keys_survive():
    meta0 = {
        "source": "btcacademy.online",
        "source_url": "https://btcacademy.online/x",
        "methodology": "منهجية",
        "note": "ملاحظة",
        "precedence": "halal_academy.json > halal_extra.json",
    }
    merged, meta, _ = merge(
        {"AAA": 0}, [row("AAA")], [post("AAA", "حلال")], meta=meta0,
    )
    for key in meta0:
        assert meta[key] == meta0[key]
    assert meta["total"] == len(merged) == 1
    assert meta["halal"] + meta["haram"] + meta["doubtful"] == meta["total"]
    assert meta["merge_only"] is True and meta["refresh_hours"] == 6
    assert meta["last_check_utc"] == "2026-11-11T06:00:00Z"


def test_values_are_strictly_0_1_2_and_loader_reads_them(tmp_path):
    merged, _, _ = merge({"AAA": 0}, [row("AAA")], [post("AAA", "مشبوهة")])
    assert set(merged.values()) <= {0, 1, 2}
    path = tmp_path / "halal_academy.json"
    path.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    assert load_primary_verdicts(str(path)) == merged


def test_real_primary_file_is_still_sane_after_refresh():
    data = json.loads((ROOT / "data/daily_trend/halal_academy.json").read_text(encoding="utf-8"))
    assert len(data) >= 700
    assert set(data.values()) <= {0, 1, 2}
    assert data["BTC"] == 0 and data["ETH"] == 0
    assert data["BNB"] == 1 and data["UNI"] == 1
    for sym in ("ACT", "BANANA", "TAO", "ZRO"):
        assert data[sym] == 1
    for sym in ("CRO", "DAI", "PAXG", "CTK", "FIDA"):
        assert data[sym] == 0


# ─────────────────────────────────────────────────────────── الوركفلو
def _workflow():
    path = ROOT / ".github/workflows/halal-academy-refresh.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8")), path.read_text(encoding="utf-8")


def test_workflow_schedule_is_every_6_hours_and_has_no_push():
    doc, text = _workflow()
    triggers = doc.get("on", doc.get(True, {}))
    assert triggers["schedule"][0]["cron"] == "0 */6 * * *"
    assert "workflow_dispatch" in triggers
    assert "push" not in triggers, "لا يجوز تشغيل البوتات عند push"
    assert "push:" not in text


def test_workflow_uses_only_default_token_and_skips_ci():
    doc, text = _workflow()
    assert (doc.get("permissions") or {}).get("contents") == "write"
    assert "${{ secrets.GITHUB_TOKEN }}" in text
    # لا توكن شخصي: نفحص أنماط الاستخدام الفعلي لا مجرد ذكر الكلمة في تعليق
    for forbidden in ("secrets.PAT", "secrets.GH_PAT", "secrets.GITHUB_PAT",
                      "ghp_", "github_pat_", "secrets.TELEGRAM"):
        assert forbidden not in text, forbidden
    script = (ROOT / "tools/update_halal_academy.py").read_text(encoding="utf-8")
    assert "[skip ci]" in script
    assert "merged = dict(current)" in script
    assert "set(merged)" in script or "dict(current)" in script
    assert "PRIMARY_PATH" in script and "META_PATH" in script


def test_workflow_is_valid_yaml_and_single_job():
    doc, _ = _workflow()
    assert isinstance(doc, dict)
    assert len(doc["jobs"]) == 1


def test_directory_html_parser_accepts_extra_classes_and_relative_links():
    html = (
        '<div class="crypto-item btca-added-article">'
        '<div class="crypto-details"><h2 class="crypto-name">Ant</h2>'
        '<p class="crypto-symbol">ANT</p></div>'
        '<div class="crypto-ruling" style="color:#000">حرام</div>'
        '<div class="crypto-links"><a class="crypto-external-link dashicons" '
        'href="/%d9%87%d9%84-%d8%b9%d9%85%d9%84%d8%a9-ant/"></a></div></div>'
    )
    rows = U.parse_directory_html(html)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "ANT" and rows[0]["label"] == "حرام"
    assert rows[0]["article"].startswith("https://btcacademy.online/")


def _jina_markdown(page: int, count: int = 10) -> str:
    head = "cpage=77\n" if page == 1 else ""
    blocks = []
    for i in range(count):
        sym = f"C{page}{i}"
        blocks.append(
            f"## {sym}\n\n{sym}\n\nحلال\n\n"
            "[](https://btcacademy.online/x/)\n"
        )
    return head + "\n" + "\n".join(blocks)


def test_directory_is_complete_or_aborts_without_writing(monkeypatch):
    """إن أخفقت صفحة في كل القنوات ⇒ لا يُكتب meta بمعلومات ناقصة."""
    import requests

    class FakeResp:
        def __init__(self, text):
            self.text = text
            self.status_code = 200

    def fake_get(session, url, **kwargs):
        m = re.search(r"cpage=(\d+)", url)
        page = int(m.group(1)) if m else 1
        return FakeResp("" if page in (23, 24, 25) else _jina_markdown(page))

    monkeypatch.setattr(U, "_get", fake_get)
    monkeypatch.setattr(U.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(U, "fetch_direct_page", lambda session, page: [])
    monkeypatch.setattr(U, "fetch_ajax_pages", lambda pages: ([], list(pages)))

    with pytest.raises(RuntimeError, match="directory incomplete"):
        U.fetch_directory(requests.Session())

    # ومع نجاح كل الصفحات ⇒ يكتمل الجدول بلا استثناء
    monkeypatch.setattr(U, "_get", lambda session, url, **kw: FakeResp(
        _jina_markdown(int(re.search(r"cpage=(\d+)", url).group(1)) if "cpage=" in url else 1)))
    rows = U.fetch_directory(requests.Session())
    assert len(rows) == 770, len(rows)


def test_publish_does_not_commit_identical_rulings(monkeypatch):
    """كل 6 ساعات يتحقق الموقع، لكن لا كومِت لملف الأحكام إن لم يتغيّر حكم."""
    current = {"AAA": 0}
    meta = {"source": "btcacademy.online"}
    calls: list[str] = []

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"commit": {"sha": "abcdef1234567890"}}

    def fake_get(path, token):
        return (current if path == U.PRIMARY_PATH else meta, "sha")

    def fake_put(path, data, sha, token):
        calls.append(path)
        return Resp()

    monkeypatch.setattr(U, "gh_get", fake_get)
    monkeypatch.setattr(U, "gh_put", fake_put)
    U.publish("token", lambda c, m: (dict(c), {**m, "last_check_utc": "x"}, {}))
    assert U.PRIMARY_PATH not in calls
    assert calls == [U.META_PATH]
