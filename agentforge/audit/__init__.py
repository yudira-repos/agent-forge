"""
Auditability SDK
================
Structured, hash-chained (tamper-evident) audit logging for every agent action, decision,
and state transition. Built for compliance teams and incident forensics.
"""

from .events import AuditEvent, EventSeverity, EventType
from .logger import AuditLogger, AuditSink, ConsoleAuditSink, FileAuditSink
from .trail import AuditQuery, AuditTrail, ChainVerification

__all__ = [
    "AuditEvent",
    "EventType",
    "EventSeverity",
    "AuditLogger",
    "ConsoleAuditSink",
    "FileAuditSink",
    "AuditSink",
    "AuditTrail",
    "AuditQuery",
    "ChainVerification",
]
