import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.notify import NotificationKit

TEST_CONFIG = {
	'EMAIL_USER': 'sender@example.com',
	'EMAIL_PASS': 'test-password',
	'EMAIL_TO': 'recipient@example.com',
	'EMAIL_SENDER': '',
	'CUSTOM_SMTP_SERVER': 'smtp.example.com',
	'PUSHPLUS_TOKEN': 'test-token',
	'SERVERPUSHKEY': 'test-key',
	'DINGDING_WEBHOOK': 'https://dingtalk.example.com/webhook',
	'FEISHU_WEBHOOK': 'https://feishu.example.com/webhook',
	'WEIXIN_WEBHOOK': 'https://weixin.example.com/webhook',
	'GOTIFY_URL': 'https://gotify.example.com/message',
	'GOTIFY_TOKEN': 'test-token',
	'GOTIFY_PRIORITY': '9',
	'TELEGRAM_BOT_TOKEN': 'test-token',
	'TELEGRAM_CHAT_ID': 'test-chat',
	'BARK_KEY': 'test-key',
	'BARK_SERVER': 'https://bark.example.com',
}


@pytest.fixture
def notification_kit(monkeypatch):
	for key, value in TEST_CONFIG.items():
		monkeypatch.setenv(key, value)
	return NotificationKit()


@pytest.fixture
def http_client(mocker):
	return mocker.patch('utils.notify.httpx.Client').return_value.__enter__.return_value


def test_send_email(mocker, notification_kit):
	server = mocker.patch('utils.notify.smtplib.SMTP_SSL').return_value.__enter__.return_value
	notification_kit.send_email('测试标题', '测试内容')

	server.login.assert_called_once_with('sender@example.com', 'test-password')
	message = server.send_message.call_args.args[0]
	assert message['To'] == 'recipient@example.com'
	assert message.get_content_type() == 'text/plain'


def test_send_pushplus(http_client, notification_kit):
	notification_kit.send_pushplus('测试标题', '测试内容')

	http_client.post.assert_called_once()
	assert http_client.post.call_args.kwargs['json']['token'] == 'test-token'


def test_send_dingtalk(http_client, notification_kit):
	notification_kit.send_dingtalk('测试标题', '测试内容')

	http_client.post.assert_called_once_with(
		'https://dingtalk.example.com/webhook', json={'msgtype': 'text', 'text': {'content': '测试标题\n测试内容'}}
	)


def test_send_feishu(http_client, notification_kit):
	notification_kit.send_feishu('测试标题', '测试内容')

	http_client.post.assert_called_once()
	assert http_client.post.call_args.kwargs['json']['card']['header']['title']['content'] == '测试标题'


def test_send_wecom(http_client, notification_kit):
	notification_kit.send_wecom('测试标题', '测试内容')

	http_client.post.assert_called_once_with(
		'https://weixin.example.com/webhook', json={'msgtype': 'text', 'text': {'content': '测试标题\n测试内容'}}
	)


def test_send_gotify(http_client, notification_kit):
	notification_kit.send_gotify('测试标题', '测试内容')

	http_client.post.assert_called_once_with(
		'https://gotify.example.com/message?token=test-token',
		json={'title': '测试标题', 'message': '测试内容', 'priority': 9},
	)


def test_missing_config(monkeypatch):
	for key in TEST_CONFIG:
		monkeypatch.delenv(key, raising=False)
	kit = NotificationKit()

	with pytest.raises(ValueError, match='Email configuration not set'):
		kit.send_email('测试', '测试')
	with pytest.raises(ValueError, match='PushPlus Token not configured'):
		kit.send_pushplus('测试', '测试')


def test_push_message_continues_when_one_channel_fails(mocker, notification_kit):
	methods = [
		'send_email',
		'send_pushplus',
		'send_serverPush',
		'send_dingtalk',
		'send_feishu',
		'send_wecom',
		'send_gotify',
		'send_telegram',
		'send_bark',
	]
	senders = [mocker.patch.object(notification_kit, name) for name in methods]
	senders[0].side_effect = RuntimeError('test delivery failure')

	notification_kit.push_message('测试标题', '测试内容')

	for sender in senders:
		sender.assert_called_once()
