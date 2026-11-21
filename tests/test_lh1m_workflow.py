"""Workflow isolation and live-vs-dry-run configuration guards."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_lh_workflow_is_isolated_spot_worker_and_messages_enabled():
    path = ROOT / '.github/workflows/lh-1m-signal.yml'
    text = path.read_text(encoding='utf-8')
    doc = yaml.safe_load(text)
    triggers = doc.get('on', doc.get(True))
    assert triggers['schedule'][0]['cron'] == '*/5 * * * *'
    assert 'workflow_dispatch' in triggers
    assert doc['permissions']['contents'] == 'write'
    assert doc['concurrency']['group'] == 'isolated-lh-1m-signals'
    assert doc['concurrency']['cancel-in-progress'] is False
    env = doc['jobs']['monitor']['env']
    assert env['TELEGRAM_MUTE'] == '0'
    assert env['TELEGRAM_BOT_TOKEN'] == '${{ secrets.TELEGRAM_BOT_TOKEN }}'
    assert env['TELEGRAM_CHAT_ID'] == '${{ secrets.TELEGRAM_CHAT_ID }}'
    assert 'run_lh1m.py --seconds 3000' in text
    assert 'git push' not in text
    assert 'nw-trend-experiment' not in text


def test_lh_market_never_calls_binance_order_endpoints():
    source = (ROOT / 'src/lh1m_market.py').read_text(encoding='utf-8')
    assert '/api/v3/order' not in source
    for required in ('/api/v3/time', '/api/v3/exchangeInfo', '/api/v3/klines'):
        assert required in source
    assert "'interval': '1m'" in source or '"interval": "1m"' in source
    assert 'isSpotTradingAllowed' in source and 'quoteAsset' in source


def test_original_user_pine_license_is_retained():
    indicator = (ROOT / 'src/lh1m_indicator.py').read_text(encoding='utf-8')
    license_text = (ROOT / 'LICENSE_TREND_LINES_MPL-2.0.txt').read_text(encoding='utf-8')
    assert 'LonesomeTheBlue' in indicator and 'MPL-2.0' in indicator
    assert 'Mozilla Public License' in license_text
