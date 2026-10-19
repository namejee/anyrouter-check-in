#!/usr/bin/env python3
"""噜皮生图（image.mlgb7.com）签到适配。

这个站不是 NewAPI/OneAPI，接口和字段都不同：

- 登录走 Linux DO 授权（`auth_source: "linuxdo"`），**没有站内密码**，
  所以只能复用浏览器的会话 cookie，无法像其它站那样用账号密码登录
- 用户信息在 `GET /api/me`：`checked_in_today` / `checkin_days` / `points`
- 签到是 `POST /api/me/checkin`，返回 `{awarded, reward, points, checked_in_today}`
- 积分明细 `GET /api/me/credits` 里有 `kind == "daily_checkin"` 的记录，
  它的 `delta` 就是当天签到实际到账的积分

以上形状都是对真实站点实测确认过的。签到金额以积分明细为准，不依赖
`checkin` 的返回值 —— 那样即使服务端返回体变了也不会误报。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

USER_AGENT = (
	'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36'
)
CHECKIN_LEDGER_KIND = 'daily_checkin'
LEDGER_SCAN = 20
BEIJING = timezone(timedelta(hours=8))


class MlgbError(RuntimeError):
	"""适配器内部错误：网络、鉴权或响应格式。"""


@dataclass
class MlgbResult:
	"""一次 mlgb7 签到的结果。"""

	checked_in: bool  # 今天是否已完成签到（来自服务端字段）
	points: float = 0.0
	checkin_days: int = 0
	username: str = ''
	awarded: bool = False  # 本次调用是否真的发放
	reward: float = 0.0  # 服务端返回的本次发放积分
	today_award: float | None = None  # 今天签到实际到账（来自积分明细）


def _to_float(value: Any, default: float = 0.0) -> float:
	try:
		return float(value)
	except (TypeError, ValueError):
		return default


def _to_int(value: Any, default: int = 0) -> int:
	try:
		return int(value)
	except (TypeError, ValueError):
		return default


def _client(domain: str, proxy_url: str | None) -> httpx.Client:
	kwargs: dict[str, Any] = {
		'timeout': 25.0,
		'follow_redirects': True,
		'headers': {
			'User-Agent': USER_AGENT,
			'Accept': 'application/json, text/plain, */*',
		},
	}
	if proxy_url:
		kwargs['proxy'] = proxy_url
	return httpx.Client(**kwargs)


def _check_auth(response: httpx.Response) -> None:
	if response.status_code == 401:
		raise MlgbError('会话 cookie 已失效（HTTP 401），需要重新获取')
	if response.status_code >= 400:
		raise MlgbError(f'HTTP {response.status_code}')


def fetch_me(client: httpx.Client, domain: str) -> dict:
	"""GET /api/me —— 用户信息与今天的签到状态。"""
	try:
		response = client.get(f'{domain}/api/me')
	except httpx.HTTPError as exc:
		raise MlgbError(f'请求用户信息失败: {exc}') from exc
	_check_auth(response)

	try:
		payload = response.json()
	except ValueError as exc:
		raise MlgbError('用户信息不是 JSON') from exc
	if not isinstance(payload, dict):
		raise MlgbError('用户信息格式异常')
	return payload


def post_checkin(client: httpx.Client, domain: str) -> dict:
	"""POST /api/me/checkin —— 该接口不需要请求体，也不需要 CSRF 头。"""
	try:
		response = client.post(f'{domain}/api/me/checkin')
	except httpx.HTTPError as exc:
		raise MlgbError(f'签到请求失败: {exc}') from exc
	_check_auth(response)

	try:
		payload = response.json()
	except ValueError:
		return {}
	return payload if isinstance(payload, dict) else {}


def today_checkin_award(client: httpx.Client, domain: str, *, now: datetime | None = None) -> float | None:
	"""从积分明细里取**今天**签到实际到账的积分；没有今天的记录就返回 None。"""
	try:
		response = client.get(f'{domain}/api/me/credits', params={'limit': LEDGER_SCAN, 'offset': 0})
	except httpx.HTTPError:
		return None
	if response.status_code != 200:
		return None

	try:
		payload = response.json()
	except ValueError:
		return None

	items = payload.get('items') if isinstance(payload, dict) else None
	if not isinstance(items, list):
		return None

	today = (now or datetime.now(BEIJING)).astimezone(BEIJING).strftime('%Y-%m-%d')
	for item in items:
		if not isinstance(item, dict) or item.get('kind') != CHECKIN_LEDGER_KIND:
			continue

		created_at = item.get('created_at')
		if not isinstance(created_at, str):
			continue
		try:
			when = datetime.fromisoformat(created_at)
		except ValueError:
			continue
		if when.tzinfo is None:
			when = when.replace(tzinfo=timezone.utc)
		if when.astimezone(BEIJING).strftime('%Y-%m-%d') != today:
			continue

		delta = item.get('delta')
		if isinstance(delta, (int, float)):
			return float(delta)

	return None


def run_check_in(domain: str, cookies: dict, *, proxy_url: str | None = None) -> MlgbResult:
	"""查状态 → 需要就签到 → 复查 → 读今天的到账金额。"""
	with _client(domain, proxy_url) as client:
		client.cookies.update(cookies)

		me = fetch_me(client, domain)
		result = MlgbResult(
			checked_in=bool(me.get('checked_in_today')),
			points=_to_float(me.get('points')),
			checkin_days=_to_int(me.get('checkin_days')),
			username=str(me.get('username') or ''),
		)

		if not result.checked_in:
			payload = post_checkin(client, domain)
			result.awarded = bool(payload.get('awarded'))
			result.reward = _to_float(payload.get('reward'))

			# 复查一次，服务端返回体将来变了也不至于误判
			me_after = fetch_me(client, domain)
			result.checked_in = bool(me_after.get('checked_in_today', result.checked_in))
			result.points = _to_float(me_after.get('points'), result.points)
			result.checkin_days = _to_int(me_after.get('checkin_days'), result.checkin_days)

		result.today_award = today_checkin_award(client, domain)
		return result
