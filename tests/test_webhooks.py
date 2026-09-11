import json
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue

import pytest

from agent_bridge.models import (
    Attachment,
    ContentType,
    ConversationType,
    UnifiedMessage,
)
from agent_bridge.webhooks import (
    WebhookDispatcher,
    WebhookSettings,
    message_payload,
    parse_webhooks,
    post_json,
)


def msg(number="1", *, kind="private", source="others", **kwargs):
    return UnifiedMessage("wechat", "login-account", "friend", ConversationType(kind),
                          "login-account" if source != "others" else "friend", number,
                          "你好，原始内容", metadata={"is_self": source != "others", "bridge_outbound": source == "ai"},
                          **kwargs)


def settings(**kwargs):
    return WebhookSettings(**{"url": "http://127.0.0.1/webhook", "enabled": True, **kwargs})


def wait_until(check, timeout=2):
    end = time.monotonic() + timeout
    while not check():
        if time.monotonic() > end:
            pytest.fail("background delivery did not complete")
        time.sleep(0.005)


@pytest.mark.parametrize("kind", ["private", "group"])
@pytest.mark.parametrize("source", ["self", "others", "ai"])
@pytest.mark.parametrize("conversation_filter", ["all", "private", "group"])
@pytest.mark.parametrize("sender_filter", ["all", "self", "others"])
@pytest.mark.parametrize("include_ai", [False, True])
@pytest.mark.parametrize("content_type", [ContentType.TEXT, ContentType.IMAGE])
def test_independent_filters(kind, source, conversation_filter, sender_filter, include_ai, content_type):
    hook = settings(conversation_type=conversation_filter, sender=sender_filter,
                    include_ai_replies=include_ai, content_types=["text"])
    expected = (conversation_filter in {"all", kind}
                and sender_filter in {"all", "self" if source in {"self", "ai"} else "others"}
                and (source != "ai" or include_ai) and content_type == ContentType.TEXT)
    message = msg(kind=kind, source=source, content_type=content_type)
    assert hook.matches(message) is expected
    assert not replace(hook, enabled=False).matches(message)


@pytest.mark.parametrize("content_type", list(ContentType))
@pytest.mark.parametrize("selected", [[], ["text"], ["text", "image"], ["unknown"]])
def test_content_type_filters(content_type, selected):
    assert settings(content_types=selected).matches(msg(content_type=content_type)) is (
        not selected or content_type.value in selected
    )
    assert settings().matches(msg(content_type=content_type))


@pytest.mark.parametrize("value", [None, "text", {}, True, 1, [None], [1], [{}], ["TEXT"], ["audio"]])
def test_invalid_content_types(value):
    with pytest.raises(ValueError, match="content_types"):
        parse_webhooks([{"content_types": value}])


def test_content_types_copied_and_deduplicated():
    selected = ["text", "image", "text"]
    hook = settings(content_types=selected)
    selected.clear()
    assert hook.content_types == ["text", "image"]
    assert settings().content_types == []


def test_dispatcher_filters_each_endpoint_before_sending():
    queues = {name: Queue() for name in ("text", "media", "all")}

    def post(endpoint, body, event_id):
        payload = json.loads(body)
        assert set(payload) == {"source", "event_id", "content", "sender", "conversation_id", "occurred_at"}
        queues[endpoint.name].put(payload["content"])
        return 204

    dispatcher = WebhookDispatcher((settings(name="text", content_types=["text"]),
                                    settings(name="media", content_types=["image", "voice"]),
                                    settings(name="all")), post=post, retry_delay=0)
    dispatcher.start()
    try:
        for kind in ContentType:
            dispatcher.submit(msg(kind.value, content_type=kind), ())
        # A literal media marker is still text, not an image.
        dispatcher.submit(replace(msg("literal"), content="[image]"), ())
        wait_until(lambda: queues["all"].qsize() == 7 and queues["text"].qsize() == 2
                   and queues["media"].qsize() == 2)
    finally:
        dispatcher.stop()
    assert [queues["text"].get_nowait() for _ in range(2)] == ["你好，原始内容", "[image]"]
    assert [queues["media"].get_nowait() for _ in range(2)] == ["[图片]", "[语音]"]
    assert queues["text"].empty() and queues["media"].empty()


