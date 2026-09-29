"""Retain sanitized observations; daily credit is an any-positive flag, never a net balance comparison."""

import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

DETAIL_FIELDS = (
	'name',
	'success',
	'before_quota',
	'after_quota',
	'check_in_reward',
	'usage_increase',
	'previous_quota',
	'previous_checked_at',
	'since_previous',
)


def sanitize_detail(detail) -> dict | None:
	if not isinstance(detail, dict) or not isinstance(detail.get('name'), str):
		return None
	result = {key: detail.get(key) for key in DETAIL_FIELDS}
	result['success'] = detail.get('success') is True
	previous = result['previous_checked_at']
	if not isinstance(previous, str):
		result['previous_checked_at'] = None
	for key in DETAIL_FIELDS:
		if key in ('name', 'success', 'previous_checked_at'):
			continue
		value = result[key]
		if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
			result[key] = None
	return result


def record_balance_run(path: str, details: list[dict], executed_at: str) -> list[dict]:
	ledger_path = Path(path)
	try:
		entries = json.loads(ledger_path.read_text(encoding='utf-8'))
		if not isinstance(entries, list):
			raise ValueError('expected a list')
	except FileNotFoundError:
		entries = []
	except (OSError, ValueError) as exc:
		print(f'[WARNING] Daily balance ledger unavailable: {exc}')
		entries = []

	cutoff = (datetime.fromisoformat(executed_at) - timedelta(days=90)).strftime('%Y-%m-%d %H:%M:%S')
	run_id = (
		f'{os.environ["GITHUB_RUN_ID"]}:{os.getenv("GITHUB_RUN_ATTEMPT", "1")}'
		if os.getenv('GITHUB_RUN_ID')
		else str(uuid4())
	)
	retained = []
	for entry in entries:
		if not isinstance(entry, dict) or not isinstance(entry.get('details'), list):
			continue
		stamp = entry.get('executed_at')
		if not isinstance(stamp, str) or not cutoff <= stamp <= executed_at or entry.get('run_id') == run_id:
			continue
		retained.append(
			{
				'run_id': str(entry.get('run_id', '')),
				'executed_at': stamp,
				'details': [clean for detail in entry['details'] if (clean := sanitize_detail(detail)) is not None],
			}
		)
	entries = retained
	entries.append(
		{
			'run_id': run_id,
			'executed_at': executed_at,
			'details': [clean for detail in details if (clean := sanitize_detail(detail)) is not None],
		}
	)
	entries.sort(key=lambda entry: entry['executed_at'])
	temporary = ledger_path.with_suffix('.tmp')
	temporary.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding='utf-8')
	temporary.replace(ledger_path)
	return entries


def format_daily_summary(entries: list[dict], executed_at: str, names: list[str], days: int = 10) -> str:
	end = datetime.fromisoformat(executed_at).date()
	lines = [
		f'## 最近 {days} 天到账记录（北京时间）',
		'',
		'当天任一轮观察到额度增加即记为“有增加”；之后的零变化或使用消耗不会覆盖它。',
		'跨日区间的增加单独标注，无法确定实际到账日期。无记录不等于没到账；今天尚未结束。',
		'',
		'| 日期 | 账号 | 当日额度增加 | 当日最近余额 | 最近读取时间 |',
		'| --- | --- | --- | ---: | --- |',
	]
	for offset in range(days - 1, -1, -1):
		day = (end - timedelta(days=offset)).isoformat()
		for name in dict.fromkeys(names):
			observations = [
				(entry['executed_at'], detail)
				for entry in sorted(entries, key=lambda item: item['executed_at'])
				if entry['executed_at'][:10] == day
				for detail in entry['details']
				if detail.get('name') == name
			]
			positive = False
			cross_day = False
			measured = False
			latest = None
			for stamp, detail in observations:
				reward = detail.get('check_in_reward')
				since = detail.get('since_previous')
				previous_day = (detail.get('previous_checked_at') or '')[:10]
				positive |= (reward or 0) > 0 or ((since or 0) > 0 and previous_day == day)
				cross_day |= (since or 0) > 0 and previous_day != day
				measured |= reward is not None or since is not None
				if detail.get('after_quota') is not None:
					latest = stamp, detail['after_quota']
			status = (
				'有增加'
				if positive
				else ('跨日区间有增加，日期待确认' if cross_day else ('未观察到增加' if measured else '无可核对记录'))
			)
			balance = f'${latest[1]:.2f}' if latest else '未读取'
			stamp = latest[0][11:] if latest else '—'
			safe_name = name.replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')
			lines.append(f'| {day} | {safe_name} | {status} | {balance} | {stamp} |')
	return '\n'.join(lines)
