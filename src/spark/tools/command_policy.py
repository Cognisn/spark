"""Command blocklist: check every command in a chain, not only the first.

A command line for the system command tool can hold several commands joined by
shell syntax: ``;``, ``&&``, ``||``, ``|``, ``&``, newlines, substitutions
(backticks, ``$( )``), groups, and nested shells (``bash -c``, ``cmd /c``,
``powershell -Command`` or ``-EncodedCommand``). This module splits a command
line in the dialect of the shell that will run it, follows nested shells into
their own dialect, and checks the name of every command it finds against the
blocklist.

The parser is conservative. Anything it cannot split with confidence (an
unterminated quote or substitution, a command name built from a variable or a
wildcard, an undecodable encoded payload) is refused rather than guessed at.
It does not execute or expand anything.
"""

from __future__ import annotations

import base64
import binascii
import platform
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "Dialect",
    "UnparseableCommand",
    "check_command",
    "command_names",
    "host_dialect",
    "normalise_name",
]


class Dialect(StrEnum):
    POSIX = "posix"
    CMD = "cmd"
    POWERSHELL = "powershell"


class UnparseableCommand(ValueError):
    """The command line cannot be split into commands with confidence."""


_MAX_DEPTH = 16

_EXTENSIONS = (".exe", ".com", ".bat", ".cmd", ".ps1")

# Words that introduce or close shell syntax rather than name a command.
_POSIX_RESERVED = frozenset(
    {
        "!",
        "{",
        "}",
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "do",
        "done",
        "while",
        "until",
        "for",
        "in",
        "case",
        "esac",
        "select",
        "function",
        "coproc",
    }
)
_POWERSHELL_RESERVED = frozenset(
    {
        "if",
        "elseif",
        "else",
        "foreach",
        "for",
        "while",
        "do",
        "until",
        "switch",
        "try",
        "catch",
        "finally",
        "function",
        "filter",
        "param",
        "begin",
        "process",
        "end",
        "return",
        "throw",
        "trap",
        "data",
    }
)

# Commands that run another command named in their arguments. After one of
# these every further non-option word is checked, so ``sudo -u root dd`` cannot
# hide ``dd`` behind an option value.
_WRAPPERS = frozenset(
    {
        "sudo",
        "doas",
        "env",
        "nohup",
        "nice",
        "time",
        "timeout",
        "command",
        "builtin",
        "exec",
        "xargs",
        "stdbuf",
        "ionice",
        "chrt",
        "setsid",
        "unbuffer",
        "caffeinate",
        "busybox",
        "strace",
        "watch",
        "start",
        "call",
    }
)

_POSIX_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "fish"})
_POWERSHELLS = frozenset({"powershell", "pwsh"})
_INVOKE_EXPRESSION = frozenset({"iex", "invoke-expression"})

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")


@dataclass
class _Word:
    text: str = ""
    dynamic: bool = False


@dataclass
class _Command:
    words: list[_Word] = field(default_factory=list)
    after_call_operator: bool = False


def normalise_name(name: str) -> str:
    """The comparable form of a command name: base name, lower case, no Windows extension."""
    base = re.split(r"[\\/]", name.strip())[-1].lower()
    for ext in _EXTENSIONS:
        if base.endswith(ext) and len(base) > len(ext):
            return base[: -len(ext)]
    return base


def host_dialect(system: str | None = None) -> Dialect:
    """The dialect of the shell the system command tool uses on this host."""
    name = system if system is not None else platform.system()
    return Dialect.CMD if name == "Windows" else Dialect.POSIX


def check_command(command: str, blocked: Iterable[str], *, dialect: Dialect) -> str | None:
    """Return a refusal message for the command line, or ``None`` when it may run."""
    blocklist = {normalise_name(b) for b in blocked if isinstance(b, str) and b.strip()}
    try:
        names = command_names(command, dialect)
    except UnparseableCommand as exc:
        return (
            f"Refused: the command could not be checked safely ({exc}). "
            "Run each command on its own, without shell substitutions or variables "
            "in command names."
        )
    for name in names:
        if name in blocklist:
            return f"Blocked: '{name}' is not permitted."
    return None


def command_names(command: str, dialect: Dialect) -> list[str]:
    """Every command name in the command line, in order, normalised.

    Raises :class:`UnparseableCommand` when the line cannot be split with confidence.
    """
    names: list[str] = []
    _collect(command, dialect, names, depth=0)
    return names


