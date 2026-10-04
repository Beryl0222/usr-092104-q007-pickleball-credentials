"""赛历资历互认领域包。"""

from .contract import EVENT_TYPES, AGGREGATE_TYPES, validate_envelope, validate_payload
from .errors import ContractError, RuleViolation
from .events import EventStore
from .state import RehydratedState
from .service import AssociationService

__all__ = [
    "EVENT_TYPES",
    "AGGREGATE_TYPES",
    "validate_envelope",
    "validate_payload",
    "ContractError",
    "RuleViolation",
    "EventStore",
    "RehydratedState",
    "AssociationService",
]
