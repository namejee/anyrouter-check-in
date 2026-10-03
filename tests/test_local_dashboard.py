import copy
import json
import sys
import threading
from datetime import datetime, timedelta
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_dashboard import BEIJING, Dashboard, DashboardError, make_handler, merge_history, now, parse_cookie_input


@pytest.fixture
def configured(tmp_path):
	accounts = [
		{'name': 'GitHub', 'provider': 'anyrouter.top', 'api_user': '1', 'cookies': {'session': 'private-old-github'}},
		{
			'name': 'LinuxDO',
			'provider': 'anyrouter.top',
			'api_user': '2',
			'cookies': {'session': 'private-old-linuxdo'},
		},
		{'name': 'Other', 'provider': 'other', 'cookies': {'session': 'private-other'}},
	]
	path = tmp_path / 'config/cookie.json'
	path.parent.mkdir()
	path.write_text(json.dumps(accounts))
	return tmp_path, accounts


def test_editor_export_preserves_session_expiry_and_filters_unneeded_cookies():
	future = (datetime.now(BEIJING) + timedelta(days=30)).timestamp()
	value = json.dumps(
		[
			{'domain': 'anyrouter.top', 'name': 'session', 'value': 'private-new', 'expirationDate': future},
			{'domain': 'anyrouter.top', 'name': 'acw_tc', 'value': 'private-waf'},
			{'domain': 'anyrouter.top', 'name': 'unneeded', 'value': 'ignored'},
		]
	)
	cookies, expires = parse_cookie_input(value)
	assert cookies == {'session': 'private-new', 'acw_tc': 'private-waf'}
	assert expires == datetime.fromtimestamp(future, BEIJING).strftime('%Y-%m-%d %H:%M:%S')


@pytest.mark.parametrize(
	'value',
	[
		'[{',
		'{"acw_tc":"only-waf"}',
		'[{"domain":"github.com","name":"session","value":"wrong-site"}]',
		'{"session":"line\\nbreak"}',
		'[{"domain":"anyrouter.top","name":"session","value":"old","expirationDate":1}]',
		'[{"domain":"anyrouter.top","name":"session","value":"one"},{"domain":"anyrouter.top","name":"session","value":"two"}]',
	],
)
def test_invalid_or_expired_export_is_rejected(value):
	with pytest.raises(DashboardError):
		parse_cookie_input(value)


def test_request_header_is_supported_without_splitting_session_equals():
	assert parse_cookie_input('Cookie: session=private==; acw_tc=waf; unrelated=discard')[0] == {
		'session': 'private==',
		'acw_tc': 'waf',
	}


def test_update_checks_identity_then_syncs_only_anyrouter_and_preserves_other_accounts(configured):
	root, accounts = configured
	calls = []
	dashboard = Dashboard(
		root,
		runner=lambda args, input_text=None: calls.append((args, input_text)),
		probe=lambda account, cookies: {'api_user': '1'},
	)
	dashboard.update('account-0', {'session': 'private-new-github'}, '2099-01-01 00:00:00')
	saved = json.loads(dashboard.config_path.read_text())
	assert saved[0]['cookies']['session'] == 'private-new-github'
	assert saved[1:] == accounts[1:]
	assert calls[0][0] == ['gh', 'secret', 'set', 'ANYROUTER_ACCOUNTS', '--repo', dashboard.repo, '--env', 'production']
	production = json.loads(calls[0][1])
	assert len(production) == 2
	assert production[1] == accounts[1]
	assert dashboard.config_path.stat().st_mode & 0o777 == 0o600
	assert 'private-' not in json.dumps(dashboard.state())
	assert not list(root.glob('config/.dashboard-secret-*'))


