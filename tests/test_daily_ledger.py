import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.daily_ledger import format_daily_summary, record_balance_run


def observation(stamp, **values):
	return {'executed_at': stamp, 'details': [{'name': 'GitHub', **values}]}


def test_daily_positive_survives_later_zero_consumption_and_failed_read():
	entries = [
		observation('2026-09-29 08:00:00', after_quota=125, check_in_reward=25),
		observation('2026-09-29 12:00:00', after_quota=90, check_in_reward=0, usage_increase=35),
		observation('2026-09-29 18:00:00', after_quota=None, success=False, failure_kind='auth_required'),
	]
	summary = format_daily_summary(entries, '2026-09-29 18:00:00', ['GitHub'])
	assert '| 2026-09-29 | GitHub | 有增加 | $90.00 | 12:00:00 |' in summary
	assert '| 2026-09-28 | GitHub | 无可核对记录 | 未读取 | — |' in summary
	assert '| 登录失效（HTTP 401） 18:00:00 |' in summary


def test_credit_before_first_read_counts_when_previous_read_is_same_day():
	entries = [
		observation(
			'2026-09-29 12:00:00',
			after_quota=125,
			check_in_reward=0,
			since_previous=25,
			previous_checked_at='2026-09-29 08:00:00',
		)
	]
	summary = format_daily_summary(entries, '2026-09-29 12:00:00', ['GitHub'])
	assert '| 2026-09-29 | GitHub | 有增加 |' in summary


def test_cross_day_credit_does_not_claim_a_specific_day():
	entries = [
		observation(
			'2026-09-29 08:00:00',
			after_quota=125,
			check_in_reward=0,
			since_previous=25,
			previous_checked_at='2026-09-28 22:00:00',
		)
	]
	summary = format_daily_summary(entries, '2026-09-29 08:00:00', ['GitHub'])
	assert '| 2026-09-29 | GitHub | 跨日区间有增加，日期待确认 |' in summary
	assert '| 2026-09-28 | GitHub | 无可核对记录 |' in summary


def test_zero_is_observed_but_missing_is_unknown_and_accounts_are_independent():
	entry = observation('2026-09-29 08:00:00', after_quota=0, check_in_reward=0)
	entry['details'].append({'name': 'LinuxDO', 'after_quota': None, 'check_in_reward': None})
	summary = format_daily_summary([entry], entry['executed_at'], ['GitHub', 'LinuxDO'])
	assert '| GitHub | 未观察到增加 | $0.00 |' in summary
	assert '| LinuxDO | 无可核对记录 | 未读取 |' in summary


def test_ledger_retains_90_days_strips_credentials_and_replaces_same_run(tmp_path, monkeypatch):
	path = tmp_path / 'daily.json'
	old = observation('2026-01-01 08:00:00', after_quota=1)
	recent = observation('2026-09-28 08:00:00', after_quota=100, cookies={'session': 'private'})
	recent['private'] = 'private'
	path.write_text(json.dumps([old, recent]))
	monkeypatch.setenv('GITHUB_RUN_ID', '123')
	monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '1')
	detail = {
		'name': 'GitHub',
		'after_quota': 125,
		'check_in_reward': 25,
		'token': 'private',
		'failure_kind': 'private',
	}
	record_balance_run(str(path), [detail], '2026-09-29 08:00:00')
	entries = record_balance_run(str(path), [detail], '2026-09-29 08:00:00')
	assert len(entries) == 2
	assert entries[-1]['run_id'] == '123:1'
	assert 'private' not in path.read_text()
	assert entries[-1]['details'][0]['failure_kind'] is None
	assert not path.with_suffix('.tmp').exists()
	assert json.loads(path.read_text()) == entries


def test_malformed_cache_recovers_without_inventing_credit(tmp_path):
	path = tmp_path / 'daily.json'
	path.write_text('{invalid json')
	entries = record_balance_run(str(path), [{'name': 'GitHub', 'check_in_reward': '25'}], '2026-09-29 08:00:00')
	assert len(entries) == 1
	assert '| GitHub | 无可核对记录 |' in format_daily_summary(entries, '2026-09-29 08:00:00', ['GitHub'])
	path.write_text(
		json.dumps(
			[
				None,
				{
					'executed_at': '2026-09-29 08:00:00',
					'details': [None, 'bad', {'name': 'GitHub', 'after_quota': True}],
				},
			]
		)
	)
	entries = record_balance_run(str(path), [], '2026-09-29 09:00:00')
	assert '| GitHub | 无可核对记录 |' in format_daily_summary(entries, '2026-09-29 09:00:00', ['GitHub'])
