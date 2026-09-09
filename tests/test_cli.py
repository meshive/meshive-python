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


# --- 금액 표시: 웹 콘솔과 같은 값 ------------------------------------------------
# 규칙은 `WebFrontend/src/common/Formatter.tsx` 가 소스다.
#   시간당 요금($/hr) → formatHourlyUsd = 3자리 고정
#   그 외 금액        → formatUsd       = 2자리
# 반올림도 콘솔(Intl.NumberFormat, halfExpand)과 같아야 한다 — 파이썬 float 포맷은
# half-even + 이진 오차라 경계값에서 갈린다.
@pytest.mark.parametrize("value,expected", [
    ("0.06770833", "$0.068"),      # 워크스페이스 시간당 합계
    ("2.10000000", "$2.100"),      # 서버 Numeric(20,8) 원문
    ("0.00097222", "$0.001"),      # nfs 10GB — 2자리면 "$0.00" 이 된다
    ("0", "$0.000"),
    ("-12.5", "-$12.500"),         # 음수는 '-$' (원장 환급행)
    (None, "-"),
    ("", "-"),
    ("nonsense", "-"),
])
def test_money_hourly_matches_console_three_digits(value, expected):
    from meshive.cli import _format as fmt
    assert fmt.money_hourly(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("2.10000000", "$2.10"),
    ("0.015", "$0.02"),            # halfExpand — 콘솔 Intl 과 같은 방향
    ("1234.5678", "$1,234.57"),
    ("-12.5", "-$12.50"),
    (None, "-"),
])
def test_money_matches_console_two_digits(value, expected):
    from meshive.cli import _format as fmt
    assert fmt.money(value) == expected
