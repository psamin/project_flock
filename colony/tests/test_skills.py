"""The CockroachDB Agent Skills loader (§6.2 required tool #3).

The claim being defended is "the agent equips itself from the skills repo at
runtime", and the ways that claim can quietly become false are: the fetch never
ran and nothing says so; a skill with no description is offered anyway and the
agent picks blind; or a 25 KB body is pasted whole into a prompt that then costs
more than the answer is worth.

`catalog()` is cached, so every test that changes SKILLS_DIR clears it.
"""

from __future__ import annotations

import pytest

from console import skills as skills_mod


@pytest.fixture(autouse=True)
def clear_cache():
    skills_mod.catalog.cache_clear()
    yield
    skills_mod.catalog.cache_clear()


@pytest.fixture
def fake_skills(tmp_path, monkeypatch):
    def write(domain: str, name: str, description: str, body: str = "do the thing") -> None:
        directory = tmp_path / domain / name
        directory.mkdir(parents=True)
        front = f"---\nname: {name}\ndescription: {description}\n---\n" if description else ""
        (directory / "SKILL.md").write_text(front + body)

    monkeypatch.setattr(skills_mod, "SKILLS_DIR", tmp_path)
    return write


def test_a_missing_directory_is_an_empty_catalogue_not_a_crash(tmp_path, monkeypatch):
    """A checkout that never ran scripts/fetch_skills.sh must still serve the
    console. Skills are an enhancement; their absence is not an outage."""
    monkeypatch.setattr(skills_mod, "SKILLS_DIR", tmp_path / "nothing-here")
    assert skills_mod.catalog() == ()
    assert skills_mod.available() is False


def test_an_absent_catalogue_says_so_in_the_prompt(tmp_path, monkeypatch):
    """Rather than leaving the model to infer it from silence and then claim a
    skill informed an answer that no skill was present for."""
    monkeypatch.setattr(skills_mod, "SKILLS_DIR", tmp_path / "nothing-here")
    prompt = skills_mod.catalog_prompt()
    assert "No CockroachDB Agent Skills are available" in prompt


def test_skills_are_routed_on_their_upstream_descriptions(fake_skills):
    """Verbatim, not paraphrased. These descriptions are written to be matched
    against; rewriting one to fit a line length changes which skill is picked."""
    fake_skills("sec", "hardening-user-privileges", "Use when auditing SQL privileges.")
    prompt = skills_mod.catalog_prompt()
    assert "hardening-user-privileges" in prompt
    assert "Use when auditing SQL privileges." in prompt


def test_a_skill_without_a_description_is_not_offered(fake_skills):
    """There is nothing to route on, so offering it asks the agent to choose
    blind — and a blind choice that loads 6 KB is worse than no choice."""
    fake_skills("ops", "nameless", "")
    assert skills_mod.catalog() == ()


def test_a_long_body_is_trimmed_and_says_where_the_rest_is(fake_skills):
    """The largest skill upstream is ~25 KB. A commander answering "which robots
    are stuck" does not need all of it, and an untrimmed body silently makes
    every question that touches a skill several times more expensive."""
    fake_skills("ops", "huge", "Use when things are large.", body="x" * 40_000)
    body = skills_mod.load("huge")
    assert len(body) < skills_mod.MAX_BODY_CHARS + 400
    assert "trimmed" in body
    assert skills_mod.UPSTREAM in body


def test_a_short_body_is_returned_whole(fake_skills):
    fake_skills("ops", "small", "Use when things are small.", body="just this")
    assert skills_mod.load("small") == "just this"


def test_the_body_has_no_frontmatter(fake_skills):
    """It is routing metadata, already in the system prompt. Sending it again
    inside the body spends tokens restating what the model just read."""
    fake_skills("ops", "one", "Use when.", body="the instructions")
    body = skills_mod.load("one")
    assert "description:" not in body
    assert body.startswith("the instructions")


def test_an_unknown_skill_returns_a_message_the_agent_can_act_on(fake_skills):
    """Not an exception. A hallucinated skill name should cost one turn and a
    readable correction, not end the loop."""
    fake_skills("ops", "real", "Use when real.")
    answer = skills_mod.load("imaginary")
    assert "no skill named" in answer
    assert "real" in answer, "the reply should list what is available"


def test_folded_yaml_descriptions_are_joined(fake_skills, tmp_path):
    """Upstream wraps long descriptions across lines. A reader that takes only
    the first line truncates the part that says when to use the skill."""
    directory = tmp_path / "ops" / "folded"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: folded\ndescription: Use when the description\n"
        "  continues onto a second line.\n---\nbody\n"
    )
    skill = skills_mod.by_name("folded")
    assert skill is not None
    assert "continues onto a second line." in skill.description


# --- the real repo, when it has been fetched --------------------------------


@pytest.mark.skipif(
    not (skills_mod.SKILLS_DIR / ".pin").exists(),
    reason="skills not fetched — run scripts/fetch_skills.sh",
)
def test_the_fetched_repo_parses():
    """Guards the pin bump. If upstream changes its frontmatter shape, the
    catalogue silently empties and the agent quietly stops using the tool this
    submission claims — which is exactly the failure nobody would notice."""
    skills_mod.catalog.cache_clear()
    catalog = skills_mod.catalog()
    assert len(catalog) >= 30, f"only {len(catalog)} skills parsed"
    assert all(s.description for s in catalog)
    assert skills_mod.by_name("cockroachdb-sql") is not None
