#!/usr/bin/env python3
"""
AnyRouter.top 自动签到脚本
"""

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from utils.config import AccountConfig, AppConfig, load_accounts_config
from utils.daily_ledger import format_daily_summary, record_balance_run
from utils.notify import notify

load_dotenv()

BALANCE_HASH_FILE = 'balance_hash.txt'
BALANCE_HISTORY_FILE = 'balance_history.json'
BALANCE_DAILY_FILE = 'balance_daily.json'
BEIJING_TIMEZONE = timezone(timedelta(hours=8))


def load_balance_history() -> dict:
	try:
		with open(BALANCE_HISTORY_FILE, encoding='utf-8') as history_file:
			history = json.load(history_file)
		if not isinstance(history, dict):
			return {}
		return {
			key: info
			for key, info in history.items()
			if isinstance(info, dict)
			and info.get('success') is True
			and all(type(info.get(field)) in (int, float) for field in ('quota', 'used_quota'))
		}
	except FileNotFoundError:
		return {}
	except (OSError, ValueError) as exc:
		print(f'[WARNING] Previous balance history is unavailable: {exc}')
		return {}


def save_balance_history(history: dict):
	with open(BALANCE_HISTORY_FILE, 'w', encoding='utf-8') as history_file:
		json.dump(history, history_file, ensure_ascii=False, indent=2)


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
	# 将包含 quota 和 used 的结构转换为简单的 quota 值用于 hash 计算
	simple_balances = {k: v['quota'] for k, v in balances.items()} if balances else {}
	balance_json = json.dumps(simple_balances, sort_keys=True, separators=(',', ':'))
	return hashlib.sha256(balance_json.encode('utf-8')).hexdigest()[:16]


def summarize_response_body(body: str, limit: int = 160) -> str:
	"""将响应体压缩成便于日志定位的短文本"""
	compact_body = ' '.join(body.split())
	if not compact_body:
		return '<empty>'
	return compact_body[:limit] + ('...' if len(compact_body) > limit else '')


async def launch_playwright_context(playwright, user_data_dir: str):
	"""创建带统一指纹配置的 Playwright 上下文"""
	return await playwright.chromium.launch_persistent_context(
		user_data_dir=user_data_dir,
		headless=False,
		user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
		viewport={'width': 1920, 'height': 1080},
		args=[
			'--disable-blink-features=AutomationControlled',
			'--disable-dev-shm-usage',
			'--disable-web-security',
			'--disable-features=VizDisplayCompositor',
			'--no-sandbox',
		],
	)


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


async def add_cookies_to_browser_context(context, domain: str, cookies: dict):
	"""把账号 Cookie（包括 WAF Cookie）完整注入浏览器上下文。

	之前的回退流程只注入了登录 Cookie，排除了 acw_tc 等 WAF Cookie，导致
	AgentRouter 在 HTTP 请求失败后进入浏览器流程时又被拦回滑块验证页。
	"""
	browser_cookies = [
		{
			'name': str(name),
			'value': str(value),
			# Playwright 要求 Cookie 使用 url 或 domain/path 二选一，不能同时传。
			'url': f'{domain.rstrip("/")}/',
		}
		for name, value in cookies.items()
		if value is not None and str(value)
	]
	if browser_cookies:
		await context.add_cookies(browser_cookies)


async def get_waf_cookies_with_playwright(account_name: str, login_url: str, required_cookies: list[str]):
	"""使用 Playwright 获取 WAF cookies（隐私模式）"""
	print(f'[PROCESSING] {account_name}: Starting browser to get WAF cookies...')

	async with async_playwright() as p:
		import tempfile

		with tempfile.TemporaryDirectory() as temp_dir:
			context = await launch_playwright_context(p, temp_dir)

			page = await context.new_page()

			try:
				print(f'[PROCESSING] {account_name}: Access login page to get initial cookies...')

				await page.goto(login_url, wait_until='domcontentloaded')

				try:
					await page.wait_for_function('document.readyState === "complete"', timeout=5000)
				except Exception:
					await page.wait_for_timeout(3000)

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
					await context.close()
					return None

				print(f'[SUCCESS] {account_name}: Successfully got all WAF cookies')

				await context.close()

				return waf_cookies

			except Exception as e:
				print(f'[FAILED] {account_name}: Error occurred while getting WAF cookies: {e}')
				await context.close()
				return None


