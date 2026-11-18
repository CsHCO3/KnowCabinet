"""远程 PDF 解析 + 翻译服务。"""

from typing import Any

from backend.services.pdf_remote.parser import PDFParser


class RemotePDFService:
    def submit_batch(self, file_list: list[str] | None = None,
                     translate_requested: bool = False) -> dict[str, Any]:
        """提交远程 PDF 批量转换任务。

        Args:
            file_list: 指定 PDF 文件列表，None 则自动扫描 pdfs/ 目录
            translate_requested: 是否标记为"后续需要翻译"
        """
        parser = PDFParser()
        try:
            if not parser.ssh_client.connect():
                return {"success": False, "error": "SSH connection failed"}
            return parser.submit_batch_task(
                file_list=file_list,
                translate_requested=translate_requested,
            )
        finally:
            parser.close()

    def submit_mineru(self,
                      file_list: list[str],
                      method: str | None = None,
                      lang: str | None = None,
                      backend: str = "pipeline",
                      formula: str | None = None,
                      table: str | None = None) -> dict[str, Any]:
        """提交远程 mineru 转换任务(pdf/pptx/docx/xlsx/图片 → Markdown)。

        产物为 {stem}.tar(md + images/)，经 check_and_collect 拉回并解包。
        """
        parser = PDFParser()
        try:
            if not parser.ssh_client.connect():
                return {"success": False, "error": "SSH connection failed"}
            return parser.submit_mineru_task(
                file_list=file_list,
                method=method,
                lang=lang,
                backend=backend,
                formula=formula,
                table=table,
            )
        finally:
            parser.close()

    def submit_translate(self,
                         md_file_names: list[str] | None = None,
                         model: str = "qwen3:8b",
                         detect: bool = True,
                         content_type: str = "paper",
                         prompt_override: str | None = None,
                         source_job_id: str | None = None) -> dict[str, Any]:
        """提交远程翻译任务。

        Args:
            md_file_names: 远程 .md 文件名列表（已在 remote_output_dir 中）
            model: 翻译模型（qwen3:8b / qwen3:32b）
            detect: 是否自动检测中文占比（跳过已有大量中文的内容）
            content_type: 内容类型（paper / other）
            prompt_override: 自定义翻译提示词
            source_job_id: 来源 convert job ID（自动推导 md 文件名）
        """
        parser = PDFParser()
        try:
            if not parser.ssh_client.connect():
                return {"success": False, "error": "SSH connection failed"}
            return parser.submit_translate_task(
                md_file_names=md_file_names,
                model=model,
                detect=detect,
                content_type=content_type,
                prompt_override=prompt_override,
                source_job_id=source_job_id,
            )
        finally:
            parser.close()

    def check_and_collect(self, output_dir: str | None = None) -> list[dict]:
        """检查所有未完成的远程作业，下载已完成的结果。

        同时处理 convert 和 translate 两种作业类型。
        output_dir 缺省走 parser 默认（data/remote_pdf），不再写 ./output（避免落点不一致）。
        """
        parser = PDFParser()
        return parser.check_and_collect(output_dir=output_dir)
