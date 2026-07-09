"""Isolation tests for debate history builders."""

from __future__ import annotations

from spark.core.debate.history import build_debater_transcript, build_judge_transcript

TURNS = [
    {
        "id": 1,
        "round": 0,
        "role": "judge",
        "turn_type": "announcement",
        "content": "Order: pro first.",
        "summary": None,
        "status": "complete",
    },
    {
        "id": 2,
        "round": 1,
        "role": "pro",
        "turn_type": "argument",
        "content": "Pro case.",
        "summary": "pro r1 summary",
        "status": "complete",
    },
    {
        "id": 3,
        "round": 1,
        "role": "con",
        "turn_type": "argument",
        "content": "Con case.",
        "summary": None,
        "status": "complete",
    },
    {
        "id": 4,
        "round": 1,
        "role": "user",
        "turn_type": "user_prompt",
        "content": "Focus on cost.",
        "summary": None,
        "status": "complete",
    },
    {
        "id": 5,
        "round": 1,
        "role": "judge",
        "turn_type": "interim",
        "content": "Address cost next.",
        "summary": None,
        "status": "complete",
    },
    {
        "id": 6,
        "round": 1,
        "role": "pro",
        "turn_type": "argument",
        "content": "FAILED DRAFT",
        "summary": None,
        "status": "failed",
    },
    {
        "id": 7,
        "round": 0,
        "role": "judge",
        "turn_type": "ruling",
        "content": "Pro wins.",
        "summary": None,
        "status": "complete",
    },
]
EXHIBITS = {2: [{"label": "A", "title": "Study", "content": "Numbers", "source": "https://x"}]}


class TestDebaterTranscript:
    def test_sees_opponent_arguments_and_exhibits(self) -> None:
        t = build_debater_transcript("con", TURNS, EXHIBITS)
        assert "Pro case." in t and "Exhibit A" in t and "Numbers" in t

    def test_sees_judge_and_user(self) -> None:
        t = build_debater_transcript("pro", TURNS, EXHIBITS)
        assert "Order: pro first." in t and "Focus on cost." in t and "Address cost next." in t

    def test_never_sees_failed_turns_or_ruling(self) -> None:
        t = build_debater_transcript("con", TURNS, EXHIBITS)
        assert "FAILED DRAFT" not in t and "Pro wins." not in t

    def test_summaries_replace_old_rounds(self) -> None:
        t = build_debater_transcript("con", TURNS, EXHIBITS, summarise_before_round=2)
        assert "pro r1 summary" in t and "Pro case." not in t


class TestJudgeTranscript:
    def test_sees_both_arguments_never_briefs(self) -> None:
        t = build_judge_transcript(TURNS, EXHIBITS)
        assert "Pro case." in t and "Con case." in t
        # The builder cannot receive a brief at all; sanity-check no leak markers
        assert "brief" not in t.lower()

    def test_qa_only_when_requested(self) -> None:
        turns = TURNS + [
            {
                "id": 8,
                "round": 0,
                "role": "user",
                "turn_type": "qa_question",
                "content": "Why?",
                "summary": None,
                "status": "complete",
            },
        ]
        assert "Why?" not in build_judge_transcript(turns, EXHIBITS)
        assert "Why?" in build_judge_transcript(turns, EXHIBITS, include_qa=True)