def parse_user_info_response(status_code: int, response_text: str):
	"""解析用户信息接口响应"""
	if status_code != 200:
		return {'success': False, 'error': f'Failed to get user info: HTTP {status_code}'}

	try:
		data = json.loads(response_text)
	except json.JSONDecodeError:
		body_preview = summarize_response_body(response_text)
		return {'success': False, 'error': f'Failed to parse user info response: {body_preview}'}

	if not isinstance(data, dict):
		return {'success': False, 'error': 'User info response must be a JSON object'}

	if data.get('success') is True:
		user_data = data.get('data')
		if not isinstance(user_data, dict) or any(
			type(user_data.get(key)) not in (int, float) for key in ('quota', 'used_quota')
		):
			return {'success': False, 'error': 'User info response is missing valid balance data'}
		quota = round(user_data['quota'] / 500000, 2)
		used_quota = round(user_data['used_quota'] / 500000, 2)
		user_id = user_data.get('id') or user_data.get('user_id') or user_data.get('userId')
		return {
			'success': True,
			'quota': quota,
			'used_quota': used_quota,
			'quota_raw': user_data['quota'],
			'used_quota_raw': user_data['used_quota'],
			'api_user': str(user_id) if user_id is not None else None,
			'display': f':money: Current balance: ${quota}, Used: ${used_quota}',
		}

	error_msg = data.get('msg', data.get('message', summarize_response_body(response_text)))
	return {'success': False, 'error': f'Failed to get user info: {error_msg}'}


def get_user_info(client, headers, user_info_url: str):
	"""通过 httpx 获取用户信息"""
	try:
		response = client.get(user_info_url, headers=headers, timeout=30)
		return parse_user_info_response(response.status_code, response.text)
	except Exception as e:
		return {'success': False, 'error': f'Failed to get user info: {str(e)[:50]}...'}


async def fetch_user_info_in_browser(page, provider_config, api_user: str | None):
	"""通过浏览器页面导航请求用户信息，确保 WAF 挑战脚本能真正执行"""
	user_info_url = f'{provider_config.domain}{provider_config.user_info_path}'
	headers = {
		'Accept': 'application/json, text/plain, */*',
	}
	if api_user:
		headers[provider_config.api_user_key] = str(api_user)

	await page.context.set_extra_http_headers(headers)

	last_result = {'success': False, 'error': 'Failed to get user info in browser context'}
	for attempt in range(1, 4):
		response = await page.goto(user_info_url, wait_until='domcontentloaded')

		try:
			await page.wait_for_function('document.readyState === "complete"', timeout=5000)
		except Exception:
			await page.wait_for_timeout(2000)

		body_text = await page.evaluate(
			"""() => {
				const bodyText = document.body?.innerText || document.documentElement?.innerText || '';
				return bodyText.trim();
			}"""
		)
		if not body_text:
			body_text = await page.content()

		status_code = response.status if response else 0
		last_result = parse_user_info_response(status_code, body_text)
		if last_result.get('success'):
			return last_result

		if 'aliyun_waf_' not in body_text.lower() or attempt == 3:
			return last_result

		print(f'[INFO] WAF challenge still active, waiting before retry ({attempt}/3)')
		await page.wait_for_timeout(3000)

	return last_result


async def find_visible_element(page, selectors: list[str]):
	"""按候选选择器查找第一个可见元素"""
	for selector in selectors:
		try:
			element = await page.query_selector(selector)
			if element and await element.is_visible():
				return element
		# Optional selectors vary between providers; try the next one if lookup fails.
		except Exception:  # nosec B112
			continue
	return None


async def login_with_credentials_in_browser(
	page,
	provider_config,
	account_name: str,
	username: str,
	password: str,
):
	"""使用站点自己的用户名/密码登录并返回登录响应中的用户 ID。

	AgentRouter 当前登录接口会在登录成功时完成当日签到；因此重新登录
	本身就是 AgentRouter 的签到动作。Turnstile 若被站点打开，则由站点页面
	负责生成 token，无法在无交互的 CI 环境中凭空绕过。
	"""
	print(f'[PROCESSING] {account_name}: Re-authenticating in browser...')

	username_input = await find_visible_element(
		page,
		[
			'input[name="username"]',
			'input[autocomplete="username"]',
			'input[name="email"]',
			'input[type="email"]',
			'input[placeholder*="邮箱"]',
			'input[placeholder*="用户名"]',
			'input[type="text"]',
		],
	)
	password_input = await find_visible_element(
		page,
		[
			'input[name="password"]',
			'input[autocomplete="current-password"]',
			'input[type="password"]',
		],
	)
	if not username_input or not password_input:
		return False, None, 'Login form was not available; the site may still be showing a WAF challenge'

	try:
		await username_input.fill(username)
		await password_input.fill(password)
	except Exception as exc:
		return False, None, f'Unable to fill login form: {str(exc)[:120]}'

	login_button = await find_visible_element(
		page,
		[
			'button[type="submit"]',
			'button:has-text("登录")',
			'button:has-text("Login")',
			'button:has-text("Sign in")',
		],
	)
	if not login_button:
		return False, None, 'Login button was not found'

	try:
		async with page.expect_response(
			lambda response: '/api/user/login' in response.url and response.request.method == 'POST',
			timeout=30000,
		) as response_info:
			await login_button.click()
		response = await response_info.value
		response_text = await response.text()
	except Exception as exc:
		return False, None, f'Login request did not complete: {str(exc)[:120]}'

	if response.status != 200:
		return False, None, f'Login failed with HTTP {response.status}'

	try:
		result = json.loads(response_text)
	except json.JSONDecodeError:
		return False, None, f'Login returned an invalid response: {summarize_response_body(response_text)}'

	if not result.get('success'):
		error_message = result.get('message', result.get('msg', 'Login failed'))
		return False, None, str(error_message)

	user_data = result.get('data') or {}
	user_id = user_data.get('id') or user_data.get('user_id') or user_data.get('userId')
	print(f'[SUCCESS] {account_name}: Browser re-authentication succeeded')
	return True, str(user_id) if user_id is not None else None, None


