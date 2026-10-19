#!/usr/bin/env python3
"""
AnyRouter.top 自动签到脚本
"""

import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

if hasattr(sys.stdout, 'reconfigure'):
	sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, 'reconfigure'):
	sys.stderr.reconfigure(line_buffering=True)

import httpx
from cloakbrowser import launch_async
from dotenv import load_dotenv

from utils.browser import (
	BrowserLoginResult,
	has_session_cookie,
	is_logged_in,
	launch_browser_with_retry,
	launch_login_context,
	load_browser_login_settings,
	login_with_email_form,
	navigate_login_page,
	prepare_browser_page,
	save_login_screenshot,
	take_pending_screenshots,
	verify_browser_login,
	wait_for_waf_ready,
)
from utils.config import AccountConfig, AppConfig, load_accounts_config
from utils.debug import debug_print, is_debug_enabled
from utils.mlgb import MlgbError
from utils.mlgb import run_check_in as mlgb_check_in
from utils.notify import notify
from utils.proxy import get_playwright_proxy, get_proxy_server

load_dotenv()

BALANCE_HASH_FILE = 'balance_hash.txt'
# 跨运行的签到状态：记录每个账号上一次观测到的「总量」（余额 + 累计消耗）。
# 单次运行的签到窗口只有几秒，抓不到在两次运行之间到账的奖励 —— 实测 agentrouter
# 的 $25 就是这么漏掉的。跨日比较总量才能看出「今天到底到账了没有」。
CHECKIN_STATE_FILE = 'checkin_state.json'
CHECKIN_STATE_VERSION = 1
DEFAULT_TZ_OFFSET_HOURS = 8

# 签到结果状态。
# 之前通知里只看余额差，导致「真的调用了签到接口」和「今天已经签过」都渲染成同一句
# 「今日已签到，无变化」，从消息里根本看不出签到到底成功没有。现在显式区分。
#
# STATUS_LOGGED_IN 的语义与另外两个不同：agentrouter 没有签到接口（sign_in_path 为
# None），而且是「登录即到账」—— 不存在独立的签到动作，也没有任何服务端回执。
# 因此它只说「已登录」，不声称「签到成功」。
STATUS_CHECKED_IN = 'checked_in'
STATUS_ALREADY_CHECKED = 'already_checked'
STATUS_LOGGED_IN = 'logged_in'
STATUS_FAILED = 'failed'

# STATUS_SKIPPED_TODAY 也不来自平台：它是本地状态机判出来的——今天早些时候已经
# 成功签过这个账号，本轮连登录请求都不发。与 ALREADY_CHECKED 的区别是证据来源：
# 那个是平台回执「今天签过了」，这个是「我们自己今天签成功过，别再去打扰它」。
STATUS_SKIPPED_TODAY = 'skipped_today'

# 精简版标签：通知改成一行一个账号，标签必须短。
CHECK_IN_STATUS_LABELS = {
	STATUS_CHECKED_IN: '✅ 签到成功',
	STATUS_ALREADY_CHECKED: '✅ 今日已签到',
	STATUS_LOGGED_IN: '✅ 已登录',
	STATUS_SKIPPED_TODAY: '⏭️ 今日已签到',
	STATUS_FAILED: '❌ 签到失败',
}


@dataclass
class CheckInOutcome:
	"""单个账号的签到结果。

	success 表示「今天该账号的签到流程已完成」，因此 already_checked / logged_in
	也算成功。但三者的证据强度不同：checked_in / already_checked 来自服务端回执，
	logged_in 只表示"登录成功"—— agentrouter 是登录即到账，没有独立的签到动作。

	message 用于在通知里补充失败原因等说明。
	note    用于成功时给出一句结论（mlgb7 这类字段完全不同的平台用它替代
	        "总量/余额"），例如「+125 积分 · 累计签到 31 天 · 余额 4227」。
	credited 表示"今天确认有到账"，供标题统计使用；非 newapi 平台自己给出，
	        newapi 平台由日增量推导。
	"""

	status: str
	message: str = ''
	note: str = ''
	credited: bool = False

	@property
	def success(self) -> bool:
		return self.status != STATUS_FAILED

	@property
	def label(self) -> str:
		return CHECK_IN_STATUS_LABELS.get(self.status, self.status)


def load_balance_hash():
	"""加载余额hash"""
	try:
		if os.path.exists(BALANCE_HASH_FILE):
			with open(BALANCE_HASH_FILE, 'r', encoding='utf-8') as f:
				return f.read().strip()
	except Exception:  # nosec B110
		pass
	return None


def save_balance_hash(balance_hash):
	"""保存余额hash"""
	try:
		with open(BALANCE_HASH_FILE, 'w', encoding='utf-8') as f:
			f.write(balance_hash)
	except Exception as e:
		print(f'Warning: Failed to save balance hash: {e}')


def generate_balance_hash(balances):
	"""生成余额数据的hash"""
	simple_balances = (
		{k: {'quota': v.get('quota'), 'used': v.get('used')} for k, v in balances.items()} if balances else {}
	)
	balance_json = json.dumps(simple_balances, sort_keys=True, separators=(',', ':'))
	return hashlib.sha256(balance_json.encode('utf-8')).hexdigest()[:16]


