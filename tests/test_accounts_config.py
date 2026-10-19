import json

from utils.config import load_accounts_config

BASE_ACCOUNTS = [
	{'name': 'any主帐号', 'cookies': {'session': 'base-session'}, 'api_user': '11111'},
	{'name': 'agLD', 'provider': 'agentrouter', 'cookies': {'session': 'ld-session'}, 'api_user': '22222'},
	{'name': 'agGithub', 'cookies': {'session': 'stale-session'}, 'api_user': '33333'},
]


def _set_env(monkeypatch, base=BASE_ACCOUNTS, extra=None):
	monkeypatch.setenv('ANYROUTER_ACCOUNTS', json.dumps(base))
	if extra is None:
		monkeypatch.delenv('ANYROUTER_ACCOUNTS_EXTRA', raising=False)
	else:
		monkeypatch.setenv('ANYROUTER_ACCOUNTS_EXTRA', json.dumps(extra))


def test_base_accounts_load_unchanged_without_extra(monkeypatch):
	_set_env(monkeypatch)

	accounts = load_accounts_config()

	assert accounts is not None
	assert [a.name for a in accounts] == ['any主帐号', 'agLD', 'agGithub']
	assert all(not a.has_login_credentials() for a in accounts)


def test_empty_extra_is_ignored(monkeypatch):
	_set_env(monkeypatch, extra=[])
	monkeypatch.setenv('ANYROUTER_ACCOUNTS_EXTRA', '   ')

	accounts = load_accounts_config()

	assert accounts is not None
	assert len(accounts) == 3


def test_extra_overrides_same_name_to_email_login(monkeypatch):
	_set_env(monkeypatch, extra=[{'name': 'agGithub', 'email': 'a@b.com', 'password': 'pw'}])

	accounts = load_accounts_config()

	assert accounts is not None
	assert [a.name for a in accounts] == ['any主帐号', 'agLD', 'agGithub']

	ag_github = accounts[2]
	assert ag_github.email == 'a@b.com'
	assert ag_github.password == 'pw'
	assert ag_github.has_login_credentials() is True

	# 其它账号不受影响
	assert accounts[0].cookies == {'session': 'base-session'}
	assert accounts[1].cookies == {'session': 'ld-session'}


def test_override_is_field_level_and_keeps_provider(monkeypatch):
	_set_env(
		monkeypatch,
		extra=[{'name': 'agLD', 'email': 'ld@b.com', 'password': 'pw'}],
	)

	accounts = load_accounts_config()

	assert accounts is not None
	ag_ld = accounts[1]
	assert ag_ld.provider == 'agentrouter'  # 未被覆盖的字段保留
	assert ag_ld.email == 'ld@b.com'
	assert ag_ld.api_user == '22222'


def test_partial_override_of_existing_account_is_allowed(monkeypatch):
	_set_env(monkeypatch, extra=[{'name': 'agGithub', 'api_user': '99999'}])

	accounts = load_accounts_config()

	assert accounts is not None
	assert len(accounts) == 3
	assert accounts[2].api_user == '99999'
	assert accounts[2].cookies == {'session': 'stale-session'}


def test_extra_appends_new_account(monkeypatch):
	_set_env(
		monkeypatch,
		extra=[{'name': 'agNew', 'provider': 'agentrouter', 'email': 'n@b.com', 'password': 'pw'}],
	)

	accounts = load_accounts_config()

	assert accounts is not None
	assert [a.name for a in accounts] == ['any主帐号', 'agLD', 'agGithub', 'agNew']
	assert accounts[3].provider == 'agentrouter'


def test_extra_without_credentials_for_unknown_account_fails(monkeypatch):
	_set_env(monkeypatch, extra=[{'name': 'ghost', 'api_user': '1'}])

	assert load_accounts_config() is None


def test_invalid_extra_json_fails_loudly(monkeypatch):
	_set_env(monkeypatch)
	monkeypatch.setenv('ANYROUTER_ACCOUNTS_EXTRA', '[{"name": "bad",}]')

	assert load_accounts_config() is None


def test_missing_base_env_returns_none(monkeypatch):
	monkeypatch.delenv('ANYROUTER_ACCOUNTS', raising=False)
	monkeypatch.setenv('ANYROUTER_ACCOUNTS_EXTRA', json.dumps([{'name': 'x', 'cookies': {'a': 'b'}}]))

	assert load_accounts_config() is None
