#!/usr/bin/env python3
"""Loopback-only HTML dashboard; account cookies stay in private config and Actions Secrets."""

import argparse
import asyncio
import base64
import copy
import json
import re
import secrets
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from utils.daily_ledger import sanitize_detail

BEIJING = timezone(timedelta(hours=8))
COOKIE_NAMES = {'session', 'acw_tc', 'cdn_sec_tc', 'acw_sc__v2'}
MAX_BODY = 256 * 1024


class DashboardError(Exception):
	pass


def now():
	return datetime.now(BEIJING).strftime('%Y-%m-%d %H:%M:%S')


def read_json(path, default):
	try:
		return json.loads(Path(path).read_text(encoding='utf-8'))
	except (OSError, ValueError):
		return copy.deepcopy(default)


def write_private_json(path, value):
	path = Path(path)
	path.parent.mkdir(parents=True, exist_ok=True)
	temporary = None
	try:
		with tempfile.NamedTemporaryFile(
			mode='w', encoding='utf-8', dir=path.parent, prefix='.dashboard-secret-', delete=False
		) as output:
			temporary = Path(output.name)
			json.dump(value, output, ensure_ascii=False, indent=2)
			output.write('\n')
		temporary.chmod(0o600)
		temporary.replace(path)
	finally:
		if temporary and temporary.exists():
			temporary.unlink()


def parse_cookie_input(value):
	"""Accept Cookie-Editor JSON, a cookie map, or a Cookie request header."""
	if not isinstance(value, str) or not value.strip():
		raise DashboardError('请先粘贴当前账号的 Cookie。')
	value = value.strip()
	expires = None
	if value.startswith(('[', '{')):
		try:
			data = json.loads(value)
		except ValueError as exc:
			raise DashboardError('JSON 格式不完整，请重新复制整个 Cookie 导出内容。') from exc
		if isinstance(data, list):
			cookies = {}
			for item in data:
				if not isinstance(item, dict) or item.get('name') not in COOKIE_NAMES:
					continue
				if str(item.get('domain', '')).lstrip('.') != 'anyrouter.top':
					raise DashboardError('请导出 anyrouter.top 的 Cookie，而不是 GitHub 或 LinuxDO 网站的 Cookie。')
				name, cookie_value = item['name'], item.get('value')
				if name in cookies and cookies[name] != cookie_value:
					raise DashboardError('导出内容包含多份不同的同名 Cookie，请只导出一个账号。')
				cookies[name] = cookie_value
				if name == 'session' and type(item.get('expirationDate')) in (int, float):
					try:
						expires = datetime.fromtimestamp(item['expirationDate'], BEIJING).strftime('%Y-%m-%d %H:%M:%S')
					except (OverflowError, OSError, ValueError) as exc:
						raise DashboardError('session 的到期时间无效，请重新导出。') from exc
		elif isinstance(data, dict):
			cookies = {key: val for key, val in data.items() if key in COOKIE_NAMES}
		else:
			raise DashboardError('请粘贴 Cookie-Editor 的 JSON 数组或 Cookie 对象。')
	else:
		cookies = {}
		for part in value.removeprefix('Cookie:').strip().split(';'):
			if '=' in part:
				key, val = part.strip().split('=', 1)
				if key in COOKIE_NAMES:
					cookies[key] = val
	if not cookies.get('session'):
		raise DashboardError('缺少 session。WAF Cookie 不能代替账号登录，请重新登录后完整导出。')
	if any(
		not isinstance(val, str) or not val or len(val) > 16000 or '\n' in val or '\r' in val
		for val in cookies.values()
	):
		raise DashboardError('Cookie 值无效，请直接粘贴原始导出内容。')
	if expires and expires <= now():
		raise DashboardError('这份 session 已过期，请重新登录后导出新的 Cookie。')
	return cookies, expires