@pytest.mark.parametrize("entry", [
    {"url": "file:///tmp/example"}, {"url": "http://user:secret@host/path"},
    {"url": "http://example.com:999999"}, {"url": "http://example.com/\npath"},
    {"url": "http://example.com/path#fragment"}, {"url": ""},
    {"enabled": "false"}, {"include_ai_replies": "false"}, {"conversation_type": "both"},
    {"sender": "me"}, {"timeout_seconds": float("nan")}, {"timeout_seconds": True},
    {"timeout_seconds": 0}, {"timeout_seconds": 61}, {"max_attempts": 0},
    {"max_attempts": 11}, {"max_attempts": 1.5}, {"max_attempts": True}, {"unk": 1},
])
def test_invalid_endpoint_config_is_rejected_without_url_secrets(entry):
    with pytest.raises((TypeError, ValueError)) as error:
        parse_webhooks([{"url": "https://example.com/hook", "enabled": True, **entry}])
    assert "secret" not in str(error.value)


def test_defaults_and_maximum_endpoints():
    assert parse_webhooks(None) == parse_webhooks([]) == ()
    assert parse_webhooks([{}]) == (WebhookSettings(),)
    with pytest.raises(TypeError):
        parse_webhooks("https://example.com")
    with pytest.raises(ValueError):
        parse_webhooks([None])
    with pytest.raises(ValueError):
        parse_webhooks([{}] * 33)


def test_payload_preserves_text_and_omits_private_media_fields():
    original = msg()
    payload = message_payload(original, ("好友备注",))
    assert payload["content"] == "你好，原始内容"
    assert set(payload) == {"source", "event_id", "content", "sender", "conversation_id", "occurred_at"}
    assert payload["source"] == "wechat"
    assert payload["sender"] == original.sender_id
    assert payload["conversation_id"] == original.conversation_id
    assert payload["occurred_at"] == original.created_at.isoformat()
    assert payload["event_id"] == message_payload(original, ())["event_id"]
    assert payload["event_id"] != message_payload(msg("2"), ())["event_id"]
    media = replace(original, content_type=ContentType.IMAGE, content='<img aeskey="secret-key"/>',
                    attachments=(Attachment("image", name=r"C:\private\photo.png", path=r"C:\private\photo.png",
                                            url="https://cdn.example/token", metadata={"key": "secret-key"}),))
    encoded = json.dumps(message_payload(media, ()))
    for secret in ("private", "secret-key", "cdn.example", "aeskey", "photo.png", "attachments"):
        assert secret not in encoded
    assert message_payload(replace(original, content_type=ContentType.IMAGE), ())["content"] == "[图片]"


@pytest.mark.parametrize("status,attempts", [(200, 1), (204, 1), (400, 1), (401, 1), (302, 1), (408, 3), (429, 3), (500, 3), (None, 3)])
def test_bounded_retries_and_stable_event_id(status, attempts, caplog):
    calls = []

    def post(s, body, event_id):
        calls.append((event_id, body))
        if status is None:
            raise TimeoutError("https://private-endpoint/?token=SECRET_TOKEN")
        return status

    dispatcher = WebhookDispatcher((settings(headers={"Authorization": "Bearer SECRET_TOKEN"}),), post=post, retry_delay=0)
    dispatcher.start()
    try:
        dispatcher.submit(msg(), ())
        wait_until(lambda: len(calls) == attempts)
        time.sleep(0.02)
        assert len(calls) == attempts
        assert len(set(calls)) == 1
        assert "SECRET_TOKEN" not in caplog.text
        assert "你好" not in caplog.text
    finally:
        dispatcher.stop()


def test_slow_endpoint_does_not_block_another_endpoint_or_submit():
    entered, release, delivered = threading.Event(), threading.Event(), threading.Event()

    def post(s, body, event_id):
        if s.name == "slow":
            entered.set()
            release.wait(2)
        else:
            delivered.set()
        return 200

    dispatcher = WebhookDispatcher((settings(name="slow"), settings(name="fast")), post=post)
    dispatcher.start()
    try:
        dispatcher.submit(msg(), ())
        assert entered.wait(1)
        assert delivered.wait(1)
        before = time.monotonic()
        dispatcher.stop()
        assert time.monotonic() - before < 0.2
    finally:
        release.set()
        dispatcher.stop()


