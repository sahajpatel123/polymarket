"""CLI smoke tests for V3 commands (help text + registration)."""

from __future__ import annotations

from typer.testing import CliRunner

from polymaker.cli import app

runner = CliRunner()

V3_COMMANDS = ("improve", "review", "explain", "capital", "memory")


def test_v3_commands_registered() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in V3_COMMANDS:
        assert name in result.stdout, f"missing command in help: {name}"


def test_improve_help() -> None:
    result = runner.invoke(app, ["improve", "--help"])
    assert result.exit_code == 0
    assert "self-improvement" in result.stdout.lower() or "improvement" in result.stdout.lower()
    assert "--paper" in result.stdout
    assert "--force" in result.stdout


def test_review_help() -> None:
    result = runner.invoke(app, ["review", "--help"])
    assert result.exit_code == 0
    assert "end-of-day" in result.stdout.lower() or "review" in result.stdout.lower()
    assert "--paper" in result.stdout


def test_explain_help() -> None:
    result = runner.invoke(app, ["explain", "--help"])
    assert result.exit_code == 0
    assert "cid" in result.stdout.lower() or "condition" in result.stdout.lower()
    assert "--paper" in result.stdout


def test_capital_help() -> None:
    result = runner.invoke(app, ["capital", "--help"])
    assert result.exit_code == 0
    assert "capital" in result.stdout.lower()
    assert "--paper" in result.stdout


def test_memory_help() -> None:
    result = runner.invoke(app, ["memory", "--help"])
    assert result.exit_code == 0
    assert "memory" in result.stdout.lower()
    assert "search" in result.stdout.lower()


def test_memory_search_help() -> None:
    result = runner.invoke(app, ["memory", "search", "--help"])
    assert result.exit_code == 0
    assert "search" in result.stdout.lower()
    assert "q" in result.stdout.lower() or "query" in result.stdout.lower()


def test_improve_and_review_dry_run_flags() -> None:
    r1 = runner.invoke(app, ["improve", "--help"])
    assert r1.exit_code == 0
    assert "--dry-run" in r1.stdout
    assert "--rollback-ts" in r1.stdout
    r2 = runner.invoke(app, ["review", "--help"])
    assert r2.exit_code == 0
    assert "--dry-run" in r2.stdout
