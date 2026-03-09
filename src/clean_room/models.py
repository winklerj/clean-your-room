from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass
class GitHubUrl:
    org: str
    repo_name: str
    slug: str
    url: str

    @property
    def ssh_url(self) -> str:
        return f"git@github.com:{self.org}/{self.repo_name}.git"


def parse_github_url(url: str) -> GitHubUrl:
    """Parse a GitHub URL into org, repo_name, and slug."""
    parsed = urlparse(url.strip().rstrip("/"))
    if parsed.hostname != "github.com":
        raise ValueError(f"Not a GitHub URL: {url}")
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"URL must include org and repo: {url}")
    org = parts[0]
    repo_name = parts[1].removesuffix(".git")
    return GitHubUrl(
        org=org,
        repo_name=repo_name,
        slug=f"{org}--{repo_name}",
        url=f"https://github.com/{org}/{repo_name}",
    )
