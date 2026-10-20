"""刷B站 · 独立常驻服务（不依赖 DeepSeek Harness）。

三层：
  ① bilibili.py  —— B站 读写（搜索 / 字幕 / 评论 / 发评论 / 私信）
  ② state.py     —— 台账（seen.md 判重表、digest 归档、out 日报、state.json 配额）
  ③ pipeline.py  —— 一轮流水线：搜索 → 判重 → 读字幕原文 → 模型打分 → 出报告 → 说话

调度器（scheduler.py）按谷时窗口叫它起来，HTTP 控制页（server.py）可以手动叫一轮。
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