def test_wrong_account_cookie_never_saves_or_syncs(configured):
	root, accounts = configured
	calls = []
	dashboard = Dashboard(root, runner=lambda *args: calls.append(args), probe=lambda *args: {'api_user': '2'})
	with pytest.raises(DashboardError, match='账号不一致'):
		dashboard.update('account-0', {'session': 'private-wrong-account'}, None)
	assert dashboard.config() == accounts
	assert calls == []


def test_sync_failure_preserves_existing_local_config(configured):
	root, accounts = configured

	def failed_sync(*args):
		raise DashboardError('GitHub 同步失败')

	dashboard = Dashboard(root, runner=failed_sync, probe=lambda *args: {'api_user': '1'})
	with pytest.raises(DashboardError, match='同步失败'):
		dashboard.update('account-0', {'session': 'private-new'}, None)
	assert dashboard.config() == accounts


def test_state_keeps_earlier_credit_and_balance_while_explaining_latest_auth_failure(configured):
	root, _ = configured
	dashboard = Dashboard(root)
	day = now()[:10]
	stamp = f'{day} 00:00:00'
	detail = {'name': 'GitHub', 'success': True, 'after_quota': 125, 'check_in_reward': 25}
	failed = {'name': 'GitHub', 'success': False, 'after_quota': None, 'failure_kind': 'auth_required'}
	dashboard.history = merge_history(
		[
			{'run_id': '1:1', 'executed_at': stamp, 'details': [detail]},
			{'run_id': '2:1', 'executed_at': now(), 'details': [failed]},
		]
	)
	state = dashboard.state()
	assert state['accounts'][0]['success'] is False
	assert state['accounts'][0]['failure_kind'] == 'auth_required'
	assert state['accounts'][0]['quota'] == 125
	assert state['accounts'][0]['quota_at'] == stamp
	assert state['daily'][-1]['accounts'][0]['status'] == 'positive'
	assert state['daily'][-1]['accounts'][0]['observed_credit'] == 25
	assert state['daily'][-1]['accounts'][1]['status'] == 'unknown'


def test_http_requires_local_host_origin_and_token_and_never_serves_private_config(configured):
	root, _ = configured
	dashboard = Dashboard(root)
	page = root / 'index.html'
	page.write_text('<html>__PANEL_TOKEN__</html>')
	server = ThreadingHTTPServer(('127.0.0.1', 0), None)
	port = server.server_address[1]
	server.RequestHandlerClass = make_handler(dashboard, page, 'test-token', port)
	thread = threading.Thread(target=server.serve_forever, daemon=True)
	thread.start()
	calls = []
	dashboard.start = lambda action, payload: calls.append((action, copy.deepcopy(payload))) or {'status': 'running'}

	def request(method, path, headers=None, body=None):
		conn = HTTPConnection('127.0.0.1', port, timeout=5)
		conn.request(method, path, body=body, headers=headers or {})
		response = conn.getresponse()
		result = response.status, response.read().decode()
		conn.close()
		return result

	try:
		assert request('GET', '/')[0] == 200
		assert request('GET', '/config/cookie.json')[0] == 404
		assert request('GET', '/', {'Host': f'evil.example:{port}'})[0] == 403
		assert request('GET', '/api/state')[0] != 200
		status, body = request('GET', '/api/state', {'X-Panel-Token': 'test-token'})
		assert status == 200 and 'private-' not in body
		common = {'X-Panel-Token': 'test-token', 'Content-Type': 'application/json'}
		assert request('POST', '/api/update', {**common, 'Origin': 'https://evil.example'}, '{}')[0] == 403
		assert (
			request(
				'POST', '/api/update', {**common, 'X-Panel-Token': 'wrong', 'Origin': f'http://127.0.0.1:{port}'}, '{}'
			)[0]
			== 403
		)
		assert calls == []
		assert request('POST', '/api/refresh', {**common, 'Origin': f'http://127.0.0.1:{port}'}, '{}')[0] == 202
		assert calls == [('refresh', {})]
	finally:
		server.shutdown()
		server.server_close()
		thread.join(timeout=5)
