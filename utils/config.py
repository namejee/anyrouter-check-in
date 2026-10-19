#!/usr/bin/env python3
"""
配置管理模块
"""

import json
import os
from dataclasses import dataclass
from typing import Dict, List, Literal


@dataclass
class ProviderConfig:
	"""Provider 配置"""

	name: str
	domain: str
	login_path: str = '/login'
	sign_in_path: str | None = '/api/user/sign_in'
	user_info_path: str = '/api/user/self'
	api_user_key: str = 'new-api-user'
	bypass_method: Literal['waf_cookies'] | None = None
	waf_cookie_names: List[str] | None = None
	use_proxy: bool = False
	persist_profile: bool = False
	# 适配器：接口形状不是 NewAPI 的平台要靠它分派。
	#   newapi —— 默认，anyrouter / agentrouter 这类
	#   mlgb7  —— 噜皮生图（image.mlgb7.com），登录走 Linux DO，只能复用会话 cookie
	adapter: str = 'newapi'

	def __post_init__(self):
		required_waf_cookies = set()
		if self.waf_cookie_names and isinstance(self.waf_cookie_names, List):
			for item in self.waf_cookie_names:
				name = '' if not item or not isinstance(item, str) else item.strip()
				if not name:
					print(f'[WARNING] Found invalid WAF cookie name: {item}')
					continue

				required_waf_cookies.add(name)

		if not required_waf_cookies:
			self.bypass_method = None

		self.waf_cookie_names = list(required_waf_cookies)

	@classmethod
	def from_dict(cls, name: str, data: dict, *, defaults: 'ProviderConfig | None' = None) -> 'ProviderConfig':
		"""从字典创建 ProviderConfig

		配置格式:
		- 基础: {"domain": "https://example.com"}
		- 完整: {"domain": "https://example.com", "login_path": "/login", "use_proxy": true, ...}
		"""
		default_use_proxy = defaults.use_proxy if defaults else False
		default_persist_profile = defaults.persist_profile if defaults else False
		return cls(
			name=name,
			domain=data['domain'],
			login_path=data.get('login_path', defaults.login_path if defaults else '/login'),
			sign_in_path=data.get('sign_in_path', defaults.sign_in_path if defaults else '/api/user/sign_in'),
			user_info_path=data.get('user_info_path', defaults.user_info_path if defaults else '/api/user/self'),
			api_user_key=data.get('api_user_key', defaults.api_user_key if defaults else 'new-api-user'),
			bypass_method=data.get('bypass_method', defaults.bypass_method if defaults else None),
			waf_cookie_names=data.get('waf_cookie_names', defaults.waf_cookie_names if defaults else None),
			use_proxy=data.get('use_proxy', default_use_proxy),
			persist_profile=data.get('persist_profile', default_persist_profile),
			adapter=data.get('adapter', defaults.adapter if defaults else 'newapi'),
		)

	def needs_waf_cookies(self) -> bool:
		"""判断是否需要获取 WAF cookies"""
		return self.bypass_method == 'waf_cookies'

	def needs_manual_check_in(self) -> bool:
		"""判断是否需要手动调用签到接口"""
		return self.sign_in_path is not None


@dataclass
class AppConfig:
	"""应用配置"""

	providers: Dict[str, ProviderConfig]

	@classmethod
	def load_from_env(cls) -> 'AppConfig':
		"""从环境变量加载配置"""
		providers = {
			'anyrouter': ProviderConfig(
				name='anyrouter',
				domain='https://anyrouter.top',
				login_path='/login',
				sign_in_path='/api/user/sign_in',
				user_info_path='/api/user/self',
				api_user_key='new-api-user',
				bypass_method='waf_cookies',
				waf_cookie_names=['acw_tc', 'cdn_sec_tc', 'acw_sc__v2'],
				use_proxy=False,
				persist_profile=True,
			),
			'agentrouter': ProviderConfig(
				name='agentrouter',
				domain='https://agentrouter.org',
				login_path='/login',
				sign_in_path=None,  # 无需签到接口，查询用户信息时自动完成签到
				user_info_path='/api/user/self',
				api_user_key='new-api-user',
				bypass_method='waf_cookies',
				waf_cookie_names=['acw_tc'],
				use_proxy=True,
				persist_profile=False,
			),
			# 噜皮生图：不是 NewAPI，接口与字段都不同，由 mlgb7 适配器处理。
			# 账号只能用会话 cookie（该站登录走 Linux DO 授权，没有站内密码）。
			'mlgb7': ProviderConfig(
				name='mlgb7',
				domain='https://image.mlgb7.com',
				login_path='',
				sign_in_path='/api/me/checkin',
				user_info_path='/api/me',
				api_user_key='',
				use_proxy=False,
				adapter='mlgb7',
			),
		}

		# 尝试从环境变量加载自定义 providers
		providers_str = os.getenv('PROVIDERS')
		if providers_str:
			try:
				providers_data = json.loads(providers_str)

				if not isinstance(providers_data, dict):
					print('[WARNING] PROVIDERS must be a JSON object, ignoring custom providers')
					return cls(providers=providers)

				# 解析自定义 providers,会覆盖默认配置
				for name, provider_data in providers_data.items():
					try:
						providers[name] = ProviderConfig.from_dict(
							name,
							provider_data,
							defaults=providers.get(name),
						)
					except Exception as e:
						print(f'[WARNING] Failed to parse provider "{name}": {e}, skipping')
						continue

				print(f'[INFO] Loaded {len(providers_data)} custom provider(s) from PROVIDERS environment variable')
			except json.JSONDecodeError as e:
				print(
					f'[WARNING] Failed to parse PROVIDERS environment variable: {e}, using default configuration only'
				)
			except Exception as e:
				print(f'[WARNING] Error loading PROVIDERS: {e}, using default configuration only')

		return cls(providers=providers)

	def get_provider(self, name: str) -> ProviderConfig | None:
		"""获取指定 provider 配置"""
		return self.providers.get(name)


