"""LLM 调用客户端 — 基础设施层，不耦合任何业务逻辑。

支持同步和异步两种调用模式：
- call()             同步：发请求 → 等响应 → 返回文本
- submit_image()     异步：提交 → 返回 JobTicket(立即返回)
- poll_job()         查询 JobTicket 状态
- wait_job()         轮询直到完成

对于 OpenAI 格式模型（Qwen、GLM）：异步走线程池
对于 PaddleOCR-VL：异步走原生 Job API（提交 → 轮询）
"""

import base64
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

import requests

logger = logging.getLogger("infosystem.llm")


@dataclass
class LLMConfig:
    endpoint: str
    api_key: str
    model: str
    max_retries: int = 3
    retry_base_delay: float = 2.0
    request_template: dict | None = None
    timeout: int = 120
    job_api_type: Literal["openai", "paddleocr"] = "openai"


@dataclass
class JobTicket:
    """异步作业票据。"""
    job_id: str
    status: Literal["pending", "running", "done", "failed"]
    result: Optional[str] = None
    error: Optional[str] = None
    _future = None  # 内部使用，仅用于线程池模式


class LLMClient:
    """通用 LLM 客户端，支持 text / image / multimodal 消息和工具调用。"""

    def __init__(self, config: LLMConfig):
        self.config = config
        self._executor: ThreadPoolExecutor | None = None

    # ── 同步调用（不变） ────────────────────────

    def call(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 2000,
        temperature: float = 0.3,
        extra_body: dict | None = None,
    ) -> str:
        """发送消息并返回文本响应（同步）。"""
        payload = {
            "model": self.config.model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self.config.request_template:
            template = dict(self.config.request_template)
            template.pop("messages", None)
            payload = {**template, **payload}
        if extra_body:
            payload.update(extra_body)
        if tools:
            payload["tools"] = tools

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

        last_error: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                resp = requests.post(
                    self.config.endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self.config.timeout,
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

                body = resp.json()
                msg = body["choices"][0]["message"]

                if tool_calls := msg.get("tool_calls"):
                    return tool_calls[0]["function"]["arguments"]
                return msg["content"]

            except Exception as exc:
                last_error = exc
                if attempt < self.config.max_retries:
                    delay = self.config.retry_base_delay * (2 ** (attempt - 1))
                    time.sleep(delay)

        raise RuntimeError(
            f"LLM call failed after {self.config.max_retries} retries: {last_error}"
        )

    # ── 异步提交（统一接口） ────────────────────

    def submit_image(self, image_path: str, prompt: str = "",
                     max_tokens: int = 4096, temperature: float = 0.3) -> JobTicket:
        """异步提交图片识别任务。

        Args:
            image_path: 图片文件路径。
            prompt:     用户提示词。
            max_tokens: 最大输出 token 数。
            temperature: 采样温度。

        Returns:
            立即返回 JobTicket，通过 poll_job / wait_job 获取结果。
        """
        job_id = uuid.uuid4().hex[:12]

        if self.config.job_api_type == "paddleocr":
            return self._submit_paddleocr(image_path, job_id, prompt)
        else:
            return self._submit_openai_image(image_path, prompt, job_id,
                                              max_tokens, temperature)

    def poll_job(self, ticket: JobTicket) -> JobTicket:
        """查询作业状态。返回更新后的 ticket（不修改原对象）。"""
        if self.config.job_api_type == "paddleocr":
            return self._poll_paddleocr(ticket)
        else:
            return self._poll_openai(ticket)

    def wait_job(self, ticket: JobTicket, poll_interval: float = 2.0,
                 timeout: float = 300.0) -> str:
        """轮询直到作业完成，返回结果文本。

        Args:
            ticket:       JobTicket。
            poll_interval: 轮询间隔（秒）。
            timeout:       最大等待时间（秒）。

        Returns:
            识别结果文本。

        Raises:
            TimeoutError: 超时。
            RuntimeError: 作业失败。
        """
        start = time.time()
        while ticket.status in ("pending", "running"):
            if time.time() - start > timeout:
                raise TimeoutError(
                    f"Job {ticket.job_id} 超时 ({timeout}s)"
                )
            time.sleep(poll_interval)
            ticket = self.poll_job(ticket)

        if ticket.status == "failed":
            raise RuntimeError(
                f"Job {ticket.job_id} 失败: {ticket.error}"
            )
        return ticket.result or ""

    # ── OpenAI 格式：线程池异步 ─────────────────

    def _submit_openai_image(self, image_path: str, prompt: str,
                              job_id: str, max_tokens: int,
                              temperature: float) -> JobTicket:
        """OpenAI 格式模型：在线程中执行同步 call，返回立即返回的 ticket。"""
        if not self._executor:
            self._executor = ThreadPoolExecutor(max_workers=4)

        messages = self.build_messages(
            system_prompt="",
            user_prompt=prompt or "请提取这张图片中的所有文字内容。",
            input_data=image_path,
            input_type="image",
        )

        ticket = JobTicket(job_id=job_id, status="running")

        def _work():
            try:
                result = self.call(
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                ticket.status = "done"
                ticket.result = result
            except Exception as e:
                ticket.status = "failed"
                ticket.error = str(e)

        ticket._future = self._executor.submit(_work)
        return ticket

    @staticmethod
    def _poll_openai(ticket: JobTicket) -> JobTicket:
        """OpenAI 格式：检查 future 是否完成。"""
        if ticket._future and ticket._future.done():
            # future 执行时已更新了 ticket 状态
            pass
        return ticket

    # ── PaddleOCR-VL：原生 Job API ──────────────

    def _submit_paddleocr(self, image_path: str, job_id: str,
                           prompt: str) -> JobTicket:
        """PaddleOCR-VL：提交文件到 AI Studio Job API。"""
        headers = {"Authorization": f"bearer {self.config.api_key}"}
        payload_data = {
            "model": self.config.model,
            "optionalPayload": json.dumps({
                "useDocOrientationClassify": False,
                "useDocUnwarping": False,
                "useChartRecognition": False,
            }),
        }
        try:
            with open(image_path, "rb") as f:
                files = {"file": f}
                resp = requests.post(
                    self.config.endpoint,
                    headers=headers,
                    data=payload_data,
                    files=files,
                    timeout=30,
                )
            if resp.status_code != 200:
                return JobTicket(
                    job_id=job_id, status="failed",
                    error=f"提交失败 HTTP {resp.status_code}: {resp.text[:200]}",
                )
            real_job_id = resp.json()["data"]["jobId"]
            return JobTicket(job_id=real_job_id, status="running")

        except Exception as e:
            return JobTicket(job_id=job_id, status="failed", error=str(e))

    def _poll_paddleocr(self, ticket: JobTicket) -> JobTicket:
        """PaddleOCR-VL：查询作业状态。"""
        try:
            headers = {"Authorization": f"bearer {self.config.api_key}"}
            url = f"{self.config.endpoint}/{ticket.job_id}"
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code != 200:
                return JobTicket(
                    job_id=ticket.job_id, status="failed",
                    error=f"查询失败 HTTP {resp.status_code}",
                )

            data = resp.json()["data"]
            state = data["state"]

            if state == "done":
                jsonl_url = data["resultUrl"]["jsonUrl"]
                text = self._paddleocr_download(jsonl_url)
                return JobTicket(
                    job_id=ticket.job_id, status="done", result=text,
                )
            elif state == "failed":
                return JobTicket(
                    job_id=ticket.job_id, status="failed",
                    error=data.get("errorMsg", "未知错误"),
                )
            else:
                # pending / running
                return JobTicket(job_id=ticket.job_id, status="running")

        except Exception as e:
            return JobTicket(job_id=ticket.job_id, status="failed", error=str(e))

    @staticmethod
    def _paddleocr_download(jsonl_url: str) -> str:
        """从 PaddleOCR JSONL 结果中提取所有文本。"""
        resp = requests.get(jsonl_url, timeout=60)
        resp.raise_for_status()
        texts = []
        for line in resp.text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            result = json.loads(line)["result"]
            for res in result["layoutParsingResults"]:
                texts.append(res["markdown"]["text"])
        return "\n\n".join(texts)

    # ── 消息构建（不变） ────────────────────────

    def build_messages(
        self,
        system_prompt: str = "",
        user_prompt: str = "",
        input_data: str = "",
        input_type: str = "text",
    ) -> list[dict]:
        """构建 OpenAI-format messages。"""
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        if input_type == "text":
            content = user_prompt or input_data
            messages.append({"role": "user", "content": content})

        elif input_type == "image":
            b64 = self._encode_image(input_data)
            messages.append({
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": user_prompt or "请分析这张图片。"},
                ],
            })

        elif input_type == "multimodal":
            parts = json.loads(input_data) if isinstance(input_data, str) else input_data
            content: list[dict] = []
            if "image" in parts:
                b64 = self._encode_image(parts["image"])
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                })
            text = user_prompt or parts.get("text", "")
            if text:
                content.append({"type": "text", "text": text})
            messages.append({"role": "user", "content": content})

        else:
            messages.append({"role": "user", "content": user_prompt or str(input_data)})

        return messages

    @staticmethod
    def _encode_image(path: str) -> str:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Image not found: {path}")
        return base64.b64encode(p.read_bytes()).decode("utf-8")