def tz_offset_hours() -> int:
	"""签到日所在时区的偏移（小时），默认 UTC+8。"""
	raw = os.getenv('CHECKIN_TZ_OFFSET', str(DEFAULT_TZ_OFFSET_HOURS)).strip()
	try:
		return int(raw)
	except ValueError:
		print(f'Warning: invalid CHECKIN_TZ_OFFSET={raw!r}, falling back to {DEFAULT_TZ_OFFSET_HOURS}')
		return DEFAULT_TZ_OFFSET_HOURS


def tz_label() -> str:
	"""时区标签，用于通知和日志，避免把 UTC 误读成本地时间。"""
	offset = tz_offset_hours()
	return '北京时间' if offset == DEFAULT_TZ_OFFSET_HOURS else f'UTC{offset:+d}'


def local_tz() -> timezone:
	"""签到日所在时区（默认 UTC+8）。"""
	return timezone(timedelta(hours=tz_offset_hours()))


def local_now() -> datetime:
	"""签到日所在时区的当前时间（默认北京时间），而不是 runner 的 UTC 时间。

	用真正的 timezone 对象而不是"UTC 加几小时"，否则会得到一个自称 UTC、
	实际是 UTC+8 的 aware datetime。
	"""
	return datetime.now(local_tz())


def current_day() -> str:
	"""当前「签到日」（YYYY-MM-DD），以北京时间 0 点为界。

	两个平台的赠送时间都锚在北京时间上（agentrouter 每天 00:01 到账、
	anyrouter 每天 08:05 之后登录才赠送），所以用北京 0 点切日最自然：
	"前一天收尾总量"就是当天该拿多少的比较基准。
	可用 CHECKIN_TZ_OFFSET 覆盖。
	"""
	return local_now().strftime('%Y-%m-%d')


def load_checkin_state() -> dict:
	"""读取跨运行的账号状态 {账号名: {day, baseline_total, baseline_day, last_total}}。"""
	try:
		if os.path.exists(CHECKIN_STATE_FILE):
			with open(CHECKIN_STATE_FILE, 'r', encoding='utf-8') as f:
				data = json.load(f)
			if isinstance(data, dict):
				accounts = data.get('accounts')
				if isinstance(accounts, dict):
					return accounts
			print(f'Warning: {CHECKIN_STATE_FILE} format unexpected, starting fresh')
	except Exception as e:
		print(f'Warning: Failed to load check-in state: {e}')
	return {}


def save_checkin_state(accounts_state: dict) -> None:
	"""保存跨运行状态。失败不影响签到本身，只影响下次的日级比较。"""
	try:
		payload = {'version': CHECKIN_STATE_VERSION, 'accounts': accounts_state}
		with open(CHECKIN_STATE_FILE, 'w', encoding='utf-8') as f:
			json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
	except Exception as e:
		print(f'Warning: Failed to save check-in state: {e}')


def resolve_baseline(stored: dict | None, today: str) -> tuple[float | None, str | None]:
	"""返回今日总量比较基准 (baseline_total, baseline_day)。

	- 同一天内沿用已建立的基准，否则会被后续运行冲掉，"今日累计"就没了
	- 跨天时用上一次观测值（即前一天的收尾总量）作为新基准
	"""
	if not stored:
		return None, None

	if stored.get('day') == today:
		return stored.get('baseline_total'), stored.get('baseline_day')

	return stored.get('last_total'), stored.get('day')


def compute_day_gain(total: float | None, baseline_total: float | None) -> float | None:
	"""今日总量增量；没有基准时返回 None（首次运行/状态丢失）。"""
	if total is None or baseline_total is None:
		return None
	return round(total - baseline_total, 2)


def parse_cookies(cookies_data):
	"""解析 cookies 数据"""
	if isinstance(cookies_data, dict):
		return cookies_data

	if isinstance(cookies_data, str):
		cookies_dict = {}
		for cookie in cookies_data.split(';'):
			if '=' in cookie:
				key, value = cookie.strip().split('=', 1)
				cookies_dict[key] = value
		return cookies_dict
	return {}


async def get_waf_cookies_with_browser(
	account_name: str,
	login_url: str,
	required_cookies: list[str],
	*,
	use_proxy: bool = False,
):
	"""使用浏览器获取 WAF cookies"""
	print(f'[PROCESSING] {account_name}: Starting browser to get WAF cookies...')

	launch_kwargs: dict = {'headless': True}
	proxy = get_playwright_proxy(use_proxy=use_proxy)
	if proxy:
		launch_kwargs['proxy'] = proxy
	browser = await launch_browser_with_retry(launch_async, **launch_kwargs)

	try:
		page = await browser.new_page()
		await prepare_browser_page(page)
		print(f'[PROCESSING] {account_name}: Access login page to get initial cookies...')

		await page.goto(login_url, wait_until='domcontentloaded')
		await wait_for_waf_ready(page)

		cookies = await page.context.cookies()

		waf_cookies = {}
		for cookie in cookies:
			cookie_name = cookie.get('name')
			cookie_value = cookie.get('value')
			if cookie_name in required_cookies and cookie_value is not None:
				waf_cookies[cookie_name] = cookie_value

		print(f'[INFO] {account_name}: Got {len(waf_cookies)} WAF cookies')

		missing_cookies = [c for c in required_cookies if c not in waf_cookies]

		if missing_cookies:
			print(f'[FAILED] {account_name}: Missing WAF cookies: {missing_cookies}')
			await browser.close()
			return None

		print(f'[SUCCESS] {account_name}: Successfully got all WAF cookies')
		await browser.close()
		return waf_cookies

	except Exception as e:
		print(f'[FAILED] {account_name}: Error occurred while getting WAF cookies: {e}')
		await browser.close()
		return None


