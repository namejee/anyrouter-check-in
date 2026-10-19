"""CHECKIN_PROVIDERS 过滤：让「00:01 只签 agentrouter+mlgb7、08:01 只签 anyrouter」成立。"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from checkin import filter_accounts_by_provider
from utils.config import AccountConfig


def account(name: str, provider: str) -> AccountConfig:
	return AccountConfig(cookies={'session': 'x'}, provider=provider, name=name)


SAMPLE = [
	account('any主帐号', 'anyrouter'),
	account('agLD', 'agentrouter'),
	account('agGithub', 'agentrouter'),
	account('mlgb7', 'mlgb7'),
]


def names(accounts):
	return [item.name for item in accounts]


def test_unset_filter_keeps_everything():
	"""定时任务不带这个变量，必须保持"一次覆盖全部账号"的兜底行为。"""
	kept, skipped = filter_accounts_by_provider(SAMPLE, None)

	assert kept == SAMPLE
	assert skipped == []


def test_blank_filter_keeps_everything():
	for raw in ('', '   ', ' , , '):
		kept, skipped = filter_accounts_by_provider(SAMPLE, raw)
		assert kept == SAMPLE
		assert skipped == []


def test_filter_selects_agentrouter_and_mlgb7_group():
	kept, skipped = filter_accounts_by_provider(SAMPLE, 'agentrouter,mlgb7')

	assert names(kept) == ['agLD', 'agGithub', 'mlgb7']
	assert names(skipped) == ['any主帐号']


def test_filter_selects_anyrouter_group():
	kept, skipped = filter_accounts_by_provider(SAMPLE, 'anyrouter')

	assert names(kept) == ['any主帐号']
	assert names(skipped) == ['agLD', 'agGithub', 'mlgb7']


def test_filter_ignores_case_and_spaces():
	kept, _ = filter_accounts_by_provider(SAMPLE, ' AgentRouter , MLGB7 ')

	assert names(kept) == ['agLD', 'agGithub', 'mlgb7']


def test_filter_preserves_original_order():
	kept, _ = filter_accounts_by_provider(SAMPLE, 'mlgb7,anyrouter')

	assert names(kept) == ['any主帐号', 'mlgb7']


def test_filter_with_no_match_yields_nothing():
	"""写错 provider 名会让本次没有任何账号可跑 —— 调用方据此退出，避免静默空跑。"""
	kept, skipped = filter_accounts_by_provider(SAMPLE, 'nonexistent')

	assert kept == []
	assert names(skipped) == ['any主帐号', 'agLD', 'agGithub', 'mlgb7']
