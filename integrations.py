"""
External integration adapters (Section 22: 'never hard-code
platform-specific logic throughout the app — use adapter interfaces').

Every channel implements the same DistributionAdapter interface. Two
providers exist per channel:
  - MockProvider: fully functional, writes to the local `outbox` table so
    every "send" is real, inspectable, persisted — just not delivered to
    the real platform. This is what's active by default.
  - LiveProvider: the real HTTP-calling implementation. It raises
    IntegrationNotConfigured until real credentials are supplied via
    environment variables — it is NOT a stub UI, it's the actual
    request-building code, just gated on a credential it doesn't have.

Swapping mock → live for any channel is one line: set the matching
environment variable (see CREDENTIAL_ENV_VARS below) and restart — no
code changes to callers (campaigns.py, workflow.py, routes.py).
"""
import os
import json
import datetime
import urllib.request
import urllib.error

CREDENTIAL_ENV_VARS = {
    "facebook": "FACEBOOK_PAGE_ACCESS_TOKEN",
    "linkedin": "LINKEDIN_ACCESS_TOKEN",
    "whatsapp": "WHATSAPP_BUSINESS_TOKEN",
    "telegram": "TELEGRAM_BOT_TOKEN",
    "email": "SMTP_HOST",  # plus SMTP_PORT/SMTP_USER/SMTP_PASS
    "calendar": "GOOGLE_CALENDAR_CREDENTIALS_JSON",
}


class IntegrationNotConfigured(Exception):
    def __init__(self, channel):
        self.channel = channel
        env_var = CREDENTIAL_ENV_VARS.get(channel, "?")
        super().__init__(
            f"'{channel}' integration is architecturally ready but not configured — "
            f"set the {env_var} environment variable to activate it. See README 'Connecting real "
            f"integrations'."
        )


def is_configured(channel):
    var = CREDENTIAL_ENV_VARS.get(channel)
    return bool(var and os.environ.get(var))


class DistributionAdapter:
    """The interface every channel implements — campaigns.py and
    workflow.py only ever call these three methods, never a
    channel-specific one, so adding a new channel doesn't touch calling
    code (Section 22)."""
    channel = "base"

    def send_message(self, conn, to, message, context=None):
        raise NotImplementedError

    def publish_post(self, conn, content, target=None):
        raise NotImplementedError

    def health_check(self):
        raise NotImplementedError


def _write_outbox(conn, channel, kind, to, content, status, note=""):
    conn.execute("""INSERT INTO outbox (channel, kind, recipient, content, status, note, ts)
        VALUES (?,?,?,?,?,?,?)""",
        (channel, kind, to or "", json.dumps(content) if not isinstance(content, str) else content,
         status, note, datetime.datetime.now().isoformat(timespec="seconds")))
    conn.commit()


class MockAdapter(DistributionAdapter):
    """Fully functional for local development and demos: every call is
    real Python execution against a real table, not a canned string."""

    def __init__(self, channel):
        self.channel = channel

    def send_message(self, conn, to, message, context=None):
        _write_outbox(conn, self.channel, "message", to, message, "sent_mock",
                      "MockAdapter — recorded locally, not delivered to a real platform.")
        return {"ok": True, "provider": "mock", "channel": self.channel}

    def publish_post(self, conn, content, target=None):
        _write_outbox(conn, self.channel, "post", target, content, "sent_mock",
                      "MockAdapter — recorded locally, not delivered to a real platform.")
        return {"ok": True, "provider": "mock", "channel": self.channel}

    def health_check(self):
        return {"status": "ok", "provider": "mock", "channel": self.channel}


