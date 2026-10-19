from checkin import (
	STATUS_ALREADY_CHECKED,
	STATUS_CHECKED_IN,
	STATUS_FAILED,
	STATUS_LOGGED_IN,
	CheckInOutcome,
	format_account_line,
	format_delta,
	short_day,
)

# --- 标签（现在只在失败时显示） ------------------------------------------------------


def test_labels_distinguish_each_status():
	labels = {
		STATUS_CHECKED_IN: '✅ 签到成功',
		STATUS_ALREADY_CHECKED: '✅ 今日已签到',
		STATUS_LOGGED_IN: '✅ 已登录',
		STATUS_FAILED: '❌ 签到失败',
	}
	for status, expected in labels.items():
		assert CheckInOutcome(status).label == expected

	assert len(set(labels.values())) == 4


def test_logged_in_label_does_not_claim_a_check_in_action():
	"""agentrouter 是登录即到账，没有独立签到动作，措辞不能声称签到成功。"""
	label = CheckInOutcome(STATUS_LOGGED_IN).label

	assert '签到成功' not in label
	assert '未获接口确认' not in label


def test_success_semantics():
	assert CheckInOutcome(STATUS_CHECKED_IN).success is True
	assert CheckInOutcome(STATUS_ALREADY_CHECKED).success is True
	assert CheckInOutcome(STATUS_LOGGED_IN).success is True
	assert CheckInOutcome(STATUS_FAILED).success is False


def test_unknown_status_is_echoed_not_swallowed():
	assert CheckInOutcome('something_new').label == 'something_new'


# --- format_delta：额度变化就是结论 -------------------------------------------------


def test_format_delta_treats_zero_as_not_credited():
	"""0 不能写成 $0.00 —— 要直接回答"没到账"。"""
	assert format_delta(0.0) == '未到账'


def test_format_delta():
	assert format_delta(25.0) == '+$25.00'
	assert format_delta(-100.0) == '-$100.00'
	assert format_delta(None) == '基线待建立'


def test_format_delta_labels_the_baseline_day():
	"""增量要说明跟哪天比，否则 +$50 会看起来像算错。"""
	assert format_delta(50.0, '2026-09-23') == '+$50.00(较09-23)'
	assert format_delta(0.0, '2026-09-24') == '未到账(较09-24)'
	assert format_delta(-100.0, '2026-09-23') == '-$100.00(较09-23)'
	assert format_delta(None, '2026-09-24') == '基线待建立'


def test_short_day():
	assert short_day('2026-09-23') == '09-23'
	assert short_day(None) == ''
	assert short_day('') == ''
	assert short_day('weird') == 'weird'


# --- format_account_line -----------------------------------------------------------


def test_line_leads_with_the_quota_change():
	line = format_account_line(
		'any主帐号',
		CheckInOutcome(STATUS_CHECKED_IN),
		total=2450.0,
		balance=615.45,
		day_gain=25.0,
		baseline_day='2026-09-24',
	)

	assert line == 'any主帐号 · +$25.00(较09-24) · 余额 $615.45 · 总量 $2450.00'


def test_line_does_not_show_login_status_on_success():
	"""✅ 已登录 和 未到账 并排会自相矛盾，成功时不该出现状态词。"""
	line = format_account_line(
		'agLD',
		CheckInOutcome(STATUS_LOGGED_IN),
		total=1125.0,
		balance=74.41,
		day_gain=0.0,
		baseline_day='2026-09-24',
	)

	assert line == 'agLD · 未到账(较09-24) · 余额 $74.41 · 总量 $1125.00'
	assert '已登录' not in line
	assert '签到成功' not in line
	assert '✅' not in line


def test_line_explains_a_double_gain_via_baseline_day():
	line = format_account_line(
		'agLD',
		CheckInOutcome(STATUS_LOGGED_IN),
		total=1125.0,
		balance=146.55,
		day_gain=50.0,
		baseline_day='2026-09-23',
	)

	assert line.startswith('agLD · +$50.00(较09-23)')


def test_line_reports_negative_gain():
	line = format_account_line('agLD', CheckInOutcome(STATUS_LOGGED_IN), total=900.0, day_gain=-100.0)

	assert 'agLD · -$100.00' in line


def test_line_without_baseline():
	line = format_account_line(
		'agLD',
		CheckInOutcome(STATUS_LOGGED_IN),
		total=1075.0,
		day_gain=None,
		baseline_day=None,
	)

	assert line.startswith('agLD · 基线待建立')


def test_line_omits_balance_when_unknown():
	line = format_account_line(
		'agLD',
		CheckInOutcome(STATUS_LOGGED_IN),
		total=1075.0,
		day_gain=25.0,
		baseline_day='2026-09-23',
	)

	assert line == 'agLD · +$25.00(较09-23) · 总量 $1075.00'


def test_failure_line_shows_status_and_reason():
	line = format_account_line(
		'agLD',
		CheckInOutcome(STATUS_FAILED, '邮箱密码登录失败'),
		total=1075.0,
		day_gain=25.0,
	)

	assert line == 'agLD · ❌ 签到失败 · 邮箱密码登录失败'
	assert '+$25.00' not in line


def test_failure_line_without_reason_still_readable():
	assert format_account_line('agLD', CheckInOutcome(STATUS_FAILED)) == 'agLD · ❌ 签到失败 · 未知原因'


def test_line_without_quota_data_says_so():
	"""拿不到额度时不能编造数字，也不能假装"未到账"。"""
	line = format_account_line('agLD', CheckInOutcome(STATUS_LOGGED_IN))

	assert line == 'agLD · 额度读取失败'


def test_one_line_per_account_has_no_newlines():
	for status in (STATUS_CHECKED_IN, STATUS_ALREADY_CHECKED, STATUS_LOGGED_IN, STATUS_FAILED):
		line = format_account_line('acct', CheckInOutcome(status, 'boom'), total=100.0, day_gain=1.0)
		assert '\n' not in line
