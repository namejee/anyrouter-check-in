import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from checkin import (
	add_cookies_to_browser_context,
	build_check_in_detail,
	check_in_account,
	format_check_in_notification,
	format_run_summary,
	load_balance_history,
	main,
	parse_check_in_response,
	parse_user_info_response,
	prepare_cookies,
)
from utils.config import AccountConfig, AppConfig, ProviderConfig, load_accounts_config


def test_browser_fallback_injects_auth_and_waf_cookies():
	class FakeContext:
		def __init__(self):
			self.cookies = None

		async def add_cookies(self, cookies):
			self.cookies = cookies

	context = FakeContext()
	asyncio.run(
		add_cookies_to_browser_context(
			context,
			'https://agentrouter.org',
			{'session': 'session-value', 'acw_tc': 'waf-value'},
		)
	)

	assert context.cookies is not None
	assert {cookie['name'] for cookie in context.cookies} == {'session', 'acw_tc'}
	assert all(cookie['url'].endswith('/') and 'path' not in cookie for cookie in context.cookies)


def test_load_accounts_accepts_cookie_and_relogin_accounts(monkeypatch):
	monkeypatch.setenv(
		'ANYROUTER_ACCOUNTS',
		json.dumps(
			[
				{'name': 'cookie-account', 'cookies': {'session': 's'}, 'api_user': '123'},
				{
					'name': 'relogin-account',
					'provider': 'agentrouter',
					'credentials': {'username': 'user@example.com', 'password': 'password'},
				},
			]
		),
	)

	accounts = load_accounts_config()

	assert accounts is not None
	assert accounts[0].cookies == {'session': 's'}
	assert accounts[1].has_credentials()
	assert accounts[1].api_user is None


def test_parse_check_in_response_accepts_already_checked():
	assert parse_check_in_response('test', 200, '{"success":false,"message":"已签到"}') is True


@pytest.mark.parametrize(
	'body',
	[
		'<html>verification unsuccessful; success requires login</html>',
		'null',
		'[]',
		'{"success":"false"}',
		'{"success":false,"code":0,"message":"login required"}',
		'{"code":false}',
		'{"ret":false,"code":0}',
	],
)
def test_check_in_rejects_unconfirmed_responses(body):
	assert parse_check_in_response('test', 200, body) is False


@pytest.mark.parametrize('body', ['{"success":true}', '{"code":0}', '{"ret":1}'])
def test_check_in_accepts_explicit_api_confirmation(body):
	assert parse_check_in_response('test', 200, body) is True


@pytest.mark.parametrize('body', ['null', '{"success":true}', '{"success":true,"data":{}}'])
def test_user_info_requires_real_balance_data(body):
	assert parse_user_info_response(200, body)['success'] is False


def test_waf_refresh_replaces_stale_values_and_keeps_login_session(monkeypatch):
	old_cookies = {'session': 'login-session', 'acw_tc': 'old-tc', 'cdn_sec_tc': 'old-cdn', 'acw_sc__v2': 'old-sc'}
	fresh_cookies = {'acw_tc': 'new-tc', 'cdn_sec_tc': 'new-cdn', 'acw_sc__v2': 'new-sc'}

	async def fetch_cookies(*args):
		return fresh_cookies

	monkeypatch.setattr('checkin.get_waf_cookies_with_playwright', fetch_cookies)
	provider = AppConfig.load_from_env().get_provider('anyrouter')
	result = asyncio.run(prepare_cookies('test', provider, old_cookies, force_refresh=True))

	assert result == {'session': 'login-session', **fresh_cookies}
	assert old_cookies['acw_tc'] == 'old-tc'


def test_anyrouter_uses_its_explicit_check_in_api():
	provider = AppConfig.load_from_env().get_provider('anyrouter.top')

	assert provider is not None
	assert provider.checkin_on_login is False
	assert provider.sign_in_path == '/api/user/sign_in'
	assert provider.needs_manual_check_in() is True


def test_checkin_on_login_skips_explicit_sign_in_api():
	provider = ProviderConfig.from_dict(
		'login-triggered',
		{
			'domain': 'https://example.com',
			'checkin_on_login': True,
		},
	)

	assert provider.needs_manual_check_in() is False


