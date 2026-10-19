"""mlgb7 适配器：用 MockTransport 覆盖各分支，不碰真实站点。

接口形状都取自对 image.mlgb7.com 的实测：
  GET  /api/me           -> {checked_in_today, checkin_days, points, username}
  POST /api/me/checkin    -> {awarded, reward, points, checked_in_today}
  GET  /api/me/credits    -> {items:[{kind:"daily_checkin", delta:125, created_at}]}
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import checkin
from utils import mlgb
from utils.config import AccountConfig, ProviderConfig
from utils.mlgb import MlgbResult

DOMAIN = 'https://image.mlgb7.com'
BEIJING = timezone(timedelta(hours=8))


def today_at_10am() -> str:
	return datetime.now(BEIJING).replace(hour=10, minute=0, second=0, microsecond=0).isoformat()


def install_transport(monkeypatch, handler):
	"""把适配器的 httpx.Client 换成 MockTransport，并记录每个请求。"""
	seen = []

	def recording_handler(request):
		seen.append((request.method, request.url.path))
		return handler(request)

	def factory(domain, proxy_url):
		return httpx.Client(transport=httpx.MockTransport(recording_handler), timeout=5.0)

	monkeypatch.setattr(mlgb, '_client', factory)
	return seen


def test_already_checked_in_does_not_post(monkeypatch):
	"""今天已签到就不要再调签到接口 —— 免得无谓地打服务端。"""

	def handler(request):
		if request.url.path == '/api/me':
			return httpx.Response(
				200,
				json={'checked_in_today': True, 'checkin_days': 31, 'points': 4257, 'username': 'Owenkeye'},
			)
		if request.url.path == '/api/me/credits':
			return httpx.Response(
				200, json={'items': [{'kind': 'daily_checkin', 'delta': 125, 'created_at': today_at_10am()}]}
			)
		return httpx.Response(404)

	seen = install_transport(monkeypatch, handler)
	result = mlgb.run_check_in(DOMAIN, {'chatgpt2api_session': 'x'})

	assert result.checked_in is True
	assert result.awarded is False
	assert result.checkin_days == 31
	assert result.points == 4257
	assert result.today_award == 125
	assert ('POST', '/api/me/checkin') not in seen


def test_performs_checkin_when_not_checked_in(monkeypatch):
	state = {'checked': False, 'points': 100.0}

	def handler(request):
		if request.url.path == '/api/me':
			return httpx.Response(
				200,
				json={
					'checked_in_today': state['checked'],
					'checkin_days': 1,
					'points': state['points'],
					'username': 'u',
				},
			)
		if request.url.path == '/api/me/checkin':
			state['checked'] = True
			state['points'] = 225.0
			return httpx.Response(200, json={'awarded': True, 'reward': 125, 'points': 225, 'checked_in_today': True})
		if request.url.path == '/api/me/credits':
			return httpx.Response(
				200, json={'items': [{'kind': 'daily_checkin', 'delta': 125, 'created_at': today_at_10am()}]}
			)
		return httpx.Response(404)

	seen = install_transport(monkeypatch, handler)
	result = mlgb.run_check_in(DOMAIN, {'chatgpt2api_session': 'x'})

	assert result.awarded is True
	assert result.reward == 125
	assert result.checked_in is True
	assert result.points == 225
	assert result.today_award == 125
	assert ('POST', '/api/me/checkin') in seen
	# 签到后必须复查一次，取服务端的权威状态
	assert sum(1 for method, path in seen if path == '/api/me') == 2


def test_expired_cookie_raises_clear_error(monkeypatch):
	install_transport(monkeypatch, lambda request: httpx.Response(401))

	with pytest.raises(mlgb.MlgbError, match='cookie'):
		mlgb.run_check_in(DOMAIN, {'chatgpt2api_session': 'stale'})


def test_checked_in_still_false_after_post_is_reported(monkeypatch):
	"""调用签到后服务端仍说没签 —— 不能当成成功。"""

	def handler(request):
		if request.url.path == '/api/me':
			return httpx.Response(200, json={'checked_in_today': False, 'checkin_days': 0, 'points': 0})
		if request.url.path == '/api/me/checkin':
			return httpx.Response(200, json={'awarded': False, 'reward': 0, 'checked_in_today': False})
		return httpx.Response(200, json={'items': []})

	install_transport(monkeypatch, handler)
	result = mlgb.run_check_in(DOMAIN, {'chatgpt2api_session': 'x'})

	assert result.checked_in is False
	assert result.today_award is None


def test_today_award_ignores_other_days_and_kinds(monkeypatch):
	now = datetime(2026, 10, 7, 12, 0, tzinfo=BEIJING)

	def handler(request):
		return httpx.Response(
			200,
			json={
				'items': [
					{'kind': 'image_reserve', 'delta': -10, 'created_at': '2026-10-07T03:14:19+00:00'},
					{'kind': 'daily_checkin', 'delta': 125, 'created_at': '2026-10-07T02:29:16+00:00'},
					{'kind': 'daily_checkin', 'delta': 999, 'created_at': '2026-10-06T02:29:16+00:00'},
				]
			},
		)

	install_transport(monkeypatch, handler)
	with httpx.Client(transport=httpx.MockTransport(handler)) as client:
		assert mlgb.today_checkin_award(client, DOMAIN, now=now) == 125


def test_today_award_returns_none_when_absent(monkeypatch):
	now = datetime(2026, 10, 7, 12, 0, tzinfo=BEIJING)
	handler = lambda request: httpx.Response(  # noqa: E731
		200, json={'items': [{'kind': 'daily_checkin', 'delta': 125, 'created_at': '2026-10-06T02:29:16+00:00'}]}
	)

	install_transport(monkeypatch, handler)
	with httpx.Client(transport=httpx.MockTransport(handler)) as client:
		assert mlgb.today_checkin_award(client, DOMAIN, now=now) is None


def test_today_award_tolerates_broken_ledger(monkeypatch):
	now = datetime(2026, 10, 7, 12, 0, tzinfo=BEIJING)

	for payload in ({'items': 'nope'}, {}, {'items': [{'kind': 'daily_checkin', 'created_at': 'not-a-date'}]}):
		handler = lambda request, p=payload: httpx.Response(200, json=p)  # noqa: E731
		install_transport(monkeypatch, handler)
		with httpx.Client(transport=httpx.MockTransport(handler)) as client:
			assert mlgb.today_checkin_award(client, DOMAIN, now=now) is None


# --- 通知那一行的两种形态（回归锁定） -------------------------------------------
#
# 同一天重复跑到时必须是「✅ 今日已签到」，首次签到才是「✅ 签到成功」，
# 两种情况后面都跟着同样的「+N 积分 · 累计签到 N 天 · 余额 N」。
# 这两种形态都已在真实推送里出现过。


def mlgb_provider() -> ProviderConfig:
	return ProviderConfig(
		name='mlgb7',
		domain=DOMAIN,
		login_path='',
		sign_in_path='/api/me/checkin',
		user_info_path='/api/me',
		api_user_key='',
		adapter='mlgb7',
	)


def mlgb_account() -> AccountConfig:
	return AccountConfig(cookies={'chatgpt2api_session': 'x'}, provider='mlgb7', name='mlgb7')


def render(monkeypatch, result: MlgbResult) -> str:
	monkeypatch.setattr(checkin, 'mlgb_check_in', lambda *args, **kwargs: result)
	outcome, _before, _after = checkin.run_mlgb_account('mlgb7', mlgb_account(), mlgb_provider())
	return checkin.format_account_line('mlgb7', outcome)


def test_first_checkin_of_the_day_renders_success(monkeypatch):
	line = render(
		monkeypatch,
		MlgbResult(
			checked_in=True,
			points=4228,
			checkin_days=32,
			awarded=True,
			reward=181,
			today_award=181,
		),
	)

	assert line == 'mlgb7 · ✅ 签到成功 · +181 积分 · 累计签到 32 天 · 余额 4228'


def test_repeat_checkin_same_day_renders_already_checked(monkeypatch):
	"""今天已经签过：状态换成「今日已签到」，但金额/天数/余额照旧带上。"""
	line = render(
		monkeypatch,
		MlgbResult(
			checked_in=True,
			points=4228,
			checkin_days=32,
			awarded=False,
			reward=0,
			today_award=181,  # 金额取自当天积分明细，重复跑也有
		),
	)

	assert line == 'mlgb7 · ✅ 今日已签到 · +181 积分 · 累计签到 32 天 · 余额 4228'


def test_repeat_checkin_without_ledger_entry_omits_the_amount(monkeypatch):
	"""拿不到当天金额时只省略那段，不谎报成 +0。"""
	line = render(
		monkeypatch,
		MlgbResult(checked_in=True, points=4228, checkin_days=32, awarded=False, today_award=None),
	)

	assert line == 'mlgb7 · ✅ 今日已签到 · 累计签到 32 天 · 余额 4228'