def estimated_expiration(cookies):
	try:
		value = cookies.get('session', '')
		stamp = int(base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)).split(b'|', 1)[0])
		return (datetime.fromtimestamp(stamp, BEIJING) + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
	except (ValueError, OSError, OverflowError, TypeError):
		return None


def merge_history(*sources):
	merged = {}
	cutoff = (datetime.now(BEIJING) - timedelta(days=90)).strftime('%Y-%m-%d %H:%M:%S')
	for source in sources:
		if not isinstance(source, list):
			continue
		for entry in source:
			if not isinstance(entry, dict) or not isinstance(entry.get('details'), list):
				continue
			try:
				stamp = datetime.fromisoformat(entry['executed_at']).strftime('%Y-%m-%d %H:%M:%S')
			except (KeyError, ValueError, TypeError):
				continue
			run_id = entry.get('run_id')
			if not isinstance(run_id, str) or not re.fullmatch(r'\d+:\d+', run_id) or not cutoff <= stamp <= now():
				continue
			merged[run_id] = {
				'run_id': run_id,
				'executed_at': stamp,
				'details': [clean for detail in entry['details'] if (clean := sanitize_detail(detail)) is not None],
			}
	return sorted(merged.values(), key=lambda entry: entry['executed_at'])


async def verify_cookie(account, cookies):
	from playwright.async_api import async_playwright

	from checkin import add_cookies_to_browser_context, fetch_user_info_in_browser
	from utils.config import AppConfig

	provider = AppConfig.load_from_env().get_provider(account.get('provider', 'anyrouter'))
	if not provider or provider.domain != 'https://anyrouter.top':
		raise DashboardError('此页面仅更新 AnyRouter 账号。')
	async with async_playwright() as playwright:
		options = {
			'headless': True,
			'args': ['--disable-blink-features=AutomationControlled'],
		}
		chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
		if not Path(playwright.chromium.executable_path).exists() and chrome.exists():
			options['executable_path'] = str(chrome)
		browser = await playwright.chromium.launch(**options)
		try:
			context = await browser.new_context(
				user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
				viewport={'width': 1280, 'height': 800},
			)
			await add_cookies_to_browser_context(context, provider.domain, cookies)
			page = await context.new_page()
			page.set_default_timeout(30000)
			await page.goto(f'{provider.domain}{provider.login_path}', wait_until='domcontentloaded')
			info = await fetch_user_info_in_browser(page, provider, account.get('api_user'))
			if not info.get('success'):
				if info.get('failure_kind') == 'auth_required':
					raise DashboardError('这份 Cookie 仍返回 HTTP 401。请确认已重新登录所选账号，再完整导出 Cookie。')
				raise DashboardError(
					'暂时无法通过站点防护并读取余额，尚未修改配置。请在 AnyRouter 正常登录后重新导出。'
				)
			if not info.get('api_user') or str(info['api_user']) != str(account.get('api_user')):
				raise DashboardError('Cookie 对应的账号与所选账号不一致，尚未保存。请检查账号选择。')
			return {'quota': info['quota'], 'api_user': info['api_user']}
		finally:
			await browser.close()


class Dashboard:
	def __init__(self, root, repo='namejee/anyrouter-check-in', runner=None, probe=None):
		self.root = Path(root)
		self.repo = repo
		self.config_path = self.root / 'config/cookie.json'
		self.history_path = self.root / 'dashboard_history.json'
		self.state_path = self.root / 'dashboard_state.json'
		self.cache = read_json(self.state_path, {})
		if not isinstance(self.cache, dict):
			self.cache = {}
		self.history = merge_history(read_json(self.history_path, []))
		self.lock = threading.RLock()
		self.job = None
		self.runner = runner or self._run
		self.probe = probe or (lambda account, cookies: asyncio.run(verify_cookie(account, cookies)))

	def _run(self, args, input_text=None):
		try:
			result = subprocess.run(args, input=input_text, text=True, capture_output=True, timeout=90, cwd=self.root)
		except (OSError, subprocess.TimeoutExpired) as exc:
			raise DashboardError('无法运行 GitHub CLI。请检查 gh 已安装并登录，以及网络连接。') from exc
		if result.returncode:
			message = 'GitHub 操作失败，请检查 gh 登录权限及网络。'
			if 'auth' in result.stderr.lower() or '401' in result.stderr or '403' in result.stderr:
				message = 'GitHub 授权不可用，请在终端运行 gh auth login 后重试。'
			raise DashboardError(message)
		return result.stdout

	def gh_json(self, args):
		try:
			return json.loads(self.runner(['gh', *args]))
		except ValueError as exc:
			raise DashboardError('GitHub 返回的记录格式异常，请稍后刷新。') from exc

	def config(self):
		accounts = read_json(self.config_path, None)
		if not isinstance(accounts, list):
			raise DashboardError('本地 config/cookie.json 缺失或格式错误。')
		return accounts

	def configured(self, accounts=None):
		return [
			(f'account-{index}', account)
			for index, account in enumerate(self.config() if accounts is None else accounts)
			if isinstance(account, dict)
			and str(account.get('provider', '')).removeprefix('https://').rstrip('/') in ('anyrouter', 'anyrouter.top')
		]

	def seed(self, source):
		with self.lock:
			self.history = merge_history(self.history, read_json(source, []))
			write_private_json(self.history_path, self.history)

	def phase(self, message):
		with self.lock:
			if self.job:
				self.job['message'] = message

	def start(self, action, payload):
		parsed = parse_cookie_input(payload.get('cookie', '')) if action == 'update' else None
		if action not in ('refresh', 'run', 'update'):
			raise DashboardError('操作不存在。')
		if action == 'update' and payload.get('account_id') not in dict(self.configured()):
			raise DashboardError('请选择一个已配置的 AnyRouter 账号。')
		with self.lock:
			if self.job and self.job['status'] == 'running':
				raise DashboardError('已有操作正在进行，请等待完成。')
			self.job = {
				'id': secrets.token_hex(8),
				'status': 'running',
				'action': action,
				'message': '正在准备…',
				'started_at': now(),
			}
		threading.Thread(target=self._worker, args=(action, payload.get('account_id'), parsed), daemon=True).start()
		return copy.deepcopy(self.job)

	def _worker(self, action, account_id, parsed):
		try:
			if action == 'refresh':
				self.refresh()
				message = '已读取最新执行与到账记录。'
			elif action == 'update':
				self.update(account_id, *parsed)
				message = self.dispatch()
			else:
				message = self.dispatch()
			with self.lock:
				self.job.update(status='done', message=message, ended_at=now())
		except DashboardError as exc:
			with self.lock:
				self.job.update(status='error', message=str(exc), ended_at=now())
		except Exception:
			with self.lock:
				self.job.update(status='error', message='操作未完成，请检查本地网络与浏览器后重试。', ended_at=now())

	def update(self, account_id, cookies, expires):
		accounts = self.config()
		selected = dict(self.configured(accounts)).get(account_id)
		if not selected:
			raise DashboardError('所选账号已变更，请刷新页面。')
		self.phase('正在验证 Cookie 和账号身份，尚未修改配置…')
		verified = self.probe(selected, cookies)
		if not verified or str(verified.get('api_user')) != str(selected.get('api_user')):
			raise DashboardError('Cookie 对应的账号与所选账号不一致，尚未保存。')
		selected['cookies'] = cookies
		production = [account for _, account in self.configured(accounts)]
		self.phase('账号验证通过，正在同步 GitHub production Secret…')
		self.runner(
			['gh', 'secret', 'set', 'ANYROUTER_ACCOUNTS', '--repo', self.repo, '--env', 'production'],
			json.dumps(production, ensure_ascii=False),
		)
		write_private_json(self.config_path, accounts)
		with self.lock:
			self.cache.setdefault('cookie_meta', {})[account_id] = {
				'saved_at': now(),
				'expires_at': expires,
				'estimated': not bool(expires),
			}
			write_private_json(self.state_path, self.cache)
		self.phase('Cookie 已保存并同步，正在发起签到验证…')

	def refresh(self):
		self.phase('正在读取 GitHub 执行记录…')
		runs = self.gh_json(
			[
				'run',
				'list',
				'--repo',
				self.repo,
				'--workflow',
				'checkin.yml',
				'--limit',
				'40',
				'--json',
				'databaseId,number,status,conclusion,event,createdAt',
			]
		)
		if not isinstance(runs, list):
			raise DashboardError('没有找到可读取的签到执行记录。')
		for run in [run for run in runs if run.get('status') == 'completed'][:6]:
			rid = run['databaseId']
			artifacts = self.gh_json(['api', f'repos/{self.repo}/actions/runs/{rid}/artifacts'])
			artifact = next(
				(
					a
					for a in artifacts.get('artifacts', [])
					if a['name'].startswith(f'balance-daily-{rid}-') and not a.get('expired')
				),
				None,
			)
			if not artifact:
				continue
			self.phase('正在核对两个账号的每日到账记录…')
			with tempfile.TemporaryDirectory(prefix='anyrouter-ledger-') as directory:
				self.runner(
					[
						'gh',
						'run',
						'download',
						str(rid),
						'--repo',
						self.repo,
						'--name',
						artifact['name'],
						'--dir',
						directory,
					]
				)
				incoming = read_json(Path(directory) / 'balance_daily.json', [])
				with self.lock:
					self.history = merge_history(self.history, incoming)
					write_private_json(self.history_path, self.history)
			break
		with self.lock:
			self.cache.update(runs=runs, refreshed_at=now())
			write_private_json(self.state_path, self.cache)

	def dispatch(self):
		self.phase('正在发起 GitHub 签到，通常约需一分钟…')
		output = self.runner(['gh', 'workflow', 'run', 'checkin.yml', '--repo', self.repo, '--ref', 'main'])
		match = re.search(r'/actions/runs/(\d+)', output)
		if match:
			rid = match.group(1)
		else:
			runs = self.gh_json(
				[
					'run',
					'list',
					'--repo',
					self.repo,
					'--workflow',
					'checkin.yml',
					'--event',
					'workflow_dispatch',
					'--limit',
					'1',
					'--json',
					'databaseId',
				]
			)
			if not runs:
				raise DashboardError('已提交补跑，但暂时找不到运行记录，请稍后刷新。')
			rid = str(runs[0]['databaseId'])
		deadline = time.monotonic() + 600
		while time.monotonic() < deadline:
			run = self.gh_json(['run', 'view', rid, '--repo', self.repo, '--json', 'status,conclusion'])
			if run['status'] == 'completed':
				self.refresh()
				if run['conclusion'] == 'success':
					return '签到验证成功，两个账号的最新余额与当天到账记录已更新。'
				return 'Cookie 已处理，补跑仍有账号未完成；请查看账号卡片的具体原因。'
			self.phase('签到任务正在运行，页面会自动更新结果…')
			time.sleep(10)
		raise DashboardError('签到仍在排队或执行，已提交的任务会继续运行；稍后点击刷新记录。')

	def state(self):
		with self.lock:
			history, cache, job = copy.deepcopy(self.history), copy.deepcopy(self.cache), copy.deepcopy(self.job)
		accounts = []
		for aid, account in self.configured():
			name = account.get('name') or aid
			obs = [(entry, detail) for entry in history for detail in entry['details'] if detail['name'] == name]
			latest = obs[-1] if obs else None
			valid = next(
				((entry, detail) for entry, detail in reversed(obs) if detail.get('after_quota') is not None), None
			)
			failure = next(
				((entry, detail) for entry, detail in reversed(obs) if detail.get('failure_kind') == 'auth_required'),
				None,
			)
			meta = cache.get('cookie_meta', {}).get(aid, {})
			cookies = account.get('cookies') if isinstance(account.get('cookies'), dict) else {}
			accounts.append(
				{
					'id': aid,
					'name': name,
					'quota': valid[1]['after_quota'] if valid else None,
					'quota_at': valid[0]['executed_at'] if valid else None,
					'success': latest[1]['success'] if latest else None,
					'failure_kind': latest[1].get('failure_kind') if latest else None,
					'checked_at': latest[0]['executed_at'] if latest else None,
					'last_auth_failure_at': failure[0]['executed_at'] if failure else None,
					'cookie_expires_at': meta.get('expires_at') or estimated_expiration(cookies),
					'expiry_estimated': not bool(meta.get('expires_at')),
					'cookie_saved_at': meta.get('saved_at'),
				}
			)
		daily = []
		end = datetime.now(BEIJING).date()
		for offset in range(9, -1, -1):
			day = (end - timedelta(days=offset)).isoformat()
			row = {'date': day, 'accounts': []}
			for account in accounts:
				obs = [
					detail
					for entry in history
					if entry['executed_at'][:10] == day
					for detail in entry['details']
					if detail['name'] == account['name']
				]
				credits = [
					max(
						detail.get('check_in_reward') or 0,
						(detail.get('since_previous') or 0)
						if (detail.get('previous_checked_at') or '')[:10] == day
						else 0,
					)
					for detail in obs
				]
				positive = max(credits, default=0)
				cross_day = any(
					(d.get('since_previous') or 0) > 0 and (d.get('previous_checked_at') or '')[:10] != day for d in obs
				)
				measured = any(d.get('check_in_reward') is not None or d.get('since_previous') is not None for d in obs)
				status = (
					'positive' if positive > 0 else ('cross_day' if cross_day else ('zero' if measured else 'unknown'))
				)
				row['accounts'].append({'id': account['id'], 'status': status, 'observed_credit': positive})
			daily.append(row)
		runs = [
			{
				'number': run['number'],
				'status': run['status'],
				'conclusion': run['conclusion'],
				'created_at': run['createdAt'],
				'event': run['event'],
				'url': f'https://github.com/{self.repo}/actions/runs/{run["databaseId"]}',
			}
			for run in cache.get('runs', [])[:8]
		]
		return {
			'accounts': accounts,
			'daily': daily,
			'runs': runs,
			'job': job,
			'refreshed_at': cache.get('refreshed_at'),
			'today': end.isoformat(),
			'repo': self.repo,
		}


def make_handler(dashboard, html_path, token, port):
	allowed_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
	allowed_origins = {f'http://{host}' for host in allowed_hosts}

	class Handler(BaseHTTPRequestHandler):
		def log_message(self, *args):
			pass

		def send(self, status, value, html=False):
			body = value.encode('utf-8') if html else json.dumps(value, ensure_ascii=False).encode('utf-8')
			self.send_response(status)
			self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json; charset=utf-8')
			self.send_header('Content-Length', str(len(body)))
			self.send_header('Cache-Control', 'no-store')
			self.send_header('X-Frame-Options', 'DENY')
			self.send_header('X-Content-Type-Options', 'nosniff')
			self.send_header('Referrer-Policy', 'no-referrer')
			self.send_header(
				'Content-Security-Policy',
				"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
			)
			self.end_headers()
			self.wfile.write(body)

		def authorized(self, mutation=False):
			return (
				self.headers.get('Host') in allowed_hosts
				and secrets.compare_digest(self.headers.get('X-Panel-Token', ''), token)
				and (not mutation or self.headers.get('Origin') in allowed_origins)
			)

		def do_GET(self):
			if self.headers.get('Host') not in allowed_hosts:
				return self.send(403, {'error': '仅允许从本机地址访问。'})
			if self.path == '/':
				return self.send(
					200, Path(html_path).read_text(encoding='utf-8').replace('__PANEL_TOKEN__', token), html=True
				)
			if self.path == '/api/state' and self.authorized():
				try:
					return self.send(200, dashboard.state())
				except DashboardError as exc:
					return self.send(422, {'error': str(exc)})
			return self.send(404, {'error': '页面不存在。'})

		def do_POST(self):
			if not self.authorized(mutation=True):
				return self.send(403, {'error': '请求不是来自本地页面，请刷新页面后重试。'})
			if self.path not in ('/api/refresh', '/api/run', '/api/update'):
				return self.send(404, {'error': '操作不存在。'})
			try:
				length = int(self.headers.get('Content-Length', '0'))
				if not 0 < length <= MAX_BODY or self.headers.get_content_type() != 'application/json':
					return self.send(413, {'error': '请提交不超过 256 KB 的 Cookie JSON。'})
				payload = json.loads(self.rfile.read(length))
				if not isinstance(payload, dict):
					raise ValueError('expected object')
				return self.send(202, dashboard.start(self.path.rsplit('/', 1)[-1], payload))
			except DashboardError as exc:
				return self.send(422, {'error': str(exc)})
			except (ValueError, TypeError):
				return self.send(400, {'error': '请求格式不完整。'})

	return Handler


def main():
	parser = argparse.ArgumentParser(description='AnyRouter 本地签到与 Cookie 更新页面')
	parser.add_argument('--port', type=int, default=8765)
	parser.add_argument('--repo', default='namejee/anyrouter-check-in')
	parser.add_argument('--seed-ledger', type=Path)
	args = parser.parse_args()
	if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo):
		parser.error('repo must be owner/name')
	root = Path(__file__).resolve().parent
	dashboard = Dashboard(root, args.repo)
	if args.seed_ledger:
		dashboard.seed(args.seed_ledger)
	token = secrets.token_hex(32)
	server = ThreadingHTTPServer(
		('127.0.0.1', args.port),
		make_handler(dashboard, root / 'designs/checkin-dashboard/index.html', token, args.port),
	)
	print(f'签到控制台：http://127.0.0.1:{args.port}', flush=True)
	try:
		server.serve_forever()
	except KeyboardInterrupt:
		pass
	finally:
		server.server_close()


if __name__ == '__main__':
	main()
