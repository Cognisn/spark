"""The command blocklist applies to every command in a chain, in every shell dialect.

The tables are pure parsing: they run on every platform, whatever the host shell.
Blocked names here are placeholders; nothing is executed.
"""

from __future__ import annotations

import base64

import pytest

from spark.tools.command_policy import (
    Dialect,
    UnparseableCommand,
    check_command,
    command_names,
    host_dialect,
)

BLOCKED = frozenset({"dd", "format", "mkfs"})

ALLOW = "allow"
BLOCK = "block"
REFUSE = "refuse"


def _encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


POSIX_CASES: list[tuple[str, str, str]] = [
    ("plain command", "ls -la /tmp", ALLOW),
    ("first command", "dd if=/dev/zero of=/tmp/x", BLOCK),
    ("semicolon", "ls; dd if=/dev/zero", BLOCK),
    ("and", "ls && dd if=/dev/zero", BLOCK),
    ("or", "ls || dd if=/dev/zero", BLOCK),
    ("pipe", "ls | dd of=/tmp/x", BLOCK),
    ("pipe stderr", "ls |& dd of=/tmp/x", BLOCK),
    ("background", "ls & dd of=/tmp/x", BLOCK),
    ("newline", "ls\ndd of=/tmp/x", BLOCK),
    ("carriage return", "ls\r\ndd of=/tmp/x", BLOCK),
    ("backticks", "echo `dd if=/dev/zero`", BLOCK),
    ("dollar paren", "echo $(dd if=/dev/zero)", BLOCK),
    ("dollar paren in double quotes", 'echo "$(dd if=/dev/zero)"', BLOCK),
    ("backticks in double quotes", 'echo "`dd`"', BLOCK),
    ("nested substitution", "echo $(echo $(dd))", BLOCK),
    ("process substitution", "cat <(dd if=/dev/zero)", BLOCK),
    ("subshell", "(dd if=/dev/zero)", BLOCK),
    ("brace group", "{ dd if=/dev/zero; }", BLOCK),
    ("if clause", "if true; then dd if=/dev/zero; fi", BLOCK),
    ("negation", "! dd if=/dev/zero", BLOCK),
    ("assignment prefix", "LANG=C dd if=/dev/zero", BLOCK),
    ("sudo wrapper", "sudo dd if=/dev/zero", BLOCK),
    ("env wrapper with assignment", "env LANG=C dd if=/dev/zero", BLOCK),
    ("timeout wrapper", "timeout 5 dd if=/dev/zero", BLOCK),
    ("nohup wrapper", "nohup dd if=/dev/zero", BLOCK),
    ("absolute path", "/bin/dd if=/dev/zero", BLOCK),
    ("upper case", "DD if=/dev/zero", BLOCK),
    ("empty quotes inside name", "d''d if=/dev/zero", BLOCK),
    ("escaped name", "\\dd if=/dev/zero", BLOCK),
    ("quoted name", '"dd" if=/dev/zero', BLOCK),
    ("bash -c payload", "bash -c 'ls; dd if=/dev/zero'", BLOCK),
    ("sh -c payload", 'sh -c "dd if=/dev/zero"', BLOCK),
    ("pwsh -Command payload", "pwsh -Command 'Get-Date; format c:'", BLOCK),
    ("separator in double quotes", 'git commit -m "fix; format docs"', ALLOW),
    ("separator in single quotes", "echo 'a && dd'", ALLOW),
    ("substitution in single quotes", "echo '$(dd)'", ALLOW),
    ("escaped separator", "echo a \\; dd", ALLOW),
    ("blocked word as an argument", "grep dd notes.txt", ALLOW),
    ("redirection and pipe", "ls -la 2>&1 | grep -v total > out.txt", ALLOW),
    ("everyday chain", "cd /tmp && git status --short", ALLOW),
    ("sudo option value", "sudo -u root dd if=/dev/zero", BLOCK),
    ("comment", "ls # dd if=/dev/zero", ALLOW),
    ("unterminated single quote", "echo 'unterminated", REFUSE),
    ("unterminated double quote", 'echo "unterminated', REFUSE),
    ("unterminated substitution", "echo $(dd", REFUSE),
    ("unterminated backtick", "echo `dd", REFUSE),
    ("expansion as command name", "$CMD if=/dev/zero", REFUSE),
    ("glob as command name", "/bin/d? if=/dev/zero", REFUSE),
    ("trailing escape", "ls \\", REFUSE),
]

CMD_CASES: list[tuple[str, str, str]] = [
    ("plain command", "dir C:\\Temp", ALLOW),
    ("first command", "format c:", BLOCK),
    ("ampersand", "dir & format c:", BLOCK),
    ("and", "dir && format c:", BLOCK),
    ("or", "dir || format c:", BLOCK),
    ("pipe", "dir | format c:", BLOCK),
    ("newline", "dir\r\nformat c:", BLOCK),
    ("semicolon", "dir ; format c:", BLOCK),
    ("group", "(format c:)", BLOCK),
    ("cmd /c", "cmd /c format c:", BLOCK),
    ("cmd /c quoted chain", 'cmd /c "dir & format c:"', BLOCK),
    ("cmd.exe /C upper case", "CMD.EXE /C format c:", BLOCK),
    ("cmd /k", "cmd /k format c:", BLOCK),
    ("full path with extension", "C:\\Windows\\System32\\format.com c:", BLOCK),
    ("single quotes do not quote", "echo 'a & format c:'", BLOCK),
    ("powershell -Command", 'powershell -Command "format c:"', BLOCK),
    ("separator in double quotes", 'echo "a & format c:"', ALLOW),
    ("caret escaped separator", "echo a ^& format c:", ALLOW),
    ("blocked word as an argument", "findstr format notes.txt", ALLOW),
    ("variable as command name", "%COMSPEC% /c dir", REFUSE),
    ("delayed expansion as command name", "!CMD! /c dir", REFUSE),
    ("unterminated double quote", 'echo "unterminated', REFUSE),
    ("trailing caret", "dir ^", REFUSE),
]