def test_anyrouter_retries_explicit_check_in_in_browser_when_http_is_blocked(monkeypatch):
	account = AccountConfig(
		name='GitHub account',
		provider='anyrouter',
		cookies={'session': 'session-value'},
		api_user='123',
	)
	calls = {}

	async def fake_prepare_cookies(account_name, provider_config, user_cookies):
		calls['prepared'] = user_cookies
		return {'session': 'session-value', 'acw_tc': 'waf-value'}

	def fake_get_user_info(client, headers, user_info_url):
		return {'success': False, 'error': 'WAF verification page'}

	def fake_execute_check_in(client, account_name, provider_config, headers):
		calls['sign_in_path'] = provider_config.sign_in_path
		return False

	async def fake_get_browser_cookies(account_name, provider_config, user_cookies, current_cookies):
		calls['retry_cookies'] = current_cookies
		return {'session': 'session-value', 'acw_tc': 'fresh-waf-value'}

	async def fake_browser_check_in(account_name, provider_config, cookies, api_user, username=None, password=None):
		calls['browser'] = {
			'cookies': cookies,
			'api_user': api_user,
			'username': username,
			'password': password,
		}
		return True, {'success': False}, {'success': True}

	class FakeCookies:
		def update(self, cookies):
			calls['client_cookies'] = cookies

	class FakeClient:
		def __init__(self, **kwargs):
			self.cookies = FakeCookies()

		def close(self):
			calls['client_closed'] = True

	monkeypatch.setattr('checkin.prepare_cookies', fake_prepare_cookies)
	monkeypatch.setattr('checkin.get_user_info', fake_get_user_info)
	monkeypatch.setattr('checkin.execute_check_in', fake_execute_check_in)
	monkeypatch.setattr('checkin.get_browser_cookies_for_retry', fake_get_browser_cookies)
	monkeypatch.setattr('checkin.execute_automatic_check_in_with_playwright', fake_browser_check_in)
	monkeypatch.setattr('checkin.httpx.Client', FakeClient)

	result = asyncio.run(check_in_account(account, 0, AppConfig.load_from_env()))

	assert result[0] is True
	assert calls['prepared'] == {'session': 'session-value'}
	assert calls['sign_in_path'] == '/api/user/sign_in'
	assert calls['retry_cookies'] == {'session': 'session-value', 'acw_tc': 'waf-value'}
	assert calls['browser']['cookies'] == {'session': 'session-value', 'acw_tc': 'fresh-waf-value'}
	assert calls['browser']['username'] is None
	assert calls['client_closed'] is True


def test_http_confirmation_without_balance_verification_fails(monkeypatch, mocker):
	mocker.patch('checkin.httpx.Client')
	provider = ProviderConfig(name='test', domain='https://example.com')
	account = AccountConfig(provider='test', cookies={'session': 'test-session'})
	responses = iter([{'success': False, 'error': 'not logged in'}, {'success': False, 'error': 'HTTP 401'}])
	monkeypatch.setattr('checkin.get_user_info', lambda *args: next(responses))
	monkeypatch.setattr('checkin.execute_check_in', lambda *args: True)

	result = asyncio.run(check_in_account(account, 0, AppConfig(providers={'test': provider})))

	assert result[0] is False
	assert result[2]['error'] == 'HTTP 401'


def test_browser_retry_preserves_the_balance_before_first_check_in(monkeypatch, mocker):
	mocker.patch('checkin.httpx.Client')
	account = AccountConfig(provider='anyrouter', cookies={'session': 'test-session'})
	before = {'success': True, 'quota': 10, 'used_quota': 0, 'display': 'before: 10'}
	after = {'success': True, 'quota': 35, 'used_quota': 0, 'display': 'after: 35'}
	responses = iter([before, {'success': False, 'error': 'WAF'}])

	async def cookies(*args, **kwargs):
		return {'session': 'test-session'}

	async def browser_retry(*args):
		# 第一次 POST 已经发放奖励，只是其后的余额查询被拦截。
		return True, after, after

	monkeypatch.setattr('checkin.prepare_cookies', cookies)
	monkeypatch.setattr('checkin.get_browser_cookies_for_retry', cookies)
	monkeypatch.setattr('checkin.get_user_info', lambda *args: next(responses))
	monkeypatch.setattr('checkin.execute_check_in', lambda *args: True)
	monkeypatch.setattr('checkin.execute_automatic_check_in_with_playwright', browser_retry)

	result = asyncio.run(check_in_account(account, 0, AppConfig.load_from_env()))

	assert result == (True, before, after)
	assert build_check_in_detail('test', *result)['check_in_reward'] == 25


