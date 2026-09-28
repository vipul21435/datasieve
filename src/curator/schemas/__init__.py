"""Record schemas for SFT and preference data.

Use :func:`parse_record` to validate a decoded JSON object; it returns one of the
concrete record models or raises :class:`~curator.errors.RecordValidationError`
with a :class:`RecordIssue` per problem.
"""

from curator.schemas.common import Message
from curator.schemas.common import RecordBase
from curator.schemas.common import Role
from curator.schemas.issues import RecordIssue
from curator.schemas.parse import RECORD_KINDS
from curator.schemas.parse import Record
from curator.schemas.parse import RecordKind
from curator.schemas.parse import UnknownFieldPolicy
from curator.schemas.parse import parse_record
from curator.schemas.parse import select_model
from curator.schemas.preference import PreferenceChatRecord
from curator.schemas.preference import PreferenceRecord
from curator.schemas.preference import PreferenceTextRecord
from curator.schemas.sft import SFTChatRecord
from curator.schemas.sft import SFTPromptResponseRecord
from curator.schemas.sft import SFTRecord

__all__ = [
    "RECORD_KINDS",
    "Message",
    "PreferenceChatRecord",
    "PreferenceRecord",
    "PreferenceTextRecord",
    "Record",
    "RecordBase",
    "RecordIssue",
    "RecordKind",
    "Role",
    "SFTChatRecord",
    "SFTPromptResponseRecord",
    "SFTRecord",
    "UnknownFieldPolicy",
    "parse_record",
    "select_model",
]
