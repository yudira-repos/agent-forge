"""
Auditability SDK
================
Structured, tamper-evident audit logging for every agent action, decision,
and state transition. Built for compliance teams and incident forensics.
"""

from .events import AuditEvent, EventType, EventSeverity
from .logger import AuditLogger, ConsoleAuditSink, FileAuditSink, AuditSink
from .trail import AuditTrail, AuditQuery

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
]
