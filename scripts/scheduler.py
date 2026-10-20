"""Independent, bounded GitHub Actions dispatcher; contains no site credentials."""

import argparse
import fcntl
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

BEIJING = timezone(timedelta(hours=8))
MAX_ATTEMPTS = 3
COOLDOWN = timedelta(minutes=30)
MAX_LOG_BYTES = 8 * 1024 * 1024


class GitHubError(RuntimeError):
	def __init__(self, status):
		self.status = status
		super().__init__(f'GitHub API HTTP {status}')


class NoRedirect(HTTPRedirectHandler):
	def redirect_request(self, req, fp, code, msg, headers, newurl):
		return None


class GitHub:
	def __init__(self, token, repo):
		self.token = token
		self.base = f'https://api.github.com/repos/{repo}'

	def request(self, path, payload=None, raw=False):
		data = json.dumps(payload).encode() if payload is not None else None
		request = Request(
			self.base + path,
			data=data,
			headers={
				'Authorization': f'Bearer {self.token}',
				'Accept': 'application/vnd.github+json',
				'X-GitHub-Api-Version': '2026-03-10',
				'User-Agent': 'anyrouter-independent-scheduler',
				'Content-Type': 'application/json',
			},
		)
		try:
			response = build_opener(NoRedirect).open(request, timeout=30)
		except HTTPError as error:
			if raw and error.code == 302:
				location = error.headers.get('Location', '')
				if urlparse(location).scheme != 'https':
					raise RuntimeError('Unsafe GitHub log redirect') from None
				# Never forward the GitHub credential to log storage.
				response = urlopen(Request(location), timeout=30)
			else:
				raise GitHubError(error.code) from None
		with response:
			body = response.read(MAX_LOG_BYTES + 1)
		if len(body) > MAX_LOG_BYTES:
			raise RuntimeError('GitHub response exceeded size limit')
		return body.decode(errors='replace') if raw else (json.loads(body) if body else {})

	def runs(self, start):
		query = urlencode({'branch': 'main', 'per_page': 100, 'created': '>=' + start.isoformat()})
		return self.request('/actions/workflows/checkin.yml/runs?' + query)['workflow_runs']

	def health(self, run, expected):
		jobs = self.request(f'/actions/runs/{run["id"]}/jobs')['jobs']
		for job in jobs:
			if job['name'] == 'checkin':
				try:
					return log_health(self.request(f'/actions/jobs/{job["id"]}/logs', raw=True), expected)
				except GitHubError as error:
					if error.status != 404:
						raise
		return None

	def dispatch(self, workflow='checkin.yml'):
		inputs = {'providers': 'anyrouter,agentrouter', 'debug': 'false', 'force': 'false'}
		return self.request(
			f'/actions/workflows/{workflow}/dispatches',
			{'ref': 'main', 'inputs': inputs if workflow == 'checkin.yml' else {}},
		)


def log_health(raw, expected=4):
	"""Check actual account summaries; green alone is insufficient."""
	providers = {}
	summaries = {}
	skipped = set()
	failed = False
	for raw_line in raw.splitlines():
		match = re.match(r'^\d{4}-\d\d-\d\dT\S+Z (.*)$', raw_line)
		if not match:
			continue
		line = re.sub(r'\*\*\*(INFO|SKIP|FAILED)\*\*\*', r'[\1]', match[1])
		provider = re.match(r'\[INFO\] (.+): Using provider "([^"]+)"', line)
		if provider:
			providers[provider[1]] = provider[2]
		skip = re.match(r'\[SKIP\] (.+): already credited today, skipping this run', line)
		if skip:
			skipped.add(skip[1])
		if line.startswith('[FAILED]') or ' · ❌ ' in line:
			failed = True
		for name in providers:
			if line.startswith(name + ' · ') and '余额 $' in line and '总量 $' in line:
				summaries[name] = line
	missing = set(providers) - set(summaries)
	uncredited = 0
	for name, line in summaries.items():
		if providers[name] == 'anyrouter':
			gain = re.search(r' · \+\$([\d.]+)', line)
			if not gain or float(gain[1]) <= 0:
				uncredited += 1
	account_count = len(set(providers) | skipped)
	return {
		'healthy': account_count == expected and not missing and not uncredited and not failed,
		'accounts': account_count,
		'missing_summaries': len(missing),
		'anyrouter_uncredited': uncredited,
		'unconfirmed_baselines': sum('基线待建立' in line for line in summaries.values()),
	}


