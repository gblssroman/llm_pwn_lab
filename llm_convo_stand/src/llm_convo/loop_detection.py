"""Conservative transcript checks; model claims never count as tool evidence."""

import json
import re
import shlex
from difflib import SequenceMatcher
from typing import Protocol


class Turn(Protocol):
    speaker: str
    content: str
    response_metadata: dict | None


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def _waiting(text: str) -> bool:
    words = _tokens(text)
    if len(words) > 100:
        return False
    normalized = " ".join(words)
    return any(
        phrase in normalized
        for phrase in (
            "standing by",
            "ready for your next",
            "ready to assist",
            "provide your next command",
            "waiting for further instructions",
            "жду дальнейших указаний",
            "ожидаю дальнейших инструкций",
            "готов к следующ",
            "ожидаю следующ",
        )
    )


def loop_reason(messages: list[Turn]) -> str | None:
    """Require both speakers and multiple idle turns, ignoring active tool windows.

    Six turns detect a repeated period of two (each speaker repeats three times).
    Four short waiting replies detect the terminal handshake seen in lab runs.
    Numbers are preserved so evolving counters do not look like repetition.
    """
    recent = messages[-6:]
    if len(recent) < 4:
        return None
    waiting = recent[-4:]
    if (
        {item.speaker for item in waiting} == {"agent_a", "agent_b"}
        and not any((item.response_metadata or {}).get("react_tool_calls") for item in waiting)
        and all(_waiting(item.content) for item in waiting)
    ):
        return "mutual_waiting"
    if len(recent) < 6 or any(
        (item.response_metadata or {}).get("react_tool_calls") for item in recent
    ):
        return None
    if {item.speaker for item in recent} != {"agent_a", "agent_b"}:
        return None
    for speaker in ("agent_a", "agent_b"):
        turns = [_tokens(item.content) for item in recent if item.speaker == speaker]
        if len(turns) != 3 or any(not turn or len(turn) > 2048 for turn in turns):
            return None
        for first, second in zip(turns, turns[1:], strict=False):
            if [w for w in first if any(c.isdigit() for c in w)] != [
                w for w in second if any(c.isdigit() for c in w)
            ] or SequenceMatcher(None, first, second, autojunk=False).ratio() < 0.92:
                return None
    return "repeated_dialogue"


def _diagnostic_command(command: str) -> bool:
    """Conservative diagnostic allowlist; scripts and writes are not idle evidence."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()\n")
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return False
    if not words or any(word and all(c in ";&|<>()\n" for c in word) for word in words):
        return False
    if words[0] in {"id", "ls", "stat", "cat", "head", "tail", "wc", "uname", "pwd"}:
        return True
    return words[0] == "docker" and len(words) > 1 and words[1] in {
        "ps", "inspect", "info", "version",
    }


def repeated_tool_reason(executions: list) -> str | None:
    """Stop after three tool turns repeat only earlier command/result pairs.

    Compare real persisted outputs, not model claims; changed output, timeout,
    incomplete execution, or a new command keeps the run active.
    """
    groups: dict[int, set[str]] = {}
    for execution in executions:
        result = execution.result
        if execution.name != "terminal":
            continue
        group = groups.setdefault(execution.message_sequence, set())
        if (
            not _diagnostic_command(str(execution.arguments.get("command", "")))
            or not isinstance(result, dict) or result.get("timed_out")
            or "error" in result or not isinstance(result.get("exit_code"), int)
        ):
            group.add("incomplete:" + str(execution.id))
            continue
        group.add(json.dumps({
            "command": execution.arguments.get("command"),
            "result": {key: result.get(key) for key in (
                "stdout", "stderr", "exit_code", "timed_out",
            )},
        }, sort_keys=True, ensure_ascii=False))
    ordered = [groups[key] for key in sorted(groups)]
    if len(ordered) < 4:
        return None
    known = set().union(*ordered[:-3])
    if all(group and group <= known for group in ordered[-3:]):
        return "repeated_tool_observations"
    return None
