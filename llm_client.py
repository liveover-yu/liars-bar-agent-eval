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
    ):
        """初始化客户端。

        temperature=0 对应 OpenAI 兼容网关的确定性模式；seed 由支持它的
        后端使用，不支持时会被忽略（不会报错）。两者都要记录进结果，
        否则复现不了实验。
        """
        self.client = OpenAI(
            api_key=api_key or DEFAULT_API_KEY,
            base_url=base_url or DEFAULT_BASE_URL,
            max_retries=max_retries,
        )
        self.temperature = temperature
        self.seed = seed
        self.log_path = Path(log_path) if log_path else None
        self.call_count = 0
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def chat(self, messages, model="deepseek-chat"):
        """发一次请求，返回 (content, reasoning_content)。

        任何网络/接口错误都会抛出，由调用方决定是重试还是记为失败。
        「静默返回空串」让上游付出了整批数据作废的代价。
        """
        started = time.time()
        response = self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=self.temperature,
            seed=self.seed,
        )
        self.call_count += 1

        message = response.choices[0].message
        content = message.content or ""
        reasoning_content = getattr(message, "reasoning_content", "") or ""

        if self.log_path:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": started,
                    "latency_s": round(time.time() - started, 3),
                    "model": model,
                    "temperature": self.temperature,
                    "seed": self.seed,
                    "messages": messages,
                    "content": content,
                    "reasoning_content": reasoning_content,
                    "usage": response.usage.model_dump() if response.usage else None,
                }, ensure_ascii=False) + "\n")

        return content, reasoning_content


if __name__ == "__main__":
    llm = LLMClient()
    print(llm.chat([{"role": "user", "content": "回复 ok"}]))
