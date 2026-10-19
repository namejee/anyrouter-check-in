"""过滤后的补签：某个槽位被 GitHub 丢掉时，别让那批账号空一整天。

真实案例：10-13 傍晚那次运行被 GitHub 丢弃（当天 18:00~24:00 一次都没跑），
于是 10-14 00:01 的 ag+mlgb7 签到整天空了 —— 而 03:12 那次落在早晨窗口、
只签 anyrouter，不会带上它们。
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from checkin import (
	already_done_this_run,
	force_checkin_enabled,
	processed_today,
	split_stale_accounts,
)
from utils.config import AccountConfig

TODAY = '2026-10-14'


def account(name: str, provider: str) -> AccountConfig:
	return AccountConfig(cookies={'session': 'x'}, provider=provider, name=name)


ANY = account('any主帐号', 'anyrouter')
AG_LD = account('agLD', 'agentrouter')
AG_GH = account('agGithub', 'agentrouter')
MLGB = account('mlgb7', 'mlgb7')


# --- processed_today ------------------------------------------------------------


def test_no_state_means_not_processed():
	assert processed_today({}, AG_LD, TODAY) is False


def test_same_day_counts_as_processed():
	state = {'agLD': {'day': TODAY, 'last_total': 100.0}}

	assert processed_today(state, AG_LD, TODAY) is True


def test_previous_day_is_not_processed():
	state = {'agLD': {'day': '2026-10-13', 'last_total': 100.0}}

	assert processed_today(state, AG_LD, TODAY) is False


def test_nameless_account_is_conservatively_not_processed():
	"""没有 name 就定位不到 state —— 宁可多跑一次也别漏签。"""
	nameless = AccountConfig(cookies={'session': 'x'}, provider='anyrouter')

	assert processed_today({'Account 1': {'day': TODAY}}, nameless, TODAY) is False


# --- split_stale_accounts -------------------------------------------------------


def test_stale_accounts_are_carried_for_catch_up():
	"""经典场景：当晚那次被丢，早晨那次只签 anyrouter，ag 两个号要被补上。"""
	state = {'any主帐号': {'day': TODAY}}  # 刚在早晨窗口签过

	carried, remaining = split_stale_accounts(state, [AG_LD, AG_GH], TODAY)

	assert [a.name for a in carried] == ['agLD', 'agGithub']
	assert remaining == []


def test_accounts_processed_today_are_left_out():
	state = {'agLD': {'day': TODAY}, 'agGithub': {'day': TODAY}}

	carried, remaining = split_stale_accounts(state, [AG_LD, AG_GH], TODAY)

	assert carried == []
	assert [a.name for a in remaining] == ['agLD', 'agGithub']


def test_mixed_state_is_split_correctly():
	state = {'agLD': {'day': TODAY}, 'agGithub': {'day': '2026-10-13'}}

	carried, remaining = split_stale_accounts(state, [AG_LD, AG_GH, MLGB], TODAY)

	assert [a.name for a in carried] == ['agGithub', 'mlgb7']
	assert [a.name for a in remaining] == ['agLD']


def test_no_skipped_accounts_is_a_noop():
	carried, remaining = split_stale_accounts({}, [], TODAY)

	assert carried == []
	assert remaining == []


def test_mlgb7_without_user_info_still_counts_once_day_is_recorded():
	"""mlgb7 不返回 user_info，靠主流程在成功时补写 day，才不会每次都被补签。"""
	state = {'mlgb7': {'day': TODAY}}

	assert processed_today(state, MLGB, TODAY) is True
	assert split_stale_accounts(state, [MLGB], TODAY) == ([], [MLGB])


# --- already_done_this_run：兜底轮不重复签今天已到账的账号 -----------------------


def test_skips_account_already_credited_today():
	"""真实案例：10-17 00:01 已到账的 agLD/agGithub/mlgb7，被 00:32 的兜底轮又跑了一遍，
	agLD/agGithub 全被挡成「邮箱密码登录失败」—— 这轮本该直接跳过它们。"""
	state = {
		'agLD': {'day': TODAY, 'credited_day': TODAY},
		'agGithub': {'day': TODAY, 'credited_day': TODAY},
		'mlgb7': {'day': TODAY, 'credited_day': TODAY},
	}

	assert [already_done_this_run(state, a, TODAY) for a in (AG_LD, AG_GH, MLGB)] == [True] * 3


def test_logged_in_but_not_credited_is_retried():
	"""anyrouter 北京 08:05 之后登录才赠送：00:32 那次只能拿到「未到账」，
	若按「今天跑过」就跳过，早晨 08:01 那轮会把当天额度整天空掉。"""
	state = {'any主帐号': {'day': TODAY, 'baseline_total': 100.0, 'last_total': 100.0}}

	assert already_done_this_run(state, ANY, TODAY) is False


def test_failed_account_is_retried_in_same_day():
	"""失败既不写 day 也不写 credited_day，所以同一天后面那轮还会再试一次。"""
	state = {'agLD': {'day': '2026-10-13', 'last_total': 100.0}}

	assert already_done_this_run(state, AG_LD, TODAY) is False


def test_yesterday_credit_does_not_skip_today():
	state = {'agLD': {'day': '2026-10-13', 'credited_day': '2026-10-13'}}

	assert already_done_this_run(state, AG_LD, TODAY) is False


def test_force_flag_overrides_skip():
	state = {'agLD': {'day': TODAY, 'credited_day': TODAY}}

	assert already_done_this_run(state, AG_LD, TODAY, relogin_allowed=True) is False


def test_force_flag_off_by_default(monkeypatch):
	monkeypatch.delenv('CHECKIN_FORCE', raising=False)
	assert force_checkin_enabled() is False

	for raw in ('1', 'true', 'TRUE', 'yes'):
		monkeypatch.setenv('CHECKIN_FORCE', raw)
		assert force_checkin_enabled() is True

	for raw in ('0', 'false', '', 'no'):
		monkeypatch.setenv('CHECKIN_FORCE', raw)
		assert force_checkin_enabled() is False
