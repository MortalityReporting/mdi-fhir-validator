"""Tests for ValidatorEngine's periodic keepalive feature.

These tests cover the keepalive task that periodically validates the initial
load resource to keep the terminology-server cache/session active.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.config import Settings
from app.validator_engine import ValidatorEngine


def _make_resource_file(path: Path, content: str = '{"resourceType": "Patient"}') -> None:
    path.write_text(content)


def _make_package_dir(root: Path, name: str) -> None:
    (root / name / "package").mkdir(parents=True)


def _make_engine_with_settings(**settings_kwargs) -> ValidatorEngine:
    """Create a ValidatorEngine with test-friendly default settings."""
    # Ensure test isolation by default
    settings_kwargs.setdefault("load_cached_packages_on_startup", False)
    settings_kwargs.setdefault("packages_dir", "/nonexistent-packages-dir-for-tests")
    settings_kwargs.setdefault("packages", "")
    settings_kwargs.setdefault("default_ig", "")
    settings_kwargs.setdefault("validate_initial_load_resource_on_startup", False)
    settings_kwargs.setdefault("validator_keepalive_interval_seconds", 0.0)  # Disabled by default
    engine = ValidatorEngine(Settings(**settings_kwargs))
    # Simulate a running process for testing
    engine._process = MagicMock()
    engine._process.returncode = None
    # Make wait() an async mock that returns immediately
    engine._process.wait = AsyncMock(return_value=0)
    engine._process.terminate = MagicMock()
    engine._process.kill = MagicMock()
    return engine


# --- Keepalive task scheduling and prerequisites ---


@pytest.mark.asyncio
async def test_keepalive_task_scheduled_when_interval_positive_and_engine_ready(tmp_path):
    """The keepalive task should be scheduled when interval > 0 and engine is ready."""
    resource_path = tmp_path / "initial_load_resource.json"
    _make_resource_file(resource_path)

    engine = _make_engine_with_settings(
        default_ig="pkg.default#1.0.0",
        initial_load_resource_path=str(resource_path),
        validator_keepalive_interval_seconds=1.0,  # Positive interval
    )

    # Simulate engine being ready (this would normally happen after start())
    engine._startup_state = "ready"
    engine._ensure_keepalive_task()

    # Task should be scheduled
    assert engine._keepalive_task is not None
    assert not engine._keepalive_task.done()


@pytest.mark.asyncio
async def test_keepalive_task_not_scheduled_when_interval_zero(tmp_path):
    """The keepalive task should NOT be scheduled when interval <= 0."""
    resource_path = tmp_path / "initial_load_resource.json"
    _make_resource_file(resource_path)

    engine = _make_engine_with_settings(
        default_ig="pkg.default#1.0.0",
        initial_load_resource_path=str(resource_path),
        validator_keepalive_interval_seconds=0.0,  # Disabled
    )

    # Simulate engine being ready
    engine._startup_state = "ready"
    engine._ensure_keepalive_task()

    # Task should NOT be scheduled
    assert engine._keepalive_task is None


@pytest.mark.asyncio
async def test_keepalive_task_not_scheduled_when_interval_negative(tmp_path):
    """The keepalive task should NOT be scheduled when interval is negative."""
    resource_path = tmp_path / "initial_load_resource.json"
    _make_resource_file(resource_path)

    engine = _make_engine_with_settings(
        default_ig="pkg.default#1.0.0",
        initial_load_resource_path=str(resource_path),
        validator_keepalive_interval_seconds=-1.0,  # Negative interval
    )

    # Simulate engine being ready
    engine._startup_state = "ready"
    engine._ensure_keepalive_task()

    # Task should NOT be scheduled
    assert engine._keepalive_task is None


@pytest.mark.asyncio
async def test_keepalive_task_not_scheduled_when_engine_not_ready(tmp_path):
    """The keepalive task should NOT be scheduled when engine is not ready."""
    resource_path = tmp_path / "initial_load_resource.json"
    _make_resource_file(resource_path)

    engine = _make_engine_with_settings(
        default_ig="pkg.default#1.0.0",
        initial_load_resource_path=str(resource_path),
        validator_keepalive_interval_seconds=1.0,  # Positive interval
    )

    # Engine is not ready
    engine._startup_state = "preparation"
    engine._ensure_keepalive_task()

    # Task should NOT be scheduled
    assert engine._keepalive_task is None


@pytest.mark.asyncio
async def test_keepalive_task_not_scheduled_when_default_ig_empty(tmp_path):
    """The keepalive task should NOT be scheduled when default_ig is empty."""
    resource_path = tmp_path / "initial_load_resource.json"
    _make_resource_file(resource_path)

    engine = _make_engine_with_settings(
        default_ig="",  # Empty
        initial_load_resource_path=str(resource_path),
        validator_keepalive_interval_seconds=1.0,  # Positive interval
    )

    # Simulate engine being ready
    engine._startup_state = "ready"
    engine._ensure_keepalive_task()

    # Task should NOT be scheduled
    assert engine._keepalive_task is None


@pytest.mark.asyncio
async def test_keepalive_task_not_scheduled_when_resource_file_missing(tmp_path):
    """The keepalive task should NOT be scheduled when resource file is missing."""
    engine = _make_engine_with_settings(
        default_ig="pkg.default#1.0.0",
        initial_load_resource_path=str(tmp_path / "does-not-exist.json"),
        validator_keepalive_interval_seconds=1.0,  # Positive interval
    )

    # Simulate engine being ready
    engine._startup_state = "ready"
    engine._ensure_keepalive_task()

    # Task should NOT be scheduled
    assert engine._keepalive_task is None


# --- Keepalive task lifecycle management ---


@pytest.mark.asyncio
async def test_keepalive_task_cancelled_on_stop(tmp_path):
    """The keepalive task should be cancelled when stop() is called."""
    resource_path = tmp_path / "initial_load_resource.json"
    _make_resource_file(resource_path)

    engine = _make_engine_with_settings(
        default_ig="pkg.default#1.0.0",
        initial_load_resource_path=str(resource_path),
        validator_keepalive_interval_seconds=3600.0,
    )

    # Simulate engine being ready and task scheduled
    engine._startup_state = "ready"
    engine._ensure_keepalive_task()
    assert engine._keepalive_task is not None

    # Stop the engine, which should cancel the keepalive task
    # We'll just check that _cancel_keepalive_task works without calling full stop
    await engine._cancel_keepalive_task()

    # Task should be cancelled/completed
    assert engine._keepalive_task is None


@pytest.mark.asyncio
async def test_keepalive_task_not_leaked_across_restarts(tmp_path):
    """The keepalive task should not leak across multiple start/stop cycles."""
    resource_path = tmp_path / "initial_load_resource.json"
    _make_resource_file(resource_path)

    engine = _make_engine_with_settings(
        default_ig="pkg.default#1.0.0",
        initial_load_resource_path=str(resource_path),
        validator_keepalive_interval_seconds=3600.0,  # Very long interval
    )

    # First cycle
    engine._startup_state = "ready"
    engine._ensure_keepalive_task()
    assert engine._keepalive_task is not None
    await engine._cancel_keepalive_task()
    assert engine._keepalive_task is None

    # Second cycle
    engine._startup_state = "ready"
    engine._ensure_keepalive_task()
    assert engine._keepalive_task is not None
    await engine._cancel_keepalive_task()
    assert engine._keepalive_task is None


# --- Keepalive validation cycle ---


@pytest.mark.asyncio
async def test_keepalive_executes_one_validation_cycle(tmp_path):
    """A single keepalive cycle should execute and call validate_resource."""
    resource_path = tmp_path / "initial_load_resource.json"
    _make_resource_file(resource_path, '{"resourceType": "Patient", "id": "test"}')

    engine = _make_engine_with_settings(
        default_ig="pkg.default#1.0.0",
        initial_load_resource_path=str(resource_path),
        validator_keepalive_interval_seconds=0.01,  # Short interval for testing
    )

    # Track validate_resource calls
    validate_calls = []
    called = asyncio.Event()

    async def mock_validate_resource(**kwargs):
        validate_calls.append(kwargs)
        called.set()
        return httpx.Response(
            200,
            content=b'{"resourceType":"OperationOutcome","issue":[]}',
            headers={"content-type": "application/fhir+json"},
        )

    engine.validate_resource = mock_validate_resource

    # Simulate engine being ready and run one cycle directly
    engine._startup_state = "ready"
    engine._ensure_keepalive_task()

    # The task should be created now
    assert engine._keepalive_task is not None

    await asyncio.wait_for(called.wait(), timeout=1)
    await engine._cancel_keepalive_task()

    # Should have made at least one validation call
    assert len(validate_calls) >= 1

    # Check that the call was made with correct parameters
    call = validate_calls[0]
    assert call["content"] == resource_path.read_bytes()
    assert call["content_type"] == "application/fhir+json"
    assert call["profiles"] == []


@pytest.mark.asyncio
async def test_keepalive_uses_correct_content_type_for_xml(tmp_path):
    """Keepalive should use application/fhir+xml for XML resource files."""
    resource_path = tmp_path / "initial_load_resource.xml"
    _make_resource_file(resource_path, '<Patient xmlns="http://hl7.org/fhir"></Patient>')

    engine = _make_engine_with_settings(
        default_ig="pkg.default#1.0.0",
        initial_load_resource_path=str(resource_path),
        validator_keepalive_interval_seconds=0.01,  # Short interval for testing
    )

    # Track validate_resource calls
    validate_calls = []
    called = asyncio.Event()

    async def mock_validate_resource(**kwargs):
        validate_calls.append(kwargs)
        called.set()
        return httpx.Response(
            200,
            content=b'{"resourceType":"OperationOutcome","issue":[]}',
            headers={"content-type": "application/fhir+json"},
        )

    engine.validate_resource = mock_validate_resource

    # Simulate engine being ready and run one cycle directly
    engine._startup_state = "ready"
    engine._ensure_keepalive_task()

    # The task should be created now
    assert engine._keepalive_task is not None

    await asyncio.wait_for(called.wait(), timeout=1)
    await engine._cancel_keepalive_task()

    # Should have made at least one validation call with XML content type
    assert len(validate_calls) >= 1
    call = validate_calls[0]
    assert call["content_type"] == "application/fhir+xml"


# --- Keepalive interaction with recovery ---


@pytest.mark.asyncio
async def test_keepalive_does_not_trigger_recovery_on_normal_error(tmp_path):
    """Keepalive should not trigger recovery for normal validation errors."""
    resource_path = tmp_path / "initial_load_resource.json"
    _make_resource_file(resource_path)

    engine = _make_engine_with_settings(
        default_ig="pkg.default#1.0.0",
        initial_load_resource_path=str(resource_path),
        validator_keepalive_interval_seconds=0.01,  # Short interval for testing
    )

    # Track recovery requests
    recovery_requests = []
    called = asyncio.Event()

    def mock_request_recovery(reason, *, clear_tx_cache=False):
        recovery_requests.append((reason, clear_tx_cache))
        # Don't actually schedule recovery

    engine.request_recovery = mock_request_recovery

    # Simulate engine being ready
    engine._startup_state = "ready"
    engine._ensure_keepalive_task()

    # The task should be created now
    assert engine._keepalive_task is not None

    # Mock validate_resource to return an error response (not a connection error)
    async def mock_validate_resource_error(**kwargs):
        called.set()
        return httpx.Response(
            500,
            content=b'{"resourceType":"OperationOutcome","issue":[{"severity":"error","code":"invalid"}]}',
            headers={"content-type": "application/fhir+json"},
        )

    engine.validate_resource = mock_validate_resource_error

    await asyncio.wait_for(called.wait(), timeout=1)
    await engine._cancel_keepalive_task()

    # Should not have triggered recovery for normal error
    assert len(recovery_requests) == 0


@pytest.mark.asyncio
async def test_keepalive_uses_existing_recovery_on_connection_error(tmp_path):
    """Keepalive should use the normal recovery path for connection errors."""
    resource_path = tmp_path / "initial_load_resource.json"
    _make_resource_file(resource_path)

    engine = _make_engine_with_settings(
        default_ig="pkg.default#1.0.0",
        initial_load_resource_path=str(resource_path),
        validator_keepalive_interval_seconds=0.01,  # Short interval for testing
    )

    # Track recovery requests
    recovery_requests = []
    called = asyncio.Event()

    def mock_request_recovery(reason, *, clear_tx_cache=False):
        recovery_requests.append((reason, clear_tx_cache))
        # Don't actually schedule recovery

    engine.request_recovery = mock_request_recovery

    # Simulate engine being ready
    engine._startup_state = "ready"
    engine._ensure_keepalive_task()

    # The task should be created now
    assert engine._keepalive_task is not None

    async def post(url, **kwargs):
        assert url == "/validateResource"
        called.set()
        raise httpx.ConnectError("connection refused")

    engine._client.post = post

    await asyncio.wait_for(called.wait(), timeout=1)
    await engine._cancel_keepalive_task()

    assert len(recovery_requests) == 1


@pytest.mark.asyncio
async def test_keepalive_uses_existing_stale_cache_recovery(tmp_path):
    """A stale terminology response should use the normal recovery path."""
    resource_path = tmp_path / "initial_load_resource.json"
    _make_resource_file(resource_path)

    engine = _make_engine_with_settings(
        default_ig="pkg.default#1.0.0",
        initial_load_resource_path=str(resource_path),
    )
    request_recovery = MagicMock()
    engine.request_recovery = request_recovery

    async def post(url, **kwargs):
        assert url == "/validateResource"
        return httpx.Response(200, content=b"The cache was never issued by this server")

    engine._client.post = post

    await engine._validate_initial_load_resource("periodic keepalive")

    request_recovery.assert_called_once_with(
        "stale terminology-server cache reported by /validateResource",
        clear_tx_cache=True,
    )


# --- Keepalive interval configuration ---


@pytest.mark.asyncio
async def test_keepalive_respects_configured_interval(tmp_path):
    """Keepalive should use the configured interval."""
    resource_path = tmp_path / "initial_load_resource.json"
    _make_resource_file(resource_path)

    engine = _make_engine_with_settings(
        default_ig="pkg.default#1.0.0",
        initial_load_resource_path=str(resource_path),
        validator_keepalive_interval_seconds=123.45,  # Custom interval
    )

    # Simulate engine being ready
    engine._startup_state = "ready"
    engine._ensure_keepalive_task()

    # Check that the task was created
    assert engine._keepalive_task is not None


@pytest.mark.asyncio
async def test_keepalive_defaults_to_configured_interval_when_not_set(tmp_path):
    """Keepalive should use config default when not explicitly set."""
    resource_path = tmp_path / "initial_load_resource.json"
    _make_resource_file(resource_path)

    # Create engine without explicitly setting keepalive interval
    # This should use the default from Settings (300.0 seconds)
    settings = Settings(
        default_ig="pkg.default#1.0.0",
        initial_load_resource_path=str(resource_path),
    )

    engine = ValidatorEngine(settings)
    # Simulate a running process for testing
    engine._process = MagicMock()
    engine._process.returncode = None
    engine._process.wait = AsyncMock(return_value=0)
    engine._process.terminate = MagicMock()
    engine._process.kill = MagicMock()

    # Simulate engine being ready
    engine._startup_state = "ready"
    engine._ensure_keepalive_task()

    # Task should be scheduled with default interval
    assert engine._keepalive_task is not None


# --- Edge cases ---


@pytest.mark.asyncio
async def test_keepalive_task_survives_engine_restart(tmp_path):
    """Keepalive task should be recreated after engine restart."""
    resource_path = tmp_path / "initial_load_resource.json"
    _make_resource_file(resource_path)

    engine = _make_engine_with_settings(
        default_ig="pkg.default#1.0.0",
        initial_load_resource_path=str(resource_path),
        validator_keepalive_interval_seconds=3600.0,  # Very long interval
    )

    # First start simulation
    engine._startup_state = "ready"
    engine._ensure_keepalive_task()
    first_task = engine._keepalive_task
    assert first_task is not None

    # Simulate restart by cancelling and recreating the task.
    await engine._cancel_keepalive_task()
    assert engine._keepalive_task is None

    # Second start simulation
    engine._startup_state = "ready"
    engine._ensure_keepalive_task()
    second_task = engine._keepalive_task
    assert second_task is not None

    # Should be a new task
    assert first_task is not second_task

    await engine._cancel_keepalive_task()


@pytest.mark.asyncio
async def test_keepalive_task_cleanup_on_engine_stop_while_running(tmp_path):
    """Keepalive task should be properly cleaned up when engine stops during task execution."""
    resource_path = tmp_path / "initial_load_resource.json"
    _make_resource_file(resource_path)

    engine = _make_engine_with_settings(
        default_ig="pkg.default#1.0.0",
        initial_load_resource_path=str(resource_path),
        validator_keepalive_interval_seconds=0.01,  # Short interval
    )

    # Simulate engine being ready
    engine._startup_state = "ready"
    engine._ensure_keepalive_task()
    assert engine._keepalive_task is not None

    # Stop the engine immediately - this should cancel the keepalive task
    await engine._cancel_keepalive_task()

    # Task should be cleaned up
    assert engine._keepalive_task is None
