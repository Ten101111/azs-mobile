"""Ошибки Ollama: причина из тела ответа доходит до пользователя и журнала.

Раньше при ответе 500 было видно только «Internal Server Error» — без причины
(нехватка памяти, модель не найдена, процесс упал). Теперь текст Ollama
и подсказка, что делать, попадают в сообщение об ошибке. Один размер контекста
для всех вызовов не даёт Ollama перезагружать модель между быстрым путём
и агентом.
"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from backend.ai import generator
from backend.ai.agent import llm


class _Handler(BaseHTTPRequestHandler):
    status = 500
    body = {"error": "model requires more system memory (9.2 GiB) than is available (6.1 GiB)"}
    seen: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        _Handler.seen.append(json.loads(self.rfile.read(length) or b"{}"))
        data = json.dumps(self.body).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class OllamaErrorTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self._host = generator.OLLAMA_HOST
        generator.OLLAMA_HOST = f"http://127.0.0.1:{self.server.server_port}"
        _Handler.seen = []

    def tearDown(self):
        generator.OLLAMA_HOST = self._host
        self.server.shutdown()
        self.server.server_close()

    def test_server_error_carries_ollama_reason_and_hint(self):
        with self.assertRaises(generator.ModelUnavailable) as caught:
            generator._post("/api/chat", {"model": "x"})
        text = str(caught.exception)
        self.assertIn("ошибкой 500", text)
        self.assertIn("model requires more system memory", text)
        self.assertIn("AI_NUM_CTX", text)

    def test_agent_and_fast_path_use_the_same_context_size(self):
        self.assertEqual(llm.NUM_CTX, generator.NUM_CTX)
        _Handler.body = {"error": "llama runner process has terminated: signal: killed"}
        try:
            with self.assertRaises(llm.ModelUnavailable) as caught:
                llm.OllamaChat().chat([{"role": "user", "content": "привет"}])
        finally:
            _Handler.body = OllamaErrorTests._memory_body()
        self.assertIn("перезапустите Ollama", str(caught.exception))
        self.assertEqual(_Handler.seen[-1]["options"]["num_ctx"], generator.NUM_CTX)
        # Модель держится в памяти дольше пяти минут Ollama по умолчанию (AI_KEEP_ALIVE).
        self.assertEqual(_Handler.seen[-1]["keep_alive"], generator.KEEP_ALIVE)

    @staticmethod
    def _memory_body():
        return {"error": "model requires more system memory (9.2 GiB) than is available (6.1 GiB)"}


if __name__ == "__main__":
    unittest.main()