# -- Name resolution -----------------------------------------------------------


def _collect(text: str, dialect: Dialect, names: list[str], *, depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise UnparseableCommand("commands are nested too deeply")
    for cmd in _split(text, dialect, depth):
        _resolve(cmd, dialect, names, depth)


def _resolve(cmd: _Command, dialect: Dialect, names: list[str], depth: int) -> None:
    words = list(cmd.words)
    reserved = _POWERSHELL_RESERVED if dialect is Dialect.POWERSHELL else _POSIX_RESERVED
    if dialect is not Dialect.CMD:
        while words and not words[0].dynamic and words[0].text.lower() in reserved:
            words.pop(0)
    if dialect is Dialect.POSIX:
        while words and _ASSIGNMENT.match(words[0].text):
            words.pop(0)
    after_call = cmd.after_call_operator
    if dialect is Dialect.POWERSHELL and words and words[0].text == "." and not words[0].dynamic:
        words.pop(0)
        after_call = True
    if not words:
        return

    first = words[0]
    if first.dynamic:
        if dialect is Dialect.POWERSHELL and not after_call:
            # An expression statement such as ``$x = format c:`` or
            # ``$_.Length -gt 1``: no command is named by the first word, so
            # every word that could name one is checked.
            names.extend(normalise_name(w.text) for w in words[1:] if _is_plain_word(w))
            return
        raise UnparseableCommand("a command name is built from a variable or a wildcard")

    name = normalise_name(first.text)
    if not name:
        return
    names.append(name)
    rest = words[1:]

    if name in _WRAPPERS:
        for word in rest:
            if _is_plain_word(word) and not _ASSIGNMENT.match(word.text):
                names.append(normalise_name(word.text))
        return
    if name in _POSIX_SHELLS:
        payload = _posix_shell_payload(rest)
        if payload is not None:
            _collect(payload, Dialect.POSIX, names, depth=depth + 1)
        return
    if name == "cmd":
        payload = _cmd_payload(rest)
        if payload is not None:
            _collect(payload, Dialect.CMD, names, depth=depth + 1)
        return
    if name in _POWERSHELLS:
        payload = _powershell_payload(rest)
        if payload is not None:
            _collect(payload, Dialect.POWERSHELL, names, depth=depth + 1)
        return
    if name in _INVOKE_EXPRESSION:
        if not rest:
            raise UnparseableCommand("Invoke-Expression without an argument cannot be checked")
        _collect(" ".join(w.text for w in rest), Dialect.POWERSHELL, names, depth=depth + 1)


def _is_plain_word(word: _Word) -> bool:
    return not word.dynamic and bool(word.text) and not word.text.startswith("-")


def _posix_shell_payload(args: list[_Word]) -> str | None:
    for i, word in enumerate(args):
        text = word.text
        if text == "--":
            return None
        if text.startswith("-") and not text.startswith("--") and "c" in text[1:]:
            if i + 1 >= len(args):
                raise UnparseableCommand("a shell -c option has no command")
            return args[i + 1].text
        if not text.startswith("-") and not text.startswith("+"):
            return None
    return None


def _cmd_payload(args: list[_Word]) -> str | None:
    for i, word in enumerate(args):
        flag = word.text.lower()
        if flag in ("/c", "/k", "/r"):
            if i + 1 >= len(args):
                raise UnparseableCommand("cmd /c has no command")
            return " ".join(w.text for w in args[i + 1 :])
        if not flag.startswith("/"):
            return None
    return None


def _powershell_payload(args: list[_Word]) -> str | None:
    for i, word in enumerate(args):
        text = word.text
        if not text.startswith("-") and not text.startswith("/"):
            # A positional argument: Windows PowerShell treats it as -Command.
            return " ".join(w.text for w in args[i:])
        option = text[1:].lower()
        if option in ("c", "command") or (len(option) >= 2 and "command".startswith(option)):
            if i + 1 >= len(args):
                raise UnparseableCommand("PowerShell -Command has no command")
            return " ".join(w.text for w in args[i + 1 :])
        if option == "ec" or (
            option.startswith("e") and option != "ex" and "encodedcommand".startswith(option)
        ):
            if i + 1 >= len(args):
                raise UnparseableCommand("PowerShell -EncodedCommand has no payload")
            return _decode_powershell(args[i + 1].text)
        if option in ("f", "file") or (len(option) >= 2 and "file".startswith(option)):
            return None
    return None


def _decode_powershell(payload: str) -> str:
    try:
        raw = base64.b64decode(payload, validate=True)
        return raw.decode("utf-16-le")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise UnparseableCommand("an encoded PowerShell command could not be decoded") from None


# -- Splitting -----------------------------------------------------------------


class _Splitter:
    """Split one command line into simple commands in one dialect."""

    def __init__(self, text: str, dialect: Dialect, depth: int) -> None:
        self.s = text
        self.n = len(text)
        self.dialect = dialect
        self.depth = depth
        self.commands: list[_Command] = []
        self.nested: list[_Command] = []
        self.current = _Command()
        self.word: _Word | None = None

    # word and command boundaries

    def _char(self, ch: str) -> None:
        if self.word is None:
            self.word = _Word()
        self.word.text += ch

    def _start_word(self) -> _Word:
        if self.word is None:
            self.word = _Word()
        return self.word

    def _end_word(self) -> None:
        if self.word is not None:
            self.current.words.append(self.word)
            self.word = None

    def _end_command(self, *, call_operator: bool = False) -> None:
        self._end_word()
        if self.current.words:
            self.commands.append(self.current)
        self.commands.extend(self.nested)
        self.nested = []
        self.current = _Command(after_call_operator=call_operator)

    def _substitute(self, inner: str, dialect: Dialect | None = None) -> None:
        self._start_word().dynamic = True
        if self.depth + 1 > _MAX_DEPTH:
            raise UnparseableCommand("commands are nested too deeply")
        self.nested.extend(_split(inner, dialect or self.dialect, self.depth + 1))

    def split(self) -> list[_Command]:
        if self.dialect is Dialect.POSIX:
            self._posix()
        elif self.dialect is Dialect.CMD:
            self._cmd()
        else:
            self._powershell()
        self._end_command()
        return self.commands

    # POSIX shells (bash, zsh, sh)

    def _posix(self) -> None:
        s, i = self.s, 0
        while i < self.n:
            c = s[i]
            nxt = s[i + 1] if i + 1 < self.n else ""
            if c == "\\":
                if not nxt:
                    raise UnparseableCommand("the command ends with an escape character")
                if nxt != "\n":
                    self._char(nxt)
                i += 2
            elif c == "'":
                end = s.find("'", i + 1)
                if end < 0:
                    raise UnparseableCommand("a single quote is not closed")
                self._start_word().text += s[i + 1 : end]
                i = end + 1
            elif c == '"':
                i = self._posix_double(i + 1)
            elif c == "`":
                end = _find_backtick(s, i + 1)
                self._substitute(s[i + 1 : end])
                i = end + 1
            elif c == "$" and nxt == "(":
                inner, end = _balanced(s, i + 1, self.dialect)
                self._substitute(inner)
                i = end + 1
            elif c in "<>" and nxt == "(":
                self._end_word()
                inner, end = _balanced(s, i + 1, self.dialect)
                self.nested.extend(_split(inner, self.dialect, self.depth + 1))
                i = end + 1
            elif c == "$" or c in "*?[":
                self._start_word().dynamic = True
                self._char(c)
                i += 1
            elif c == "#" and self.word is None:
                end = s.find("\n", i)
                i = self.n if end < 0 else end
            elif c in " \t":
                self._end_word()
                i += 1
            elif c in ";&|\n\r()":
                self._end_command()
                i += 1
            else:
                self._char(c)
                i += 1

    def _posix_double(self, i: int) -> int:
        s = self.s
        word = self._start_word()
        while i < self.n:
            c = s[i]
            nxt = s[i + 1] if i + 1 < self.n else ""
            if c == '"':
                return i + 1
            if c == "\\" and nxt in ("$", "`", '"', "\\", "\n"):
                if nxt != "\n":
                    word.text += nxt
                i += 2
            elif c == "`":
                end = _find_backtick(s, i + 1)
                self._substitute(s[i + 1 : end])
                i = end + 1
            elif c == "$" and nxt == "(":
                inner, end = _balanced(s, i + 1, self.dialect)
                self._substitute(inner)
                i = end + 1
            else:
                if c == "$":
                    word.dynamic = True
                word.text += c
                i += 1
        raise UnparseableCommand("a double quote is not closed")

    # cmd.exe

    def _cmd(self) -> None:
        s, i = self.s, 0
        while i < self.n:
            c = s[i]
            if c == "^":
                if i + 1 >= self.n:
                    raise UnparseableCommand("the command ends with an escape character")
                self._char(s[i + 1])
                i += 2
            elif c == '"':
                end = s.find('"', i + 1)
                if end < 0:
                    raise UnparseableCommand("a double quote is not closed")
                word = self._start_word()
                content = s[i + 1 : end]
                if "%" in content or "!" in content:
                    word.dynamic = True
                word.text += content
                i = end + 1
            elif c in "%!":
                self._start_word().dynamic = True
                self._char(c)
                i += 1
            elif c in " \t,=":
                self._end_word()
                i += 1
            elif c in "&|;\n\r()":
                self._end_command()
                i += 1
            else:
                self._char(c)
                i += 1

    # PowerShell

    def _powershell(self) -> None:
        s, i = self.s, 0
        while i < self.n:
            c = s[i]
            nxt = s[i + 1] if i + 1 < self.n else ""
            if c == "`":
                if not nxt:
                    raise UnparseableCommand("the command ends with an escape character")
                if nxt not in "\r\n":
                    self._char(nxt)
                i += 2
            elif c == "'":
                i = self._powershell_single(i + 1)
            elif c == '"':
                i = self._powershell_double(i + 1)
            elif c in "$@" and nxt == "(":
                inner, end = _balanced(s, i + 1, self.dialect)
                self._substitute(inner)
                i = end + 1
            elif c == "$":
                self._start_word().dynamic = True
                self._char(c)
                i += 1
            elif c == "#" and self.word is None:
                end = s.find("\n", i)
                i = self.n if end < 0 else end
            elif c in " \t":
                self._end_word()
                i += 1
            elif c == "&":
                if nxt == "&":
                    self._end_command()
                    i += 2
                else:
                    # The call operator: the next word is invoked as a command.
                    self._end_command(call_operator=True)
                    i += 1
            elif c in ";|\n\r(){}":
                self._end_command()
                i += 1
            else:
                self._char(c)
                i += 1

    def _powershell_single(self, i: int) -> int:
        s = self.s
        word = self._start_word()
        while i < self.n:
            if s[i] == "'":
                if i + 1 < self.n and s[i + 1] == "'":
                    word.text += "'"
                    i += 2
                    continue
                return i + 1
            word.text += s[i]
            i += 1
        raise UnparseableCommand("a single quote is not closed")

    def _powershell_double(self, i: int) -> int:
        s = self.s
        word = self._start_word()
        while i < self.n:
            c = s[i]
            nxt = s[i + 1] if i + 1 < self.n else ""
            if c == '"':
                if nxt == '"':
                    word.text += '"'
                    i += 2
                    continue
                return i + 1
            if c == "`" and nxt:
                word.text += nxt
                i += 2
            elif c == "$" and nxt == "(":
                inner, end = _balanced(s, i + 1, self.dialect)
                self._substitute(inner)
                i = end + 1
            else:
                if c == "$":
                    word.dynamic = True
                word.text += c
                i += 1
        raise UnparseableCommand("a double quote is not closed")


def _split(text: str, dialect: Dialect, depth: int) -> list[_Command]:
    if depth > _MAX_DEPTH:
        raise UnparseableCommand("commands are nested too deeply")
    return _Splitter(text, dialect, depth).split()


def _find_backtick(s: str, start: int) -> int:
    i = start
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == "`":
            return i
        i += 1
    raise UnparseableCommand("a backtick substitution is not closed")


def _balanced(s: str, open_index: int, dialect: Dialect) -> tuple[str, int]:
    """Return the text inside the parenthesis at ``open_index`` and the index of its close."""
    escape = "`" if dialect is Dialect.POWERSHELL else "\\"
    depth = 0
    quote = ""
    i = open_index
    while i < len(s):
        c = s[i]
        if c == escape and quote != "'":
            i += 2
            continue
        if quote:
            if c == quote:
                quote = ""
        elif c in ("'", '"'):
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return s[open_index + 1 : i], i
        i += 1
    raise UnparseableCommand("a command substitution is not closed")
