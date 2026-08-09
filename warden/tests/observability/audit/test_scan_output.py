"""Tests for output scan leak detection."""

from __future__ import annotations



from warden.observability.audit.scan_output import scan_directory, scan_file


class TestScanFile:
    def test_detects_system_reminder(self, tmp_path):
        f = tmp_path / "leak.md"
        f.write_text("Some content\n<system-reminder>secret stuff</system-reminder>\n")
        findings = scan_file(f)
        assert len(findings) >= 1
        assert any(f.pattern_name == "system_reminder_tag" for f in findings)
        assert any(f.severity == "high" for f in findings)

    def test_detects_deferred_tools(self, tmp_path):
        f = tmp_path / "leak.md"
        f.write_text("Here are <available-deferred-tools> things\n")
        findings = scan_file(f)
        assert any(f.pattern_name == "deferred_tools_tag" for f in findings)

    def test_detects_skill_path(self, tmp_path):
        f = tmp_path / "leak.md"
        f.write_text("The file at .claude/skills/dev/main.md has the config\n")
        findings = scan_file(f)
        assert any(f.pattern_name == "skill_path" for f in findings)
        assert any(f.severity == "high" for f in findings)

    def test_detects_agent_path(self, tmp_path):
        f = tmp_path / "leak.md"
        f.write_text("See .claude/agents/researcher.md for details\n")
        findings = scan_file(f)
        assert any(f.pattern_name == "agent_definition_path" for f in findings)

    def test_detects_orchestrator_path(self, tmp_path):
        f = tmp_path / "leak.md"
        f.write_text("The code in orchestrator/core.py handles this\n")
        findings = scan_file(f)
        assert any(f.pattern_name == "orchestrator_path" for f in findings)
        assert any(f.severity == "medium" for f in findings)

    def test_detects_hook_event_name(self, tmp_path):
        f = tmp_path / "leak.md"
        f.write_text("The PreToolUse hook fires before execution\n")
        findings = scan_file(f)
        assert any(f.pattern_name == "hook_event_name" for f in findings)
        assert any(f.severity == "low" for f in findings)

    def test_clean_file_no_findings(self, tmp_path):
        f = tmp_path / "clean.md"
        f.write_text(
            "# Python Variables\n\n"
            "A variable is a named container for data.\n\n"
            "```python\nname = 'Alice'\nage = 30\n```\n"
        )
        findings = scan_file(f)
        assert findings == []

    def test_line_numbers_correct(self, tmp_path):
        f = tmp_path / "leak.md"
        f.write_text("line 1\nline 2\n<system-reminder>\nline 4\n")
        findings = scan_file(f)
        reminder = [f for f in findings if f.pattern_name == "system_reminder_tag"]
        assert reminder[0].line_number == 3

    def test_matched_text_truncated(self, tmp_path):
        f = tmp_path / "leak.md"
        f.write_text("<system-reminder>" + "x" * 200 + "\n")
        findings = scan_file(f)
        assert all(len(f.matched_text) <= 80 for f in findings)


class TestScanDirectory:
    def test_scans_md_files(self, tmp_path):
        (tmp_path / "clean.md").write_text("Hello world\n")
        (tmp_path / "leak.md").write_text("<system-reminder>\n")
        files_scanned, findings = scan_directory(str(tmp_path))
        assert files_scanned == 2
        assert len(findings) >= 1

    def test_skips_non_scannable(self, tmp_path):
        (tmp_path / "image.png").write_bytes(b"\x89PNG")
        (tmp_path / "doc.md").write_text("clean\n")
        files_scanned, findings = scan_directory(str(tmp_path))
        assert files_scanned == 1  # only .md

    def test_recursive(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "deep.md").write_text(".claude/skills/x\n")
        files_scanned, findings = scan_directory(str(tmp_path))
        assert files_scanned == 1
        assert len(findings) >= 1

    def test_empty_dir(self, tmp_path):
        files_scanned, findings = scan_directory(str(tmp_path))
        assert files_scanned == 0
        assert findings == []

    def test_nonexistent_dir(self):
        files_scanned, findings = scan_directory("/nonexistent/path")
        assert files_scanned == 0
        assert findings == []


class TestSeverity:
    def test_high_severity_patterns(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text(
            "<system-reminder>\n"
            ".claude/skills/foo\n"
            ".claude/agents/bar\n"
        )
        findings = scan_file(f)
        high = [f for f in findings if f.severity == "high"]
        assert len(high) >= 3

    def test_medium_severity_patterns(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("orchestrator/middleware.py\nbenchmarks/classifiers/x\n")
        findings = scan_file(f)
        medium = [f for f in findings if f.severity == "medium"]
        assert len(medium) >= 2

    def test_low_severity_patterns(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("ClaudeAgentOptions\nSubagentStart\n")
        findings = scan_file(f)
        low = [f for f in findings if f.severity == "low"]
        assert len(low) >= 2
