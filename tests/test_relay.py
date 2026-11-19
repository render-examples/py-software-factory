from __future__ import annotations

import random

import pytest

from factory.agent_runs import LineRelay
from factory.agent_runs.relay import decode

LINES = [
    '{"type":"started","n":1}',
    '{"type":"assistant_delta","text":"caf\\u00e9 \\u2603"}',
    "",
    '{"type":"tool_call","args":{"command":"ls -la"}}',
    '{"type":"result","output":{}}',
]
FILE = "".join(f"{line}\n" for line in LINES).encode("ascii")


def chunks(data: bytes, seed: int) -> list[bytes]:
    rng = random.Random(seed)
    pieces: list[bytes] = []
    start = 0
    while start < len(data):
        end = min(len(data), start + rng.randint(1, 17))
        pieces.append(data[start:end])
        start = end
    return pieces


def test_lines_are_ascii() -> None:
    assert FILE.isascii()


@pytest.mark.parametrize("seed", range(20))
def test_offsets_stay_exact_across_chunk_boundaries(seed: int) -> None:
    relayed: list[str] = []
    relay = LineRelay(relayed.append)
    consumed = 0
    for piece in chunks(FILE, seed):
        relay.push(decode(piece))
        consumed += len(piece)
        assert relay.offset == FILE.rfind(b"\n", 0, consumed) + 1
    assert relayed == LINES
    assert relay.offset == len(FILE)


def test_partial_trailing_line_is_held_until_it_completes() -> None:
    relayed: list[str] = []
    relay = LineRelay(relayed.append)
    relay.push('{"a":1}\n{"b"')
    assert relayed == ['{"a":1}']
    assert relay.offset == len(b'{"a":1}\n')
    relay.push(":2}")
    assert relayed == ['{"a":1}']
    relay.push("\n")
    assert relayed == ['{"a":1}', '{"b":2}']
    assert relay.offset == len(b'{"a":1}\n{"b":2}\n')


@pytest.mark.parametrize("seed", range(10))
def test_polls_from_offset_with_restart_never_repeat_or_skip(seed: int) -> None:
    relayed: list[str] = []
    relay = LineRelay(relayed.append)
    rng = random.Random(seed)
    written = 0
    while written < len(FILE):
        written = min(len(FILE), written + rng.randint(1, 40))
        relay.restart()
        relay.push(decode(FILE[relay.offset : written]))
    assert relayed == LINES


@pytest.mark.parametrize("cut", range(len(FILE) + 1))
def test_finish_relays_only_the_missing_tail(cut: int) -> None:
    relayed: list[str] = []
    relay = LineRelay(relayed.append)
    relay.push(decode(FILE[:cut]))
    relay.finish(FILE)
    assert relayed == LINES
    assert relay.offset == len(FILE)
    relay.finish(FILE)
    assert relayed == LINES


def test_offsets_count_bytes_even_when_a_chunk_splits_a_character() -> None:
    data = "naïve ☃\nnext\n".encode()
    split = data.index("☃".encode()) + 1
    relayed: list[str] = []
    relay = LineRelay(relayed.append)
    relay.push(decode(data[:split]))
    relay.push(decode(data[split:]))
    assert relay.offset == len(data)
    assert relayed[1] == "next"
    relay.finish(data)
    assert len(relayed) == 2
