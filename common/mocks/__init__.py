"""Mock customer systems with failure injection: Jira-, CRM- and warehouse-style APIs."""

from .base import Fault, MockServer, RecordedRequest, Reply
from .crm import CrmMock
from .jira import JiraMock
from .warehouse import WarehouseMock, validate_order

__all__ = [
    "CrmMock",
    "Fault",
    "JiraMock",
    "MockServer",
    "RecordedRequest",
    "Reply",
    "WarehouseMock",
    "validate_order",
]
