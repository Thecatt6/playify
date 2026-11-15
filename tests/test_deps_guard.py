"""The requirements guard: dependency drift must be visible in the log.

The davey 0.2 incident cost hours because everything looked healthy while
nobody could hear the bot. These tests keep the guard honest.
"""

from __future__ import annotations

from src.playify.services import deps_guard


def test_read_requirements_keeps_specifiers_and_drops_comments(tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text(
        "# a comment\n"
        'discord.py>=2.7,<2.8\n'
        "PyNaCl\n"
        "davey>=0.1.6,<0.2   # DAVE transitions\n"
        "spotifyscraper[all]==3.7.0\n"
        "-r extra.txt\n"
    )

    specs = deps_guard.read_requirements(path)

    assert specs["discord.py"] == ">=2.7,<2.8"
    assert specs["davey"] == ">=0.1.6,<0.2"
    assert specs["pynacl"] == ""
    assert specs["spotifyscraper"] == "==3.7.0"
    assert "-r" not in specs


def test_the_davey_0_2_combination_is_flagged():
    requirements = {"davey": ">=0.1.6,<0.2", "discord.py": ">=2.7,<2.8"}

    problems = deps_guard.pin_problems(
        requirements, version_of=lambda dist: {"davey": "0.2.0", "discord.py": "2.7.1"}[dist]
    )

    assert len(problems) == 1
    assert "davey 0.2.0" in problems[0]


def test_matching_versions_report_nothing():
    requirements = {"davey": ">=0.1.6,<0.2", "discord.py": ">=2.7,<2.8"}

    problems = deps_guard.pin_problems(
        requirements, version_of=lambda dist: {"davey": "0.1.7", "discord.py": "2.7.1"}[dist]
    )

    assert problems == []


def test_missing_package_is_reported():
    problems = deps_guard.pin_problems({"davey": ">=0.1.6,<0.2"}, version_of=lambda dist: None)

    assert problems and "not installed" in problems[0]


def test_unpinned_requirements_are_not_version_checked():
    problems = deps_guard.pin_problems({"PyNaCl": ""}, version_of=lambda dist: "9.9.9")

    assert problems == []


def test_unreadable_specifier_is_ignored():
    problems = deps_guard.pin_problems({"weird": "@ https://example.invalid/x.whl"},
                                       version_of=lambda dist: "1.0")

    assert problems == []


def test_version_tuple_handles_suffixes():
    assert deps_guard.version_tuple("0.2.0rc1") == (0, 2, 0)
    assert deps_guard.version_tuple(None) == ()
    assert deps_guard.satisfies("2.7.1", ">=2.7,<2.8") is True
    assert deps_guard.satisfies("2.8.0", ">=2.7,<2.8") is False
