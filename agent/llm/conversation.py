"""Bounded, untrusted visible conversation context for the shared Copilot."""

from pydantic import BaseModel, ConfigDict, Field


class CopilotConversationTurn(BaseModel):
    """One completed question/answer exchange, never authoritative evidence."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=1000)
    answer: str = Field(min_length=1, max_length=4000)


def bounded_conversation_context(turns: list[dict] | None) -> list[dict[str, str]]:
    """Keep the latest two exchanges within a shared 4,000-character ceiling."""
    remaining = 4000
    selected = []
    for raw in reversed(list(turns or [])[-2:]):
        turn = CopilotConversationTurn.model_validate(raw)
        question = turn.question
        if remaining <= len(question):
            break
        answer = turn.answer[:remaining - len(question)]
        selected.append({"question": question, "answer": answer})
        remaining -= len(question) + len(answer)
    return list(reversed(selected))
