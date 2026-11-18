"""集中化路径管理。

arXiv 相关数据统一归到 PROJECT_ROOT/data/arxiv/ 下，按处理阶段分目录。

data/
  arxiv/
    queries/
      search/       # 快速搜索结果（取代 output/search/）
      surveys/      # 调研管线结果（取代 output/survey/）
    sources/        # 原始 PDF，按 {arxiv_id}/paper.pdf 组织
    ocr/            # OCR 结果，按 {arxiv_id}/ocr.md + pages/ 组织
    translations/   # 翻译结果，按 {arxiv_id}/translated.md 组织
    analysis/       # LLM 解析报告，按 {arxiv_id}/value.md 组织
    jobs/           # 任务进度（保持现状）

其余非 arXiv 运行时路径仍从 OUTPUT_ROOT 派生。
"""

from pathlib import Path

from backend.config.settings import Settings
from backend.path_setup import PROJECT_ROOT


class Paths:
    """解析后的项目路径。所有子目录均基于 output_root 计算。"""

    def __init__(self, settings: Settings | None = None):
        s = settings or Settings()
        root = Path(s.output_root).resolve()

        self.project_root = PROJECT_ROOT
        self.output_root = root

        # ── arXiv 统一根目录 ──
        self.arxiv = PROJECT_ROOT / "data" / "arxiv"
        self.arxiv_queries_search = self.arxiv / "queries" / "search"
        self.arxiv_queries_surveys = self.arxiv / "queries" / "surveys"
        self.arxiv_sources = self.arxiv / "sources"
        self.arxiv_ocr = self.arxiv / "ocr"
        self.arxiv_translations = self.arxiv / "translations"
        self.arxiv_analysis = self.arxiv / "analysis"
        self.arxiv_jobs = self.arxiv / "jobs"

        # ── 旧 arXiv 路径（逐步迁移用） ──
        self.search = self.arxiv_queries_search

        # ── 非 arXiv 路径 ──
        self.data = root / "data"
        self.jobs = root / "data" / "jobs"
        self.pdfs = PROJECT_ROOT / "data" / "pdfs"
        # B站本地字幕批量分析的落点（URL 分析报告默认落到 data/videos/{bvid}/，见 bilibili_analyze.compose_report）
        self.bilibili = PROJECT_ROOT / "data" / "videos" / "_analysis"
        self.remote_pdf = PROJECT_ROOT / "data" / "remote_pdf"
        self.frames = root / "frames"
        self.file_chat = root / "file_chat"

    def ensure_all(self) -> None:
        for d in [self.arxiv, self.arxiv_queries_search, self.arxiv_queries_surveys,
                  self.arxiv_sources, self.arxiv_ocr, self.arxiv_translations,
                  self.arxiv_analysis, self.arxiv_jobs,
                  self.data, self.jobs, self.pdfs, self.bilibili,
                  self.remote_pdf, self.frames]:
            d.mkdir(parents=True, exist_ok=True)