def test_invalid_account_returns_a_complete_failure_result():
	account = AccountConfig(provider='missing', cookies={'session': 'test-session'})
	assert asyncio.run(check_in_account(account, 0, AppConfig(providers={}))) == (False, None, None)


def test_balance_calculation_does_not_invent_reward_from_rounding():
	before = parse_user_info_response(200, '{"success":true,"data":{"quota":2500,"used_quota":3000}}')
	after = parse_user_info_response(200, '{"success":true,"data":{"quota":1499,"used_quota":4001}}')
	detail = build_check_in_detail('test', True, before, after)

	assert detail['check_in_reward'] == 0
	assert '今日已签到' not in format_check_in_notification(detail)


def test_summary_retains_failures_and_unknown_balances():
	after = {'success': True, 'quota': 35, 'used_quota': 2}
	details = [
		build_check_in_detail('successful', True, None, after),
		build_check_in_detail('failed', False, None, None),
	]
	summary = format_run_summary(details, '2026-09-13 12:00:00')

	assert '| successful | 已确认完成 | 未读取 | $35.00 | 未读取 | 未读取 |' in summary
	assert '| failed | 失败 / 未确认 | 未读取 | 未读取 | 未读取 | 未读取 |' in summary
	assert '北京时间：2026-09-13 12:00:00' in summary
	assert '无法计算' in format_check_in_notification(details[0])


def test_history_captures_credit_added_before_first_balance_read():
	previous = {'success': True, 'quota': 100, 'used_quota': 50, 'checked_at': '2026-01-01 08:30:00'}
	current = {'success': True, 'quota': 80, 'used_quota': 95}
	detail = build_check_in_detail('test', True, current, current, previous)

	assert detail['check_in_reward'] == 0
	assert detail['since_previous'] == 25
	assert detail['previous_quota'] == 100
	assert '2026-01-01 08:30:00' in format_run_summary([detail], '2026-01-01 12:07:00')


@pytest.mark.parametrize('content', ['broken JSON', '[]', '{"account":"invalid"}', '{"account":{"success":true}}'])
def test_invalid_history_does_not_become_a_zero_balance(monkeypatch, tmp_path, content):
	path = tmp_path / 'history.json'
	path.write_text(content)
	monkeypatch.setattr('checkin.BALANCE_HISTORY_FILE', str(path))
	assert load_balance_history() == {}


@pytest.mark.parametrize('verified, exit_code', [(True, 0), (False, 1)])
def test_main_always_writes_summary_and_fails_unverified_accounts(monkeypatch, tmp_path, verified, exit_code):
	account = AccountConfig(name='test-account', cookies={'session': 'test-session'})
	info = {'success': True, 'quota': 35, 'used_quota': 2, 'display': 'balance: 35'}
	output = tmp_path / 'summary.md'
	history_file = tmp_path / 'history.json'

	async def check_account(*args):
		return True, info, info if verified else {'success': False, 'error': 'HTTP 401'}

	monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(output))
	monkeypatch.setattr('checkin.BALANCE_HISTORY_FILE', str(history_file))
	monkeypatch.setattr('checkin.load_accounts_config', lambda: [account])
	monkeypatch.setattr('checkin.check_in_account', check_account)
	monkeypatch.setattr('checkin.load_balance_hash', lambda: 'unchanged')
	monkeypatch.setattr('checkin.generate_balance_hash', lambda *args: 'unchanged')
	monkeypatch.setattr('checkin.save_balance_hash', lambda *args: None)
	monkeypatch.setattr('checkin.notify.push_message', lambda *args, **kwargs: None)

	with pytest.raises(SystemExit) as stopped:
		asyncio.run(main())

	assert stopped.value.code == exit_code
	summary = output.read_text()
	assert 'test-account' in summary
	assert ('已确认完成' if verified else '失败 / 未确认') in summary
	assert '$35.00' in summary
	if verified:
		history = json.loads(history_file.read_text())
		assert history['https://anyrouter.top|test-account']['quota'] == 35
		assert 'session' not in history_file.read_text()
