import re
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import checkin
from checkin import (
	compute_day_gain,
	generate_balance_hash,
	resolve_baseline,
)

# --- 余额 hash（原有） ------------------------------------------------------------


def test_balance_hash_changes_when_quota_changes():
	before = {'account_1': {'quota': 100.0, 'used': 20.0}}
	after = {'account_1': {'quota': 125.0, 'used': 20.0}}

	assert generate_balance_hash(before) != generate_balance_hash(after)


def test_balance_hash_changes_when_used_quota_changes():
	before = {'account_1': {'quota': 100.0, 'used': 20.0}}
	after = {'account_1': {'quota': 100.0, 'used': 21.0}}

	assert generate_balance_hash(before) != generate_balance_hash(after)


def test_balance_hash_is_stable_for_equivalent_balances():
	left = {
		'account_2': {'quota': 50.0, 'used': 1.0},
		'account_1': {'quota': 100.0, 'used': 20.0},
	}
	right = {
		'account_1': {'used': 20.0, 'quota': 100.0},
		'account_2': {'used': 1.0, 'quota': 50.0},
	}

	assert generate_balance_hash(left) == generate_balance_hash(right)


# --- 时区 / 签到日 ------------------------------------------------------------------


def test_tz_offset_hours_defaults_to_beijing(monkeypatch):
	monkeypatch.delenv('CHECKIN_TZ_OFFSET', raising=False)
	assert checkin.tz_offset_hours() == 8


def test_tz_offset_hours_can_be_overridden(monkeypatch):
	monkeypatch.setenv('CHECKIN_TZ_OFFSET', '0')
	assert checkin.tz_offset_hours() == 0

	monkeypatch.setenv('CHECKIN_TZ_OFFSET', '-5')
	assert checkin.tz_offset_hours() == -5


def test_tz_offset_hours_tolerates_invalid(monkeypatch):
	monkeypatch.setenv('CHECKIN_TZ_OFFSET', 'beijing')
	assert checkin.tz_offset_hours() == 8


def test_tz_label(monkeypatch):
	monkeypatch.setenv('CHECKIN_TZ_OFFSET', '8')
	assert checkin.tz_label() == '北京时间'

	monkeypatch.setenv('CHECKIN_TZ_OFFSET', '0')
	assert checkin.tz_label() == 'UTC+0'


def test_local_now_is_really_in_the_target_zone(monkeypatch):
	"""local_now() 必须是"真正的 UTC+8"，而不是伪装成 UTC 的偏移量。"""
	from datetime import datetime, timezone

	monkeypatch.setenv('CHECKIN_TZ_OFFSET', '8')
	local = checkin.local_now()

	assert local.utcoffset().total_seconds() / 3600 == 8
	# 与真实 UTC 的挂钟时间相差 8 小时
	wall_delta = local.replace(tzinfo=None) - datetime.now(timezone.utc).replace(tzinfo=None)
	assert 7.9 < wall_delta.total_seconds() / 3600 < 8.1


def test_current_day_follows_local_now(monkeypatch):
	"""北京 0 点切日：签到日必须跟北京日期一致，而不是 runner 的 UTC 日期。"""
	monkeypatch.setenv('CHECKIN_TZ_OFFSET', '8')

	assert checkin.current_day() == checkin.local_now().strftime('%Y-%m-%d')


def test_current_day_returns_iso_date(monkeypatch):
	monkeypatch.setenv('CHECKIN_TZ_OFFSET', '8')

	assert re.fullmatch(r'\d{4}-\d{2}-\d{2}', checkin.current_day())


def test_current_day_tolerates_invalid_offset(monkeypatch):
	monkeypatch.setenv('CHECKIN_TZ_OFFSET', 'nonsense')

	assert re.fullmatch(r'\d{4}-\d{2}-\d{2}', checkin.current_day())


def test_current_day_offset_ordering(monkeypatch):
	"""UTC-12 的日期不可能晚于 UTC+14 的日期（跨度 26 小时）。"""
	monkeypatch.setenv('CHECKIN_TZ_OFFSET', '-12')
	west = checkin.current_day()
	monkeypatch.setenv('CHECKIN_TZ_OFFSET', '14')
	east = checkin.current_day()

	assert west <= east


# --- 基准解析 resolve_baseline -----------------------------------------------------


def test_resolve_baseline_without_state():
	assert resolve_baseline(None, '2026-09-23') == (None, None)
	assert resolve_baseline({}, '2026-09-23') == (None, None)


def test_resolve_baseline_same_day_keeps_established_baseline():
	"""同一天内必须沿用已建立的基准，否则"今日累计"会被后续运行冲掉。"""
	stored = {
		'day': '2026-09-23',
		'baseline_total': 1000.0,
		'baseline_day': '2026-09-22',
		'last_total': 1025.0,
	}

	assert resolve_baseline(stored, '2026-09-23') == (1000.0, '2026-09-22')


def test_resolve_baseline_new_day_uses_previous_close():
	"""跨天时用上一次观测值（前一天的收尾总量）作为新基准。"""
	stored = {
		'day': '2026-09-22',
		'baseline_total': 975.0,
		'baseline_day': '2026-09-21',
		'last_total': 1000.0,
	}

	assert resolve_baseline(stored, '2026-09-23') == (1000.0, '2026-09-22')


# --- 日增量 compute_day_gain -------------------------------------------------------


def test_compute_day_gain():
	assert compute_day_gain(1075.0, 1050.0) == 25.0
	assert compute_day_gain(1000.0, 1000.0) == 0.0


def test_compute_day_gain_is_neutral_to_consumption():
	"""消费让余额下降、累计消耗上升，总量不变 —— 所以日增量必须是 0。"""
	quota_before, used_before = 540.45, 1834.55
	consumed = 47.07
	quota_after = quota_before - consumed
	used_after = used_before + consumed

	total_before = quota_before + used_before
	total_after = quota_after + used_after

	assert total_after == total_before
	assert compute_day_gain(total_after, total_before) == 0.0


def test_compute_day_gain_without_baseline():
	assert compute_day_gain(1075.0, None) is None
	assert compute_day_gain(None, 1050.0) is None


def test_compute_day_gain_can_be_negative():
	"""平台若重置累计消耗，总量会倒退，如实反映而不是藏起来。"""
	assert compute_day_gain(900.0, 1000.0) == -100.0


# --- 状态文件读写 ------------------------------------------------------------------


def test_state_round_trip(monkeypatch, tmp_path):
	state_file = tmp_path / 'checkin_state.json'
	monkeypatch.setattr(checkin, 'CHECKIN_STATE_FILE', str(state_file))

	assert checkin.load_checkin_state() == {}

	accounts = {
		'any主帐号': {
			'day': '2026-09-23',
			'baseline_total': 1000.0,
			'baseline_day': '2026-09-22',
			'last_total': 1025.0,
		}
	}
	checkin.save_checkin_state(accounts)

	assert checkin.load_checkin_state() == accounts


def test_state_load_tolerates_corrupt_file(monkeypatch, tmp_path):
	state_file = tmp_path / 'checkin_state.json'
	state_file.write_text('{not json', encoding='utf-8')
	monkeypatch.setattr(checkin, 'CHECKIN_STATE_FILE', str(state_file))

	assert checkin.load_checkin_state() == {}


def test_state_load_tolerates_unexpected_shape(monkeypatch, tmp_path):
	state_file = tmp_path / 'checkin_state.json'
	state_file.write_text('[1, 2, 3]', encoding='utf-8')
	monkeypatch.setattr(checkin, 'CHECKIN_STATE_FILE', str(state_file))

	assert checkin.load_checkin_state() == {}