def parse_check_in_response(account_name: str, status_code: int, response_text: str):
	"""统一解析 HTTP 与浏览器发出的签到响应"""
	print(f'[RESPONSE] {account_name}: Response status code {status_code}')

	if status_code != 200:
		print(f'[FAILED] {account_name}: Check-in failed - HTTP {status_code}')
		return False

	try:
		result = json.loads(response_text)
	except json.JSONDecodeError:
		print(f'[FAILED] {account_name}: Check-in failed - Invalid response format')
		return False

	if not isinstance(result, dict):
		print(f'[FAILED] {account_name}: Check-in response must be a JSON object')
		return False

	error_msg = str(result.get('msg', result.get('message', 'Unknown error')))
	already_checked_keywords = [
		'已经签到',
		'已签到',
		'今日已签到',
		'重复签到',
		'already checked',
		'already signed',
		'already checkin',
	]
	if any(keyword in error_msg.lower() for keyword in already_checked_keywords):
		print(f'[SUCCESS] {account_name}: Already checked in today')
		return True

	# 有 success 字段时以明确的布尔值为准，不能把字符串 "false" 或网页中的
	# "success" 文本当成签到成功，也不能让 code=0 覆盖 success=false。
	if 'success' in result:
		confirmed = result['success'] is True
	elif 'ret' in result:
		confirmed = type(result['ret']) is int and result['ret'] == 1
	else:
		confirmed = type(result.get('code')) is int and result['code'] == 0
	if confirmed:
		print(f'[SUCCESS] {account_name}: Check-in API confirmed completion')
		return True

	print(f'[FAILED] {account_name}: Check-in failed - {error_msg}')
	return False


async def execute_check_in_in_browser(page, account_name: str, provider_config, api_user: str | None):
	"""在已经通过浏览器 WAF/登录态的上下文中执行签到"""
	if not provider_config.sign_in_path:
		return True

	checkin_url = f'{provider_config.domain}{provider_config.sign_in_path}'
	headers = {
		'Accept': 'application/json, text/plain, */*',
		'Content-Type': 'application/json',
		'X-Requested-With': 'XMLHttpRequest',
	}
	if api_user:
		headers[provider_config.api_user_key] = str(api_user)

	try:
		result = await page.evaluate(
			"""async ({url, headers}) => {
				const response = await fetch(url, {
					method: 'POST',
					headers,
					credentials: 'include',
				});
				return {status: response.status, body: await response.text()};
			}""",
			{'url': checkin_url, 'headers': headers},
		)
		return parse_check_in_response(account_name, result.get('status', 0), result.get('body', ''))
	except Exception as exc:
		print(f'[FAILED] {account_name}: Browser check-in request failed - {str(exc)[:120]}')
		return False


