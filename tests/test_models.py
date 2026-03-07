import pytest
from hypothesis import given
from hypothesis import strategies as st

from clean_room.models import parse_github_url


class TestParseGitHubUrl:
    def test_parses_standard_url(self):
        """Standard GitHub HTTPS URL parses to org and repo."""
        result = parse_github_url("https://github.com/anthropics/claude-code")
        assert result.org == "anthropics"
        assert result.repo_name == "claude-code"
        assert result.slug == "anthropics--claude-code"

    def test_parses_url_with_trailing_slash(self):
        result = parse_github_url("https://github.com/anthropics/claude-code/")
        assert result.org == "anthropics"
        assert result.repo_name == "claude-code"

    def test_parses_url_with_dot_git(self):
        result = parse_github_url("https://github.com/anthropics/claude-code.git")
        assert result.repo_name == "claude-code"

    def test_rejects_non_github_url(self):
        with pytest.raises(ValueError, match="GitHub"):
            parse_github_url("https://gitlab.com/foo/bar")

    def test_rejects_url_missing_repo(self):
        with pytest.raises(ValueError):
            parse_github_url("https://github.com/anthropics")

    @given(
        org=st.from_regex(r"[a-zA-Z][a-zA-Z0-9\-]{0,30}", fullmatch=True),
        repo=st.from_regex(r"[a-zA-Z][a-zA-Z0-9\-_.]{0,30}", fullmatch=True),
    )
    def test_roundtrip_any_valid_org_repo(self, org, repo):
        """Property: any valid org/repo parses and produces correct slug."""
        url = f"https://github.com/{org}/{repo}"
        result = parse_github_url(url)
        assert result.org == org
        assert result.repo_name == repo
        assert result.slug == f"{org}--{repo}"
