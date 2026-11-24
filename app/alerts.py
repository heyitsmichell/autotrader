"""Alerts to your phone or inbox, so the app can run without you watching it.

Channels (set any of them in .env; none is required):
  - ntfy      ALERT_NTFY_TOPIC (free push app, https://ntfy.sh; pick a long random
              topic name, because anyone who knows it can read your alerts)
  - Telegram  ALERT_TELEGRAM_BOT_TOKEN + ALERT_TELEGRAM_CHAT_ID
  - Webhook   ALERT_WEBHOOK_URL (Discord or Slack incoming webhook)
  - Email     ALERT_SMTP_HOST, ALERT_SMTP_PORT, ALERT_SMTP_USER, ALERT_SMTP_PASSWORD,
              ALERT_EMAIL_FROM, ALERT_EMAIL_TO
  - Local     Always enabled (desktop toast, flag file for dashboard banner)

What is sent: every activity-log line at a level in ALERT_LEVELS (default:
error), plus lines the code marks as alerts (orders placed, stops/targets
filled, kill switch, broker down, login expiring, daily summary).
Identical messages are sent at most once per ALERT_DEDUPE_MINUTES, and never
more than ALERT_MAX_PER_HOUR in total.

Alerts only ever leave this computer through the channels you configure, and
they carry no credentials.
"""

from __future__ import annotations

import asyncio
import re
import logging
import smtplib
import time
from collections import deque
from email.message import EmailMessage

import httpx

from .config import Settings
from .platform import send_desktop_notification

log = logging.getLogger(__name__)

PRIORITY = {"error": "high", "warn": "default", "info": "default"}


