#!/usr/bin/env python3
"""Run the configured AnyRouter accounts locally when hosted execution is unavailable."""

import argparse
import asyncio
import fcntl
import os
from pathlib import Path

import checkin
from local_dashboard import Dashboard, merge_history, read_json, write_private_json
from utils.config import AccountConfig


async def run(root: Path, *, force=False):
	state_dir = root / 'local_checkin_state'
	state_dir.mkdir(mode=0o700, exist_ok=True)
	with (state_dir / 'run.lock').open('w') as lock:
		try:
			fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except BlockingIOError:
			print('另一轮本机签到正在运行，本轮跳过。')
			return 0
		config = read_json(root / 'config/cookie.json', [])
		accounts = [
			AccountConfig.from_dict(a, i)
			for i, a in enumerate(config)
			if str(a.get('provider', '')).removeprefix('https://').rstrip('/') in ('anyrouter', 'anyrouter.top')
		]
		if not accounts:
			print('没有配置 AnyRouter 账号。')
			return 1
		ledger_path = state_dir / 'balance_daily.json'
		history = merge_history(read_json(root / 'dashboard_history.json', []), read_json(ledger_path, []))
		write_private_json(ledger_path, history)
		today = checkin.datetime.now(checkin.BEIJING_TIMEZONE).strftime('%Y-%m-%d')
		all_credited = all(
			any(
				entry['executed_at'][:10] == today
				and d['name'] == account.get_display_name(i)
				and (
					(d.get('check_in_reward') or 0) > 0
					or ((d.get('since_previous') or 0) > 0 and (d.get('previous_checked_at') or '')[:10] == today)
				)
				for entry in history
				for d in entry['details']
			)
			for i, account in enumerate(accounts)
		)
		if all_credited and not force:
			print('今天两个账号均已有到账记录，跳过重复签到。')
			return 0

		async def launch(playwright, user_data_dir):
			options = dict(
				headless=True,
				user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
				viewport={'width': 1280, 'height': 800},
				args=['--disable-blink-features=AutomationControlled'],
			)
			chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
			if not Path(playwright.chromium.executable_path).exists() and chrome.exists():
				options['executable_path'] = str(chrome)
			return await playwright.chromium.launch_persistent_context(user_data_dir, **options)

		async def browser_check(account, index, config):
			provider = config.get_provider(account.provider)
			return await checkin.execute_automatic_check_in_with_playwright(
				account.get_display_name(index),
				provider,
				checkin.parse_cookies(account.cookies),
				account.api_user,
				account.username,
				account.password,
			)

		checkin.launch_playwright_context = launch
		checkin.check_in_account = browser_check
		checkin.load_accounts_config = lambda: accounts
		checkin.BALANCE_HASH_FILE = str(state_dir / 'balance_hash.txt')
		checkin.BALANCE_HISTORY_FILE = str(state_dir / 'balance_history.json')
		checkin.BALANCE_DAILY_FILE = str(ledger_path)
		os.environ['GITHUB_STEP_SUMMARY'] = str(state_dir / 'latest-summary.md')
		(state_dir / 'latest-summary.md').write_text('', encoding='utf-8')
		# No external notification channel is configured or needed for the local runner.
		checkin.notify.push_message = lambda *args, **kwargs: None
		code = 0
		try:
			await checkin.main()
		except SystemExit as exc:
			code = int(exc.code or 0)
		dashboard = Dashboard(root)
		dashboard.seed(ledger_path)
		return code


def main():
	parser = argparse.ArgumentParser(description='本机 AnyRouter 签到；当天已到账时跳过重复执行')
	parser.add_argument('--force', action='store_true', help='即使今天已有到账记录也重新验证')
	args = parser.parse_args()
	return asyncio.run(run(Path(__file__).resolve().parent, force=args.force))


if __name__ == '__main__':
	raise SystemExit(main())
