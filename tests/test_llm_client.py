"""客户端回归测试：不发网络请求，检查计费元数据与错误日志。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from llm_client import LLMClient


class ClientTests(unittest.TestCase):
    def test_usage_survives_unparseable_output_and_next_error_does_not_reuse_it(self):
        requests = []
        def create(**kwargs):
            requests.append(kwargs)
            if len(requests) == 2:
                raise RuntimeError("sensitive-provider-body")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="invalid-json"))],
                usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 11, "completion_tokens": 3}),
                model="resolved-model-version", _request_id="request-1",
            )
        closed = []
        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                               close=lambda: closed.append(True))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "raw.jsonl"
            with LLMClient(client=fake, log_path=path, seed=17, max_output_tokens=64) as llm:
                content, _ = llm.chat([{"role": "user", "content": "test"}], tag="case:0")
                self.assertEqual(content, "invalid-json")
                self.assertEqual(llm.last_response["usage"]["prompt_tokens"], 11)
                self.assertEqual(llm.last_response["model"], "resolved-model-version")
                with self.assertRaises(RuntimeError):
                    llm.chat([], tag="case:1")
                self.assertEqual(llm.last_response["usage"], {})
                self.assertEqual(llm.call_count, 2)
            self.assertTrue(closed)
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([r["outcome"] for r in records], ["SUCCESS", "ERROR"])
            self.assertEqual(records[0]["tag"], "case:0")
            self.assertNotIn("sensitive-provider-body", path.read_text(encoding="utf-8"))
            self.assertEqual(requests[0]["max_tokens"], 64)
            self.assertEqual(requests[0]["seed"], 17)

    def test_none_seed_omits_unsupported_parameter(self):
        requests = []
        def create(**kwargs):
            requests.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
                                   usage=None, model="test")
        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        LLMClient(client=fake, seed=None).chat([])
        self.assertNotIn("seed", requests[0])


if __name__ == "__main__":
    unittest.main()
