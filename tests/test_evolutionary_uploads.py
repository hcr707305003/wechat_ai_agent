import hashlib
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from urllib.parse import parse_qs, urlsplit

import pytest
from test_webhook_uploads import media
from test_webhooks import msg, settings, wait_until

from agent_bridge.webhook_uploads import MIB, UploadError, UploadSettings, upload_media
from agent_bridge.webhooks import WebhookDispatcher


@pytest.fixture
def file_api():
    state = {"calls": [], "parts": {}, "fault": None}
    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            parsed = urlsplit(self.path)
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state["calls"].append((self.command, parsed.path, parse_qs(parsed.query), dict(self.headers), data))
            status = 200
            if parsed.path == "/v1/uploads":
                spec = json.loads(data)
                state["spec"] = spec
                result = {"id": "upload_test", "status": "uploading", "part_size": spec["part_size"],
                          "part_count": (spec["size"] + spec["part_size"] - 1) // spec["part_size"]}
                if state["fault"] == "init-id":
                    result["id"] = "../../other"
                elif state["fault"] == "init-size":
                    result["part_size"] += 1
                status = 201
            elif "/parts/" in parsed.path:
                index = int(parsed.path.rsplit("/", 1)[-1])
                state["parts"][index] = data
                result = {"upload_id": "upload_test", "index": index, "size": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()}
                if state["fault"] == "part-hash":
                    result["sha256"] = "0" * 64
                elif state["fault"] == "part-http":
                    status = 503
                elif state["fault"] == "cancel":
                    state["stop"].set()
            else:
                if parsed.path.endswith("/complete"):
                    data = b"".join(state["parts"][i] for i in sorted(state["parts"]))
                result = {"file_id": "file_test", "size": len(data), "status": "uploaded",
                          "sha256": hashlib.sha256(data).hexdigest()}
                if state["fault"] == "final-hash":
                    result["sha256"] = "0" * 64
            self.send_response(status)
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        do_POST = handle_request
        do_PUT = handle_request
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    config = UploadSettings(protocol="evolutionary", url=base + "/v1/files", chunk_url=base + "/v1/uploads",
                            file_id_path="file_id", chunk_size_mb=1, chunk_threshold_mb=1,
                            headers={"Authorization": "Bearer TEST_ONLY"})
    try:
        yield state, config
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize("kind", ["image", "voice", "video"])
@pytest.mark.parametrize("size", [120, MIB, 2 * MIB + 17])
def test_raw_and_chunked_wire_contract(file_api, tmp_path, kind, size):
    state, config = file_api
    message = media(tmp_path, kind, size=size)
    assert upload_media(config, message, "test-event", threading.Event()) == "file_test"
    expected_kind = "audio" if kind == "voice" else kind
    for method, path, query, headers, body in state["calls"]:
        headers = {k.lower(): v for k, v in headers.items()}
        assert headers["authorization"] == "Bearer TEST_ONLY"
        assert headers["x-agent-bridge-event-id"] == "test-event"
        assert "private-name" not in path
        if path == "/v1/uploads":
            spec = json.loads(body)
            assert spec["memory_scope"]["account_id"] == "login-account"
            assert spec["kind"] == expected_kind
            assert spec["filename"].startswith("media.")
        else:
            assert query["source"] == ["wechat"]
            assert query["account_id"] == ["login-account"]
            assert query["conversation_id"] == ["friend"]
            assert query["conversation_type"] == ["private"]
        if path == "/v1/files":
            assert body == b"X" * size
            assert query["kind"] == [expected_kind]
            assert query["sha256"] == [hashlib.sha256(body).hexdigest()]
            assert headers["content-type"] == "application/octet-stream"
    if size > MIB:
        assert list(state["parts"]) == [0, 1, 2]
        assert [len(part) for part in state["parts"].values()] == [MIB, MIB, 17]
        assert state["calls"][-1][1].endswith("/complete")
    else:
        assert len(state["calls"]) == 1


@pytest.mark.parametrize("fault", ["init-id", "init-size", "part-hash", "part-http", "cancel", "final-hash"])
def test_protocol_failures_stop_before_notification(file_api, tmp_path, fault):
    state, config = file_api
    stopped = threading.Event()
    state.update(fault=fault, stop=stopped)
    with pytest.raises(UploadError):
        upload_media(config, media(tmp_path, size=MIB + 1), "e", stopped)
    if fault != "final-hash":
        assert not any(call[1].endswith("/complete") for call in state["calls"])


@pytest.mark.parametrize("kind", ["image", "voice", "video"])
def test_memory_payload_after_real_adapter_upload(file_api, tmp_path, kind):
    _state, config = file_api
    result = Queue()
    dispatcher = WebhookDispatcher((settings(upload=config, payload_format="memory"),),
                                   post=lambda s, b, e: result.put(json.loads(b)) or 202)
    dispatcher.start()
    try:
        dispatcher.submit(media(tmp_path, kind), ())
        wait_until(lambda: not result.empty())
        body = result.get_nowait()
        assert body["content"] == "file_test"
        assert body["content_type"] == ("audio" if kind == "voice" else kind)
        assert body["account_id"] == "login-account"
        assert body["conversation_type"] == "private"
        assert len(body) == 9
    finally:
        dispatcher.stop()


def test_memory_text_and_placeholder_never_label_marker_as_media():
    from agent_bridge.models import ContentType

    result = Queue()
    dispatcher = WebhookDispatcher((settings(payload_format="memory"),),
                                   post=lambda s, b, e: result.put(json.loads(b)) or 202)
    dispatcher.start()
    try:
        dispatcher.submit(msg("text"), ())
        dispatcher.submit(replace(msg("image"), content_type=ContentType.IMAGE), ())
        wait_until(lambda: result.qsize() == 2)
        bodies = [result.get_nowait(), result.get_nowait()]
        assert [body["content"] for body in bodies] == ["你好，原始内容", "[图片]"]
        assert all(body["content_type"] == "text" for body in bodies)
    finally:
        dispatcher.stop()


def test_evolutionary_config_invalid():
    for kwargs in ({"protocol": "bad"}, {"protocol": "evolutionary", "method": "PUT"},
                   {"protocol": "evolutionary", "url": "http://localhost/files?secret=x"}):
        with pytest.raises(ValueError):
            UploadSettings(**kwargs)
    with pytest.raises(ValueError):
        settings(payload_format="unknown")
