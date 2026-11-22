"""Offline tests: HTTP is mocked, nothing touches the network."""
import logging

import pytest
import requests

import cf_mail

TO = ["a@example.test", "b@example.test"]


class Resp:
    def __init__(self, status=200, payload=None, raw_error=False):
        self.status_code = status
        self._payload = payload
        self._raw_error = raw_error

    def json(self):
        if self._raw_error:
            raise ValueError("not json")
        return self._payload


def ok(**result):
    base = {"message_id": "m1", "delivered": [], "queued": [], "permanent_bounces": [], "suppressed_recipients": []}
    base.update(result)
    return {"success": True, "errors": [], "result": base}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct1")
    monkeypatch.setenv("CLOUDFLARE_EMAIL_API_TOKEN", "tok-secret")
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)


def patch_post(monkeypatch, resp=None, exc=None, calls=None):
    def fake(url, headers=None, json=None, timeout=None):
        if calls is not None:
            calls.append((url, headers, json, timeout))
        if exc:
            raise exc
        return resp

    monkeypatch.setattr(cf_mail.requests, "post", fake)


def send():
    cf_mail.send_html_email("from@example.test", TO, "subj", "<p>x</p>")


def test_success_with_delivered_and_queued(env, monkeypatch):
    calls = []
    patch_post(monkeypatch, Resp(200, ok(delivered=[TO[0]], queued=["B@example.test"])), calls=calls)
    send()
    url, headers, body, timeout = calls[0]
    assert url == "https://api.cloudflare.com/client/v4/accounts/acct1/email/sending/send"
    assert headers["Authorization"] == "Bearer tok-secret"
    assert body == {"to": TO, "from": "from@example.test", "subject": "subj", "html": "<p>x</p>"}
    assert timeout == 20


@pytest.mark.parametrize("field", ["permanent_bounces", "suppressed_recipients"])
def test_bounced_or_suppressed_recipient_fails(env, monkeypatch, caplog, field):
    patch_post(monkeypatch, Resp(200, ok(delivered=[TO[0]], **{field: [TO[1]]})))
    with caplog.at_level(logging.INFO, logger="yt_summarizer"):
        with pytest.raises(RuntimeError, match="b@example.test"):
            send()
    assert "message_id=m1" in caplog.text


def test_success_false_and_http_error(env, monkeypatch):
    patch_post(monkeypatch, Resp(200, {"success": False, "errors": [{"code": 7, "message": "nope"}], "result": None}))
    with pytest.raises(RuntimeError, match="nope"):
        send()
    patch_post(monkeypatch, Resp(500, ok(delivered=TO)))
    with pytest.raises(RuntimeError, match="HTTP 500"):
        send()


@pytest.mark.parametrize("bad", [None, ["x"], {"success": True}, {"success": True, "result": "x"}])
def test_malformed_body(env, monkeypatch, bad):
    patch_post(monkeypatch, Resp(200, bad))
    with pytest.raises(RuntimeError):
        send()
    patch_post(monkeypatch, Resp(200, raw_error=True))
    with pytest.raises(RuntimeError, match="unreadable"):
        send()


def test_timeout_and_transport_error_never_leak_token(env, monkeypatch):
    patch_post(monkeypatch, exc=requests.Timeout("slow"))
    with pytest.raises(RuntimeError, match="timed out"):
        send()
    patch_post(monkeypatch, exc=requests.ConnectionError("boom tok-secret boom"))
    with pytest.raises(RuntimeError) as err:
        send()
    assert "tok-secret" not in str(err.value)


def test_provider_selection(monkeypatch):
    for var in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_EMAIL_API_TOKEN", "SENDGRID_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert cf_mail.email_provider() == ""
    with pytest.raises(RuntimeError, match="No email provider"):
        send()
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.x")
    assert cf_mail.email_provider() == "sendgrid"
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct1")
    assert cf_mail.email_provider() == "sendgrid"  # token missing
    monkeypatch.setenv("CLOUDFLARE_EMAIL_API_TOKEN", "t")
    assert cf_mail.email_provider() == "cloudflare"


def test_sendgrid_fallback_is_used_without_cloudflare_vars(monkeypatch):
    for var in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_EMAIL_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.x")
    seen = []
    monkeypatch.setattr(cf_mail, "_send_sendgrid", lambda *a: seen.append(a))
    patch_post(monkeypatch, exc=AssertionError("must not call Cloudflare"))
    send()
    assert seen == [("SG.x", "from@example.test", TO, "subj", "<p>x</p>")]


def test_main_parses_recipient_lists_and_sends_through_helper(env, monkeypatch):
    main = pytest.importorskip("main")
    assert main.parse_recipient_emails("a@example.test; b@example.test, ") == TO
    sent = []
    monkeypatch.setattr(main, "send_html_email", lambda *a: sent.append(a))
    monkeypatch.setenv("SENDER_EMAIL", "from@example.test")
    monkeypatch.delenv("RECIPIENT_EMAILS", raising=False)
    monkeypatch.setenv("ALERT_RECIPIENT_EMAILS", "x@example.test,y@example.test")
    main.send_error_alert_email(["boom"])
    assert sent[0][0] == "from@example.test" and sent[0][1] == ["x@example.test", "y@example.test"]
