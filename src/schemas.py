"""The messages agents pass to each other. Typed, so a bad handoff fails loudly instead of silently."""

from pydantic import BaseModel, Field


class SubQuestion(BaseModel):
    id: int
    question: str = Field(description="One question a single web search can answer")
    why: str = Field(description="What this adds to the report")


class Plan(BaseModel):
    topic: str
    sub_questions: list[SubQuestion]


class Finding(BaseModel):
    sub_question_id: int
    claim: str
    source_urls: list[str]  # every claim must point at the pages it came from
    snippet: str = ""  # exact words from the page that support the claim
    retrieved_at: str = ""  # when the page was fetched; web pages change


class Section(BaseModel):
    heading: str
    findings: list[Finding]


class Report(BaseModel):
    topic: str
    summary: str
    sections: list[Section]
