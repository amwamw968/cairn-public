"""四段式讲解（tutor）包。

- ``service`` — 会话推进与生成编排（唯一公开入口）
- ``leak``    — 答案泄漏的确定性检测（NFKC + 小写 + 去空白包含比对）
- ``llm``     — OpenAI 兼容 chat 客户端（asr.py 同款约定，测试替换 ``_post``）
"""

from cairn.tutor.service import (
    QuestionNotFound,
    SessionNotFound,
    STAGES,
    StageExhausted,
    TutorConflict,
    TutorError,
    next_tutor,
    reveal_tutor,
    start_tutor,
)

__all__ = [
    "STAGES",
    "TutorError",
    "QuestionNotFound",
    "SessionNotFound",
    "StageExhausted",
    "TutorConflict",
    "start_tutor",
    "next_tutor",
    "reveal_tutor",
]
