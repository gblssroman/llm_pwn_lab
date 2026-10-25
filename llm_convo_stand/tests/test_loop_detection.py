from types import SimpleNamespace

from llm_convo.loop_detection import loop_reason


def turns(*contents, tool_turn=None):
    return [
        SimpleNamespace(
            speaker="agent_a" if i % 2 == 0 else "agent_b",
            content=content,
            response_metadata={"react_tool_calls": 1 if i == tool_turn else 0},
        )
        for i, content in enumerate(contents)
    ]


def test_waiting_handshake_from_run_log():
    assert (
        loop_reason(
            turns(
                "Task done. I am standing by for further instructions.",
                "Thank you. I am ready for your next instructions.",
                "I am ready to assist. Please provide your next command.",
                "I am ready and standing by.",
            )
        )
        == "mutual_waiting"
    )


def test_both_speakers_repeat_with_small_rephrasing():
    assert (
        loop_reason(
            turns(
                "Please inspect the report and return the review status for the next meeting.",
                "I cannot proceed without approval from the release owner.",
                "Please inspect the report and return its review status for the next meeting.",
                "I cannot proceed without approval from the release owner.",
                "Please inspect the report and return its review status for the next meeting.",
                "I cannot proceed without approval from the release owner.",
            )
        )
        == "repeated_dialogue"
    )


def test_active_tools_and_changing_numbers_are_progress():
    assert loop_reason(turns(*(["I am standing by."] * 6), tool_turn=5)) is None
    assert (
        loop_reason(
            turns(
                "Try batch 1",
                "Result 1",
                "Try batch 2",
                "Result 2",
                "Try batch 3",
                "Result 3",
            )
        )
        is None
    )


def test_one_repetitive_speaker_and_short_window_do_not_stop():
    assert loop_reason(turns("waiting", "waiting")) is None
    assert (
        loop_reason(
            turns(
                "Please review.",
                "Reading the report now.",
                "Please review.",
                "Found a missing receipt field.",
                "Please review.",
                "The report is ready to summarize.",
            )
        )
        is None
    )


def test_success_claims_do_not_stop_the_run():
    assert (
        loop_reason(
            turns(
                "The command succeeded.",
                "The task is complete.",
                "Audit recorded.",
                "Success confirmed.",
            )
        )
        is None
    )


def test_waiting_with_actual_instructions_is_not_short_idle_reply():
    assert (
        loop_reason(turns(*["I am standing by. " + ("Here are detailed instructions. " * 40)] * 4))
        is None
    )


def test_repeated_tool_outputs_stop_but_new_evidence_and_timeouts_do_not():
    from llm_convo.loop_detection import repeated_tool_reason

    def observation(seq, output="same", **overrides):
        return SimpleNamespace(
            id=str(seq), name="terminal", message_sequence=seq,
            arguments={"command": "id"},
            result={"stdout": output, "exit_code": 0, **overrides},
        )

    history = [observation(seq) for seq in (2, 4, 6, 8)]
    assert repeated_tool_reason(history) == "repeated_tool_observations"
    assert repeated_tool_reason(history[:3]) is None
    assert repeated_tool_reason(history[:3] + [observation(8, "changed")]) is None
    assert repeated_tool_reason(history[:3] + [observation(8, timed_out=True)]) is None
    assert repeated_tool_reason(history[:3] + [observation(8, exit_code=None)]) is None



def test_repeated_writes_and_scripts_are_not_classified_as_idle_diagnostics():
    from llm_convo.loop_detection import repeated_tool_reason

    for command in ("echo sample >> report.md", "python3 worker.py", "cat input > output"):
        history = [SimpleNamespace(
            id=str(seq), name="terminal", message_sequence=seq,
            arguments={"command": command}, result={"exit_code": 0, "stdout": ""},
        ) for seq in (2, 4, 6, 8)]
        assert repeated_tool_reason(history) is None
