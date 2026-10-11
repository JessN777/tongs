"""Tests for repository discovery via filesystem scanning."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tongs.scanner.discovery import _pick_primary_remote, discover_repos
from tongs.scanner.repo import ForgeType, Remote, Repo


def _init_repo(repo_dir: Path, remotes: dict[str, str]) -> None:
    """Initialize a real git repo with the given remotes."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    for name, url in remotes.items():
        subprocess.run(["git", "remote", "add", name, url], cwd=repo_dir, check=True)


def add_worktree(main_dir: Path, worktree_dir: Path) -> None:
    """Commit once in main_dir and add a real linked worktree at worktree_dir."""
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=tongs",
            "-c",
            "user.email=tongs@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "--allow-empty",
            "--no-verify",
            "-m",
            "initial",
        ],
        cwd=main_dir,
        check=True,
    )
    subprocess.run(
        ["git", "worktree", "add", "-q", "--detach", str(worktree_dir)],
        cwd=main_dir,
        check=True,
    )


@pytest.fixture
def mock_repo_tree(tmp_path):
    """Create a mock directory tree with real git repos."""
    _init_repo(
        tmp_path / "org/repo-a",
        {"origin": "https://github.com/org/repo-a.git"},
    )
    _init_repo(
        tmp_path / "group/subgroup/repo-b",
        {"origin": "git@gitlab.com:group/subgroup/repo-b.git"},
    )
    _init_repo(
        tmp_path / "solo-repo",
        {"origin": "https://github.com/user/solo-repo.git"},
    )
    (tmp_path / "not-a-repo").mkdir()
    (tmp_path / "random-file.md").write_text("hello")

    return tmp_path


class TestDiscoverRepos:
    def test_finds_repos_in_tree(self, mock_repo_tree):
        repos = discover_repos(mock_repo_tree)
        assert len(repos) == 3

    def test_repos_sorted_by_display_name(self, tmp_path):
        _init_repo(tmp_path / "a", {"origin": "https://github.com/zzz/a.git"})
        _init_repo(tmp_path / "b", {"origin": "https://github.com/AAA/b.git"})
        repos = discover_repos(tmp_path)
        assert [r.display_name for r in repos] == ["AAA/b", "zzz/a"]

    def test_nonexistent_root_returns_empty(self, tmp_path):
        repos = discover_repos(tmp_path / "nonexistent")
        assert repos == []


class TestPickPrimaryRemote:
    def _remote(self, name: str) -> Remote:
        return Remote(
            name=name,
            url=f"https://github.com/x/{name}.git",
            hostname="github.com",
            repo_path=f"x/{name}",
            forge_type=ForgeType.GITHUB,
        )

    def test_prefers_origin(self):
        remotes = [
            self._remote("upstream"),
            self._remote("origin"),
            self._remote("fork"),
        ]
        assert _pick_primary_remote(remotes).name == "origin"

    def test_falls_back_to_upstream(self):
        remotes = [self._remote("fork"), self._remote("upstream")]
        assert _pick_primary_remote(remotes).name == "upstream"

    def test_falls_back_to_first(self):
        remotes = [self._remote("fork"), self._remote("other")]
        assert _pick_primary_remote(remotes).name == "fork"

    def test_empty_list_returns_none(self):
        assert _pick_primary_remote([]) is None


class TestRepoProperties:
    def test_display_name_from_primary(self):
        remote = Remote(
            name="origin",
            url="https://gitlab.com/redhat/rhel-ai/builder.git",
            hostname="gitlab.com",
            repo_path="redhat/rhel-ai/builder",
            forge_type=ForgeType.GITLAB,
        )
        repo = Repo(path=Path("/tmp/builder"), remotes=(remote,), primary_remote=remote)
        assert repo.display_name == "redhat/rhel-ai/builder"
        assert repo.namespace == "redhat/rhel-ai"
        assert repo.forge_type == ForgeType.GITLAB
        assert repo.hostname == "gitlab.com"

    def test_display_name_fallback_to_dirname(self):
        repo = Repo(path=Path("/tmp/my-repo"), remotes=())
        assert repo.display_name == "my-repo"
        assert repo.namespace == ""
        assert repo.forge_type is None

    def test_namespace_single_segment(self):
        remote = Remote(
            name="origin",
            url="https://github.com/repo.git",
            hostname="github.com",
            repo_path="repo",
            forge_type=ForgeType.GITHUB,
        )
        repo = Repo(path=Path("/tmp/repo"), remotes=(remote,), primary_remote=remote)
        assert repo.namespace == ""


