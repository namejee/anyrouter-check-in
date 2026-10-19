"""align 的对齐计划：把运行落到北京 00:01 / 08:01。"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from utils import align
from utils.align import (
	EVENING_PROVIDERS,
	MORNING_PROVIDERS,
	plan,
	sleep_with_heartbeat,
)

BJ = timezone(timedelta(hours=8))


def at(hour: int, minute: int = 0, day: int = 8) -> datetime:
	return datetime(2026, 10, day, hour, minute, tzinfo=BJ)


# --- 两个目标窗口 ---------------------------------------------------------------


def test_evening_run_targets_next_midnight_for_ag_and_mlgb7():
	"""UTC 12:10 槽实测落在北京 ~20:15 —— 睡到次日 00:01 再签 ag+mlgb7。"""
	result = plan(at(20, 15))

	assert result.providers == EVENING_PROVIDERS
	assert result.target == at(0, 1, day=9)
	assert result.sleep_seconds == 3 * 3600 + 46 * 60


def test_early_morning_run_targets_same_day_morning_for_anyrouter():
	"""UTC 16:10 槽实测落在北京 ~03:30 —— 睡到当日 08:01 再签 anyrouter。"""
	result = plan(at(3, 30))

	assert result.providers == MORNING_PROVIDERS
	assert result.target == at(8, 1, day=8)
	assert result.sleep_seconds == 4 * 3600 + 31 * 60


def test_daytime_runs_are_fallbacks():
	"""北京 08:00~18:00 落地的槽（比如 UTC 00:10 槽落在 13:30）不睡觉，跑全部账号。"""
	for hour in (8, 10, 13, 15, 17):
		result = plan(at(hour, 30))
		assert result.providers == '', f'{hour}:30 应该是兜底'
		assert result.sleep_seconds == 0
		assert result.target is None


# --- 边界 -----------------------------------------------------------------------


def test_window_boundaries():
	# 晚间窗口从 18:00 开始，但距 00:01 超过睡眠上限时退回兜底（不能睡到逼近 6h job 上限）
	assert plan(at(17, 59)).providers == ''
	assert plan(at(18, 0)).providers == ''  # 还有 6h01m，超限
	assert plan(at(19, 0)).providers == ''  # 还有 5h01m，仍超限
	assert plan(at(19, 1)).providers == EVENING_PROVIDERS  # 正好 5h，可以睡
	assert plan(at(23, 59)).providers == EVENING_PROVIDERS

	# 早晨窗口 [00:00, 08:00)
	assert plan(at(0, 0)).providers == ''  # 还有 8h01m，超限
	assert plan(at(0, 5)).providers == ''
	assert plan(at(3, 1)).providers == MORNING_PROVIDERS  # 正好 5h
	assert plan(at(7, 59)).providers == MORNING_PROVIDERS
	assert plan(at(8, 0)).providers == ''  # 已出窗口


def test_evening_run_just_before_midnight_sleeps_briefly():
	result = plan(at(23, 59))

	assert result.providers == EVENING_PROVIDERS
	assert result.sleep_seconds == 2 * 60


# --- 睡眠上限 -------------------------------------------------------------------


def test_too_far_from_target_falls_back():
	"""北京 00:05 距 08:01 还有近 8 小时，超过单 job 上限就别睡了。"""
	result = plan(at(0, 5), max_sleep_seconds=5 * 3600)

	assert result.providers == ''
	assert result.sleep_seconds == 0
	assert '超过上限' in result.note


def test_sleep_cap_edge():
	# 距 08:01 正好 5 小时 —— 可以睡
	result = plan(at(3, 1), max_sleep_seconds=5 * 3600)
	assert result.providers == MORNING_PROVIDERS
	assert result.sleep_seconds == 5 * 3600

	# 再多一分钟就超限，退回兜底
	result = plan(at(3, 0), max_sleep_seconds=5 * 3600 - 60)
	assert result.providers == ''
	assert result.sleep_seconds == 0


def test_utc_input_is_converted_correctly():
	"""main() 传进来的是 UTC，转换必须落到北京小时上。"""
	# 北京 2026-10-08 20:15 == UTC 2026-10-08 12:15
	utc = datetime(2026, 10, 8, 12, 15, tzinfo=timezone.utc)
	result = plan(utc)

	assert result.providers == EVENING_PROVIDERS
	assert result.target == at(0, 1, day=9)


# --- 心跳睡眠 -------------------------------------------------------------------


def test_sleep_with_heartbeat_returns_quickly_for_short_sleeps():
	sleep_with_heartbeat(0.05)  # 不该抛异常、不该真的睡很久
	sleep_with_heartbeat(0)  # 0 秒直接返回


# --- main() 的胶水：写 CHECKIN_PROVIDERS 并睡觉 ---------------------------------


def freeze_clock(monkeypatch, moment: datetime):
	"""把 align 模块里的 datetime.now() 冻结到指定时刻。"""

	class FrozenDatetime(datetime):
		@classmethod
		def now(cls, tz=None):  # noqa: ANN001
			return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

	monkeypatch.setattr(align, 'datetime', FrozenDatetime)


def prepare(monkeypatch, tmp_path):
	env_file = tmp_path / 'github_env'
	env_file.write_text('', encoding='utf-8')
	monkeypatch.setenv('GITHUB_ENV', str(env_file))
	monkeypatch.delenv('MANUAL_PROVIDERS', raising=False)
	monkeypatch.setenv('ALIGN_MAX_SLEEP_SECONDS', '18000')
	slept: list[float] = []
	monkeypatch.setattr(align, 'sleep_with_heartbeat', slept.append)
	return env_file, slept


def test_main_sleeps_and_scopes_to_ag_group(monkeypatch, tmp_path):
	"""北京 23:59 落地 -> 睡 2 分钟到次日 00:01，只签 ag+mlgb7。"""
	env_file, slept = prepare(monkeypatch, tmp_path)
	freeze_clock(monkeypatch, at(23, 59))

	assert align.main() == 0

	assert 'CHECKIN_PROVIDERS=agentrouter,mlgb7' in env_file.read_text(encoding='utf-8')
	assert slept == [120.0]


def test_main_sleeps_and_scopes_to_anyrouter(monkeypatch, tmp_path):
	"""北京 07:59 落地 -> 睡 2 分钟到当日 08:01，只签 anyrouter。"""
	env_file, slept = prepare(monkeypatch, tmp_path)
	freeze_clock(monkeypatch, at(7, 59))

	assert align.main() == 0

	assert 'CHECKIN_PROVIDERS=anyrouter' in env_file.read_text(encoding='utf-8')
	assert slept == [120.0]


def test_main_fallback_sleeps_not_and_writes_nothing(monkeypatch, tmp_path):
	"""北京 13:30 落地（比如 UTC 00:10 槽）-> 不睡也不限制，跑全部账号。"""
	env_file, slept = prepare(monkeypatch, tmp_path)
	freeze_clock(monkeypatch, at(13, 30))

	assert align.main() == 0

	assert 'CHECKIN_PROVIDERS' not in env_file.read_text(encoding='utf-8')
	assert slept == []


def test_main_honours_manual_providers_without_sleeping(monkeypatch, tmp_path):
	"""手动触发指定了 providers 时不参与对齐。"""
	env_file, slept = prepare(monkeypatch, tmp_path)
	monkeypatch.setenv('MANUAL_PROVIDERS', 'anyrouter')
	freeze_clock(monkeypatch, at(23, 59))  # 即便落在对齐窗口也不睡

	assert align.main() == 0

	assert 'CHECKIN_PROVIDERS=anyrouter' in env_file.read_text(encoding='utf-8')
	assert slept == []


def test_main_without_github_env_does_not_crash(monkeypatch, tmp_path):
	"""本地跑没有 $GITHUB_ENV，只打日志、不写文件、不报错。"""
	_, slept = prepare(monkeypatch, tmp_path)
	monkeypatch.delenv('GITHUB_ENV', raising=False)
	freeze_clock(monkeypatch, at(23, 59))

	assert align.main() == 0
	assert slept == [120.0]