def parse_time(value):
	return datetime.fromisoformat(value.replace('Z', '+00:00'))


def decide(api, state, now, expected=4):
	"""Pure decision apart from read-only API requests; never dispatch here."""
	start = now.astimezone(BEIJING).replace(hour=8, minute=3, second=0, microsecond=0)
	if now < start:
		return {'action': 'wait', 'reason': 'before_daily_start'}
	if state.get('healthy'):
		return {'action': 'done', 'reason': 'verified_today', 'run_id': state.get('run_id')}
	runs = api.runs(start.astimezone(timezone.utc))
	active = False
	logs_pending = False
	for run in runs:
		if parse_time(run['created_at']) < start:
			continue
		if run.get('conclusion') == 'success' or run['status'] != 'completed':
			health = api.health(run, expected)
			if health and health['healthy']:
				return {'action': 'done', 'reason': 'four_accounts_verified', 'run_id': run['id'], **health}
			if run['status'] != 'completed':
				active = True
			elif health is None:
				logs_pending = True
	if active:
		return {'action': 'wait', 'reason': 'existing_run_active'}
	if logs_pending:
		return {'action': 'wait', 'reason': 'completed_run_logs_pending'}
	attempts = state.get('attempts', [])
	if len(attempts) >= MAX_ATTEMPTS:
		return {'action': 'attention', 'reason': 'daily_attempt_limit', 'attempts': len(attempts)}
	if attempts and now - parse_time(attempts[-1]) < COOLDOWN:
		return {'action': 'wait', 'reason': 'retry_cooldown'}
	return {'action': 'dispatch', 'reason': 'missing_or_unhealthy_daily_run', 'attempt': len(attempts) + 1}


def save_state(path, state):
	temporary = path.with_suffix('.tmp')
	temporary.write_text(json.dumps(state) + '\n')
	temporary.chmod(0o600)
	temporary.replace(path)


def token():
	credentials = os.environ.get('CREDENTIALS_DIRECTORY', '')
	path = Path(credentials) / 'github-token' if credentials else Path(os.environ['GITHUB_TOKEN_FILE'])
	return path.read_text().strip()


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument('--inspect', action='store_true', help='Read-only check; never dispatch or save health')
	parser.add_argument('--probe', action='store_true', help='Dispatch the credential-free connectivity workflow')
	args = parser.parse_args()
	api = GitHub(token(), os.getenv('WATCHDOG_REPO', 'namejee/anyrouter-check-in'))
	if args.probe:
		result = api.dispatch('scheduler-probe.yml')
		print(json.dumps({'action': 'probe_submitted', **result}))
		return 0
	now = datetime.now(BEIJING)
	directory = Path(os.getenv('STATE_DIRECTORY', '/var/lib/anyrouter-scheduler'))
	directory.mkdir(mode=0o700, parents=True, exist_ok=True)
	with (directory / 'scheduler.lock').open('w') as lock:
		fcntl.flock(lock, fcntl.LOCK_EX)
		path = directory / 'state.json'
		state = json.loads(path.read_text()) if path.exists() else {}
		if state.get('date') != now.date().isoformat():
			state = {'date': now.date().isoformat(), 'attempts': []}
		decision = decide(api, state, now, int(os.getenv('WATCHDOG_EXPECTED_ACCOUNTS', '4')))
		if not args.inspect:
			if decision['action'] == 'done':
				state.update(healthy=True, run_id=decision.get('run_id'))
				save_state(path, state)
			elif decision['action'] == 'dispatch':
				# Count before POST: an ambiguous network failure must not cause rapid duplicate submissions.
				state['attempts'].append(now.isoformat())
				save_state(path, state)
				result = api.dispatch()
				decision.update(submitted=True, run_id=result.get('workflow_run_id'))
		print(json.dumps({'time_beijing': now.isoformat(), 'inspect_only': args.inspect, **decision}))
		return 1 if decision['action'] == 'attention' else 0


if __name__ == '__main__':
	try:
		raise SystemExit(main())
	except Exception as error:
		# HTTP errors can contain signed log URLs; credentials and raw responses never enter the journal.
		print(json.dumps({'action': 'error', 'error_type': type(error).__name__}))
		raise SystemExit(1) from None
