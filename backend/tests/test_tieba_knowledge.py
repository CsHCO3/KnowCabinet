"""贴吧知识点：链接还原 / 项目 URL 归属 / kb md 渲染（无 LLM、无网络）。"""
from __future__ import annotations

from backend.services.community.tieba.knowledge import (
    TiebaKnowledgeExtractor,
    _unwrap_links,
    render_kb_markdown,
)

WRAPPED = (
    "网页链接  http://tieba.baidu.com/mo/q/checkurl?url=https%3A%2F%2Fgithub.com%2FWSK-build%2FDSHBox"
    "&urlrefer=1a004eb06b04ae82cef26b500a7e7fee"
)


def test_unwrap_links_restores_plain_url():
    assert _unwrap_links(WRAPPED) == "网页链接  https://github.com/WSK-build/DSHBox"


def test_unwrap_links_leaves_plain_text_alone():
    text = "GitHub 上搜一下 astrbot，下载一下桌面客户端"
    assert _unwrap_links(text) == text


def test_project_url_matches_slug():
    text = "项目地址 https://github.com/pinganbbOvO/ai-companion-kit 按教程操作"
    assert TiebaKnowledgeExtractor._pick_project_url(text, "ai-companion-kit") == (
        "https://github.com/pinganbbOvO/ai-companion-kit"
    )


def test_project_url_not_guessed_for_other_project_in_same_row():
    """一条楼中楼同时提到 qq-bridge-plus 与 DSHBox 时，不能把 DSHBox 的链接挂给前者。"""
    text = "qq-bridge-plus这个项目，如果在手机上玩就需要 https://github.com/WSK-build/DSHBox"
    assert TiebaKnowledgeExtractor._pick_project_url(text, "qq-bridge-plus") == ""
    assert TiebaKnowledgeExtractor._url_fits("https://github.com/WSK-build/DSHBox", "qq-bridge-plus") is False
    assert TiebaKnowledgeExtractor._url_fits("https://github.com/WSK-build/DSHBox", "DSHBox") is True


def test_url_fits_trusts_model_for_cjk_object():
    assert TiebaKnowledgeExtractor._url_fits("https://github.com/a/b", "中文项目名") is True


def test_render_kb_markdown_shape():
    state = {
        "tid": "1",
        "title": "标题",
        "url": "https://tieba.baidu.com/p/1",
        "forum_name": "deepseek",
        "updated_at": "2026-10-20T00:00:00+08:00",
        "entries": [
            {
                "id": "E001",
                "theme": "00-备选调研方向",
                "kind": "project",
                "object": "ai-companion-kit",
                "statement": "一句话结论",
                "resource_url": "https://github.com/pinganbbOvO/ai-companion-kit",
                "source_markers": ["#1#F002-C35"],
            }
        ],
        "evidence": {"comment:1": {"marker": "#1#F002-C35", "text": "原文"}},
    }
    md = render_kb_markdown(state)
    assert md.startswith("# 贴吧知识：标题")
    assert "## ai-companion-kit" in md
    assert "资源：<https://github.com/pinganbbOvO/ai-companion-kit>" in md
    assert "证据[#1#F002-C35]：原文" in md