class TestDiscoverReposEdgeCases:
    def test_repo_with_only_local_remote_excluded(self, tmp_path):
        _init_repo(tmp_path / "local-only", {"origin": "/home/user/other-repo"})
        repos = discover_repos(tmp_path)
        assert len(repos) == 0

    def test_repo_with_mixed_remotes(self, tmp_path):
        repo_dir = tmp_path / "mixed"
        _init_repo(
            repo_dir,
            {
                "local": "/home/user/local",
                "origin": "https://github.com/org/mixed.git",
            },
        )
        repos = discover_repos(tmp_path)
        assert len(repos) == 1
        assert repos[0].primary_remote.name == "origin"
        assert len(repos[0].remotes) == 1

    def test_multi_remote_picks_origin(self, tmp_path):
        _init_repo(
            tmp_path / "multi",
            {
                "upstream": "https://github.com/upstream/repo.git",
                "origin": "https://github.com/user/repo.git",
                "fork": "https://github.com/fork/repo.git",
            },
        )
        repos = discover_repos(tmp_path)
        assert len(repos) == 1
        assert repos[0].primary_remote.name == "origin"
        assert len(repos[0].remotes) == 3

    def test_permission_denied_dir_skipped(self, tmp_path):
        _init_repo(
            tmp_path / "good-repo", {"origin": "https://github.com/org/good.git"}
        )
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        os.chmod(blocked, 0o000)
        try:
            repos = discover_repos(tmp_path)
            assert len(repos) == 1
            assert repos[0].display_name == "org/good"
        finally:
            os.chmod(blocked, 0o755)

    def test_broken_symlink_skipped(self, tmp_path):
        _init_repo(tmp_path / "good", {"origin": "https://github.com/org/good.git"})
        (tmp_path / "broken-link").symlink_to(tmp_path / "nonexistent")
        repos = discover_repos(tmp_path)
        assert len(repos) == 1

    def test_repo_at_exact_max_depth_found(self, tmp_path):
        _init_repo(
            tmp_path / "a" / "b" / "deep-repo",
            {"origin": "https://github.com/org/deep-repo.git"},
        )
        repos = discover_repos(tmp_path, max_depth=3)
        assert len(repos) == 1
        assert repos[0].path.name == "deep-repo"

    def test_repo_beyond_max_depth_missed(self, tmp_path):
        _init_repo(
            tmp_path / "a" / "b" / "c" / "too-deep",
            {"origin": "https://github.com/org/too-deep.git"},
        )
        repos = discover_repos(tmp_path, max_depth=3)
        assert len(repos) == 0

    def test_hidden_directory_is_not_scanned(self, tmp_path):
        _init_repo(
            tmp_path / ".cache" / "hidden",
            {"origin": "https://github.com/org/hidden.git"},
        )
        assert discover_repos(tmp_path) == []

    def test_nested_repository_is_not_reported_separately(self, tmp_path):
        outer = tmp_path / "outer"
        _init_repo(outer, {"origin": "https://github.com/org/outer.git"})
        _init_repo(
            outer / "vendor" / "inner",
            {"origin": "https://github.com/org/inner.git"},
        )
        repos = discover_repos(tmp_path)
        assert [repo.display_name for repo in repos] == ["org/outer"]


