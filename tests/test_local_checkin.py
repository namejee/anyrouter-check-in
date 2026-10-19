import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import checkin
from local_checkin import run
from utils.daily_ledger import record_balance_run


def test_local_run_uses_only_anyrouter_and_merges_private_ledger(tmp_path, monkeypatch):
	config = tmp_path / 'config/cookie.json'
	config.parent.mkdir()
	config.write_text(
		json.dumps(
			[
				{'name': 'GitHub', 'provider': 'anyrouter.top', 'api_user': '1', 'cookies': {'session': 'private'}},
				{'name': 'Other', 'provider': 'other', 'api_user': '2', 'cookies': {'session': 'other-private'}},
			]
		)
	)
	for name in [
		'launch_playwright_context',
		'check_in_account',
		'load_accounts_config',
		'BALANCE_HASH_FILE',
		'BALANCE_HISTORY_FILE',
		'BALANCE_DAILY_FILE',
	]:
		monkeypatch.setattr(checkin, name, getattr(checkin, name))
	monkeypatch.setenv('GITHUB_STEP_SUMMARY', '')
	monkeypatch.delenv('GITHUB_RUN_ID', raising=False)
	monkeypatch.setattr(checkin.notify, 'push_message', lambda *args, **kwargs: None)
	seen = []

	async def fake_main():
		accounts = checkin.load_accounts_config()
		seen.extend(account.name for account in accounts)
		record_balance_run(
			checkin.BALANCE_DAILY_FILE,
			[{'name': 'GitHub', 'success': True, 'after_quota': 25, 'check_in_reward': 25}],
			checkin.datetime.now(checkin.BEIJING_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S'),
		)
		raise SystemExit(0)

	monkeypatch.setattr(checkin, 'main', fake_main)
	assert asyncio.run(run(tmp_path)) == 0
	assert seen == ['GitHub']
	assert json.loads((tmp_path / 'dashboard_history.json').read_text())[-1]['run_id'].startswith('local:')
	assert 'private' not in (tmp_path / 'dashboard_history.json').read_text()
	assert asyncio.run(run(tmp_path)) == 0
	assert seen == ['GitHub']  # A confirmed same-day credit prevents repeated runs.
