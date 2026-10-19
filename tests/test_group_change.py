"""账号分组变化的提示：平时不出现，变了才提示一句。"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from checkin import detect_group_change


def test_no_previous_group_is_not_a_change():
	"""第一次见到分组（比如刚加上 group 字段）不算变化，不该打扰用户。"""
	assert detect_group_change('agLD', None, 'core') is None
	assert detect_group_change('agLD', '', 'core') is None


def test_missing_current_group_is_not_a_change():
	"""拿不到当前分组（平台没这个字段）时不报。"""
	assert detect_group_change('mlgb7', 'core', None) is None
	assert detect_group_change('mlgb7', 'core', '') is None


def test_same_group_is_not_a_change():
	assert detect_group_change('agLD', 'core', 'core') is None


def test_group_change_is_reported():
	assert detect_group_change('agLD', 'default', 'core') == 'agLD default → core'
	assert detect_group_change('agLD', 'core', 'default') == 'agLD core → default'


def test_change_message_has_no_newlines():
	"""要能拼进一行通知里。"""
	message = detect_group_change('agLD', 'default', 'core')

	assert message is not None
	assert '\n' not in message
