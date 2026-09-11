import json
import threading
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue

import pytest
from test_webhooks import msg, settings, wait_until

from agent_bridge.models import Attachment, ContentType
from agent_bridge.webhook_uploads import (
    MIB,
    ChunkProtocolUnavailable,
    UploadError,
    UploadSettings,
    extract_file_id,
    multipart_upload,
    parse_upload,
    path_tokens,
    upload_media,
)
from agent_bridge.webhooks import WebhookDispatcher, message_payload, parse_webhooks


@pytest.mark.parametrize("response,path", [
    ({"field": "app_id"}, "field"),
    ({"data": {"field": "app_id"}}, "data->field"),
    ({"data": [{"field": "app_id"}]}, "data[0]->field"),
    ({"a": [[{"id": "app_id"}]]}, "a[0][0]->id"),
])
def test_user_file_id_paths(response, path):
    assert extract_file_id(response, path) == "app_id"


@pytest.mark.parametrize("path", ["", "data->", "->field", "data[-1]", "data[*]", "data[01]",
                                   "data[]", "data[0]field", "a\n", "data[1:2]", None, "a" * 257])
def test_invalid_paths(path):
    with pytest.raises(ValueError):
        path_tokens(path)


@pytest.mark.parametrize("response,path", [({}, "field"), ({"data": []}, "data[0]->field"),
    ({"data": {}}, "data[0]"), ({"data": []}, "data->field"),
    *[({"field": v}, "field") for v in (None, True, False, [], {}, "", "  ", "a\nb", 1.2)]])
def test_missing_or_invalid_id_is_failure(response, path):
    with pytest.raises(UploadError):
        extract_file_id(response, path)
    assert extract_file_id({"id": 0}, "id") == "0"


@pytest.mark.parametrize("config", [None, [], {"unknown": 1}, {"method": "GET"},
    {"url": "file:///secret"}, {"url": "https://secret:secret@example.com"},
    {"chunk_size_mb": True}, {"chunk_threshold_mb": 0}, {"max_file_mb": 3},
    {"timeout_seconds": float("nan")}, {"file_field": 'file"\r\nx:'},
    {"headers": {"Content-Type": "multipart/form-data"}},
    {"headers": {"Authorization": "secret\r\nX: x"}}])
def test_invalid_upload_settings(config):
    with pytest.raises((TypeError, ValueError)) as error:
        parse_webhooks([{"upload": config}])
    assert "secret" not in str(error.value)


def test_config_roundtrip_and_independence():
    rows = [{"name": "one", "upload": {"url": "https://one.example/upload", "file_id_path": "field",
                                       "headers": {"X-Key": "ONE"}}},
            {"name": "two", "upload": {"url": "https://two.example/upload", "file_id_path": "data[0]->field"}}]
    hooks = parse_webhooks(rows)
    assert hooks == parse_webhooks([asdict(hook) for hook in hooks])
    rows[0]["upload"]["headers"].clear()
    assert hooks[0].upload.headers == {"X-Key": "ONE"}
    assert hooks[1].upload.headers == {}
    assert parse_upload({}).url == ""


def media(tmp_path, kind="image", size=10, suffix=None):
    path = tmp_path / ("private-name" + (suffix or {"image": ".png", "voice": ".mp3", "video": ".mp4"}[kind]))
    path.write_bytes(b"X" * size)
    return msg(content_type=ContentType(kind), attachments=(Attachment(kind, path=str(path)),))


@pytest.mark.parametrize("kind", ["image", "voice", "video"])
@pytest.mark.parametrize("size,expected", [(MIB, "small"), (MIB + 1, "chunked")])
def test_size_routing_and_final_response_id(tmp_path, kind, size, expected):
    calls = []
    def small(*args):
        calls.append("small")
        return {"data": [{"field": "FINAL_SMALL"}]}
    def chunks(*args):
        calls.append("chunked")
        return {"data": [{"field": "FINAL_CHUNKED"}]}
    config = UploadSettings(chunk_threshold_mb=1, file_id_path="data[0]->field")
    assert upload_media(config, media(tmp_path, kind, size), "event", threading.Event(),
                        small=small, chunked=chunks) == "FINAL_" + expected.upper()
    assert calls == [expected]


def test_no_chunk_protocol_never_falls_back_to_whole_file(tmp_path):
    with pytest.raises(ChunkProtocolUnavailable):
        upload_media(UploadSettings(chunk_threshold_mb=1), media(tmp_path, size=MIB + 1),
                     "event", threading.Event(), small=lambda *a: pytest.fail("must not upload whole file"))


@pytest.mark.parametrize("kind,suffix", [("voice", ".silk"), ("video", ".avi")])
def test_unconverted_media_not_uploaded(tmp_path, kind, suffix):
    with pytest.raises(UploadError, match="转换"):
        upload_media(UploadSettings(), media(tmp_path, kind, suffix=suffix), "e", threading.Event(),
                     small=lambda *a: pytest.fail("must not upload unconverted media"))