async def execute_automatic_check_in_with_playwright(
	account_name: str,
	provider_config,
	user_cookies: dict,
	api_user: str | None,
	username: str | None = None,
	password: str | None = None,
):
	"""在浏览器上下文中执行签到，可选地先用用户名/密码重新登录"""
	print(f'[PROCESSING] {account_name}: Starting browser-based automatic check-in...')

	async with async_playwright() as p:
		import tempfile

		with tempfile.TemporaryDirectory() as temp_dir:
			context = await launch_playwright_context(p, temp_dir)
			page = await context.new_page()

			try:
				# 必须注入完整 Cookie 集合。WAF Cookie 也属于浏览器回退流程的
				# 必要状态，不能只保留 session 等登录 Cookie。
				await add_cookies_to_browser_context(context, provider_config.domain, user_cookies)

				login_url = f'{provider_config.domain}{provider_config.login_path}'
				print(f'[PROCESSING] {account_name}: Opening login page in browser context...')
				await page.goto(login_url, wait_until='domcontentloaded')

				try:
					await page.wait_for_function('document.readyState === "complete"', timeout=5000)
				except Exception:
					await page.wait_for_timeout(3000)

				effective_api_user = api_user
				if username and password:
					login_success, login_api_user, login_error = await login_with_credentials_in_browser(
						page,
						provider_config,
						account_name,
						username,
						password,
					)
					if not login_success:
						print(f'[FAILED] {account_name}: Re-authentication failed - {login_error}')
						await context.close()
						return False, None, {'success': False, 'error': f'Re-authentication failed: {login_error}'}
					effective_api_user = login_api_user or effective_api_user

				user_info_before = await fetch_user_info_in_browser(page, provider_config, effective_api_user)
				if user_info_before and user_info_before.get('success'):
					print(user_info_before['display'])
				elif user_info_before:
					print(user_info_before.get('error', 'Unknown error'))

				if user_info_before and user_info_before.get('api_user'):
					effective_api_user = user_info_before['api_user']

				if provider_config.needs_manual_check_in():
					checkin_success = await execute_check_in_in_browser(
						page,
						account_name,
						provider_config,
						effective_api_user,
					)
					if not checkin_success:
						await context.close()
						return (
							False,
							user_info_before,
							{
								'success': False,
								'error': 'Browser check-in request failed',
							},
						)
				else:
					print(f'[INFO] {account_name}: Verifying automatic check-in via browser user info request')

				await asyncio.sleep(1)
				user_info_after = await fetch_user_info_in_browser(page, provider_config, effective_api_user)
				if user_info_after and user_info_after.get('success'):
					print(f'[SUCCESS] {account_name}: Check-in verified in browser context')
					await context.close()
					return True, user_info_before, user_info_after

				error_msg = user_info_after.get('error', 'Unknown error') if user_info_after else 'Unknown error'
				print(f'[FAILED] {account_name}: Automatic check-in could not be verified - {error_msg}')
				print(f'[INFO] {account_name}: This usually means cookies expired or the site now requires re-login')
				await context.close()
				return False, user_info_before, user_info_after
			except Exception as e:
				print(f'[FAILED] {account_name}: Browser-based automatic check-in error - {str(e)[:50]}...')
				await context.close()
				return False, None, None


async def prepare_cookies(
	account_name: str,
	provider_config,
	user_cookies: dict,
	force_refresh: bool = False,
) -> dict | None:
	"""准备请求所需的 cookies（可能包含 WAF cookies）。"""
	waf_cookies = {}

	if provider_config.needs_waf_cookies():
		required_waf_cookies = provider_config.waf_cookie_names or []
		user_supplied_waf_cookies = {
			name: user_cookies[name] for name in required_waf_cookies if user_cookies.get(name)
		}
		missing_waf_cookies = [name for name in required_waf_cookies if name not in user_supplied_waf_cookies]

		if not force_refresh and not missing_waf_cookies and user_supplied_waf_cookies:
			print(f'[INFO] {account_name}: Using WAF cookies from account configuration')
			waf_cookies = user_supplied_waf_cookies
		else:
			if force_refresh:
				print(f'[INFO] {account_name}: Refreshing WAF cookies for browser verification')
				cookies_to_fetch = required_waf_cookies
			elif user_supplied_waf_cookies:
				print(
					f'[INFO] {account_name}: Reusing {len(user_supplied_waf_cookies)} '
					f'user-provided WAF cookie(s), fetching {len(missing_waf_cookies)} missing cookie(s)'
				)
				cookies_to_fetch = missing_waf_cookies
			else:
				cookies_to_fetch = required_waf_cookies

			login_url = f'{provider_config.domain}{provider_config.login_path}'
			fetched_waf_cookies = await get_waf_cookies_with_playwright(
				account_name,
				login_url,
				cookies_to_fetch,
			)
			if not fetched_waf_cookies:
				if force_refresh and user_supplied_waf_cookies:
					print(f'[WARNING] {account_name}: Fresh WAF cookies unavailable, reusing configured WAF cookies')
					waf_cookies = user_supplied_waf_cookies
				else:
					print(f'[FAILED] {account_name}: Unable to get WAF cookies')
					return None
			elif force_refresh:
				# 新获取的 WAF Cookie 优先，账号 Cookie 中的 session 等认证 Cookie 保留。
				waf_cookies = {**user_supplied_waf_cookies, **fetched_waf_cookies}
			else:
				waf_cookies = {**fetched_waf_cookies, **user_supplied_waf_cookies}
	else:
		print(f'[INFO] {account_name}: Bypass WAF not required, using user cookies directly')

	return {**user_cookies, **waf_cookies}


