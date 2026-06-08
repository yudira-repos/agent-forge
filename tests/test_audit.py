"""Tests for the Auditability SDK."""



from agentforge.audit import (
    AuditLogger,
    AuditQuery,
    AuditTrail,
    EventSeverity,
    EventType,
)
from agentforge.audit.logger import InMemoryAuditSink


class TestAuditLogger:
    def test_log_creates_event(self):
        sink = InMemoryAuditSink()
        logger = AuditLogger(sinks=[sink])
        event = logger.log(EventType.TOOL_INVOKED, "agent-1", "run-1")
        assert event.event_type == EventType.TOOL_INVOKED
        assert len(sink.events) == 1

    def test_chain_links_events(self):
        sink = InMemoryAuditSink()
        logger = AuditLogger(sinks=[sink])
        e1 = logger.log(EventType.AGENT_STARTED, "agent-1", "run-1")
        e2 = logger.log(EventType.TOOL_INVOKED, "agent-1", "run-1")
        assert e2.previous_event_id == e1.event_id

    def test_different_correlations_dont_chain(self):
        sink = InMemoryAuditSink()
        logger = AuditLogger(sinks=[sink])
        logger.log(EventType.AGENT_STARTED, "agent-1", "run-A")
        e2 = logger.log(EventType.AGENT_STARTED, "agent-1", "run-B")
        assert e2.previous_event_id is None

    def test_convenience_tool_invoked(self):
        sink = InMemoryAuditSink()
        logger = AuditLogger(sinks=[sink])
        logger.tool_invoked("a", "r", "send_email")
        assert sink.events[0].payload["tool"] == "send_email"

    def test_policy_denied_sets_warning(self):
        sink = InMemoryAuditSink()
        logger = AuditLogger(sinks=[sink])
        logger.policy_denied("a", "r", ["soc2/delete-hitl"], {})
        assert sink.events[0].severity == EventSeverity.WARNING


class TestAuditTrail:
    def setup_method(self):
        self.sink = InMemoryAuditSink()
        self.logger = AuditLogger(sinks=[self.sink])
        self.trail = AuditTrail(self.sink)

        self.logger.log(EventType.AGENT_STARTED, "agent-1", "run-A")
        self.logger.log(EventType.TOOL_INVOKED, "agent-1", "run-A")
        self.logger.log(EventType.POLICY_DENIED, "agent-1", "run-A",
                        severity=EventSeverity.WARNING)
        self.logger.log(EventType.AGENT_STARTED, "agent-2", "run-B")

    def test_query_by_agent(self):
        results = self.trail.query(AuditQuery(agent_id="agent-1"))
        assert all(e.agent_id == "agent-1" for e in results)
        assert len(results) == 3

    def test_query_by_event_type(self):
        results = self.trail.query(AuditQuery(event_types=[EventType.TOOL_INVOKED]))
        assert len(results) == 1

    def test_query_min_severity(self):
        results = self.trail.query(AuditQuery(min_severity=EventSeverity.WARNING))
        assert all(e.severity in (EventSeverity.WARNING, EventSeverity.ERROR, EventSeverity.CRITICAL)
                   for e in results)

    def test_timeline_is_chronological(self):
        timeline = self.trail.timeline("run-A")
        timestamps = [e.timestamp for e in timeline]
        assert timestamps == sorted(timestamps)

    def test_replay_returns_steps(self):
        steps = self.trail.replay("run-A")
        assert len(steps) == 3
        assert steps[0]["step"] == 1
        assert "type" in steps[0]

    def test_violations_returns_deny_events(self):
        violations = self.trail.violations()
        assert any(v.event_type == EventType.POLICY_DENIED for v in violations)

    def test_stats(self):
        stats = self.trail.stats()
        assert stats["total_events"] == 4
        assert stats["by_agent"]["agent-1"] == 3
