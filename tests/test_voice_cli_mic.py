"""CLI ``--mic``：录 → 转 → 回合 → 播，约束只打印不念；失败不打断循环。

不碰真麦克风、不碰真 ASR、不出声：``cli`` 里的 ``listen`` / ``transcribe`` /
``speak`` / ``run_turn`` 全部替换成替身。
"""

from __future__ import annotations

import pytest

from cairn.voice import cli as cli_module
from cairn.voice.mic import DEVICE_ERROR, NO_SPEECH, OK, TOO_SHORT, Utterance
from cairn.voice.turn import Turn

SPOKEN = "第一题，英语。请拼出对应的英文单词：苹果。"
NEXT_STEP = "必须把孩子说的原话交给 submit_answer"
HEARD = "给我出一道英语题"


@pytest.fixture()
def db_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'cli.db'}"


def scripted_listen(utterances: list[Utterance]):
    """按脚本返回；脚本走完就抛 ``KeyboardInterrupt``（等于用户按了 Ctrl-C）。"""
    queue = list(utterances)
    calls = {"count": 0}

    def listen():
        calls["count"] += 1
        if not queue:
            raise KeyboardInterrupt
        return queue.pop(0)

    return listen, calls


def test_mic_round_prints_and_speaks_but_does_not_speak_constraint(
    monkeypatch, db_url, capsys
) -> None:
    listen, calls = scripted_listen([Utterance(wav=b"RIFF", reason=OK)])
    spoken: list[str] = []
    monkeypatch.setattr(cli_module, "listen", listen)
    monkeypatch.setattr(cli_module, "transcribe", lambda wav: HEARD)
    monkeypatch.setattr(
        cli_module,
        "run_turn",
        lambda session, learner_id, text: Turn(
            kind="question", spoken=SPOKEN, next_step=NEXT_STEP
        ),
    )
    monkeypatch.setattr(cli_module, "speak", spoken.append)

    assert cli_module.main(["--mic", "--db-url", db_url]) == 0

    captured = capsys.readouterr()
    assert f"{cli_module.HEARD_PROMPT}{HEARD}" in captured.out
    assert f"{cli_module.SPEAKER}{SPOKEN}" in captured.out
    assert f"{cli_module.CONSTRAINT_PREFIX}{NEXT_STEP}" in captured.out
    # 进喇叭的只有 spoken：约束一个字都没念
    assert spoken == [SPOKEN]
    assert calls["count"] == 2
    assert "Traceback" not in captured.err


def test_asr_failure_does_not_stop_the_loop(monkeypatch, db_url, capsys) -> None:
    listen, _ = scripted_listen(
        [Utterance(wav=b"a", reason=OK), Utterance(wav=b"b", reason=OK)]
    )
    attempts: list[bytes] = []

    def flaky_transcribe(wav):
        attempts.append(wav)
        if len(attempts) == 1:
            raise RuntimeError("连不上 ASR 服务")
        return HEARD

    spoken: list[str] = []
    monkeypatch.setattr(cli_module, "listen", listen)
    monkeypatch.setattr(cli_module, "transcribe", flaky_transcribe)
    monkeypatch.setattr(
        cli_module,
        "run_turn",
        lambda session, learner_id, text: Turn(kind="chat", spoken="嗯，我在。", next_step=None),
    )
    monkeypatch.setattr(cli_module, "speak", spoken.append)

    assert cli_module.main(["--mic", "--db-url", db_url]) == 0

    captured = capsys.readouterr()
    assert f"{cli_module.TURN_FAILED_PREFIX}连不上 ASR 服务" in captured.err
    assert spoken == ["嗯，我在。"]
    assert len(attempts) == 2


def test_turn_failure_does_not_stop_the_loop(monkeypatch, db_url, capsys) -> None:
    listen, _ = scripted_listen(
        [Utterance(wav=b"a", reason=OK), Utterance(wav=b"b", reason=OK)]
    )
    rounds: list[str] = []

    def flaky_turn(session, learner_id, text):
        rounds.append(text)
        if len(rounds) == 1:
            raise RuntimeError("工具抽风")
        return Turn(kind="chat", spoken="嗯，我在。", next_step=None)

    monkeypatch.setattr(cli_module, "listen", listen)
    monkeypatch.setattr(cli_module, "transcribe", lambda wav: HEARD)
    monkeypatch.setattr(cli_module, "run_turn", flaky_turn)
    monkeypatch.setattr(cli_module, "speak", lambda text: None)

    assert cli_module.main(["--mic", "--db-url", db_url]) == 0

    captured = capsys.readouterr()
    assert f"{cli_module.TURN_FAILED_PREFIX}工具抽风" in captured.err
    assert rounds == [HEARD, HEARD]
    assert "Traceback" not in captured.err


def test_no_speech_just_prints_and_listens_again(monkeypatch, db_url, capsys) -> None:
    listen, calls = scripted_listen(
        [Utterance(None, NO_SPEECH, "10 秒内没听到说话，请再说一遍。")]
    )
    monkeypatch.setattr(cli_module, "listen", listen)
    monkeypatch.setattr(
        cli_module, "transcribe", lambda wav: pytest.fail("没有音频不该转写")
    )

    assert cli_module.main(["--mic", "--db-url", db_url]) == 0

    captured = capsys.readouterr()
    assert "没听到说话" in captured.out
    assert calls["count"] == 2


def test_too_short_is_announced(monkeypatch, db_url, capsys) -> None:
    listen, _ = scripted_listen([Utterance(None, TOO_SHORT, "只听到 0.06 秒，太短了，请再说一遍。")])
    monkeypatch.setattr(cli_module, "listen", listen)

    assert cli_module.main(["--mic", "--db-url", db_url]) == 0

    assert "太短" in capsys.readouterr().out


def test_device_error_stops_instead_of_spinning(monkeypatch, db_url, capsys) -> None:
    """麦克风打不开：提示去看权限就停，不空转刷屏。"""
    listen, calls = scripted_listen(
        [Utterance(None, DEVICE_ERROR, "麦克风不可用：Device unavailable")]
    )
    monkeypatch.setattr(cli_module, "listen", listen)

    assert cli_module.main(["--mic", "--db-url", db_url]) == 0

    captured = capsys.readouterr()
    assert cli_module.MIC_STOPPED in captured.err
    assert calls["count"] == 1


def test_ctrl_c_exits_cleanly(monkeypatch, db_url, capsys) -> None:
    def listen():
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "listen", listen)

    assert cli_module.main(["--mic", "--db-url", db_url]) == 0

    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


def test_text_mode_is_unchanged(monkeypatch, db_url, capsys) -> None:
    """``--mic`` 不选时还是 M1 的文本循环，提示符与退出词都不变。"""
    lines = iter(["嗯", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
    monkeypatch.setattr(
        cli_module,
        "run_turn",
        lambda session, learner_id, text: Turn(kind="chat", spoken="嗯，我在。", next_step=None),
    )

    assert cli_module.main(["--db-url", db_url]) == 0

    captured = capsys.readouterr()
    assert "cairn voice M1:" in captured.out
    assert f"{cli_module.SPEAKER}嗯，我在。" in captured.out
