"""批处理执行器 — 在 JobStore 驱动的进度追踪下并发运行处理函数。"""

import logging
import signal
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from backend.core.job.schema import Job, JobItem
from backend.core.job.store import JobStore
from backend.core.job.tracker import JobTracker

logger = logging.getLogger("infosystem.batch")

ProcessFunc = Callable[[dict], str]
"""处理函数签名：接收 item.input dict，返回结果字符串。"""


class BatchProcessor:
    """并发批处理，带断点续传和 Ctrl+C 安全停止。

    Args:
        store:         JobStore 实例。
        process_item:  接收 item.input dict，返回结果文本。
        workers:       最大并行线程数。
        on_success:    每个 item 成功后的回调 (item, result)。
    """

    def __init__(
        self,
        store: JobStore,
        process_item: ProcessFunc,
        workers: int = 5,
        on_success: Callable[[JobItem, str], None] | None = None,
    ):
        self.store = store
        self.process_item = process_item
        self.workers = workers
        self.on_success = on_success
        self._stop = False

    def run(self, job: Job) -> Job:
        tracker = JobTracker(self.store, job)
        pending = tracker.get_pending()

        if not pending:
            logger.info("没有待处理条目")
            return job

        summary = tracker.summary()
        logger.info(
            "开始处理：总计 %(total)d，已完成 %(success)d，待处理 %(pending)d",
            summary,
        )

        signal.signal(signal.SIGINT, self._handle_interrupt)

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            future_to_item: dict = {}

            for item in pending:
                if self._stop:
                    break
                tracker.mark_start(item.id)
                future = executor.submit(self._safe_process, item)
                future_to_item[future] = item

            for future in as_completed(future_to_item):
                if self._stop:
                    logger.warning("处理被中断，进度已保存")
                    break
                item = future_to_item[future]
                ok, result = future.result()
                if ok:
                    tracker.commit_success(item.id, result)
                    if self.on_success:
                        self._safe_callback(item, result)
                else:
                    tracker.commit_failed(item.id, result)

        final = tracker.summary()
        logger.info("批处理结束：成功 %(success)d，失败 %(failed)d，剩余 %(pending)d", final)
        return job

    def _safe_process(self, item: JobItem) -> tuple[bool, str]:
        """捕获异常确保线程不崩溃。"""
        try:
            result = self.process_item(item.input)
            return True, result
        except Exception as e:
            return False, str(e)

    def _safe_callback(self, item: JobItem, result: str) -> None:
        try:
            self.on_success(item, result)
        except Exception as cb_err:
            logger.error("on_success 回调出错 (item %d): %s", item.id, cb_err)

    def _handle_interrupt(self, signum, frame) -> None:
        logger.warning("收到中断信号，正在安全停止...")
        self._stop = True
