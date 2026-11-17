"""LLM 客户端封装（OpenAI 兼容接口）。

相对上游的三处改动，都是为了「测量」而不是「表演」：

1. ``temperature`` / ``seed`` 显式传参——上游一个都没设，导致同一 prompt
   两次采样结果不同，策略差异和采样噪声混在一起，无法归因。
2. 出错时抛异常而不是返回 ``("", "")``——上游把错误吞掉返回空串，
   调用方拿到的空串会被当成「格式不对」而重试 5 次，最后崩掉整批。
3. 可选 ``log_path``：每次请求和响应的原始内容落盘，便于事后复查
   「是模型没答对，还是解析错了」。

配置走环境变量，避免 key 进 Git：
    export LIARS_API_BASE_URL=https://...
    export LIARS_API_KEY=sk-...
"""

import json
import os
import time
import threading
from pathlib import Path

from openai import OpenAI

DEFAULT_BASE_URL = os.environ.get("LIARS_API_BASE_URL", "https://api.openai.com/v1")
DEFAULT_API_KEY = os.environ.get("LIARS_API_KEY", "")


class LLMClient:
    def __init__(
        self,
        api_key=None,
        base_url=None,
        temperature=0.0,
        seed=0,
        max_retries=3,
        log_path=None,
        timeout=30.0,
        max_output_tokens=None,
        client=None,
    ):
        """初始化客户端。

        temperature/seed 是实验参数，不保证供应商输出确定。供应商不支持
        seed 时会显式报错；不会静默删除参数。worker 将 SDK 重试设为 0，
        每次外部调用先在控制面预留预算，再由 worker 分类重试。
        """
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_output_tokens is not None and max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        self.client = client or OpenAI(
            api_key=api_key or DEFAULT_API_KEY,
            base_url=base_url or DEFAULT_BASE_URL,
            max_retries=max_retries,
            timeout=timeout,
        )
        self.temperature = temperature
        self.seed = seed
        self.max_output_tokens = max_output_tokens
        self.last_response = {}
        self._log_lock = threading.Lock()
        self.log_path = Path(log_path) if log_path else None
        self.call_count = 0
        #: 探针用：本次调用的标签，写进日志供断点续跑精确匹配。
        self.tag = None
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def chat(self, messages, model="deepseek-chat", tag=None):
        """发一次请求，返回 (content, reasoning_content)。

        任何网络/接口错误都会抛出，由调用方决定是重试还是记为失败。
        「静默返回空串」让上游付出了整批数据作废的代价。

        ``tag`` 是调用方给的标签（探针用来记 rule_base + probe_id），只写进
        日志，不影响请求。没有它就只能靠 prompt 文本反推这是哪个牌局，
        断点续跑会变得很脆。
        """
        started = time.time()
        monotonic_started = time.monotonic()
        self.last_response = {}
        self.call_count += 1
        request = dict(model=model, messages=messages, temperature=self.temperature)
        if self.seed is not None:
            request["seed"] = self.seed
        if self.max_output_tokens is not None:
            request["max_tokens"] = self.max_output_tokens
        record = {
            "ts": started, "model": model, "requested_model": model, "temperature": self.temperature,
            "seed": self.seed, "tag": tag or self.tag, "messages": messages,
        }
        try:
            response = self.client.chat.completions.create(**request)
            usage = response.usage.model_dump() if response.usage else {}
            self.last_response = {
                "model": getattr(response, "model", None) or model,
                "request_id": getattr(response, "_request_id", None),
                "usage": usage,
                "latency_s": time.monotonic() - monotonic_started,
            }
            message = response.choices[0].message
            content = message.content or ""
            reasoning_content = getattr(message, "reasoning_content", "") or ""
            record.update(self.last_response, outcome="SUCCESS", content=content,
                          reasoning_content=reasoning_content)
            return content, reasoning_content
        except Exception as exc:
            # 只记录异常类型；供应商异常正文可能含请求头、URL 或敏感配置。
            self.last_response.setdefault("model", model)
            self.last_response.setdefault("usage", {})
            self.last_response.update(
                latency_s=time.monotonic() - monotonic_started,
                request_id=getattr(exc, "request_id", None),
                error_type=type(exc).__name__,
            )
            record.update(self.last_response, outcome="ERROR")
            raise
        finally:
            if self.log_path:
                with self._log_lock, self.log_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


if __name__ == "__main__":
    llm = LLMClient()
    print(llm.chat([{"role": "user", "content": "回复 ok"}]))