def test_stop_discards_queue_and_restart_does_not_replay():
    calls, entered, release = [], threading.Event(), threading.Event()

    def post(s, body, event_id):
        calls.append(json.loads(body)["event_id"])
        if len(calls) == 1:
            entered.set()
            release.wait(2)
        return 500

    dispatcher = WebhookDispatcher((settings(max_attempts=1),), post=post, retry_delay=0)
    dispatcher.start()
    try:
        dispatcher.submit(msg("1"), ())
        assert entered.wait(1)
        dispatcher.submit(msg("2"), ())
        dispatcher.stop()
        dispatcher.submit(msg("ignored"), ())
        release.set()
        dispatcher.start()
        dispatcher.submit(msg("3"), ())
        wait_until(lambda: calls == [message_payload(msg(n), ())["event_id"] for n in ("1", "3")])
    finally:
        release.set()
        dispatcher.stop()


def test_dedup_uses_row_id_not_content_and_does_not_leak_filtered_ai():
    calls = []
    dispatcher = WebhookDispatcher((settings(sender="all"),), post=lambda s, b, e: calls.append(json.loads(b)["event_id"]) or 200)
    dispatcher.start()
    try:
        dispatcher.submit(msg("ai-row", source="ai"), ())
        dispatcher.submit(msg("ai-row", source="self"), ())
        dispatcher.submit(msg("1"), ())
        dispatcher.submit(msg("1"), ())
        dispatcher.submit(msg("2"), ())
        dispatcher.submit(msg("old", created_at=datetime.now(timezone.utc) - timedelta(days=1)), ())
        wait_until(lambda: len(calls) == 2)
        assert calls == [message_payload(msg(n), ())["event_id"] for n in ("1", "2")]
    finally:
        dispatcher.stop()


def test_full_queue_is_bounded_and_only_reports_summary(caplog):
    entered, release = threading.Event(), threading.Event()

    def post(s, b, e):
        entered.set()
        release.wait(2)
        return 200

    dispatcher = WebhookDispatcher((settings(),), post=post)
    dispatcher.start()
    try:
        dispatcher.submit(msg(), ())
        assert entered.wait(1)
        for index in range(2, 250):
            dispatcher.submit(msg(str(index)), ())
        assert dispatcher._workers[0].queue.qsize() == 200
        assert caplog.text.count("Webhook 队列已满") == 1
        assert "你好" not in caplog.text
    finally:
        dispatcher.stop()
        release.set()


@pytest.fixture
def local_endpoint():
    received = Queue()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            received.put((self.path, dict(self.headers), body))
            status = 302 if self.path == "/redirect" else 204
            if self.path == "/auth":
                status = 202 if (self.headers.get("Authorization") == "Bearer TEST_SECRET"
                                 and self.headers.get("X-API-Key") == "TEST_KEY") else 401
            if self.path.startswith("/method/"):
                status = 204 if self.path == f"/method/{self.command}" else 405
            if self.path == "/six-field-contract":
                data = json.loads(body)
                expected = {"source", "event_id", "content", "sender", "conversation_id", "occurred_at"}
                status = 202 if (set(data) == expected and data["source"] == "wechat"
                                 and all(isinstance(v, str) for v in data.values())) else 422
            self.send_response(status)
            if self.path == "/redirect":
                self.send_header("Location", "/must-not-forward")
            self.end_headers()

        do_PUT = do_POST
        do_PATCH = do_POST

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_actual_http_post_is_json_and_does_not_follow_redirects(local_endpoint, monkeypatch):
    base, received = local_endpoint
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    body = json.dumps({"content": "本地模拟消息"}, ensure_ascii=False).encode("utf-8")
    assert post_json(settings(url=base + "/ok"), body, "test-event") == 204
    path, headers, actual = received.get(timeout=1)
    assert path == "/ok" and actual == body
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert headers["X-Agent-Bridge-Event-Id"] == "test-event"
    assert post_json(settings(url=base + "/redirect"), body, "test-event") == 302
    assert received.get(timeout=1)[0] == "/redirect"
    assert received.empty()


