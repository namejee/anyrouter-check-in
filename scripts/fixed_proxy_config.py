"""Convert a private fixed proxy URL to a local mihomo configuration."""

import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


def build_config(url: str, local_port: int) -> dict:
	try:
		parsed = urlsplit(url.strip())
		port = parsed.port
	except ValueError:
		raise ValueError('Invalid FIXED_PROXY_URL') from None
	if (
		parsed.scheme not in {'http', 'https', 'socks5', 'socks5h'}
		or not parsed.hostname
		or not port
		or parsed.path not in {'', '/'}
		or parsed.query
		or parsed.fragment
	):
		raise ValueError('FIXED_PROXY_URL must use http(s):// or socks5:// with a host and port')
	proxy = {
		'name': 'fixed',
		'type': 'socks5' if parsed.scheme.startswith('socks5') else 'http',
		'server': parsed.hostname,
		'port': port,
	}
	if parsed.username is not None:
		proxy['username'] = unquote(parsed.username)
		proxy['password'] = unquote(parsed.password or '')
	if parsed.scheme == 'https':
		proxy['tls'] = True
	return {
		'mixed-port': local_port,
		'bind-address': '127.0.0.1',
		'allow-lan': False,
		'ipv6': False,
		'mode': 'rule',
		'log-level': 'warning',
		'proxies': [proxy],
		'rules': ['MATCH,fixed'],
	}


if __name__ == '__main__':
	try:
		config = build_config(os.environ['FIXED_PROXY_URL'], int(os.environ.get('PROXY_PORT', '7890')))
	except (KeyError, ValueError) as error:
		print(f'[FAILED] Fixed proxy configuration: {error}', file=sys.stderr)
		sys.exit(1)
	path = Path(sys.argv[1])
	path.write_text(json.dumps(config), encoding='utf-8')
	path.chmod(0o600)
