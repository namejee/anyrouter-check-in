import os
import smtplib
from email.mime.text import MIMEText
from typing import Any, Literal

import httpx


class NotificationKit:
	"""多平台通知工具。

	所有配置项都是**惰性属性**，在真正发送时才读取环境变量，而不是在实例化时快照。

	注意：`notify` 是本模块级单例，会在 `utils.notify` 被导入时就创建。如果在这里快照
	环境变量，那么任何「先导入 utils.notify、后调用 load_dotenv()」的入口（checkin.py
	即是如此）都会拿到一份空配置，导致本地 .env 里配好的通知渠道全部失效，且失败信息
	会误导为 "xxx not configured"。保持惰性读取可以让配置与导入顺序无关。
	"""

	@property
	def email_user(self) -> str:
		return os.getenv('EMAIL_USER', '')

	@property
	def email_pass(self) -> str:
		return os.getenv('EMAIL_PASS', '')

	@property
	def email_to(self) -> str:
		return os.getenv('EMAIL_TO', '')

	@property
	def email_sender(self) -> str:
		return os.getenv('EMAIL_SENDER', '')

	@property
	def smtp_server(self) -> str:
		return os.getenv('CUSTOM_SMTP_SERVER', '')

	@property
	def pushplus_token(self) -> str | None:
		return os.getenv('PUSHPLUS_TOKEN')

	@property
	def server_push_key(self) -> str | None:
		return os.getenv('SERVERPUSHKEY')

	@property
	def dingding_webhook(self) -> str | None:
		return os.getenv('DINGDING_WEBHOOK')

	@property
	def feishu_webhook(self) -> str | None:
		return os.getenv('FEISHU_WEBHOOK')

	@property
	def weixin_webhook(self) -> str | None:
		return os.getenv('WEIXIN_WEBHOOK')

	@property
	def gotify_url(self) -> str | None:
		return os.getenv('GOTIFY_URL')

	@property
	def gotify_token(self) -> str | None:
		return os.getenv('GOTIFY_TOKEN')

	@property
	def gotify_priority(self) -> int:
		gotify_priority_env = os.getenv('GOTIFY_PRIORITY', '9')
		return int(gotify_priority_env) if gotify_priority_env.strip() else 9

	@property
	def telegram_bot_token(self) -> str | None:
		return os.getenv('TELEGRAM_BOT_TOKEN')

	@property
	def telegram_chat_id(self) -> str | None:
		return os.getenv('TELEGRAM_CHAT_ID')

	@property
	def bark_key(self) -> str | None:
		return os.getenv('BARK_KEY')

	@property
	def bark_server(self) -> str:
		return os.getenv('BARK_SERVER', 'https://api.day.app')

	def _post_json(self, service: str, url: str, data: dict[str, Any]) -> httpx.Response:
		with httpx.Client(timeout=30.0) as client:
			response = client.post(url, json=data)

		if response.status_code >= 400:
			raise RuntimeError(f'{service} request failed: HTTP {response.status_code}')

		try:
			payload = response.json()
		except ValueError:
			return response

		if not isinstance(payload, dict):
			return response

		error_msg = payload.get('errmsg') or payload.get('message') or payload.get('msg') or payload.get('error')
		if payload.get('ok') is False:
			raise RuntimeError(f'{service} request failed: {error_msg or payload.get("description") or "ok=false"}')
		if payload.get('errcode') not in (None, 0):
			raise RuntimeError(f'{service} request failed: {error_msg or payload.get("errcode")}')
		if payload.get('StatusCode') not in (None, 0):
			raise RuntimeError(f'{service} request failed: {error_msg or payload.get("StatusCode")}')
		if payload.get('code') not in (None, 0, 200):
			raise RuntimeError(f'{service} request failed: {error_msg or payload.get("code")}')
		if payload.get('ret') not in (None, 0, 1, 200):
			raise RuntimeError(f'{service} request failed: {error_msg or payload.get("ret")}')

		return response

	def send_email(self, title: str, content: str, msg_type: Literal['text', 'html'] = 'text'):
		if not self.email_user or not self.email_pass or not self.email_to:
			raise ValueError('Email configuration not set')

		# 如果未设置 EMAIL_SENDER，使用 EMAIL_USER 作为默认值
		sender = self.email_sender if self.email_sender else self.email_user

		# MIMEText 需要 'plain' 或 'html'，而不是 'text'
		mime_subtype = 'plain' if msg_type == 'text' else 'html'
		msg = MIMEText(content, mime_subtype, 'utf-8')
		msg['From'] = f'AnyRouter Assistant <{sender}>'
		msg['To'] = self.email_to
		msg['Subject'] = title

		smtp_server = self.smtp_server if self.smtp_server else f'smtp.{self.email_user.split("@")[1]}'
		with smtplib.SMTP_SSL(smtp_server, 465) as server:
			server.login(self.email_user, self.email_pass)
			server.send_message(msg)

	def send_pushplus(self, title: str, content: str):
		if not self.pushplus_token:
			raise ValueError('PushPlus Token not configured')

		data = {'token': self.pushplus_token, 'title': title, 'content': content, 'template': 'html'}
		self._post_json('PushPlus', 'http://www.pushplus.plus/send', data)

	def send_serverPush(self, title: str, content: str):
		if not self.server_push_key:
			raise ValueError('Server Push key not configured')

		data = {'title': title, 'desp': content}
		self._post_json('Server Push', f'https://sctapi.ftqq.com/{self.server_push_key}.send', data)

	def send_dingtalk(self, title: str, content: str):
		if not self.dingding_webhook:
			raise ValueError('DingTalk Webhook not configured')

		data = {'msgtype': 'text', 'text': {'content': f'{title}\n{content}'}}
		self._post_json('DingTalk', self.dingding_webhook, data)

	def send_feishu(self, title: str, content: str):
		if not self.feishu_webhook:
			raise ValueError('Feishu Webhook not configured')

		data = {
			'msg_type': 'interactive',
			'card': {
				'elements': [{'tag': 'markdown', 'content': content, 'text_align': 'left'}],
				'header': {'template': 'blue', 'title': {'content': title, 'tag': 'plain_text'}},
			},
		}
		self._post_json('Feishu', self.feishu_webhook, data)

	def send_wecom(self, title: str, content: str):
		if not self.weixin_webhook:
			raise ValueError('WeChat Work Webhook not configured')

		data = {'msgtype': 'text', 'text': {'content': f'{title}\n{content}'}}
		self._post_json('WeChat Work', self.weixin_webhook, data)

	def send_gotify(self, title: str, content: str):
		if not self.gotify_url or not self.gotify_token:
			raise ValueError('Gotify URL or Token not configured')

		# 使用环境变量配置的优先级，默认为9
		priority = self.gotify_priority

		# 确保优先级在有效范围内 (1-10)
		priority = max(1, min(10, priority))

		data = {'title': title, 'message': content, 'priority': priority}

		url = f'{self.gotify_url}?token={self.gotify_token}'
		self._post_json('Gotify', url, data)

	def send_telegram(self, title: str, content: str):
		if not self.telegram_bot_token or not self.telegram_chat_id:
			raise ValueError('Telegram Bot Token or Chat ID not configured')

		message = f'<b>{title}</b>\n\n{content}'
		data = {'chat_id': self.telegram_chat_id, 'text': message, 'parse_mode': 'HTML'}
		url = f'https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage'
		self._post_json('Telegram', url, data)

	def send_bark(self, title: str, content: str):
		if not self.bark_key:
			raise ValueError('Bark Key not configured')

		# Bark API 支持 GET/POST，这里使用 POST JSON 方式支持更多参数
		# 文档: https://bark.day.app/#/tutorial
		url = f'{self.bark_server.rstrip("/")}/push'
		data = {
			'device_key': self.bark_key,
			'title': title,
			'body': content,
			'icon': 'https://anyrouter.top/favicon.ico',  # 可选：尝试使用 AnyRouter 图标
			'group': 'AnyRouter',
		}

		self._post_json('Bark', url, data)

	def push_message(self, title: str, content: str, msg_type: Literal['text', 'html'] = 'text'):
		notifications = [
			('Email', lambda: self.send_email(title, content, msg_type)),
			('PushPlus', lambda: self.send_pushplus(title, content)),
			('Server Push', lambda: self.send_serverPush(title, content)),
			('DingTalk', lambda: self.send_dingtalk(title, content)),
			('Feishu', lambda: self.send_feishu(title, content)),
			('WeChat Work', lambda: self.send_wecom(title, content)),
			('Gotify', lambda: self.send_gotify(title, content)),
			('Telegram', lambda: self.send_telegram(title, content)),
			('Bark', lambda: self.send_bark(title, content)),
		]

		for name, func in notifications:
			try:
				func()
				print(f'[{name}]: Message push successful!')
			except Exception as e:
				print(f'[{name}]: Message push failed! Reason: {str(e)}')


notify = NotificationKit()
