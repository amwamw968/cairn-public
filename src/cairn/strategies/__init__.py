"""可插拔复习策略层（docs/REVIEW-STRATEGIES.md 落地顺序 1）。

「到期时间怎么算」从写死的 FSRS 抽成策略协议；本卡只做等价重构：
默认且唯一注册的策略是 ``fsrs``，参数与调用路径一个不改，
同一份历史必须给出完全相同的到期时间（tests/test_strategy_layer.py 证明）。
"""
