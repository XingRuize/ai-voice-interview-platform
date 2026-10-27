import http.client
import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    requests_stub = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    requests_stub.RequestException = RequestException
    requests_stub.get = lambda *args, **kwargs: None
    requests_stub.post = lambda *args, **kwargs: None
    sys.modules["requests"] = requests_stub

import web_app


class HttpResilienceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_data_dir = web_app.DATA_DIR
        self.original_log_dir = web_app.LOG_DIR
        self.original_runtime_log = web_app.RUNTIME_LOG
        web_app.DATA_DIR = Path(self.temp_dir.name) / "data"
        web_app.LOG_DIR = Path(self.temp_dir.name) / "logs"
        web_app.RUNTIME_LOG = web_app.LOG_DIR / "web_app.log"
        web_app.DATA_DIR.mkdir(parents=True)
        self.server = web_app.ThreadingHTTPServer(("127.0.0.1", 0), web_app.AppHandler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        web_app.DATA_DIR = self.original_data_dir
        web_app.LOG_DIR = self.original_log_dir
        web_app.RUNTIME_LOG = self.original_runtime_log
        self.temp_dir.cleanup()

    def request(self, method, path, body=b"", headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=5
        )
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, dict(response.headers), payload

    def test_empty_upload_returns_json_instead_of_dropping_connection(self):
        status, headers, payload = self.request(
            "POST",
            "/api/sessions/missing/audio",
            headers={"Content-Type": "audio/webm", "X-Request-Id": "empty-test"},
        )
        self.assertEqual(status, 400)
        self.assertIn("没有收到录音数据", payload["error"])
        self.assertEqual(payload["request_id"], "empty-test")
        self.assertEqual(headers["X-Request-Id"], "empty-test")

    def test_retried_upload_uses_saved_result(self):
        session_id = "retry_session"
        session = {
            "session_id": session_id,
            "completed": False,
            "responses": [],
            "pending_audio": {
                "upload_id": "same-upload",
                "asr_text": "已经识别的回答",
                "audio_duration_seconds": 3.25,
            },
        }
        web_app.save_session(session)
        status, _, payload = self.request(
            "POST",
            f"/api/sessions/{session_id}/audio",
            body=b"x",
            headers={
                "Content-Type": "audio/webm",
                "X-Upload-Id": "same-upload",
                "X-Request-Id": "retry-test",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["asr_text"], "已经识别的回答")
        self.assertEqual(payload["duration_seconds"], 3.25)

    def test_backend_exception_is_returned_as_json(self):
        original = web_app.AppHandler.upload_audio

        def fail_upload(*args, **kwargs):
            raise RuntimeError("simulated conversion failure")

        web_app.AppHandler.upload_audio = fail_upload
        try:
            status, headers, payload = self.request(
                "POST",
                "/api/sessions/test/audio",
                body=b"audio",
                headers={
                    "Content-Type": "audio/webm",
                    "X-Request-Id": "exception-test",
                },
            )
        finally:
            web_app.AppHandler.upload_audio = original
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], "simulated conversion failure")
        self.assertEqual(payload["request_id"], "exception-test")
        self.assertEqual(headers["X-Request-Id"], "exception-test")


if __name__ == "__main__":
    unittest.main()
