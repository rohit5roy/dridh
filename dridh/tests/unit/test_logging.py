from __future__ import annotations

import io
import json

from dridh.common.logging import LogContext, StructuredLogger


def test_log_record_contains_correlation_metadata() -> None:
    stream = io.StringIO()
    logger = StructuredLogger(
        stream=stream,
        context=LogContext(
            workspace_id="ws-1",
            feature_id="F-1",
            story_id="ST-1.1",
            session_id="ses_1",
            agent_id="dridh/coordinator",
            correlation_id="req-1",
            operation="runtime.ping",
        ),
    )
    logger.info("test.event", "hello", extra="value")
    record = json.loads(stream.getvalue())
    assert record["timestamp"].endswith("Z")
    assert record["severity"] == "INFO"
    assert record["workspace_id"] == "ws-1"
    assert record["feature_id"] == "F-1"
    assert record["story_id"] == "ST-1.1"
    assert record["session_id"] == "ses_1"
    assert record["agent_id"] == "dridh/coordinator"
    assert record["correlation_id"] == "req-1"
    assert record["operation"] == "runtime.ping"
    assert record["event"] == "test.event"
    assert record["extra"] == "value"


def test_logging_redacts_message_fields_and_nested_data() -> None:
    stream = io.StringIO()
    logger = StructuredLogger(stream=stream)
    logger.warning(
        "redaction.test",
        "authorization=Bearer abcdefghijklmnop",
        api_key="sk-proj-abcdefghijklmnop",
        nested={"password": "hunter2"},
    )
    text = stream.getvalue()
    assert "abcdefghijklmnop" not in text
    assert "hunter2" not in text
    record = json.loads(text)
    assert record["severity"] == "WARNING"


def test_level_threshold_and_bind_keep_the_same_stream() -> None:
    stream = io.StringIO()
    logger = StructuredLogger(stream=stream, minimum_level="ERROR")
    logger.info("ignored", "not written")
    assert stream.getvalue() == ""
    bound = logger.bind(story_id="ST-1.2")
    bound.error("written", "failure", correlation_id="req-2")
    record = json.loads(stream.getvalue())
    assert record["story_id"] == "ST-1.2"
    assert record["correlation_id"] == "req-2"
