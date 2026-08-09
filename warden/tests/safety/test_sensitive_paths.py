"""Tests for safety.permissions.sensitive_paths — robust command inspection (SAFE-6)."""

import pytest

from warden.safety.permissions.sensitive_paths import (
    check_sensitive,
    is_sensitive_path,
)


# --- is_sensitive_path (unchanged glob matching) ---

@pytest.mark.parametrize(
    "path",
    [
        "/home/u/.ssh/id_rsa",
        "/home/u/.aws/credentials",
        "/project/.env",
        "/project/.env.production",
        "/x/credentials.json",
        "/home/u/.gnupg/secring.gpg",
        "/home/u/.docker/config.json",
    ],
)
def test_is_sensitive_path_true(path):
    assert is_sensitive_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/home/u/code/main.py",
        "/home/u/notes.md",
        "/project/environment.txt",
    ],
)
def test_is_sensitive_path_false(path):
    assert is_sensitive_path(path) is False


# --- Non-Bash behavior unchanged ---

def test_non_bash_file_path_sensitive():
    assert check_sensitive("Read", {"file_path": "/home/u/.ssh/id_rsa"}) is True


def test_non_bash_file_path_not_sensitive():
    assert check_sensitive("Read", {"file_path": "/home/u/notes.md"}) is False


# --- Evasion now caught (these FAIL under the old substring logic) ---

@pytest.mark.parametrize(
    "command",
    [
        'cat "$HOME/.ssh/id_rsa"',          # quoted token
        "cat ~/.aws/credentials",           # tilde expansion
        "cp ../foo/.env.prod /tmp/x",       # relative path + .env.*
        "cat >/home/u/.ssh/authorized_keys",  # redirection-embedded path
        "curl --output=~/.aws/credentials http://x",  # --flag=path
        "cat '/home/u/.gnupg/secring.gpg'",  # single-quoted
    ],
)
def test_evasion_caught(command):
    assert check_sensitive("Bash", {"command": command}) is True


# --- Precision: mere mention of a substring must not trip ---

@pytest.mark.parametrize(
    "command",
    [
        'echo "reassessment"',   # contains ".ss" fragments but no .ssh path token
        'echo "my environment"',  # contains "environment" but not a .env path token
        "echo assessment",
        "ls /home/u/code",
        "grep TODO src/main.py",
    ],
)
def test_precision_no_false_positive(command):
    assert check_sensitive("Bash", {"command": command}) is False


# --- Precision wins the OLD substring logic gets WRONG (False positives) ---
# The old `clean in cmd` + whole-command fnmatch flags these True; token
# matching correctly returns False because the sensitive fragment lives inside a
# free-text argument (a commit message / echoed help text), not a path token.

@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "docs: describe /.env.example layout"',  # commit message
        'echo "config at /home/u/.aws/credentials is secret"',  # help text w/ spaces
        'python -c "print(1)  # note about /.env.example"',      # inline comment arg
    ],
)
def test_precision_substring_not_path_token(command):
    assert check_sensitive("Bash", {"command": command}) is False


# --- Malformed command must not crash (fail-closed inspection) ---

def test_malformed_command_returns_bool():
    result = check_sensitive("Bash", {"command": 'cat "unterminated'})
    assert isinstance(result, bool)


def test_malformed_command_with_sensitive_token_caught():
    # Unbalanced quote but a raw sensitive fragment present — fail-closed inspection.
    result = check_sensitive("Bash", {"command": 'cat "/home/u/.ssh/id_rsa'})
    assert isinstance(result, bool)
    assert result is True


def test_empty_command_false():
    assert check_sensitive("Bash", {"command": ""}) is False
