"""Names have one pipeline and one avatar (LEAD-UIUX code audit C19). A person is named through person_label.ts (their
label: `personName`, `nameText`, `displayName`; the full name: `fullNameOf`), never as a record with `labelOf`, and
their initials are drawn by the one `avatar()` in dom.ts.

The compiler refuses `labelOf` on a typed person row (types.ts PersonRow, `NotPerson`). This static guard covers the
paths types cannot see: a person read by a name the code spells out (`person`, `people.get(…)`, `find('people', …)`),
and any second initials disc or avatar class."""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "apps" / "web"
SOURCES = sorted((WEB / "src").glob("*.ts"))
CSS = (WEB / "public" / "assets" / "app.css").read_text(encoding="utf-8")

# labelOf( given an expression that reads a person: a variable named person/people, a people list, map or lookup.
PERSON_ARG = re.compile(
    r"\blabelOf\(\s*(?:person\b|persons?\(|people\s*[.\[]|peopleById\b|personById\b|source\.person\b|"
    r"(?:rows|cat|find|all)\(\s*'people'|lookup\([^)]*'people'|[\w.?]*catalogues(?:\?\.|\.)people\b|\(ctx\.catalogues\.people)")
INITIALS = re.compile(r"\binitials\(")
AVATAR_CLASS = re.compile(r"class:\s*[`'\"](?:[^`'\"]*\s)?(?:wk-avatar|tl-av|avatar)\b")


def offenders(pattern, allow=lambda path, line: False):
    found = []
    for path in SOURCES:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and not allow(path, line):
                found.append(f"{path.name}:{n}: {line.strip()[:140]}")
    return found


def test_no_person_is_named_as_a_record():
    found = offenders(PERSON_ARG)
    assert not found, "Name people through person_label.ts (personName / nameText / fullNameOf), not labelOf:\n" + "\n".join(found)


def test_one_avatar():
    in_avatar = lambda path, line: path.name == "dom.ts" and ("export const avatar=" in line or "export const initials=" in line)
    assert not offenders(INITIALS, in_avatar), "Draw initials only through dom.ts avatar():\n" + "\n".join(offenders(INITIALS, in_avatar))
    assert not offenders(AVATAR_CLASS, in_avatar), "Use dom.ts avatar(), not a hand-made disc:\n" + "\n".join(offenders(AVATAR_CLASS, in_avatar))
    for retired in (".wk-avatar", ".tl-av{", ".tl-av "):
        assert retired not in CSS, f"{retired} is retired: the one avatar is .avatar (with .duty, .muted, .me)"


def test_the_guard_sees_what_it_guards():
    """The patterns still match the shapes they are for (a guard that matches nothing guards nothing)."""
    for bad in ("labelOf(person)", "labelOf(people.get(id))", "labelOf(people[id])", "labelOf(find('people',pid))",
                "labelOf(ctx.catalogues.people.find(p=>p.id===id))", "labelOf(lookup(ctx,'people',id))", "labelOf(source.person(id))"):
        assert PERSON_ARG.search(bad), bad
    for fine in ("labelOf(location)", "labelOf(locations.get(s.location_id))", "labelOf(ctx.catalogues.services.find(x=>x))", "labelOf(policy)"):
        assert not PERSON_ARG.search(fine), fine
    assert AVATAR_CLASS.search("el('span',{class:'wk-avatar muted'},x)") and AVATAR_CLASS.search("el('span',{class:'avatar'},x)")
    assert not AVATAR_CLASS.search("el('span',{class:'avatars-list'},x)")