@dataclass
class AccountConfig:
	"""账号配置"""

	cookies: dict | str | None
	api_user: str | None = None
	provider: str = 'anyrouter'
	name: str | None = None
	email: str | None = None
	password: str | None = None

	@classmethod
	def from_dict(cls, data: dict, index: int) -> 'AccountConfig':
		"""从字典创建 AccountConfig"""
		provider = data.get('provider', 'anyrouter')
		name = data.get('name', f'Account {index + 1}')

		return cls(
			cookies=data.get('cookies'),
			api_user=data.get('api_user'),
			provider=provider,
			name=name if name else None,
			email=data.get('email'),
			password=data.get('password'),
		)

	def has_login_credentials(self) -> bool:
		"""是否配置了邮箱密码登录"""
		return bool(self.email and self.password)

	def get_display_name(self, index: int) -> str:
		"""获取显示名称"""
		return self.name if self.name else f'Account {index + 1}'


BASE_ACCOUNTS_ENV = 'ANYROUTER_ACCOUNTS'
EXTRA_ACCOUNTS_ENV = 'ANYROUTER_ACCOUNTS_EXTRA'


def _parse_accounts_json(raw: str, source: str) -> list[dict] | None:
	"""解析账号 JSON 数组，返回原始字典列表（凭据校验留到合并之后）。"""
	try:
		accounts_data = json.loads(raw)
	except json.JSONDecodeError as e:
		print(f'ERROR: {source} JSON 解析失败: {e}')
		print('HINT: 常见原因 - 末尾多余逗号、使用了单引号、包含注释、或换行格式问题')
		return None

	if not isinstance(accounts_data, list):
		print(f'ERROR: {source} must use array format [{{}}]')
		return None

	accounts: list[dict] = []
	for i, account_dict in enumerate(accounts_data):
		if not isinstance(account_dict, dict):
			print(f'ERROR: {source} account {i + 1} configuration format is incorrect')
			return None

		if 'name' in account_dict and not account_dict['name']:
			print(f'ERROR: {source} account {i + 1} name field cannot be empty')
			return None

		accounts.append(account_dict)

	return accounts


def _merge_accounts(base: list[dict], extra: list[dict]) -> tuple[list[dict], list[str]]:
	"""按 name 合并账号：同名做字段级覆盖，新名字追加到末尾。

	覆盖时 extra 的字段优先，且允许只给片段。因此可以在 ANYROUTER_ACCOUNTS_EXTRA 里写
	{"name": "agGithub", "email": "...", "password": "..."}，让已有账号改用邮箱密码登录，
	而不必重写基础的 ANYROUTER_ACCOUNTS（GitHub secret 只写不可读，改不动单个账号）。

	被覆盖条目里遗留的 cookies 会保留在字典中，但 check_in_account 优先使用邮箱密码，
	所以不会生效。没有 name 的条目无法定位到已有账号，直接追加。
	"""
	merged = [dict(item) for item in base]
	index_by_name = {item['name']: i for i, item in enumerate(merged) if item.get('name')}

	overridden: list[str] = []
	for item in extra:
		name = item.get('name')
		if name and name in index_by_name:
			merged[index_by_name[name]].update(item)
			overridden.append(name)
		else:
			merged.append(dict(item))
			if name:
				index_by_name[name] = len(merged) - 1

	return merged, overridden


def _validate_account(account_dict: dict, index: int) -> bool:
	"""校验合并后的单个账号条目。"""
	label = account_dict.get('name') or f'Account {index + 1}'
	has_cookies = bool(account_dict.get('cookies'))
	has_login = bool(account_dict.get('email') and account_dict.get('password'))

	if not has_cookies and not has_login:
		print(f'ERROR: Account "{label}" must have either cookies or email+password')
		return False

	if 'api_user' not in account_dict and not has_login:
		print(f'ERROR: Account "{label}" missing required field (api_user) - only email+password login can omit it')
		return False

	return True


def load_accounts_config() -> list[AccountConfig] | None:
	"""从环境变量加载账号配置。

	- ANYROUTER_ACCOUNTS：基础账号数组（必填）
	- ANYROUTER_ACCOUNTS_EXTRA：可选增量数组，按 name 覆盖同名账号或追加新账号
	"""
	accounts_str = os.getenv(BASE_ACCOUNTS_ENV)
	if not accounts_str:
		print(f'ERROR: {BASE_ACCOUNTS_ENV} environment variable not found')
		return None

	accounts_data = _parse_accounts_json(accounts_str, BASE_ACCOUNTS_ENV)
	if accounts_data is None:
		return None

	merged = accounts_data
	extra_str = (os.getenv(EXTRA_ACCOUNTS_ENV) or '').strip()
	if extra_str:
		extra_data = _parse_accounts_json(extra_str, EXTRA_ACCOUNTS_ENV)
		if extra_data is None:
			return None

		merged, overridden = _merge_accounts(accounts_data, extra_data)
		print(f'[INFO] {EXTRA_ACCOUNTS_ENV}: merged {len(extra_data)} account(s) into {len(accounts_data)}')
		for name in overridden:
			print(f'[INFO] {EXTRA_ACCOUNTS_ENV} overrides account "{name}"')

	try:
		accounts = []
		for i, account_dict in enumerate(merged):
			if not _validate_account(account_dict, i):
				return None

			accounts.append(AccountConfig.from_dict(account_dict, i))

		return accounts
	except Exception as e:
		print(f'ERROR: Account configuration format is incorrect: {e}')
		return None
