from unittest.mock import AsyncMock

import pytest

from scripts.fixed_proxy_config import build_config
from utils.browser import launch_browser_with_retry


def test_fixed_proxy_decodes_authentication_and_keeps_tls_enabled():
	config = build_config('https://user%40example:p%40ss%3Aword@proxy.example:443', 7890)
	assert config['proxies'] == [
		{
			'name': 'fixed',
			'type': 'http',
			'server': 'proxy.example',
			'port': 443,
			'username': 'user@example',
			'password': 'p@ss:word',
			'tls': True,
		}
	]
	assert config['allow-lan'] is False
	assert config['bind-address'] == '127.0.0.1'


def test_fixed_socks_proxy_accepts_ipv6():
	config = build_config('socks5://[2001:db8::1]:1080', 7890)
	assert config['proxies'][0]['type'] == 'socks5'
	assert config['proxies'][0]['server'] == '2001:db8::1'


@pytest.mark.parametrize(
	'url', ['ss://credential@example:443', 'http://host', 'http://host:invalid', 'http://host:443/path']
)
def test_invalid_proxy_does_not_expose_its_input(url):
	with pytest.raises(ValueError) as error:
		build_config(url, 7890)
	assert url not in str(error.value)


@pytest.mark.asyncio
async def test_browser_startup_crash_is_retried_without_retrying_login(monkeypatch):
	monkeypatch.setattr('utils.browser.asyncio.sleep', AsyncMock())
	browser = object()
	launcher = AsyncMock(side_effect=[RuntimeError('BrowserType.launch: SIGSEGV'), browser])
	assert await launch_browser_with_retry(launcher, headless=True) is browser
	assert launcher.await_count == 2


@pytest.mark.asyncio
async def test_browser_startup_retry_is_bounded(monkeypatch):
	monkeypatch.setattr('utils.browser.asyncio.sleep', AsyncMock())
	launcher = AsyncMock(side_effect=RuntimeError('Target page, context or browser has been closed'))
	with pytest.raises(RuntimeError):
		await launch_browser_with_retry(launcher)
	assert launcher.await_count == 3


@pytest.mark.asyncio
async def test_permanent_browser_configuration_error_is_not_retried():
	launcher = AsyncMock(side_effect=ValueError('invalid configuration'))
	with pytest.raises(ValueError):
		await launch_browser_with_retry(launcher)
	assert launcher.await_count == 1
