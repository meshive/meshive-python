import subprocess
import sys
from pathlib import Path

import pytest

import meshive
from meshive.cli.main import build_parser, main


def test_parser_has_version_action():
    parser = build_parser()
    actions = {a.dest: a for a in parser._actions}
    assert "version" in actions


def test_version_flag_prints_version_and_exits(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0

    captured = capsys.readouterr()
    assert meshive.__version__ in captured.out


def test_no_args_prints_help(capsys):
    exit_code = main([])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "meshive" in captured.out.lower()
    assert "--version" in captured.out


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_flag_prints_help_and_exits(flag, capsys):
    with pytest.raises(SystemExit) as exc_info:
        main([flag])
    assert exc_info.value.code == 0

    captured = capsys.readouterr()
    assert "usage:" in captured.out
    assert "--version" in captured.out
    assert "Meshive GPU Cloud CLI" in captured.out
    assert "Examples:" in captured.out


def test_unknown_argument_exits_with_error(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["nonexistent-command"])
    assert exc_info.value.code != 0


def test_console_script_runs():
    """End-to-end: the installed `meshive` console script works."""
    script = Path(sys.executable).parent / "meshive"
    if not script.exists():
        pytest.skip(f"console script not found at {script}")
    result = subprocess.run(
        [str(script), "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert meshive.__version__ in result.stdout


def test_module_invocation_runs():
    """End-to-end: `python -m meshive --version` works."""
    result = subprocess.run(
        [sys.executable, "-m", "meshive", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert meshive.__version__ in result.stdout