class Alerts:
    def __init__(self, settings: Settings, db=None) -> None:
        self.s = settings
        self._recent: dict[str, float] = {}
        self._sent_times: deque[float] = deque()
        self._tasks: set[asyncio.Task] = set()
        self.last_error: str | None = None
        self.sent = 0
        self.suppressed = 0
        if db is not None:
            db.listeners.append(self._on_event)

    @property
    def channels(self) -> list[str]:
        s, out = self.s, []
        if s.alert_ntfy_topic:
            out.append("ntfy")
        if s.alert_telegram_token and s.alert_telegram_chat_id:
            out.append("telegram")
        if s.alert_webhook_url:
            out.append("webhook")
        if s.alert_smtp_host and s.alert_email_to:
            out.append("email")
        return out

    def status(self) -> dict:
        return {"channels": self.channels, "levels": sorted(self.s.alert_levels),
                "sent": self.sent, "suppressed": self.suppressed, "last_error": self.last_error,
                "daily_summary": self.s.alert_daily_summary}

    # -- entry points ----------------------------------------------------------
    def _on_event(self, level: str, message: str, alert: bool) -> None:
        if alert or level in self.s.alert_levels:
            self.notify(message, level=level)

    def notify(self, message: str, level: str = "info", title: str | None = None,
               force: bool = False) -> bool:
        """Queue an alert. Returns False if it was deduplicated, rate-limited or
        there is no channel. Safe to call from sync code inside the event loop."""
        # Always consider "local" as a channel - local notifications are always enabled
        now = time.time()
        if not force:
            last = self._recent.get(message)
            if last and now - last < self.s.alert_dedupe_minutes * 60:
                self.suppressed += 1
                return False
            while self._sent_times and now - self._sent_times[0] > 3600:
                self._sent_times.popleft()
            if len(self._sent_times) >= self.s.alert_max_per_hour:
                self.suppressed += 1
                return False
        self._recent[message] = now
        self._sent_times.append(now)
        if len(self._recent) > 500:   # forget old messages
            cutoff = now - self.s.alert_dedupe_minutes * 60
            self._recent = {k: v for k, v in self._recent.items() if v >= cutoff}
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        task = loop.create_task(self.send(message, level, title))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def send(self, message: str, level: str = "info", title: str | None = None) -> dict:
        """Send now to every configured channel. Returns {channel: "ok" | error text}."""
        env = self.s.broker_label
        title = title or ("Trader" if level == "info" else f"Trader {level.upper()}")
        body = f"{message}\n\n[{env}]"
        results: dict[str, str] = {}

        # Always send local notification (desktop toast + flag file for dashboard)
        try:
            await self._local(title, body, level)
            results["local"] = "ok"
        except Exception as e:
            results["local"] = f"{type(e).__name__}: {e}"
            log.warning("local alert failed: %s", e)

        # External channels
        senders = {"ntfy": self._ntfy, "telegram": self._telegram, "webhook": self._webhook,
                   "email": self._email}
        for name in self.channels:
            try:
                await senders[name](title, body, level)
                results[name] = "ok"
            except Exception as e:  # noqa: BLE001 - report, never raise
                err = self._redact(f"{type(e).__name__}: {e}")
                results[name] = err
                self.last_error = f"{name}: {err}"
                log.warning("alert via %s failed: %s", name, err)   # never the raw exception (H4)
        if any(v == "ok" for v in results.values()):
            self.sent += 1
        return results

    async def _local(self, title: str, body: str, level: str) -> None:
        """Send local desktop notification and write flag file for dashboard banner."""
        # Desktop toast (Windows/macOS/Linux)
        send_desktop_notification(title, body, urgent=(level == "error"))

        # Write flag file for dashboard banner
        from .config import DATA_DIR
        flag_file = DATA_DIR / "alert.flag"
        import json
        flag_data = {
            "timestamp": int(time.time()),
            "title": title,
            "message": body,
            "level": level,
        }
        flag_file.write_text(json.dumps(flag_data))

    # -- channels --------------------------------------------------------------
    def _redact(self, text: str) -> str:
        """Remove every configured secret and any bot-token-shaped string from a message."""
        for secret in (self.s.alert_telegram_token, self.s.alert_smtp_password,
                       self.s.alert_ntfy_topic, self.s.alert_webhook_url):
            if secret:
                text = text.replace(secret, "***")
        return re.sub(r"bot\d+:[\w-]+", "bot***", text)

    async def _post(self, url: str, **kw) -> None:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, **kw)
        except httpx.HTTPError as e:
            # httpx error text can embed the request URL (and so the Telegram token).
            raise RuntimeError(self._redact(f"{type(e).__name__}: {e}")) from None
        if r.status_code >= 300:
            raise RuntimeError(self._redact(f"HTTP {r.status_code}: {r.text[:200]}"))

    async def _ntfy(self, title: str, body: str, level: str) -> None:
        await self._post(f"{self.s.alert_ntfy_server}/{self.s.alert_ntfy_topic}",
                         content=body.encode("utf-8"),
                         headers={"Title": title.encode("ascii", "ignore").decode(),
                                  "Priority": PRIORITY.get(level, "default"),
                                  "Tags": "rotating_light" if level == "error" else "chart"})

    async def _telegram(self, title: str, body: str, level: str) -> None:
        await self._post(f"https://api.telegram.org/bot{self.s.alert_telegram_token}/sendMessage",
                         json={"chat_id": self.s.alert_telegram_chat_id,
                               "text": f"{title}\n{body}", "disable_web_page_preview": True})

    async def _webhook(self, title: str, body: str, level: str) -> None:
        text = f"**{title}**\n{body}"
        # "content" is what Discord reads, "text" is what Slack reads.
        await self._post(self.s.alert_webhook_url, json={"content": text[:1900], "text": text})

    async def _email(self, title: str, body: str, level: str) -> None:
        s = self.s
        msg = EmailMessage()
        msg["Subject"], msg["From"] = title, s.alert_email_from or s.alert_smtp_user
        msg["To"] = s.alert_email_to
        msg.set_content(body)

        def go() -> None:
            if s.alert_smtp_port == 465:
                server = smtplib.SMTP_SSL(s.alert_smtp_host, s.alert_smtp_port, timeout=20)
            else:
                server = smtplib.SMTP(s.alert_smtp_host, s.alert_smtp_port, timeout=20)
                server.starttls()
            with server:
                if s.alert_smtp_user:
                    server.login(s.alert_smtp_user, s.alert_smtp_password)
                server.send_message(msg)

        await asyncio.to_thread(go)
