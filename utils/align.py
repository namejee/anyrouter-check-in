#!/usr/bin/env python3
"""把一次运行对齐到北京时间的签到目标时刻。

GitHub 的 schedule 会晚几小时（实测 0~5.5 小时，且随槽位变化），所以没法直接把
cron 排到 00:01。反过来做：让运行**提前**落地，起来后先睡到目标时刻再签到 ——
这样落点就是精确的。

按运行开始时的北京小时分派（落点依据 workflow 注释里的实测）：

  [18:00, 24:00)  目标 = 次日 00:01，只签 agentrouter + mlgb7
  [00:00, 08:00)  目标 = 当日 08:01，只签 anyrouter
  其余时段        不对齐，处理全部账号（兜底）

距目标超过 ALIGN_MAX_SLEEP_SECONDS（默认 5 小时）时也退回兜底：与其睡到逼近单
job 的 6 小时上限，不如照常跑一遍，把准点让给别的槽位。
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
DEFAULT_MAX_SLEEP_SECONDS = 5 * 60 * 60
HEARTBEAT_SECONDS = 600

EVENING_START_HOUR = 18  # 之后进入"次日 00:01"窗口
MORNING_START_HOUR = 8  # 之前处于"当日 08:01"窗口

EVENING_PROVIDERS = 'agentrouter,mlgb7'
MORNING_PROVIDERS = 'anyrouter'


@dataclass
class Alignment:
	"""一次运行该怎么对齐。"""

	providers: str = ''  # 写进 CHECKIN_PROVIDERS；空 = 不限制
	target: datetime | None = None  # 对齐目标（北京时间）
	sleep_seconds: float = 0.0
	note: str = ''


def plan(now: datetime, *, max_sleep_seconds: float = DEFAULT_MAX_SLEEP_SECONDS) -> Alignment:
	"""根据运行开始时刻决定目标时刻与账号范围。"""
	local = now.astimezone(BEIJING)

	if local.hour >= EVENING_START_HOUR:
		target = (local + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
		providers = EVENING_PROVIDERS
	elif local.hour < MORNING_START_HOUR:
		target = local.replace(hour=8, minute=1, second=0, microsecond=0)
		providers = MORNING_PROVIDERS
	else:
		return Alignment(note='不在对齐时段，按兜底处理全部账号')

	remaining = (target - local).total_seconds()
	if remaining <= 0:
		return Alignment(providers=providers, target=target, note='已过目标时刻，直接执行')

	if remaining > max_sleep_seconds:
		return Alignment(
			target=target,
			note=f'距目标还有 {remaining / 3600:.1f} 小时，超过上限 {max_sleep_seconds / 3600:.1f} 小时，退回兜底',
		)

	return Alignment(providers=providers, target=target, sleep_seconds=remaining, note='')


def _write_env(name: str, value: str) -> None:
	"""把变量交给后续 step（GitHub Actions 的 $GITHUB_ENV 机制）。"""
	path = os.getenv('GITHUB_ENV', '').strip()
	if not path:
		print(f'[ALIGN] GITHUB_ENV 未设置（本地运行？），跳过写入 {name}')
		return
	with open(path, 'a', encoding='utf-8') as handle:
		handle.write(f'{name}={value}\n')
	print(f'[ALIGN] {name}={value}')


def sleep_with_heartbeat(seconds: float) -> None:
	"""分段睡眠并定期打日志，避免长时间无输出。"""
	remaining = float(seconds)
	while remaining > 0:
		chunk = min(remaining, HEARTBEAT_SECONDS)
		time.sleep(chunk)
		remaining -= chunk
		if remaining > 0:
			print(f'[ALIGN] 继续等待，还剩 {remaining / 60:.0f} 分钟')


def main(argv: list[str] | None = None) -> int:
	manual = (os.getenv('MANUAL_PROVIDERS') or '').strip()
	if manual:
		print(f'[ALIGN] 手动指定 providers={manual}，跳过对齐')
		_write_env('CHECKIN_PROVIDERS', manual)
		return 0

	max_sleep = float(os.getenv('ALIGN_MAX_SLEEP_SECONDS', str(DEFAULT_MAX_SLEEP_SECONDS)))
	result = plan(datetime.now(timezone.utc), max_sleep_seconds=max_sleep)

	if result.note:
		print(f'[ALIGN] {result.note}')

	if result.sleep_seconds > 0 and result.target is not None:
		print(
			f'[ALIGN] 现在睡 {result.sleep_seconds / 60:.0f} 分钟，'
			f'到 {result.target.strftime("%m-%d %H:%M")} 北京时间再签到'
		)
		sleep_with_heartbeat(result.sleep_seconds)

	if result.providers:
		_write_env('CHECKIN_PROVIDERS', result.providers)
	else:
		print('[ALIGN] 本次不限制 provider，处理全部账号')

	return 0


if __name__ == '__main__':
	sys.exit(main(sys.argv[1:]))