class TestDiscoverSymlinks:
    def test_symlink_to_repo_under_root_is_found_once(self, tmp_path):
        real_repo = tmp_path / "real"
        _init_repo(real_repo, {"origin": "https://github.com/org/real.git"})
        (tmp_path / "link-to-repo").symlink_to(real_repo)
        (tmp_path / "zz-second-link").symlink_to(real_repo)
        repos = discover_repos(tmp_path)
        assert [repo.path for repo in repos] == [real_repo.resolve()]

    def test_symlink_to_repo_outside_root_is_found_at_its_real_path(self, tmp_path):
        outside = tmp_path / "elsewhere" / "project"
        _init_repo(outside, {"origin": "https://github.com/org/project.git"})
        root = tmp_path / "root"
        root.mkdir()
        (root / "project").symlink_to(outside)
        repos = discover_repos(root)
        assert [repo.display_name for repo in repos] == ["org/project"]
        assert repos[0].path == outside.resolve()
        assert repos[0].worktree is False

    def test_symlinked_folder_of_repos_outside_root_is_walked(self, tmp_path):
        outside = tmp_path / "elsewhere"
        _init_repo(outside / "one", {"origin": "https://github.com/org/one.git"})
        _init_repo(outside / "two", {"origin": "https://github.com/org/two.git"})
        root = tmp_path / "root"
        root.mkdir()
        (root / "linked").symlink_to(outside)
        repos = discover_repos(root)
        assert [repo.display_name for repo in repos] == ["org/one", "org/two"]

    def test_link_back_to_parent_ends_without_duplicates(self, tmp_path):
        root = tmp_path / "root"
        _init_repo(root / "repo", {"origin": "https://github.com/org/repo.git"})
        (root / "a").symlink_to("..")
        (root / "repo-parent").symlink_to(root)
        repos = discover_repos(root, max_depth=50)
        assert [repo.path for repo in repos] == [(root / "repo").resolve()]

    def test_link_up_to_a_repository_holding_the_root_keeps_its_repos(self, tmp_path):
        # A home folder kept in git, scanned from a folder inside it.
        home = tmp_path / "home"
        _init_repo(home, {"origin": "https://github.com/user/dotfiles.git"})
        root = home / "git"
        _init_repo(root / "proj", {"origin": "https://github.com/org/proj.git"})
        (root / "homelink").symlink_to(home)
        (root / "a").symlink_to("..")
        repos = discover_repos(root, max_depth=50)
        assert [repo.path for repo in repos] == [(root / "proj").resolve()]

    def test_two_links_pointing_at_each_other_end(self, tmp_path):
        _init_repo(tmp_path / "repo", {"origin": "https://github.com/org/repo.git"})
        (tmp_path / "a").symlink_to(tmp_path / "b")
        (tmp_path / "b").symlink_to(tmp_path / "a")
        repos = discover_repos(tmp_path, max_depth=50)
        assert [repo.display_name for repo in repos] == ["org/repo"]

    def test_folder_cycle_through_links_finds_each_repo_once(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        _init_repo(left / "l-repo", {"origin": "https://github.com/org/l.git"})
        _init_repo(right / "r-repo", {"origin": "https://github.com/org/r.git"})
        (left / "to-right").symlink_to(right)
        (right / "to-left").symlink_to(left)
        repos = discover_repos(tmp_path, max_depth=50)
        assert [repo.display_name for repo in repos] == ["org/l", "org/r"]

    def test_link_counts_as_one_depth_level(self, tmp_path):
        outside = tmp_path / "elsewhere" / "deep"
        _init_repo(outside, {"origin": "https://github.com/org/deep.git"})
        root = tmp_path / "root"
        (root / "a").mkdir(parents=True)
        (root / "a" / "link").symlink_to(outside)
        assert [r.display_name for r in discover_repos(root, max_depth=2)] == [
            "org/deep"
        ]
        assert discover_repos(root, max_depth=1) == []

    def test_shorter_path_through_a_link_is_used(self, tmp_path):
        root = tmp_path / "root"
        _init_repo(
            root / "a" / "b" / "c" / "repo",
            {"origin": "https://github.com/org/repo.git"},
        )
        # "a" sorts first, but the link reaches "b" one level sooner, and
        # only that shorter path keeps the repository within the depth.
        (root / "z-short").symlink_to(root / "a" / "b")
        repos = discover_repos(root, max_depth=3)
        assert [repo.display_name for repo in repos] == ["org/repo"]

    def test_hidden_symlink_is_not_followed(self, tmp_path):
        outside = tmp_path / "elsewhere" / "project"
        _init_repo(outside, {"origin": "https://github.com/org/project.git"})
        root = tmp_path / "root"
        root.mkdir()
        (root / ".project").symlink_to(outside)
        assert discover_repos(root) == []

    def test_link_into_a_repository_does_not_report_the_nested_one(self, tmp_path):
        outer = tmp_path / "outer"
        _init_repo(outer, {"origin": "https://github.com/org/outer.git"})
        _init_repo(
            outer / "vendor" / "inner",
            {"origin": "https://github.com/org/inner.git"},
        )
        # Sorts and walks before "outer", so the nested check must not depend
        # on which repository the walk reaches first.
        (tmp_path / "a-inner").symlink_to(outer / "vendor" / "inner")
        repos = discover_repos(tmp_path)
        assert [repo.display_name for repo in repos] == ["org/outer"]

    def test_link_inside_a_repository_working_tree_is_not_followed(self, tmp_path):
        # The linked repository lies outside both root and the outer checkout,
        # so only the rule that a checkout's working tree is never walked keeps
        # it out; the nested-repository check cannot catch it.
        elsewhere = tmp_path / "elsewhere" / "other"
        _init_repo(elsewhere, {"origin": "https://github.com/org/other.git"})
        root = tmp_path / "root"
        outer = root / "outer"
        _init_repo(outer, {"origin": "https://github.com/org/outer.git"})
        (outer / "linked").symlink_to(elsewhere)
        repos = discover_repos(root)
        assert [repo.display_name for repo in repos] == ["org/outer"]

    def test_unreadable_link_target_is_skipped(self, tmp_path):
        _init_repo(tmp_path / "good", {"origin": "https://github.com/org/good.git"})
        blocked = tmp_path / "elsewhere" / "blocked"
        blocked.mkdir(parents=True)
        (tmp_path / "link").symlink_to(blocked)
        os.chmod(blocked, 0o000)
        try:
            repos = discover_repos(tmp_path)
        finally:
            os.chmod(blocked, 0o755)
        assert [repo.display_name for repo in repos] == ["org/good"]


class TestDiscoverWorktrees:
    def test_worktree_is_found_with_its_remotes(self, tmp_path):
        main = tmp_path / "main"
        _init_repo(
            main,
            {
                "origin": "https://github.com/org/project.git",
                "upstream": "git@gitlab.com:group/project.git",
            },
        )
        root = tmp_path / "root"
        root.mkdir()
        add_worktree(main, root / "feature")
        assert (root / "feature" / ".git").is_file()

        repos = discover_repos(root)

        assert len(repos) == 1
        repo = repos[0]
        assert repo.path == (root / "feature").resolve()
        assert repo.worktree is True
        assert repo.primary_remote.name == "origin"
        assert repo.display_name == "org/project"
        assert sorted((r.name, r.hostname) for r in repo.remotes) == [
            ("origin", "github.com"),
            ("upstream", "gitlab.com"),
        ]

    def test_main_checkout_and_worktree_are_both_found(self, tmp_path):
        main = tmp_path / "main"
        _init_repo(main, {"origin": "https://github.com/org/project.git"})
        add_worktree(main, tmp_path / "feature")

        repos = discover_repos(tmp_path)

        assert sorted((repo.path.name, repo.worktree) for repo in repos) == [
            ("feature", True),
            ("main", False),
        ]
        assert {repo.display_name for repo in repos} == {"org/project"}

    def test_worktree_in_a_hidden_folder_is_not_scanned(self, tmp_path):
        main = tmp_path / "main"
        _init_repo(main, {"origin": "https://github.com/org/project.git"})
        add_worktree(main, main / ".worktrees" / "feature")
        add_worktree(main, tmp_path / ".worktrees" / "other")

        repos = discover_repos(tmp_path)

        assert [(repo.path.name, repo.worktree) for repo in repos] == [("main", False)]

    def test_git_file_without_gitdir_line_is_not_a_repository(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / ".git").write_text("not a pointer\n")
        _init_repo(plain / "child", {"origin": "https://github.com/org/child.git"})
        repos = discover_repos(tmp_path)
        assert [repo.display_name for repo in repos] == ["org/child"]

    def test_empty_gitdir_pointer_is_not_a_repository(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / ".git").write_text("gitdir:   \n")
        assert discover_repos(tmp_path) == []


def test_repo_forge_type_follows_primary_remote() -> None:
    github = Remote(
        name="upstream",
        url="https://github.com/org/repo.git",
        hostname="github.com",
        repo_path="org/repo",
        forge_type=ForgeType.GITHUB,
    )
    gitlab = Remote(
        name="origin",
        url="https://gitlab.com/org/repo.git",
        hostname="gitlab.com",
        repo_path="org/repo",
        forge_type=ForgeType.GITLAB,
    )
    repo = Repo(
        path=Path("/tmp/repo"),
        remotes=(github, gitlab),
        primary_remote=gitlab,
    )
    assert repo.forge_type is ForgeType.GITLAB
