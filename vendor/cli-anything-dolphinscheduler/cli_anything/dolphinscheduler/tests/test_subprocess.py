"""Subprocess tests for the installed CLI command.

These tests verify that `cli-anything-dolphinscheduler` works as an installed
binary from PATH, not just as a Python module.

Prerequisites:
1. Run `pip install -e .` from the repository root
2. Set CLI_ANYTHING_FORCE_INSTALLED=1 to enable these tests

Run with:
    pip install -e .
    export CLI_ANYTHING_FORCE_INSTALLED=1
    pytest -v tests/test_subprocess.py
"""

import json
import os
import subprocess

import pytest


def _resolve_cli(command: str) -> str:
    """Resolve installed CLI command to full path.

    Skips tests if not installed or FORCE_INSTALLED guard not set.
    """
    if not os.environ.get("CLI_ANYTHING_FORCE_INSTALLED"):
        pytest.skip(
            f"Subprocess tests require pip install -e . and "
            f"CLI_ANYTHING_FORCE_INSTALLED=1"
        )

    result = subprocess.run(
        ["which", command], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        pytest.skip(f"{command} not found in PATH")

    return result.stdout.strip()


def test_cli_version():
    """Installed CLI --version works."""
    cli_path = _resolve_cli("cli-anything-dolphinscheduler")
    result = subprocess.run(
        [cli_path, "--version"], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0
    assert "1.0.0" in result.stdout


def test_cli_help():
    """Installed CLI --help shows command groups."""
    cli_path = _resolve_cli("cli-anything-dolphinscheduler")
    result = subprocess.run(
        [cli_path, "--help"], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0
    assert "project" in result.stdout
    assert "workflow" in result.stdout
    assert "resource" in result.stdout
    assert "datasource" in result.stdout
    assert "run" in result.stdout
    assert "instance" in result.stdout
    assert "log" in result.stdout


def test_cli_resource_help():
    """Installed CLI resource help exposes Resource Center commands."""
    cli_path = _resolve_cli("cli-anything-dolphinscheduler")
    result = subprocess.run(
        [cli_path, "resource", "--help"], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0
    assert "create-file" in result.stdout
    assert "upload" in result.stdout
    assert "download" in result.stdout
    assert "delete" in result.stdout


def test_cli_instance_help():
    """Installed CLI instance help exposes workflow and task-instance commands."""
    cli_path = _resolve_cli("cli-anything-dolphinscheduler")
    result = subprocess.run(
        [cli_path, "instance", "--help"], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0
    assert "task-list" in result.stdout
    assert "tasks" in result.stdout
    assert "force-task-success" in result.stdout
    assert "stop-task" in result.stdout


def test_cli_datasource_help():
    """Installed CLI datasource help exposes datasource lifecycle commands."""
    cli_path = _resolve_cli("cli-anything-dolphinscheduler")
    result = subprocess.run(
        [cli_path, "datasource", "--help"], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0
    assert "create" in result.stdout
    assert "test-param" in result.stdout
    assert "databases" in result.stdout
    assert "columns" in result.stdout


def test_cli_log_help():
    """Installed CLI log help exposes task-log commands."""
    cli_path = _resolve_cli("cli-anything-dolphinscheduler")
    result = subprocess.run(
        [cli_path, "log", "--help"], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0
    assert "detail" in result.stdout
    assert "download" in result.stdout


def test_cli_config_show():
    """config show works with synthetic config."""
    cli_path = _resolve_cli("cli-anything-dolphinscheduler")
    result = subprocess.run(
        [
            cli_path,
            "--url",
            "http://test/dolphinscheduler",
            "--token",
            "test123",
            "config",
            "show",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "http://test/dolphinscheduler" in result.stdout


def test_cli_project_list_json_no_server():
    """project list --json returns structured error when server unreachable."""
    cli_path = _resolve_cli("cli-anything-dolphinscheduler")
    result = subprocess.run(
        [
            cli_path,
            "--url",
            "http://localhost:99999/dolphinscheduler",
            "--user",
            "admin",
            "--password",
            "admin",
            "--json",
            "project",
            "list",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    # Should exit non-zero and emit JSON error
    assert result.returncode != 0
    try:
        data = json.loads(result.stderr)
        assert data["success"] is False
        assert "error" in data
    except (json.JSONDecodeError, KeyError):
        pytest.fail("Expected structured JSON error on stderr")


@pytest.mark.e2e
def test_cli_project_list_json_with_server():
    """project list --json works against a real server (E2E)."""
    cli_path = _resolve_cli("cli-anything-dolphinscheduler")
    result = subprocess.run(
        [
            cli_path,
            "--url",
            "http://localhost:12345/dolphinscheduler",
            "--user",
            "admin",
            "--password",
            "dolphinscheduler123",
            "--json",
            "project",
            "list",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        pytest.skip("DolphinScheduler server not available")

    data = json.loads(result.stdout)
    assert data["success"] is True
    assert "data" in data