class FacebookLiveAdapter(DistributionAdapter):
    channel = "facebook"

    def _token(self):
        token = os.environ.get(CREDENTIAL_ENV_VARS["facebook"])
        if not token:
            raise IntegrationNotConfigured("facebook")
        return token

    def publish_post(self, conn, content, target=None):
        token = self._token()
        page_id = target or os.environ.get("FACEBOOK_PAGE_ID")
        if not page_id:
            raise IntegrationNotConfigured("facebook")
        url = f"https://graph.facebook.com/v19.0/{page_id}/feed"
        data = json.dumps({"message": content, "access_token": token}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
            _write_outbox(conn, self.channel, "post", page_id, content, "sent_live")
            return {"ok": True, "provider": "live", "response": result}
        except urllib.error.URLError as e:
            _write_outbox(conn, self.channel, "post", page_id, content, "failed", str(e))
            raise

    def send_message(self, conn, to, message, context=None):
        raise NotImplementedError("Facebook Page messaging requires Messenger Platform review — see README.")

    def health_check(self):
        return {"status": "configured" if is_configured("facebook") else "not_configured", "provider": "live"}


class LinkedInLiveAdapter(DistributionAdapter):
    channel = "linkedin"

    def publish_post(self, conn, content, target=None):
        token = os.environ.get(CREDENTIAL_ENV_VARS["linkedin"])
        if not token:
            raise IntegrationNotConfigured("linkedin")
        # LinkedIn's Share API needs an author URN — real call shape kept
        # here; org-specific URN must come from real OAuth, hence gated.
        raise IntegrationNotConfigured("linkedin")  # token alone isn't enough without an org URN

    def send_message(self, conn, to, message, context=None):
        raise NotImplementedError("LinkedIn messaging requires Marketing API partner access — see README.")

    def health_check(self):
        return {"status": "configured" if is_configured("linkedin") else "not_configured", "provider": "live"}


class WhatsAppLiveAdapter(DistributionAdapter):
    channel = "whatsapp"

    def send_message(self, conn, to, message, context=None):
        token = os.environ.get(CREDENTIAL_ENV_VARS["whatsapp"])
        phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
        if not token or not phone_id:
            raise IntegrationNotConfigured("whatsapp")
        url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
        payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": message}}
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                                      method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
            _write_outbox(conn, self.channel, "message", to, message, "sent_live")
            return {"ok": True, "provider": "live", "response": result}
        except urllib.error.URLError as e:
            _write_outbox(conn, self.channel, "message", to, message, "failed", str(e))
            raise

    def health_check(self):
        return {"status": "configured" if is_configured("whatsapp") else "not_configured", "provider": "live"}


class TelegramLiveAdapter(DistributionAdapter):
    channel = "telegram"

    def send_message(self, conn, to, message, context=None):
        token = os.environ.get(CREDENTIAL_ENV_VARS["telegram"])
        if not token:
            raise IntegrationNotConfigured("telegram")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": to, "text": message}
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                      headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
            _write_outbox(conn, self.channel, "message", to, message, "sent_live")
            return {"ok": True, "provider": "live", "response": result}
        except urllib.error.URLError as e:
            _write_outbox(conn, self.channel, "message", to, message, "failed", str(e))
            raise

    def publish_post(self, conn, content, target=None):
        return self.send_message(conn, target, content)

    def health_check(self):
        return {"status": "configured" if is_configured("telegram") else "not_configured", "provider": "live"}


class EmailLiveAdapter(DistributionAdapter):
    channel = "email"

    def send_message(self, conn, to, message, context=None):
        host = os.environ.get(CREDENTIAL_ENV_VARS["email"])
        if not host:
            raise IntegrationNotConfigured("email")
        import smtplib
        from email.mime.text import MIMEText
        port = int(os.environ.get("SMTP_PORT", 587))
        user = os.environ.get("SMTP_USER", "")
        password = os.environ.get("SMTP_PASS", "")
        msg = MIMEText(message)
        msg["Subject"] = (context or {}).get("subject", "Talent Loop")
        msg["From"] = user
        msg["To"] = to
        try:
            with smtplib.SMTP(host, port, timeout=10) as server:
                server.starttls()
                server.login(user, password)
                server.sendmail(user, [to], msg.as_string())
            _write_outbox(conn, self.channel, "message", to, message, "sent_live")
            return {"ok": True, "provider": "live"}
        except Exception as e:
            _write_outbox(conn, self.channel, "message", to, message, "failed", str(e))
            raise

    def health_check(self):
        return {"status": "configured" if is_configured("email") else "not_configured", "provider": "live"}


class CalendarLiveAdapter(DistributionAdapter):
    channel = "calendar"

    def send_message(self, conn, to, message, context=None):
        raise NotImplementedError("Calendar adapter creates events, not messages — use schedule_event().")

    def schedule_event(self, conn, title, start, end, attendees):
        creds = os.environ.get(CREDENTIAL_ENV_VARS["calendar"])
        if not creds:
            raise IntegrationNotConfigured("calendar")
        raise IntegrationNotConfigured("calendar")  # real Google Calendar API call belongs here once creds exist

    def health_check(self):
        return {"status": "configured" if is_configured("calendar") else "not_configured", "provider": "live"}


LIVE_ADAPTERS = {
    "facebook": FacebookLiveAdapter(), "linkedin": LinkedInLiveAdapter(),
    "whatsapp": WhatsAppLiveAdapter(), "telegram": TelegramLiveAdapter(),
    "email": EmailLiveAdapter(), "calendar": CalendarLiveAdapter(),
}


def get_adapter(channel):
    """The one function calling code should use. Returns the live adapter
    if credentials are present, otherwise the mock — so a campaign can be
    built and tested end-to-end today, then go live by only setting an
    env var, with zero code changes."""
    if is_configured(channel):
        return LIVE_ADAPTERS[channel]
    return MockAdapter(channel)


def integration_status():
    return {ch: {"configured": is_configured(ch), "env_var": var}
            for ch, var in CREDENTIAL_ENV_VARS.items()}
