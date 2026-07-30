from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Mode(str, Enum):
    PARSER = "parser"
    RESPONSE = "response"


class AIDecision(str, Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    REVIEW = "REVIEW"


@dataclass
class Vacancy:
    id: Optional[int]
    channel_id: int
    message_id: int
    text: str
    ai_decision: Optional[AIDecision] = None
    ai_reason: Optional[str] = None


@dataclass
class LLMResult:
    decision: AIDecision
    reason: str
    price_estimate: Optional[float]
