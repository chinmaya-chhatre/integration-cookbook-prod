"""Mock customer systems with failure injection: Jira-, CRM-, warehouse- and Slack-style APIs."""

from .base import Fault, MockServer, RecordedRequest, Reply
from .crm import CrmMock
from .jira import JiraMock
from .slack import SlackMock
from .warehouse import WarehouseMock, validate_order

__all__ = [
    "CrmMock",
    "Fault",
    "JiraMock",
    "MockServer",
    "RecordedRequest",
    "Reply",
    "SlackMock",
    "WarehouseMock",
    "validate_order",
]