def test_dispatcher_delivers_exact_receiver_contract(local_endpoint, caplog):
    base, received = local_endpoint
    endpoint = settings(url=base + "/six-field-contract", headers={"Authorization": "Bearer TEST_TOKEN"})
    message = msg()
    payload = message_payload(message, ("LOCAL_LABEL_MUST_NOT_LEAK",))
    # The receiver forbids extra fields and requires source, reproducing the reported 422.
    assert post_json(endpoint, json.dumps({**payload, "schema_version": 1}).encode(), "old") == 422
    received.get(timeout=1)
    assert post_json(endpoint, json.dumps({k: v for k, v in payload.items() if k != "source"}).encode(), "old") == 422
    received.get(timeout=1)
    caplog.set_level("INFO", logger="agent_bridge.webhooks")
    dispatcher = WebhookDispatcher((endpoint,))
    dispatcher.start()
    try:
        dispatcher.submit(message, ("LOCAL_LABEL_MUST_NOT_LEAK",))
        _, headers, body = received.get(timeout=2)
        assert json.loads(body) == payload
        assert headers["X-Agent-Bridge-Event-Id"] == payload["event_id"]
        assert headers["Authorization"] == "Bearer TEST_TOKEN"
        assert "LOCAL_LABEL_MUST_NOT_LEAK" not in body.decode()
        wait_until(lambda: "Webhook 推送成功" in caplog.text)
        assert "TEST_TOKEN" not in caplog.text
    finally:
        dispatcher.stop()


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
def test_custom_method_and_case_insensitive_headers(local_endpoint, method):
    base, received = local_endpoint
    s = settings(url=base + "/method/" + method, method=method,
                 headers={"content-type": "application/json", "USER-Agent": "CustomSender", "X-Custom": "value"})
    assert post_json(s, b'{"test":true}', "event") == 204
    _, headers, body = received.get(timeout=1)
    assert body == b'{"test":true}'
    assert headers["Content-Type"] == "application/json"
    assert headers["User-Agent"] == "CustomSender"
    assert headers["X-Custom"] == "value"
    assert headers["X-Agent-Bridge-Event-Id"] == "event"


def test_custom_headers_pass_local_bearer_and_api_key_auth_without_leaking(local_endpoint, caplog):
    base, received = local_endpoint
    credentials = {"authorization": "Bearer TEST_SECRET", "x-api-key": "TEST_KEY"}
    s = settings(url=base + "/auth", headers=credentials)
    assert post_json(settings(url=base + "/auth"), b"{}", "event") == 401
    received.get(timeout=1)
    assert post_json(s, b"{}", "event") == 202
    received.get(timeout=1)
    assert post_json(settings(url=base + "/ok"), b"{}", "other-event") == 204
    assert "Authorization" not in received.get(timeout=1)[1]
    assert "TEST_SECRET" not in repr(s) and "TEST_KEY" not in repr(s)
    assert "TEST_SECRET" not in caplog.text and "TEST_KEY" not in caplog.text
    credentials["authorization"] = "changed"
    assert s.headers["authorization"] == "Bearer TEST_SECRET"


@pytest.mark.parametrize("headers", [None, "invalid", {"": "TEST_SECRET"}, {"bad name": "TEST_SECRET"},
    {"X-A": "TEST_SECRET\r\nInjected: yes"}, {"X-A": "中文"}, {"X-A": 123},
    {"X-A": "one", "x-a": "TEST_SECRET"}, [{"name": "X-A", "value": "one"}, {"name": "X-A", "value": "TEST_SECRET"}],
    {"Host": "TEST_SECRET"}, {"Content-Length": "0"}, {"Transfer-Encoding": "chunked"},
    {"Connection": "close"}, {"Proxy-Authorization": "TEST_SECRET"}, {"X-Agent-Bridge-Event-Id": "TEST_SECRET"},
    {"Content-Type": "application/x-www-form-urlencoded"}, {"X-A": "TEST_SECRET" * 2000},
    {f"X-{i}": "TEST_SECRET" for i in range(33)}])
def test_invalid_headers_are_rejected_without_printing_values(headers):
    with pytest.raises((ValueError, TypeError)) as error:
        settings(headers=headers)
    assert "TEST_SECRET" not in str(error.value)


@pytest.mark.parametrize("method", ["GET", "DELETE", "post", "POST\r\n", None, 1])
def test_invalid_method_is_rejected(method):
    with pytest.raises(ValueError, match="method"):
        settings(method=method)