async def get_browser_cookies_for_retry(
	account_name: str,
	provider_config,
	user_cookies: dict,
	current_cookies: dict,
) -> dict:
	"""为浏览器回退刷新 WAF Cookie，同时保留账号认证 Cookie。"""
	if not provider_config.needs_waf_cookies():
		return current_cookies

	refreshed_cookies = await prepare_cookies(
		account_name,
		provider_config,
		user_cookies,
		force_refresh=True,
	)
	return refreshed_cookies or current_cookies


def execute_check_in(client, account_name: str, provider_config, headers: dict):
	"""执行签到请求"""
	print(f'[NETWORK] {account_name}: Executing check-in')

	checkin_headers = headers.copy()
	checkin_headers.update({'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})

	sign_in_url = f'{provider_config.domain}{provider_config.sign_in_path}'
	response = client.post(sign_in_url, headers=checkin_headers, timeout=30)
	return parse_check_in_response(account_name, response.status_code, response.text)


def balance_total_change(before: dict, after: dict) -> float:
	before_quota = before.get('quota_raw', before['quota'] * 500000)
	after_quota = after.get('quota_raw', after['quota'] * 500000)
	before_used = before.get('used_quota_raw', before['used_quota'] * 500000)
	after_used = after.get('used_quota_raw', after['used_quota'] * 500000)
	return float(round((after_quota + after_used - before_quota - before_used) / 500000, 2))


def build_check_in_detail(
	account_name: str, success: bool, before: dict | None, after: dict | None, previous: dict | None = None
) -> dict:
	"""只记录本轮实际读取的余额，不把缺失值补成零或把零变化当成已签到。"""
	detail = {'name': account_name, 'success': success}
	for prefix, info in (('before', before), ('after', after)):
		if info and info.get('success'):
			detail[f'{prefix}_quota'] = info['quota']
			detail[f'{prefix}_used'] = info['used_quota']
		else:
			detail[f'{prefix}_quota'] = None
			detail[f'{prefix}_used'] = None

	detail.update(
		check_in_reward=None, usage_increase=None, balance_change=None, previous_quota=None, since_previous=None
	)
	detail['previous_checked_at'] = None
	if previous and previous.get('success'):
		detail['previous_quota'] = previous['quota']
		detail['previous_checked_at'] = previous.get('checked_at')
		if after and after.get('success'):
			detail['since_previous'] = balance_total_change(previous, after)
	if before and after and before.get('success') and after.get('success'):
		# 使用原始额度计算，最后再四舍五入，避免分别取两位小数制造虚假收益。
		before_quota = before.get('quota_raw', before['quota'] * 500000)
		after_quota = after.get('quota_raw', after['quota'] * 500000)
		before_used = before.get('used_quota_raw', before['used_quota'] * 500000)
		after_used = after.get('used_quota_raw', after['used_quota'] * 500000)
		detail['check_in_reward'] = balance_total_change(before, after)
		detail['usage_increase'] = round((after_used - before_used) / 500000, 2)
		detail['balance_change'] = round((after_quota - before_quota) / 500000, 2)
	return detail


def format_amount(value, signed: bool = False) -> str:
	if value is None:
		return '未读取'
	return f'${value:+.2f}' if signed else f'${value:.2f}'


def format_run_summary(details: list[dict], executed_at: str) -> str:
	"""每次执行都输出账号明细，供 Actions 摘要及本地日志复核。"""
	lines = [
		'## 签到执行明细',
		'',
		f'北京时间：{executed_at}',
		'',
		'| 账号 | 执行结果 | 本轮前余额 | 本轮后余额 | 本轮额度变化 | 本轮消耗 | 上次记录余额 | 较上次额度变化 |',
		'| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |',
	]
	for detail in details:
		name = detail['name'].replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')
		status = '已确认完成' if detail['success'] else '失败 / 未确认'
		values = [
			format_amount(detail['before_quota']),
			format_amount(detail['after_quota']),
			format_amount(detail['check_in_reward'], signed=True),
			format_amount(detail['usage_increase']),
			format_amount(detail['previous_quota']),
			format_amount(detail['since_previous'], signed=True),
		]
		lines.append(f'| {name} | {status} | ' + ' | '.join(values) + ' |')
	lines.extend(
		[
			'',
			'本轮额度变化 =（后余额 + 后累计消耗）−（前余额 + 前累计消耗）。',
			'余额无变化不代表当日已领到奖励；此表仅记录本轮执行，不把历史余额当作当前余额。',
			'较上次额度变化按上次成功读取至本轮结束计算，并补回期间消耗；该区间可能跨天。',
		]
	)
	for detail in details:
		if detail['previous_checked_at']:
			lines.append(f'- {detail["name"]} 上次记录（北京时间）：{detail["previous_checked_at"]}')
	return '\n'.join(lines)


def format_check_in_notification(detail: dict) -> str:
	"""格式化签到通知消息

	Args:
		detail: 包含签到详情的字典

	Returns:
		格式化后的通知消息
	"""
	lines = [
		f'[CHECK-IN] {detail["name"]}',
		'  状态：' + ('已确认完成' if detail['success'] else '失败 / 未确认'),
		'  ━━━━━━━━━━━━━━━━━━━━',
		'  📍 签到前',
		f'     💵 余额: {format_amount(detail["before_quota"])}  |  📊 累计消耗: {format_amount(detail["before_used"])}',
		'  📍 签到后',
		f'     💵 余额: {format_amount(detail["after_quota"])}  |  📊 累计消耗: {format_amount(detail["after_used"])}',
	]
	if detail['check_in_reward'] is None:
		lines.append('  ℹ️  余额数据不完整，无法计算本轮额度变化')
		return '\n'.join(lines)

	# 判断是否有变化
	has_reward = detail['check_in_reward'] != 0
	has_usage = detail['usage_increase'] != 0

	if has_reward or has_usage:
		lines.append('  ━━━━━━━━━━━━━━━━━━━━')

		# 余额变化不能单独证明签到奖励到账。
		if not has_reward and has_usage:
			lines.append('  ℹ️  本轮未观察到额度增加（期间有使用）')

		# 签到获得
		if has_reward:
			lines.append(f'  🎁 本轮额度变化: {format_amount(detail["check_in_reward"], signed=True)}')

		# 期间消耗
		if has_usage:
			lines.append(f'  📉 期间消耗: ${detail["usage_increase"]:.2f}')

		# 余额变化
		if detail['balance_change'] != 0:
			change_symbol = '+' if detail['balance_change'] > 0 else ''
			change_emoji = '📈' if detail['balance_change'] > 0 else '📉'
			lines.append(f'  {change_emoji} 余额变化: {change_symbol}${detail["balance_change"]:.2f}')
	else:
		# 无任何变化
		lines.extend(['  ━━━━━━━━━━━━━━━━━━━━', '  ℹ️  本轮余额无变化，不能据此判断当日奖励是否到账'])

	return '\n'.join(lines)


async def check_in_account(account: AccountConfig, account_index: int, app_config: AppConfig):
	"""为单个账号执行签到操作"""
	account_name = account.get_display_name(account_index)
	print(f'\n[PROCESSING] Starting to process {account_name}')

	provider_config = app_config.get_provider(account.provider)
	if not provider_config:
		print(f'[FAILED] {account_name}: Provider "{account.provider}" not found in configuration')
		return False, None, None

	print(f'[INFO] {account_name}: Using provider "{account.provider}" ({provider_config.domain})')

	user_cookies = parse_cookies(account.cookies)
	if not user_cookies and not account.has_credentials():
		print(f'[FAILED] {account_name}: Configure cookies or username/password credentials')
		return False, None, None

	# 没有 Cookie 时直接走浏览器登录；这正是 AgentRouter 重新登录账号的用法。
	if not user_cookies:
		return await execute_automatic_check_in_with_playwright(
			account_name,
			provider_config,
			{},
			account.api_user,
			account.username,
			account.password,
		)

	all_cookies = await prepare_cookies(account_name, provider_config, user_cookies)
	if not all_cookies:
		if account.has_credentials():
			print(f'[INFO] {account_name}: WAF cookies unavailable, continuing with browser re-authentication')
			all_cookies = user_cookies
		else:
			return False, None, None

	# 部分平台会在登录页访问时完成签到；这类平台仍需走浏览器上下文，
	# 并以用户信息接口可用作为登录状态验证。
	if provider_config.checkin_on_login:
		print(f'[INFO] {account_name}: Check-in is triggered by opening the login page')
		browser_result = await execute_automatic_check_in_with_playwright(
			account_name,
			provider_config,
			all_cookies,
			account.api_user,
		)
		if browser_result[0] or not provider_config.needs_waf_cookies():
			return browser_result

		refreshed_cookies = await get_browser_cookies_for_retry(
			account_name,
			provider_config,
			user_cookies,
			all_cookies,
		)
		if refreshed_cookies != all_cookies:
			print(f'[INFO] {account_name}: Retrying login-page check-in with refreshed WAF cookies')
			return await execute_automatic_check_in_with_playwright(
				account_name,
				provider_config,
				refreshed_cookies,
				account.api_user,
			)
		return browser_result

	client = httpx.Client(http2=True, timeout=30.0)

	try:
		client.cookies.update(all_cookies)

		headers = {
			'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
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
		if account.api_user:
			headers[provider_config.api_user_key] = str(account.api_user)

		user_info_url = f'{provider_config.domain}{provider_config.user_info_path}'
		user_info_before = get_user_info(client, headers, user_info_url)
		if user_info_before and user_info_before.get('success'):
			print(user_info_before['display'])
			if user_info_before.get('api_user'):
				account.api_user = user_info_before['api_user']
				headers[provider_config.api_user_key] = account.api_user
		elif user_info_before:
			print(user_info_before.get('error', 'Unknown error'))

		if provider_config.needs_manual_check_in():
			success = execute_check_in(client, account_name, provider_config, headers)
			# 签到后再次获取用户信息，用于计算签到收益
			user_info_after = get_user_info(client, headers, user_info_url)
			if success and user_info_after and user_info_after.get('success'):
				return success, user_info_before, user_info_after
			success = False
			if provider_config.needs_waf_cookies():
				print(
					f'[INFO] {account_name}: HTTP check-in or balance verification failed, retrying in browser context'
				)
				browser_cookies = await get_browser_cookies_for_retry(
					account_name,
					provider_config,
					user_cookies,
					all_cookies,
				)
				browser_success, browser_before, browser_after = await execute_automatic_check_in_with_playwright(
					account_name,
					provider_config,
					browser_cookies,
					account.api_user,
					account.username,
					account.password,
				)
				before = user_info_before if user_info_before and user_info_before.get('success') else browser_before
				return browser_success, before, browser_after
			if account.has_credentials():
				print(f'[INFO] {account_name}: Cookie check-in failed, retrying with browser credentials')
				browser_success, browser_before, browser_after = await execute_automatic_check_in_with_playwright(
					account_name,
					provider_config,
					all_cookies,
					account.api_user,
					account.username,
					account.password,
				)
				before = user_info_before if user_info_before and user_info_before.get('success') else browser_before
				return browser_success, before, browser_after
			return success, user_info_before, user_info_after
		else:
			if provider_config.needs_waf_cookies() and not (user_info_before and user_info_before.get('success')):
				print(f'[INFO] {account_name}: HTTP verification blocked, retrying in browser context')
				browser_cookies = await get_browser_cookies_for_retry(
					account_name,
					provider_config,
					user_cookies,
					all_cookies,
				)
				return await execute_automatic_check_in_with_playwright(
					account_name,
					provider_config,
					browser_cookies,
					account.api_user,
					account.username,
					account.password,
				)

			print(f'[INFO] {account_name}: Verifying automatic check-in via user info request')
			await asyncio.sleep(1)
			# 自动签到的情况，再次获取用户信息
			user_info_after = get_user_info(client, headers, user_info_url)
			if user_info_after and user_info_after.get('success'):
				print(f'[SUCCESS] {account_name}: Automatic check-in verified')
				return True, user_info_before, user_info_after

			if provider_config.needs_waf_cookies():
				print(f'[INFO] {account_name}: HTTP verification still blocked, retrying in browser context')
				browser_cookies = await get_browser_cookies_for_retry(
					account_name,
					provider_config,
					user_cookies,
					all_cookies,
				)
				return await execute_automatic_check_in_with_playwright(
					account_name,
					provider_config,
					browser_cookies,
					account.api_user,
					account.username,
					account.password,
				)

			error_msg = user_info_after.get('error', 'Unknown error') if user_info_after else 'Unknown error'
			print(f'[FAILED] {account_name}: Automatic check-in could not be verified - {error_msg}')
			print(f'[INFO] {account_name}: This usually means cookies expired or the site now requires re-login')
			return False, user_info_before, user_info_after

	except Exception as e:
		print(f'[FAILED] {account_name}: Error occurred during check-in process - {str(e)[:50]}...')
		return False, None, None
	finally:
		client.close()


async def main():
	"""主函数"""
	print('[SYSTEM] AnyRouter.top multi-account auto check-in script started (using Playwright)')
	print(f'[TIME] Execution time (UTC+08:00): {datetime.now(BEIJING_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")}')

	app_config = AppConfig.load_from_env()
	print(f'[INFO] Loaded {len(app_config.providers)} provider configuration(s)')

	accounts = load_accounts_config()
	if not accounts:
		print('[FAILED] Unable to load account configuration, program exits')
		sys.exit(1)

	print(f'[INFO] Found {len(accounts)} account configurations')

	last_balance_hash = load_balance_hash()
	balance_history = load_balance_history()

	success_count = 0
	total_count = len(accounts)
	notification_content = []
	current_balances = {}
	account_check_in_details = {}  # 存储每个账号的签到详情
	need_notify = False  # 是否需要发送通知
	balance_changed = False  # 余额是否有变化

	for i, account in enumerate(accounts):
		account_key = f'account_{i + 1}'
		try:
			success, user_info_before, user_info_after = await check_in_account(account, i, app_config)
			success = bool(success and user_info_after and user_info_after.get('success'))
			provider = app_config.get_provider(account.provider)
			identity = (user_info_after or {}).get('api_user') or account.api_user or account.get_display_name(i)
			history_key = f'{provider.domain if provider else account.provider}|{identity}'
			previous = balance_history.get(history_key)
			account_check_in_details[account_key] = build_check_in_detail(
				account.get_display_name(i), success, user_info_before, user_info_after, previous
			)
			if success:
				success_count += 1

			should_notify_this_account = False

			if not success:
				should_notify_this_account = True
				need_notify = True
				account_name = account.get_display_name(i)
				print(f'[NOTIFY] {account_name} failed, will send notification')

			# 存储签到前后的余额信息
			if user_info_after and user_info_after.get('success'):
				current_quota = user_info_after['quota']
				current_used = user_info_after['used_quota']
				current_balances[account_key] = {'quota': current_quota, 'used': current_used}
				balance_history[history_key] = {
					key: user_info_after[key]
					for key in ('success', 'quota', 'used_quota', 'quota_raw', 'used_quota_raw')
					if key in user_info_after
				}
				balance_history[history_key]['checked_at'] = datetime.now(BEIJING_TIMEZONE).strftime(
					'%Y-%m-%d %H:%M:%S'
				)

			if should_notify_this_account:
				account_name = account.get_display_name(i)
				status = '[SUCCESS]' if success else '[FAIL]'
				account_result = f'{status} {account_name}'
				if user_info_after and user_info_after.get('success'):
					account_result += f'\n{user_info_after["display"]}'
				elif user_info_after:
					account_result += f'\n{user_info_after.get("error", "Unknown error")}'
				notification_content.append(account_result)

		except Exception as e:
			account_name = account.get_display_name(i)
			print(f'[FAILED] {account_name} processing exception: {e}')
			account_check_in_details[account_key] = build_check_in_detail(account_name, False, None, None)
			need_notify = True  # 异常也需要通知
			notification_content.append(f'[FAIL] {account_name} exception: {str(e)[:50]}...')

	# 检查余额变化
	current_balance_hash = generate_balance_hash(current_balances) if current_balances else None
	if current_balance_hash:
		if last_balance_hash is None:
			# 首次运行
			balance_changed = True
			need_notify = True
			print('[NOTIFY] First run detected, will send notification with current balances')
		elif current_balance_hash != last_balance_hash:
			# 余额有变化
			balance_changed = True
			need_notify = True
			print('[NOTIFY] Balance changes detected, will send notification')
		else:
			print('[INFO] No balance changes detected')

	# 为有余额变化的情况添加所有成功账号到通知内容
	if balance_changed:
		for i, account in enumerate(accounts):
			account_key = f'account_{i + 1}'
			if account_key in account_check_in_details:
				detail = account_check_in_details[account_key]
				account_name = detail['name']

				# 使用格式化函数生成通知消息
				account_result = format_check_in_notification(detail)

				# 检查是否已经在通知内容中（避免重复）
				if not any(account_name in item for item in notification_content):
					notification_content.append(account_result)

	# 保存当前余额hash
	if current_balance_hash:
		save_balance_hash(current_balance_hash)
		save_balance_history(balance_history)

	# 即使本轮余额无变化、没有配置推送，也要留下两个账号的可复核明细。
	executed_at = datetime.now(BEIJING_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')
	run_summary = format_run_summary(list(account_check_in_details.values()), executed_at)
	ledger = record_balance_run(BALANCE_DAILY_FILE, list(account_check_in_details.values()), executed_at)
	run_summary += '\n\n' + format_daily_summary(
		ledger, executed_at, [detail['name'] for detail in account_check_in_details.values()]
	)
	print('\n' + run_summary)
	if summary_path := os.getenv('GITHUB_STEP_SUMMARY'):
		with open(summary_path, 'a', encoding='utf-8') as summary_file:
			summary_file.write(run_summary + '\n')

	if need_notify and notification_content:
		# 构建通知内容
		summary = [
			'[STATS] Check-in result statistics:',
			f'[SUCCESS] Success: {success_count}/{total_count}',
			f'[FAIL] Failed: {total_count - success_count}/{total_count}',
		]

		if success_count == total_count:
			summary.append('[SUCCESS] All accounts check-in successful!')
		elif success_count > 0:
			summary.append('[WARN] Some accounts check-in successful')
		else:
			summary.append('[ERROR] All accounts check-in failed')

		time_info = f'[TIME] Execution time (UTC+08:00): {datetime.now(BEIJING_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")}'

		notify_content = '\n\n'.join([time_info, '\n'.join(notification_content), '\n'.join(summary)])

		print(notify_content)
		notify.push_message('AnyRouter Check-in Alert', notify_content, msg_type='text')
		print('[NOTIFY] Notification delivery attempted; see individual channel results above')
	else:
		print('[INFO] All accounts successful and no balance changes detected, notification skipped')

	# 设置退出码
	sys.exit(0 if success_count == total_count else 1)


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
