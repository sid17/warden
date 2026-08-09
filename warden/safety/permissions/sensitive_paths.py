"""Sensitive-path detection for tool invocations.

For file tools (Read/Edit/Write/...) the path is read directly from
``file_path``/``path``. For ``Bash`` we cannot trust a naive substring scan of
the command string — it is both evadable (quoting, splitting) and imprecise
(matches sensitive fragments buried inside unrelated text). Instead we
*tokenize* the command with :func:`shlex.split` and match each path-like token
against the sensitive globs (SAFE-6).
"""

import fnmatch
import shlex

SENSITIVE_PATHS = (
    "*/.ssh/*",
    "*/.aws/credentials",
    "*/.env",
    "*/.env.*",
    "*/credentials.json",
    "*/.gnupg/*",
    "*/.docker/config.json",
)

# Home marker substituted for a leading ``~`` so a token like ``~/.ssh/id_rsa``
# has a leading path segment for the ``*/`` prefix of the globs to bind to.
_HOME_MARKER = "/home/_user_"

# Leading prefixes that embed a path in a shell token (redirections / flags).
_REDIRECT_CHARS = "<>&|"


def _strip_token_prefix(token: str) -> str:
    """Strip redirection / ``--flag=`` prefixes so an embedded path is exposed.

    ``>/home/u/.ssh/authorized_keys`` -> ``/home/u/.ssh/authorized_keys``
    ``--output=~/.aws/credentials``   -> ``~/.aws/credentials``
    """
    # Redirection operators: 2>, >>, <, >, &> etc. Drop leading redirect chars
    # and any file-descriptor digits that precede them.
    i = 0
    while i < len(token) and (token[i] in _REDIRECT_CHARS or token[i].isdigit()):
        i += 1
    if i and i < len(token):
        token = token[i:]

    # --flag=value / -o=value: keep the value side (where a path would live).
    if token.startswith("-") and "=" in token:
        token = token.split("=", 1)[1]

    return token


def _normalize_token(token: str) -> str:
    """Normalize a token enough to compare against the sensitive globs."""
    token = _strip_token_prefix(token)
    if token.startswith("~"):
        token = _HOME_MARKER + token[1:]
    elif token.startswith("$HOME"):
        token = _HOME_MARKER + token[len("$HOME"):]
    return token


def _tokenize(cmd: str) -> list[str]:
    """Tokenize a shell command, failing closed on malformed input.

    A command ``shlex`` cannot parse (e.g. an unbalanced quote) falls back to a
    naive whitespace split (with quote characters stripped) so path-like
    fragments are still inspected rather than silently skipped (fail-closed).
    """
    try:
        return shlex.split(cmd)
    except ValueError:
        return [tok.strip("\"'") for tok in cmd.split()]


def _looks_like_path(token: str) -> bool:
    """A single filesystem path has no internal whitespace and is non-empty.

    A shlex token that still contains spaces (e.g. a commit message or a
    free-text argument) is not a path and must not be glob-matched — matching it
    would let ``*`` in the sensitive globs span unrelated words.
    """
    return bool(token) and not any(c.isspace() for c in token)


def _bash_sensitive_token(cmd: str) -> str | None:
    """Return the first sensitive path-like token in a Bash command, else None."""
    for raw in _tokenize(cmd):
        token = _normalize_token(raw)
        if _looks_like_path(token) and is_sensitive_path(token):
            return raw
    return None


def _extract_path(tool_name: str, tool_input: dict) -> str | None:
    """Extract a sensitive file path from tool input.

    For file tools returns the ``file_path``/``path`` value directly. For
    ``Bash`` returns the offending token when the command references a sensitive
    path, else ``None``.
    """
    for key in ("file_path", "path"):
        val = tool_input.get(key)
        if val:
            return str(val)
    if tool_name == "Bash":
        return _bash_sensitive_token(str(tool_input.get("command", "")))
    return None


def is_sensitive_path(path: str) -> bool:
    """Check if a path matches any sensitive path pattern."""
    for pattern in SENSITIVE_PATHS:
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


def check_sensitive(tool_name: str, tool_input: dict) -> bool:
    """Check if a tool invocation targets a sensitive path."""
    path = _extract_path(tool_name, tool_input)
    if path is None:
        return False
    # Bash tokens are pre-filtered by _bash_sensitive_token; file-tool paths
    # still need the glob check applied here.
    if tool_name == "Bash":
        return True
    return is_sensitive_path(path)
