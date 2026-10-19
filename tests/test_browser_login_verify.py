"""verify_browser_login 的重试行为。

真实故障（到达 /console 但 /api/user/self 没返回）无法按需复现，所以用桩把
三个分支钉死：重试后成功 / 重试耗尽 / 停在登录页直接放弃。
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from utils import browser


class FakePage:
	def __init__(self, url='https://agentrouter.org/console'):
		self.url = url


def _stub_capture(monkeypatch, results):
	"""让 _capture_user_profile 依次返回 results 里的值，并记录调用次数。"""
	calls = []

	async def fake_capture(page, console_url, timeout_ms):
		calls.append(page.url)
		index = min(len(calls) - 1, len(results) - 1)
		return results[index]

	monkeypatch.setattr(browser, '_capture_user_profile', fake_capture)
	monkeypatch.setattr(browser, 'VERIFY_RETRY_DELAY_SECONDS', 0)
	return calls


async def test_retries_and_succeeds_when_console_reached(monkeypatch):
	"""到达 /console 说明登录已成功，接口抖一下必须重试而不是判定失败。"""
	profile = {'id': 42, 'username': 'agLD'}
	calls = _stub_capture(monkeypatch, [None, None, profile])

	result = await browser.verify_browser_login(FakePage(), 'https://agentrouter.org/console', 1000)

	assert result == profile
	assert len(calls) == 3


async def test_gives_up_after_max_attempts(monkeypatch):
	calls = _stub_capture(monkeypatch, [None])

	result = await browser.verify_browser_login(FakePage(), 'https://agentrouter.org/console', 1000)

	assert result is None
	assert len(calls) == browser.VERIFY_ATTEMPTS


async def test_does_not_retry_when_still_on_login_page(monkeypatch):
	"""停在登录页 = 真的没登录进去，重试没有意义，不该白等。"""
	calls = _stub_capture(monkeypatch, [None])

	page = FakePage('https://agentrouter.org/login')
	result = await browser.verify_browser_login(page, 'https://agentrouter.org/console', 1000)

	assert result is None
	assert len(calls) == 1


async def test_no_retry_needed_on_first_success(monkeypatch):
	profile = {'id': 7}
	calls = _stub_capture(monkeypatch, [profile])

	result = await browser.verify_browser_login(FakePage(), 'https://agentrouter.org/console', 1000)

	assert result == profile
	assert len(calls) == 1
