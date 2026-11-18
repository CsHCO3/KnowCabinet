"""可配置的对话切块解析器。

支持自定义用户/AI 段落标记正则，将带标记的对话文件解析为结构化的 ChatRound 列表。

用法:
    parser = ChatParser(
        user_pattern=r"^##\\s*【您】",
        ai_pattern=r"^##\\s*【对方】",
    )
    rounds: list[ChatRound] = parser.parse("chat_log.md")
"""

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ChatRound:
    """一轮对话：用户发言 + AI 回复。"""
    user_content: str
    ai_content: str
    round_no: int
    user_line: int = 0       # 用户块起始行号
    ai_line: int = 0         # AI 块起始行号


class ChatParser:
    """按正则标记将对话文件切块并配对为 (user, ai) 轮次。

    扫描规则:
        - 匹配 user_pattern 的行 → 新用户块开始
        - 匹配 ai_pattern 的行 → 新 AI 块开始
        - 两块之间的所有行归入前一块
        - 最后配对为 用户→AI 的轮次
    """

    def __init__(self, user_pattern: str, ai_pattern: str):
        self._user_re = re.compile(user_pattern)
        self._ai_re = re.compile(ai_pattern)

    @classmethod
    def from_markers(cls, user_marker: str, ai_marker: str) -> "ChatParser":
        """从字面标记字符串构造（自动转义 + 行首匹配）。"""
        return cls(
            user_pattern=rf"^\s*##\s*{re.escape(user_marker)}",
            ai_pattern=rf"^\s*##\s*{re.escape(ai_marker)}",
        )

    # ------------------------------------------------------------------
    def _match_header(self, line: str):
        """若行匹配标记，返回 (speaker, inline_text)；否则 None。"""
        line = line.lstrip("﻿")
        m = self._user_re.search(line)
        if m:
            return "user", line[m.end() :].strip()
        m = self._ai_re.search(line)
        if m:
            return "ai", line[m.end() :].strip()
        return None

    # ------------------------------------------------------------------
    def parse(self, input_file: str) -> list[ChatRound]:
        """解析文件 → ChatRound 列表。"""
        text = Path(input_file).read_text(encoding="utf-8")
        lines = text.split("\n")

        # ── 1. 切块 ────────────────────────────────────────────────
        raw: list[tuple[str, str, int]] = []  # (speaker, content, start_line)
        speaker: str | None = None
        buf: list[str] = []
        start = 0

        for idx, line in enumerate(lines, start=1):
            marker = self._match_header(line)
            if marker is not None:
                if speaker is not None:
                    raw.append((speaker, "\n".join(buf).strip(), start))
                speaker, inline = marker
                buf = [inline] if inline else []
                start = idx
            else:
                if speaker is not None:
                    buf.append(line)

        if speaker is not None:
            raw.append((speaker, "\n".join(buf).strip(), start))

        # ── 2. 校验 ────────────────────────────────────────────────
        if not raw:
            raise ValueError("未找到任何对话块，请检查标记格式")
        if len(raw) % 2 != 0:
            last_sp, _, last_ln = raw[-1]
            raise ValueError(
                f"对话块不成对：第 {last_ln} 行开始的「{last_sp}」块没有对应的回复"
            )

        # ── 3. 配对 ────────────────────────────────────────────────
        rounds: list[ChatRound] = []
        for i in range(0, len(raw), 2):
            rn = (i // 2) + 1
            sp1, c1, ln1 = raw[i]
            sp2, c2, ln2 = raw[i + 1]

            if sp1 != "user":
                raise ValueError(
                    f"第 {rn} 轮奇数列应为 user，实际为 {sp1}（第 {ln1} 行）"
                )
            if sp2 != "ai":
                raise ValueError(
                    f"第 {rn} 轮偶数列应为 ai，实际为 {sp2}（第 {ln2} 行）"
                )

            rounds.append(ChatRound(
                user_content=c1,
                ai_content=c2,
                round_no=rn,
                user_line=ln1,
                ai_line=ln2,
            ))

        return rounds
