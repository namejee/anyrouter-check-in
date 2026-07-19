import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from checkin import add_cookies_to_browser_context, check_in_account, parse_check_in_response
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