async def login_with_credentials(
	account_name: str,
	provider_config,
	provider_name: str,
	email: str,
	password: str,
) -> BrowserLoginResult | None:
	"""使用邮箱密码通过浏览器登录，返回 cookies 与拦截到的 api user id。"""
	print(f'[PROCESSING] {account_name}: Logging in with email/password...')

	login_url = f'{provider_config.domain}{provider_config.login_path}'
	settings = load_browser_login_settings(
		account_name,
		provider_name,
		persist_profile=provider_config.persist_profile,
	)
	timeout_ms = settings.wait_timeout_ms

	debug_print(
		f'[INFO] {account_name}: Browser profile={settings.profile_dir}, '
		f'persist={settings.persist_profile}, headless={settings.headless}, '
		f'humanize={settings.humanize}, timeout={timeout_ms}ms'
	)

	print(
		f'[INFO] {account_name}: Provider proxy={"enabled" if provider_config.use_proxy else "disabled"} '
		f'({provider_name})'
	)

	try:
		context = await launch_login_context(settings, use_proxy=provider_config.use_proxy)
	except Exception as e:
		print(f'[FAILED] {account_name}: Browser launch failed: {e}')
		return None

	page = None
	try:
		page = await context.new_page()
		await prepare_browser_page(page)
		await navigate_login_page(
			page,
			login_url,
			timeout_ms,
			provider=provider_name,
			account_name=account_name,
		)

		if not await is_logged_in(page):
			if await has_session_cookie(page):
				print(f'[WARN] {account_name}: Stale session cookie on login page, forcing email login')
			await save_login_screenshot(page, provider_name, account_name, 'before-email-login')
			await login_with_email_form(
				page,
				email,
				password,
				timeout_ms,
				provider=provider_name,
				account_name=account_name,
			)
		else:
			print(f'[INFO] {account_name}: Browser profile already logged in')

		console_url = f'{provider_config.domain}/console'
		user_profile = await verify_browser_login(page, console_url, timeout_ms)
		if not user_profile:
			cookies = await context.cookies()
			cookie_names = [c.get('name') for c in cookies if c.get('name')]
			print(f'[FAILED] {account_name}: Login failed - /api/user/self not verified')
			debug_print(f'[INFO] {account_name}: Current URL: {page.url}')
			debug_print(f'[INFO] {account_name}: Got cookies: {cookie_names}')
			await save_login_screenshot(page, provider_name, account_name, 'not-authenticated')
			await context.close()
			return None

		cookies = await context.cookies()
		all_cookies = {
			cookie.get('name'): cookie.get('value') for cookie in cookies if cookie.get('name') and cookie.get('value')
		}
		api_user = str(user_profile['id']) if user_profile.get('id') is not None else None

		success_msg = f'[SUCCESS] {account_name}: Login successful, got {len(all_cookies)} cookies'
		if is_debug_enabled() and api_user:
			success_msg += f', api_user={api_user}'
		print(success_msg)
		await context.close()
		return BrowserLoginResult(cookies=all_cookies, api_user=api_user)

	except Exception as e:
		print(f'[FAILED] {account_name}: Error during login: {e}')
		if page is not None:
			await save_login_screenshot(page, provider_name, account_name, 'login-error')
		await context.close()
		return None


def get_user_info(client, headers, user_info_url: str):
	"""获取用户信息"""
	try:
		response = client.get(user_info_url, headers=headers, timeout=30)

		if response.status_code == 200:
			data = response.json()
			if data.get('success'):
				user_data = data.get('data', {})
				quota = round(user_data.get('quota', 0) / 500000, 2)
				used_quota = round(user_data.get('used_quota', 0) / 500000, 2)

				# NewAPI 会在 /api/user/self 里给出账号所属分组（不同分组计费/可用模型不同），
				# 顺便读出来。字段缺失时把可用字段名打出来，方便排查不同部署的差异。
				group = user_data.get('group')
				display = f':money: Current balance: ${quota}, Used: ${used_quota}'
				if group:
					display += f' | group: {group}'
				else:
					print(f'[WARN] /api/user/self 没有 group 字段，可用字段: {sorted(user_data.keys())}')

				return {
					'success': True,
					'quota': quota,
					'used_quota': used_quota,
					'group': group,
					'display': display,
				}
		return {'success': False, 'error': f'Failed to get user info: HTTP {response.status_code}'}
	except Exception as e:
		return {'success': False, 'error': f'Failed to get user info: {str(e)[:50]}...'}


