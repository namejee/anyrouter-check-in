import io
from datetime import datetime
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from scripts.scheduler import BEIJING, GitHub, decide, log_health


def sample_logs(any_increment='+$25.00', agent_summary='基线待建立'):
	lines = []
	for index, provider in enumerate(['anyrouter', 'anyrouter', 'agentrouter', 'agentrouter']):
		name = f'Account {index}'
		increment = any_increment if provider == 'anyrouter' else agent_summary
		lines += [
			f'[INFO] {name}: Using provider "{provider}"',
			f'{name} · {increment} · 余额 $50.00 · 总量 $100.00',
		]
	return '\n'.join('2026-10-20T01:00:00Z ' + line for line in lines)


def test_receipts_and_real_increment_are_verified_without_exposing_balances():
	result = log_health(sample_logs())
	assert result['healthy']
	assert result['accounts'] == 4
	assert result['unconfirmed_baselines'] == 2
	assert 'balance' not in result


def test_green_run_with_anyrouter_not_credited_requires_retry():
	assert not log_health(sample_logs(any_increment='未到账'))['healthy']


def test_agentrouter_login_without_a_known_daily_increase_requires_retry():
	result = log_health(sample_logs(agent_summary='未到账(较10-19)'))
	assert not result['healthy']
	assert result['agentrouter_uncredited'] == 2


def test_missing_account_and_failed_account_cannot_be_healthy():
	assert not log_health(sample_logs().split('\n', 2)[2])['healthy']
	assert not log_health(sample_logs() + '\n2026-10-20T01:00:00Z [FAILED] Account 0: timeout')['healthy']


def test_already_credited_accounts_are_verified_without_repeat_requests():
	raw = '\n'.join(
		f'2026-10-20T01:00:00Z ***SKIP*** Account {i}: already credited today, skipping this run' for i in range(4)
	)
	assert log_health(raw)['healthy']


def test_before_0803_never_dispatches():
	api = Mock()
	assert decide(api, {}, datetime(2026, 10, 20, 8, 2, tzinfo=BEIJING))['action'] == 'wait'
	api.runs.assert_not_called()


def test_missing_daily_run_dispatches_at_0803():
	api = Mock()
	api.runs.return_value = []
	assert decide(api, {}, datetime(2026, 10, 20, 8, 3, tzinfo=BEIJING))['action'] == 'dispatch'


@pytest.mark.parametrize('status', ['queued', 'in_progress'])
def test_existing_run_prevents_duplicate_dispatch(status):
	api = Mock()
	api.runs.return_value = [{'id': 1, 'created_at': '2026-10-20T00:03:00Z', 'status': status, 'conclusion': None}]
	api.health.return_value = None
	assert decide(api, {}, datetime(2026, 10, 20, 8, 18, tzinfo=BEIJING))['reason'] == 'existing_run_active'


def test_completed_worker_logs_override_delayed_run_status():
	api = Mock()
	api.runs.return_value = [
		{'id': 1, 'created_at': '2026-10-20T00:03:00Z', 'status': 'in_progress', 'conclusion': None}
	]
	api.health.return_value = log_health(sample_logs())
	assert decide(api, {}, datetime(2026, 10, 20, 8, 18, tzinfo=BEIJING))['action'] == 'done'


def test_verified_day_does_not_read_github_again():
	api = Mock()
	assert (
		decide(api, {'healthy': True, 'run_id': 1}, datetime(2026, 10, 20, 8, 18, tzinfo=BEIJING))['action'] == 'done'
	)
	api.runs.assert_not_called()


def test_retry_cooldown_and_daily_attempt_limit_are_enforced():
	api = Mock()
	api.runs.return_value = []
	now = datetime(2026, 10, 20, 9, 0, tzinfo=BEIJING)
	assert decide(api, {'attempts': ['2026-10-20T08:45:00+08:00']}, now)['reason'] == 'retry_cooldown'
	assert decide(api, {'attempts': ['2026-10-20T08:03:00+08:00'] * 3}, now)['reason'] == 'daily_attempt_limit'


def test_success_before_morning_window_cannot_skip_the_morning_checkin():
	api = Mock()
	api.runs.return_value = [
		{'id': 1, 'created_at': '2026-10-19T16:14:18Z', 'status': 'completed', 'conclusion': 'success'}
	]
	assert decide(api, {}, datetime(2026, 10, 20, 8, 3, tzinfo=BEIJING))['action'] == 'dispatch'
	api.health.assert_not_called()


def test_log_storage_never_receives_the_github_credential(monkeypatch):
	opener = Mock()
	opener.open.side_effect = HTTPError(
		'https://api.github.com/logs', 302, 'redirect', {'Location': 'https://logs.example/job.txt'}, None
	)
	download = Mock(return_value=io.BytesIO(b'completed job logs'))
	monkeypatch.setattr('scripts.scheduler.build_opener', Mock(return_value=opener))
	monkeypatch.setattr('scripts.scheduler.urlopen', download)
	assert GitHub('private-test-token', 'owner/repo').request('/logs', raw=True) == 'completed job logs'
	assert download.call_args.args[0].get_header('Authorization') is None