def test_missing_oversize_and_cancelled(tmp_path):
    config = UploadSettings(chunk_threshold_mb=1, max_file_mb=1)
    with pytest.raises(UploadError, match="本地媒体"):
        upload_media(config, msg(content_type=ContentType.IMAGE), "e", threading.Event())
    for size in (0, MIB + 1):
        with pytest.raises(UploadError, match="最大文件|为空"):
            upload_media(config, media(tmp_path, size=size), "e", threading.Event())
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(UploadError, match="取消"):
        upload_media(config, media(tmp_path), "e", stopped)


def test_independent_upload_ids_and_no_repeat_on_webhook_retry(tmp_path):
    results, uploads, attempts = Queue(), Queue(), {}
    def upload(config, message, event_id, stopped):
        uploads.put(config.url)
        return "id-" + config.file_id_path
    def post(endpoint, body, event_id):
        attempts[endpoint.name] = attempts.get(endpoint.name, 0) + 1
        if attempts[endpoint.name] == 1:
            return 503
        results.put((endpoint.name, json.loads(body)))
        return 204
    hooks = tuple(settings(name=name, upload={"url": "https://" + name + ".example/upload",
                                             "file_id_path": name}) for name in ("one", "two"))
    dispatcher = WebhookDispatcher(hooks, upload=upload, post=post, retry_delay=0)
    dispatcher.start()
    try:
        message = media(tmp_path)
        dispatcher.submit(message, ())
        wait_until(lambda: results.qsize() == 2)
        assert uploads.qsize() == 2
        for _ in range(2):
            name, payload = results.get_nowait()
            assert payload == {**message_payload(message, ()), "content": "id-" + name}
    finally:
        dispatcher.stop()


def test_no_url_placeholders_text_and_filters_never_upload(tmp_path):
    results = Queue()
    hooks = (settings(name="markers"), settings(name="text", content_types=["text"],
                                               upload={"url": "https://example.com/upload"}))
    dispatcher = WebhookDispatcher(hooks, upload=lambda *a: pytest.fail("unexpected upload"),
                                   post=lambda s, b, e: results.put((s.name, json.loads(b)["content"])) or 200)
    dispatcher.start()
    try:
        for kind in ("image", "voice", "video"):
            dispatcher.submit(replace(media(tmp_path, kind), message_id=kind), ())
        dispatcher.submit(msg("text"), ())
        wait_until(lambda: results.qsize() == 5)
        assert sorted(results.queue) == sorted([
            ("markers", "[图片]"), ("markers", "[语音]"), ("markers", "[视频]"),
            ("markers", "你好，原始内容"), ("text", "你好，原始内容")])
    finally:
        dispatcher.stop()


def test_failure_does_not_send_placeholder_or_block_other_endpoint(tmp_path, caplog):
    caplog.set_level("INFO")
    calls = Queue()
    def failing(*args):
        raise RuntimeError("URL SECRET response BODY")
    dispatcher = WebhookDispatcher((settings(name="bad", upload={"url": "https://example.com/upload"}),
                                    settings(name="good")), upload=failing,
                                   post=lambda s, b, e: calls.put(s.name) or 200)
    dispatcher.start()
    try:
        dispatcher.submit(media(tmp_path), ())
        wait_until(lambda: calls.qsize() == 1 and "媒体上传失败" in caplog.text)
        assert calls.get_nowait() == "good"
        assert "SECRET" not in caplog.text
    finally:
        dispatcher.stop()


@pytest.fixture
def upload_server():
    requests = Queue()
    response = {"status": 200, "body": b'{"data":[{"field":"file-123"}]}'}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.put((dict(self.headers), self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(response["status"])
            self.send_header("Location", "/should-not-follow")
            self.end_headers()
            self.wfile.write(response["body"])
        do_PUT = do_POST
        do_PATCH = do_POST
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/upload", requests, response
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
def test_real_localhost_multipart_headers_and_file_id(tmp_path, upload_server, method):
    url, requests, _response = upload_server
    config = UploadSettings(url=url, method=method, headers={"Authorization": "Bearer TEST"},
                            file_field="media", file_id_path="data[0]->field")
    message = media(tmp_path, size=150000)
    assert upload_media(config, message, "event-123", threading.Event()) == "file-123"
    headers, body = requests.get_nowait()
    headers = {key.lower(): value for key, value in headers.items()}
    assert headers["authorization"] == "Bearer TEST"
    assert headers["x-agent-bridge-event-id"] == "event-123"
    assert "multipart/form-data; boundary=" in headers["content-type"]
    assert len(body) == int(headers["content-length"])
    assert b'name="media"; filename="media.png"' in body
    assert b"X" * 150000 in body
    assert b"private-name" not in body


@pytest.mark.parametrize("status,body,reason", [(302, b"", "HTTP 302"), (422, b"SECRET", "HTTP 422"),
    (200, b"SECRET_NOT_JSON", "有效 JSON"), (200, b"X" * 65537, "64 KiB")],
    ids=["redirect", "http-error", "invalid-json", "oversized"])
def test_upload_rejects_redirect_error_and_invalid_response(tmp_path, upload_server, status, body, reason):
    url, requests, response = upload_server
    response.update(status=status, body=body)
    with pytest.raises(UploadError, match=reason) as error:
        multipart_upload(UploadSettings(url=url), Path(media(tmp_path).attachments[0].path),
                         "e", threading.Event())
    assert "SECRET" not in str(error.value)
    assert requests.qsize() == 1