async def prepare_cookies(account_name: str, provider_config, user_cookies: dict) -> dict | None:
	"""准备请求所需的 cookies（可能包含 WAF cookies）"""
	waf_cookies = {}

	if provider_config.needs_waf_cookies():
		login_url = f'{provider_config.domain}{provider_config.login_path}'
		waf_cookies = await get_waf_cookies_with_browser(
			account_name,
			login_url,
			provider_config.waf_cookie_names,
			use_proxy=provider_config.use_proxy,
		)
		if not waf_cookies:
			print(f'[FAILED] {account_name}: Unable to get WAF cookies')
			return None
	else:
		print(f'[INFO] {account_name}: Bypass WAF not required, using user cookies directly')

	return {**waf_cookies, **user_cookies}


def execute_check_in(client, account_name: str, provider_config, headers: dict) -> CheckInOutcome:
	"""执行签到请求，返回区分「签到成功」与「今日已签到」的结果。"""
	print(f'[NETWORK] {account_name}: Executing check-in')

	checkin_headers = headers.copy()
	checkin_headers.update({'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})

	sign_in_url = f'{provider_config.domain}{provider_config.sign_in_path}'
	response = client.post(sign_in_url, headers=checkin_headers, timeout=30)

	print(f'[RESPONSE] {account_name}: Response status code {response.status_code}')

	if response.status_code != 200:
		print(f'[FAILED] {account_name}: Check-in failed - HTTP {response.status_code}')
		return CheckInOutcome(STATUS_FAILED, f'HTTP {response.status_code}')

	try:
		result = response.json()
	except json.JSONDecodeError:
		if 'success' in response.text.lower():
			print(f'[SUCCESS] {account_name}: Check-in successful!')
			return CheckInOutcome(STATUS_CHECKED_IN)
		print(f'[FAILED] {account_name}: Check-in failed - Invalid response format')
		return CheckInOutcome(STATUS_FAILED, '响应不是有效 JSON')

	if result.get('ret') == 1 or result.get('code') == 0 or result.get('success'):
		print(f'[SUCCESS] {account_name}: Check-in successful!')
		return CheckInOutcome(STATUS_CHECKED_IN)

	error_msg = result.get('msg', result.get('message', 'Unknown error'))
	already_checked_keywords = ['已经签到', '已签到', '重复签到', 'already checked', 'already signed']
	if any(keyword in error_msg.lower() for keyword in already_checked_keywords):
		print(f'[SUCCESS] {account_name}: Already checked in today')
		return CheckInOutcome(STATUS_ALREADY_CHECKED)

	print(f'[FAILED] {account_name}: Check-in failed - {error_msg}')
	return CheckInOutcome(STATUS_FAILED, str(error_msg))


def short_day(day: str | None) -> str:
	"""'2026-09-23' -> '09-23'，用于在增量后面紧凑地标出基准日。"""
	if not day:
		return ''
	return day[5:] if len(day) >= 10 else day


def format_delta(day_gain: float | None, baseline_day: str | None = None) -> str:
	"""额度有没有变化 —— 这一行才回答"签到到底成没成"。

	+$25.00(较09-24) / 未到账(较09-24) / -$100.00(较09-24) / 基线待建立
	"""
	if day_gain is None:
		return '基线待建立'

	if day_gain > 0:
		text = f'+${day_gain:.2f}'
	elif day_gain < 0:
		text = f'-${abs(day_gain):.2f}'
	else:
		text = '未到账'

	day_label = short_day(baseline_day)
	return f'{text}(较{day_label})' if day_label else text


def format_account_line(
	account_name: str,
	outcome: CheckInOutcome,
	*,
	total: float | None = None,
	balance: float | None = None,
	day_gain: float | None = None,
	baseline_day: str | None = None,
) -> str:
	"""一行一个账号，主角是「额度有没有变化」。

	成功时不显示登录状态 —— 那不重要，还会和「未到账」并排造成误读
	（曾经出现 ✅ 已登录 + $0.00 同时出现，看起来自相矛盾）。
	只有失败时才显示状态和原因。
	"""
	if not outcome.success:
		return f'{account_name} · {outcome.label} · {outcome.message or "未知原因"}'

	# 字段完全不同的平台（mlgb7）自带一句结论，不走"总量/余额"那套
	if outcome.note:
		return f'{account_name} · {outcome.label} · {outcome.note}'

	if total is None:
		return f'{account_name} · 额度读取失败'

	parts = [account_name, format_delta(day_gain, baseline_day)]
	if balance is not None:
		parts.append(f'余额 ${balance:.2f}')
	parts.append(f'总量 ${total:.2f}')

	return ' · '.join(parts)


def run_mlgb_account(
	account_name: str, account: AccountConfig, provider_config
) -> tuple[CheckInOutcome, dict | None, dict | None]:
	"""噜皮生图（mlgb7）签到。

	该站登录走 Linux DO 授权、没有站内密码，所以只能复用会话 cookie。
	它自己有权威的签到状态（`checked_in_today`），因此不需要日增量推断；
	到账金额直接取自积分明细里的 `daily_checkin` 记录。
	"""
	user_cookies = parse_cookies(account.cookies)
	if not user_cookies:
		print(f'[FAILED] {account_name}: 缺少会话 cookies')
		return CheckInOutcome(STATUS_FAILED, '缺少会话 cookies'), None, None

	proxy_url = get_proxy_server(use_proxy=provider_config.use_proxy)
	print(f'[NETWORK] {account_name}: Querying {provider_config.domain}/api/me')

	try:
		result = mlgb_check_in(provider_config.domain, user_cookies, proxy_url=proxy_url)
	except MlgbError as exc:
		print(f'[FAILED] {account_name}: {exc}')
		return CheckInOutcome(STATUS_FAILED, str(exc)), None, None

	if not result.checked_in:
		print(f'[FAILED] {account_name}: Still not checked in after the request')
		return CheckInOutcome(STATUS_FAILED, '调用签到后服务端仍未显示已签到'), None, None

	status = STATUS_CHECKED_IN if result.awarded else STATUS_ALREADY_CHECKED
	award = result.today_award if result.today_award is not None else result.reward

	parts = []
	if award and award > 0:
		parts.append(f'+{award:.0f} 积分')
	if result.checkin_days:
		parts.append(f'累计签到 {result.checkin_days} 天')
	parts.append(f'余额 {result.points:.0f}')

	if result.awarded:
		print(f'[SUCCESS] {account_name}: Check-in successful (+{award:.0f} 积分)')
	else:
		print(f'[SUCCESS] {account_name}: Already checked in today')

	return (
		CheckInOutcome(status, note=' · '.join(parts), credited=bool(award and award > 0)),
		None,
		None,
	)


async def check_in_account(
	account: AccountConfig, account_index: int, app_config: AppConfig
) -> tuple[CheckInOutcome, dict | None, dict | None]:
	"""为单个账号执行签到操作"""
	account_name = account.get_display_name(account_index)
	print(f'\n[PROCESSING] Starting to process {account_name}')

	provider_config = app_config.get_provider(account.provider)
	if not provider_config:
		print(f'[FAILED] {account_name}: Provider "{account.provider}" not found in configuration')
		return CheckInOutcome(STATUS_FAILED, f'provider "{account.provider}" 未配置'), None, None

	print(f'[INFO] {account_name}: Using provider "{account.provider}" ({provider_config.domain})')

	# 非 NewAPI 平台走各自的适配器
	if provider_config.adapter == 'mlgb7':
		return run_mlgb_account(account_name, account, provider_config)

	# 邮箱密码优先
	all_cookies = None
	resolved_api_user: str | None = None
	auth_method = None
	if account.has_login_credentials():
		print(f'[INFO] {account_name}: Attempting email/password login (priority)...')
		assert account.email is not None and account.password is not None
		login_result = await login_with_credentials(
			account_name,
			provider_config,
			account.provider,
			account.email,
			account.password,
		)
		if login_result:
			all_cookies = login_result.cookies
			resolved_api_user = login_result.api_user
			auth_method = 'email/password'
		else:
			print(f'[FAILED] {account_name}: Email/password login failed, will not use stale session cookies')
			return CheckInOutcome(STATUS_FAILED, '邮箱密码登录失败'), None, None
	else:
		user_cookies = parse_cookies(account.cookies)
		if not user_cookies:
			print(f'[FAILED] {account_name}: Invalid configuration format')
			return CheckInOutcome(STATUS_FAILED, '账号缺少可用的 cookies 或邮箱密码'), None, None
		all_cookies = await prepare_cookies(account_name, provider_config, user_cookies)
		auth_method = 'session cookies'

	if not all_cookies:
		return CheckInOutcome(STATUS_FAILED, '未能获取到可用 cookies'), None, None

	print(f'[AUTH] {account_name}: Using auth method -> {auth_method}')

	return run_check_in_requests(
		all_cookies,
		account,
		account_name,
		provider_config,
		api_user_override=resolved_api_user,
		use_proxy=provider_config.use_proxy,
	)


def run_check_in_requests(
	all_cookies: dict,
	account: AccountConfig,
	account_name: str,
	provider_config,
	*,
	api_user_override: str | None = None,
	use_proxy: bool = False,
) -> tuple[CheckInOutcome, dict | None, dict | None]:
	"""执行 HTTP 签到请求（同步，避免在 async 上下文中使用阻塞 httpx）。"""
	try:
		client_kwargs: dict = {'http2': True, 'timeout': 30.0}
		proxy_url = get_proxy_server(use_proxy=use_proxy)
		if proxy_url:
			client_kwargs['proxy'] = proxy_url
			if is_debug_enabled():
				print(f'[INFO] {account_name}: HTTP client proxy enabled: {proxy_url}')
			else:
				print(f'[INFO] {account_name}: HTTP client proxy enabled')
		elif use_proxy:
			print(f'[WARN] {account_name}: Provider requires proxy but CHECKIN_PROXY_URL is not set')

		with httpx.Client(**client_kwargs) as client:
			client.cookies.update(all_cookies)

			headers = {
				'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
				'Accept': 'application/json, text/plain, */*',
				'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
				'Accept-Encoding': 'gzip, deflate, br, zstd',
				'Referer': provider_config.domain,
				'Origin': provider_config.domain,
				'Connection': 'keep-alive',
				'Sec-Fetch-Dest': 'empty',
				'Sec-Fetch-Mode': 'cors',
				'Sec-Fetch-Site': 'same-origin',
			}

			api_user = api_user_override or account.api_user
			if api_user:
				headers[provider_config.api_user_key] = api_user

			user_info_url = f'{provider_config.domain}{provider_config.user_info_path}'
			user_info_before = get_user_info(client, headers, user_info_url)
			if user_info_before and user_info_before.get('success'):
				print(user_info_before['display'])
			elif user_info_before:
				print(user_info_before.get('error', 'Unknown error'))

			if provider_config.needs_manual_check_in():
				outcome = execute_check_in(client, account_name, provider_config, headers)
				user_info_after = get_user_info(client, headers, user_info_url)
				return outcome, user_info_before, user_info_after

			user_info_after = get_user_info(client, headers, user_info_url)
			if user_info_after and user_info_after.get('success'):
				print(f'[INFO] {account_name}: Logged in and user info fetched (this provider credits on login)')
				return CheckInOutcome(STATUS_LOGGED_IN), user_info_before, user_info_after
			error = user_info_after.get('error', 'Unknown error') if user_info_after else 'Unknown error'
			print(f'[FAILED] {account_name}: Login/user-info failed - {error}')
			return CheckInOutcome(STATUS_FAILED, str(error)), user_info_before, user_info_after

	except Exception as e:
		print(f'[FAILED] {account_name}: Error occurred during check-in process - {str(e)[:50]}...')
		return CheckInOutcome(STATUS_FAILED, str(e)[:50]), None, None


def filter_accounts_by_provider(
	accounts: list[AccountConfig], raw_filter: str | None
) -> tuple[list[AccountConfig], list[AccountConfig]]:
	"""按 CHECKIN_PROVIDERS 收窄本次要处理的账号。

	留空 = 不限制（定时任务默认行为，一次覆盖全部账号，当作兜底）。
	设置后只处理 provider 命中的账号，用来实现「00:01 只签 agentrouter + mlgb7、
	08:01 只签 anyrouter」这类分组 —— 配合外部定时器触发 workflow_dispatch。

	返回 (待处理, 被跳过)。
	"""
	names = {item.strip().lower() for item in (raw_filter or '').split(',') if item.strip()}
	if not names:
		return accounts, []

	kept = [account for account in accounts if account.provider.lower() in names]
	skipped = [account for account in accounts if account.provider.lower() not in names]
	return kept, skipped


def force_checkin_enabled() -> bool:
	"""是否强制忽略「今天已签过」直接重跑（CHECKIN_FORCE=1）。

	平时不需要开：GitHub 的 schedule 槽位经常延迟落地，同一批账号一天里会被
	触发两三次，重复签到不但没有收益，还会撞上站点的二次登录限流
	（10-17 00:32 那轮就是把 00:01 刚签成功的 agLD/agGithub 又登了一遍，
	两张号全被挡成「邮箱密码登录失败」）。只在排查时需要时手动强制。
	"""
	return os.getenv('CHECKIN_FORCE', '').strip().lower() in ('1', 'true', 'yes')


def processed_today(state: dict, account: AccountConfig, today: str) -> bool:
	"""该账号今天是否已经成功处理过（用于过滤后的补签判断）。

	state 以账号显示名为键；没有显式 name 的账号定位不到，保守地当作"没处理过"——
	宁可多跑一次，也别漏签。
	"""
	if not account.name:
		return False
	return (state.get(account.name) or {}).get('day') == today


def already_done_this_run(state: dict, account: AccountConfig, today: str, *, relogin_allowed: bool = False) -> bool:
	"""本轮该不该跳过这个账号：今天已经**确认到账**过，且没开强制重跑。

	判据是 credited_day 而不是 day，这点很关键：anyrouter 在北京 08:05 之后登录才
	赠送，凌晨那次登录只会拿回「未到账」。若按「今天跑过」跳过，早晨那轮会把它的
	额度整天空掉。失败、未到账都不写 credited_day，所以后续轮次仍会重试。
	"""
	if relogin_allowed:
		return False
	if not account.name:
		return False
	return (state.get(account.name) or {}).get('credited_day') == today


def split_stale_accounts(
	state: dict, skipped: list[AccountConfig], today: str
) -> tuple[list[AccountConfig], list[AccountConfig]]:
	"""把被 provider 过滤掉的账号分成「今天还没处理（要补签）」和「今天已处理」。

	返回 (要补签的, 可以跳过的)。
	"""
	carried = [account for account in skipped if not processed_today(state, account, today)]
	carried_ids = {id(account) for account in carried}
	remaining = [account for account in skipped if id(account) not in carried_ids]
	return carried, remaining


def detect_group_change(account_name: str, previous_group: str | None, current_group: str | None) -> str | None:
	"""分组变化时给出一句提示，否则返回 None。

	分组平时是静态的，所以不占用通知版面；但它一旦变了（可用模型、计费倍率都会
	跟着变），那才是值得单独知道的事。拿不到分组（比如 mlgb7 这类平台）时不报。
	"""
	if not previous_group or not current_group or previous_group == current_group:
		return None
	return f'{account_name} {previous_group} → {current_group}'


async def main():
	"""主函数"""
	if is_debug_enabled():
		print('[INFO] DEBUG_MODE enabled')
		proxy_server = os.getenv('CHECKIN_PROXY_URL', '').strip()
		if proxy_server:
			print(f'[INFO] Proxy endpoint available: {proxy_server} (enabled per provider use_proxy)')
		else:
			print('[INFO] CHECKIN_PROXY_URL not set; providers with use_proxy=true will run without proxy')
	else:
		print('[INFO] Debug mode disabled (set DEBUG_MODE=true to enable screenshots and verbose logs)')

	print('[SYSTEM] AnyRouter.top multi-account auto check-in script started')
	print(
		f'[TIME] Execution time: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")} UTC'
		f' / {local_now().strftime("%Y-%m-%d %H:%M:%S")} {tz_label()}'
	)

	app_config = AppConfig.load_from_env()
	print(f'[INFO] Loaded {len(app_config.providers)} provider configuration(s)')
	if is_debug_enabled():
		for provider_name, provider in sorted(app_config.providers.items()):
			print(f'[INFO] Provider "{provider_name}": use_proxy={provider.use_proxy}')

	accounts = load_accounts_config()
	if not accounts:
		error_msg = '[FAILED] Unable to load account configuration, program exits'
		print(error_msg)
		notify.push_message('AnyRouter Check-in Alert', error_msg, msg_type='text')
		sys.exit(1)

	print(f'[INFO] Found {len(accounts)} account configurations')

	last_balance_hash = load_balance_hash()
	checkin_state = load_checkin_state()
	today = current_day()
	print(f'[INFO] Sign-in day: {today}, state loaded for {len(checkin_state)} account(s)')

	# 按 provider 收窄本次范围；但被跳过的账号里「今天还没成功处理过的」仍然补上。
	# 否则某个槽位被 GitHub 丢弃时（实测经常发生），那批账号会整整一天不签到 ——
	# 比如 10-13 傍晚那次被丢，ag+mlgb7 的 00:01 签到就整天空了。
	accounts, skipped_accounts = filter_accounts_by_provider(accounts, os.getenv('CHECKIN_PROVIDERS'))
	if skipped_accounts:
		carried, skipped_accounts = split_stale_accounts(checkin_state, skipped_accounts, today)
		if carried:
			for account in carried:
				print(f'[CATCH-UP] {account.name or account.provider}: 今天还没签到过，本次一并处理')
			accounts = accounts + carried

	for skipped in skipped_accounts:
		print(f'[SKIP] {skipped.name or skipped.provider}: provider "{skipped.provider}" 不在本次范围内')
	if not accounts:
		print('[FAILED] CHECKIN_PROVIDERS 过滤后没有待处理的账号，程序退出')
		sys.exit(1)
	if skipped_accounts:
		print(f'[INFO] Processing {len(accounts)} account(s), skipped {len(skipped_accounts)}')

	success_count = 0
	total_count = len(accounts)
	gained_count = 0
	credit_known = 0
	status_lines: list[str] = []
	group_changes: list[str] = []
	current_balances = {}
	need_notify = False
	# 今天已确认到账的账号不再重跑：schedule 槽位延迟落地会让同一批账号一天被触发两三次，
	# 重复签到没有收益，还会被平台按二次登录挡掉（详见 force_checkin_enabled 注释）。
	relogin_allowed = force_checkin_enabled()
	skipped_today: list[str] = []
	if relogin_allowed:
		print('[INFO] CHECKIN_FORCE 已开启：忽略「今天已到账」，全部账号重跑')

	for i, account in enumerate(accounts):
		account_key = f'account_{i + 1}'
		account_name = account.get_display_name(i)
		stored_state = checkin_state.get(account_name)
		baseline_total, baseline_day = resolve_baseline(stored_state, today)
		previous_group = (stored_state or {}).get('group')

		if already_done_this_run(checkin_state, account, today, relogin_allowed=relogin_allowed):
			skipped_today.append(account_name)
			status_lines.append(
				f'{account_name} · {CHECK_IN_STATUS_LABELS[STATUS_SKIPPED_TODAY]} · 本轮跳过，不重复请求'
			)
			print(f'[SKIP] {account_name}: already credited today, skipping this run')
			continue

		try:
			outcome, user_info_before, user_info_after = await check_in_account(account, i, app_config)
			if outcome.success:
				success_count += 1
				# 记下"今天已处理"，供过滤后的补签判断使用。
				# 失败时**不写**，这样下次运行仍会被补签捞起来重试。
				checkin_state.setdefault(account_name, {})['day'] = today

			# 跨运行的总量比较：抓单次运行窗口之外的到账。
			# 总量 = 余额 + 累计消耗，消费时两者一增一减恰好抵消，所以它不受日常消耗干扰。
			total_after = None
			balance_after = None
			day_gain = None
			if user_info_after and user_info_after.get('success'):
				balance_after = user_info_after['quota']
				total_after = round(balance_after + user_info_after['used_quota'], 2)
				day_gain = compute_day_gain(total_after, baseline_total)
				current_balances[account_key] = {
					'quota': user_info_after['quota'],
					'used': user_info_after['used_quota'],
				}

				# 分组平时不变，所以不占用通知版面；一旦变了（可用模型/计费都会跟着变）
				# 才单独提示一句。
				group = user_info_after.get('group') or previous_group
				change = detect_group_change(account_name, previous_group, group)
				if change:
					group_changes.append(change)
					print(f'[WARN] {account_name}: group changed {previous_group} -> {group}')
					need_notify = True

				checkin_state[account_name] = {
					'day': today,
					'baseline_total': baseline_total,
					'baseline_day': baseline_day,
					'last_total': total_after,
					'group': group,
				}
			elif user_info_before and user_info_before.get('success'):
				checkin_state[account_name] = {
					'day': today,
					'baseline_total': baseline_total,
					'baseline_day': baseline_day,
					'last_total': round(user_info_before['quota'] + user_info_before['used_quota'], 2),
					'group': user_info_before.get('group') or previous_group,
				}

			# 「确认到账」才是真正的完成态，也是后面几轮是否跳过它的唯一依据。
			# 登录成功但没到账（anyrouter 北京 08:05 前登录就是这种）不算完成。
			if outcome.success and (outcome.credited or (day_gain is not None and day_gain > 0)):
				checkin_state.setdefault(account_name, {})['credited_day'] = today

			status_lines.append(
				format_account_line(
					account_name,
					outcome,
					total=total_after,
					balance=balance_after,
					day_gain=day_gain,
					baseline_day=baseline_day,
				)
			)

			if not outcome.success:
				need_notify = True
				print(f'[NOTIFY] {account_name} failed, will send notification')
			if day_gain is not None:
				credit_known += 1
				if day_gain > 0:
					gained_count += 1
				print(f'[INFO] {account_name}: total ${total_after:.2f}, day gain {day_gain:+.2f} vs {baseline_day}')
			elif outcome.credited:
				# 自带到账状态的平台（mlgb7）由它自己给出，不靠日增量推断
				credit_known += 1
				gained_count += 1

		except Exception as e:
			print(f'[FAILED] {account_name} processing exception: {e}')
			need_notify = True
			status_lines.append(f'{account_name} · ❌ 执行异常 · {str(e)[:50]}...')

	save_checkin_state(checkin_state)

	# 跳过的账号不进「到账 x/y」的分母：这轮根本没去查它们，算进去只会被读成「没到账」。
	total_count = len(accounts) - len(skipped_today)
	if skipped_today:
		print(f'[INFO] {len(skipped_today)} account(s) already done today, skipped: {", ".join(skipped_today)}')

	current_balance_hash = generate_balance_hash(current_balances) if current_balances else None
	if current_balance_hash:
		if last_balance_hash is None:
			need_notify = True
			print('[NOTIFY] First run detected, will send notification with current balances')
		elif current_balance_hash != last_balance_hash:
			need_notify = True
			print('[NOTIFY] Balance changes detected, will send notification')
		else:
			print('[INFO] No balance changes detected')

	if current_balance_hash:
		save_balance_hash(current_balance_hash)

	if need_notify:
		header = f'📊 AnyRouter 签到 · {local_now().strftime("%m-%d %H:%M")} {tz_label()}'
		if credit_known:
			# 标题直接给"几个账号到账了"，而不是"几个账号登录成功了"
			header += f' · 到账 {gained_count}/{total_count}'
		notify_content = '\n\n'.join([header, '\n'.join(status_lines)])
		if group_changes:
			notify_content += '\n\n' + '\n'.join(f'⚠️ 分组变化：{item}' for item in group_changes)

		screenshot_paths = take_pending_screenshots() if is_debug_enabled() else []
		if screenshot_paths:
			github_run_id = os.getenv('GITHUB_RUN_ID', '').strip()
			github_repo = os.getenv('GITHUB_REPOSITORY', '').strip()
			screenshot_hint = f'[SCREENSHOT] {len(screenshot_paths)} debug screenshot(s) saved'
			if github_run_id and github_repo:
				run_url = f'https://github.com/{github_repo}/actions/runs/{github_run_id}'
				screenshot_hint += f'. Download artifact `checkin-screenshots-{github_run_id}` from: {run_url}'
			else:
				screenshot_hint += ' to `checkin_screenshots/`'
			notify_content += f'\n\n{screenshot_hint}'

		print(notify_content)
		notify.push_message('AnyRouter Check-in Alert', notify_content, msg_type='text')
		print('[NOTIFY] Notification sent due to failures or balance changes')
	else:
		print('[INFO] All accounts successful and no balance changes detected, notification skipped')

	# 今天已签过而被跳过的账号也算「这轮没出错」，否则全跳过的兜底轮会被标成失败。
	sys.exit(0 if (success_count + len(skipped_today)) > 0 else 1)


def run_main():
	"""运行主函数的包装函数"""
	try:
		asyncio.run(main())
	except KeyboardInterrupt:
		print('\n[WARNING] Program interrupted by user')
		sys.exit(1)
	except Exception as e:
		print(f'\n[FAILED] Error occurred during program execution: {e}')
		sys.exit(1)


if __name__ == '__main__':
	run_main()