POWERSHELL_CASES: list[tuple[str, str, str]] = [
    ("plain command", "Get-ChildItem C:\\Temp", ALLOW),
    ("first command", "format c:", BLOCK),
    ("semicolon", "Get-Date; format c:", BLOCK),
    ("and", "Get-Date && format c:", BLOCK),
    ("or", "Get-Date || format c:", BLOCK),
    ("pipe", "Get-Date | format c:", BLOCK),
    ("newline", "Get-Date\nformat c:", BLOCK),
    ("call operator", "& format c:", BLOCK),
    ("call operator with quoted name", "& 'format' c:", BLOCK),
    ("subexpression", "Write-Output $(format c:)", BLOCK),
    ("subexpression in double quotes", 'Write-Output "$(format c:)"', BLOCK),
    ("array subexpression", "Write-Output @(format c:)", BLOCK),
    ("script block", "1..3 | ForEach-Object { format c: }", BLOCK),
    ("Invoke-Expression", "Invoke-Expression 'format c:'", BLOCK),
    ("iex alias", 'iex "Get-Date; format c:"', BLOCK),
    ("nested -Command", 'powershell -NoProfile -Command "Get-Date; format c:"', BLOCK),
    ("pwsh -c", "pwsh -c 'format c:'", BLOCK),
    ("-EncodedCommand", f"powershell -EncodedCommand {_encoded('format c:')}", BLOCK),
    ("-enc prefix", f"powershell -enc {_encoded('Get-Date; format c:')}", BLOCK),
    ("-e prefix", f"pwsh -e {_encoded('format c:')}", BLOCK),
    ("cmd /c from powershell", "cmd /c format c:", BLOCK),
    ("subexpression in single quotes", "Write-Output '$(format c:)'", ALLOW),
    ("separator in single quotes", "Write-Output 'a; format c:'", ALLOW),
    ("backtick escaped separator", "Write-Output a `; format c:", ALLOW),
    ("harmless -Command", 'powershell -NoProfile -Command "Get-Date"', ALLOW),
    ("expression filter", "Get-ChildItem | Where-Object { $_.Length -gt 1 }", ALLOW),
    ("assignment from a command", "$x = format c:", BLOCK),
    ("dot-sourced variable", ". $script", REFUSE),
    ("piped Invoke-Expression", "'format c:' | iex", REFUSE),
    ("variable as command name", "& $cmd c:", REFUSE),
    ("undecodable -EncodedCommand", "powershell -EncodedCommand not-base64!", REFUSE),
    ("missing -Command payload", "powershell -Command", REFUSE),
    ("unterminated subexpression", "Write-Output $(format c:", REFUSE),
    ("unterminated single quote", "Write-Output 'unterminated", REFUSE),
]


def _outcome(command: str, dialect: Dialect) -> str:
    message = check_command(command, BLOCKED, dialect=dialect)
    if message is None:
        return ALLOW
    if message.startswith("Blocked:"):
        return BLOCK
    assert message.startswith("Refused:"), message
    return REFUSE


@pytest.mark.parametrize(("label", "command", "expected"), POSIX_CASES)
def test_posix_chains(label: str, command: str, expected: str) -> None:
    assert _outcome(command, Dialect.POSIX) == expected, label


@pytest.mark.parametrize(("label", "command", "expected"), CMD_CASES)
def test_cmd_chains(label: str, command: str, expected: str) -> None:
    assert _outcome(command, Dialect.CMD) == expected, label


@pytest.mark.parametrize(("label", "command", "expected"), POWERSHELL_CASES)
def test_powershell_chains(label: str, command: str, expected: str) -> None:
    assert _outcome(command, Dialect.POWERSHELL) == expected, label


class TestMessages:
    def test_blocked_message_names_the_command(self) -> None:
        message = check_command("ls; dd if=/dev/zero", BLOCKED, dialect=Dialect.POSIX)
        assert message == "Blocked: 'dd' is not permitted."

    def test_refusal_explains_why(self) -> None:
        message = check_command("echo $(dd", BLOCKED, dialect=Dialect.POSIX)
        assert message is not None
        assert message.startswith("Refused: the command could not be checked safely")
        assert "substitution" in message

    def test_empty_blocklist_still_refuses_unparseable_input(self) -> None:
        assert check_command("echo 'open", frozenset(), dialect=Dialect.POSIX) is not None

    def test_blocklist_names_are_case_insensitive(self) -> None:
        assert check_command("ls; dd", frozenset({"DD"}), dialect=Dialect.POSIX) is not None

    def test_blocklist_names_ignore_windows_extensions(self) -> None:
        assert check_command("dir & format.exe c:", BLOCKED, dialect=Dialect.CMD) is not None


class TestCommandNames:
    def test_every_command_in_a_chain_is_listed(self) -> None:
        assert command_names("ls; cat a | grep b && echo $(date)", Dialect.POSIX) == [
            "ls",
            "cat",
            "grep",
            "echo",
            "date",
        ]

    def test_unparseable_raises(self) -> None:
        with pytest.raises(UnparseableCommand):
            command_names("echo 'open", Dialect.POSIX)


@pytest.mark.parametrize(
    ("system", "dialect"),
    [("Windows", Dialect.CMD), ("Darwin", Dialect.POSIX), ("Linux", Dialect.POSIX)],
)
def test_host_dialect(system: str, dialect: Dialect) -> None:
    assert host_dialect(system) is dialect
