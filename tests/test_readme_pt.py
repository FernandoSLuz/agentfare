"""The Brazilian Portuguese README (README.pt-BR.md) follows the English one.

What may differ is the prose: headings, sentences, table text, the alt text of images and the in-page anchors that follow the
translated headings. Everything that has to be true in both languages is compared with README.md:

* the headings, in the same number, order and level;
* every fenced code block (shell, YAML, command output), every `<!-- run: ... -->` marker and every generated table, byte for
  byte and in the same order, so a command, a figure or a price is never different in one language;
* every image, and every link to a file, to another document or to the web, in the same order (an in-page `#anchor` link follows
  the translated heading, so it is checked against the headings of its own file);
* the language switch on the first line of each file, and the sentences that name the version;
* the same style as the English file: no emoji, no long dashes, no spaces at the end of a line.

A paragraph left in English is caught too: a long line of prose that is identical in both files fails the test.

To copy the code blocks and generated tables of README.md into README.pt-BR.md after the English file changed (the translated
prose around them stays), run:

    python tests/test_readme_pt.py --write

Then translate the prose that changed. The links to the English documents (docs/*.md) stay as they are: those documents are in
English.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from agentfare import __version__

ROOT = Path(__file__).resolve().parent.parent
EN = ROOT / "README.md"
PT = ROOT / "README.pt-BR.md"

FENCE_OPEN = re.compile(r"^([ \t]*)```([\w-]*)[ \t]*$")
FENCE_CLOSE = re.compile(r"^[ \t]*```[ \t]*$")
RUN_MARKER = re.compile(r"^<!-- run: .*? -->$")
GENERATED = re.compile(r"<!-- generated: [a-z-]+ -->\n.*?<!-- /generated -->", re.DOTALL)
HEADING = re.compile(r"^(#{1,6})[ \t]+(?P<title>.+?)[ \t#]*$")
LINK = re.compile(r"!?\[[^\]]*\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
HTML_REF = re.compile(r"<(?:img|a)\b[^>]*?\b(?:src|href)=\"(?P<target>[^\"]+)\"")
MD_IMAGE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<target>[^)\s]+)\)")
HTML_IMAGE = re.compile(r"<img\b[^>]*?\bsrc=\"(?P<target>[^\"]+)\"[^>]*?\balt=\"(?P<alt>[^\"]*)\"")
EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿⭐⬆✅❌]")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---- the parts of a README ---------------------------------------------------------------------------------------------
def fences(text: str) -> list[tuple[int, int]]:
    """(first line, last line) of every fenced code block, the run marker above it included, indented fences too."""
    lines = text.split("\n")
    found: list[tuple[int, int]] = []
    start: int | None = None
    for i, line in enumerate(lines):
        if start is None and FENCE_OPEN.match(line):
            start = i
        elif start is not None and FENCE_CLOSE.match(line):
            first = start - 1 if start and RUN_MARKER.match(lines[start - 1]) else start
            found.append((first, i))
            start = None
    assert start is None, "a code fence is never closed"
    return found


def spans(text: str) -> list[tuple[int, int, str]]:
    """(start, end, text) of the parts of the file that are the same in both languages, in file order: fenced blocks with their
    run marker, and generated tables."""
    lines = text.split("\n")
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)
    found = [(offsets[first], offsets[last] + len(lines[last]), "\n".join(lines[first : last + 1])) for first, last in fences(text)]
    found += [(m.start(), m.end(), m.group(0)) for m in GENERATED.finditer(text)]
    return sorted(found)


def verbatim(text: str) -> list[str]:
    return [block for _, _, block in spans(text)]


def prose(text: str) -> str:
    """The text without the verbatim parts."""
    for block in sorted(verbatim(text), key=len, reverse=True):
        text = text.replace(block, "")
    return text


def headings(text: str) -> list[tuple[int, str]]:
    out = []
    inside = False
    for line in text.split("\n"):
        if FENCE_OPEN.match(line) and not inside:
            inside = True
        elif FENCE_CLOSE.match(line) and inside:
            inside = False
        elif not inside and (m := HEADING.match(line)):
            out.append((len(m.group(1)), m["title"]))
    return out


def slugs(text: str) -> set[str]:
    """The anchors GitHub gives the headings of a markdown file (letters with accents stay)."""
    seen: dict[str, int] = {}
    found = set()
    for _, title in headings(text):
        title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title).replace("`", "").lower()
        slug = "".join(c for c in title if c.isalnum() or c in " -_").replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        found.add(slug if count == 0 else f"{slug}-{count}")
    return found


def body(text: str) -> str:
    """The file without its first line, the language switch."""
    return text.split("\n", 1)[1]


def targets(text: str) -> list[str]:
    """Every link and image target outside code, in file order, with an in-page anchor written as ``#``."""
    cleaned = body(text)
    for block in verbatim(text):
        cleaned = cleaned.replace(block, "")
    cleaned = re.sub(r"`[^`\n]*`", "", cleaned)
    found = sorted([(m.start(), m["target"]) for m in LINK.finditer(cleaned)] + [(m.start(), m["target"]) for m in HTML_REF.finditer(cleaned)])
    return ["#" if t.startswith("#") else t for _, t in found]


def images(text: str) -> list[tuple[str, str]]:
    """(target, alt) of every image, Markdown or HTML, in file order."""
    cleaned = body(text)
    for block in verbatim(text):
        cleaned = cleaned.replace(block, "")
    found = [(m.start(), m["target"], m["alt"]) for m in MD_IMAGE.finditer(cleaned)]
    found += [(m.start(), m["target"], m["alt"]) for m in HTML_IMAGE.finditer(cleaned)]
    return [(target, alt) for _, target, alt in sorted(found)]


EN_TEXT, PT_TEXT = read(EN), read(PT)


# ---- the two files line up ---------------------------------------------------------------------------------------------
def test_each_file_links_to_the_other_on_its_first_line() -> None:
    assert "(README.pt-BR.md)" in EN_TEXT.split("\n", 1)[0]
    assert "(README.md)" in PT_TEXT.split("\n", 1)[0]
    assert EN_TEXT.startswith("**English** | ") and PT_TEXT.startswith("[English](README.md) | **Português (Brasil)**")


def test_the_headings_have_the_same_levels_in_the_same_order() -> None:
    en, pt = headings(EN_TEXT), headings(PT_TEXT)
    assert len(pt) == len(en), f"README.md has {len(en)} headings and README.pt-BR.md has {len(pt)}"
    assert [level for level, _ in pt] == [level for level, _ in en], "a heading has another level"
    assert [t for _, t in pt] != [t for _, t in en], "the headings are not translated"


def test_the_headings_are_translated() -> None:
    same = [title for (_, title), (_, other) in zip(headings(EN_TEXT), headings(PT_TEXT)) if title == other]
    # names that are the same word in both languages, or code, stay
    assert len(same) <= 6, f"headings left in English: {same}"


def test_every_code_block_output_and_generated_table_is_identical_and_in_order() -> None:
    en, pt = verbatim(EN_TEXT), verbatim(PT_TEXT)
    assert len(pt) == len(en), f"README.md has {len(en)} code blocks and generated tables, README.pt-BR.md has {len(pt)}"
    for number, (a, b) in enumerate(zip(en, pt), 1):
        assert a == b, f"block {number} differs; run `python tests/test_readme_pt.py --write`:\n{a[:200]}\n---\n{b[:200]}"


def test_the_run_markers_and_generated_markers_are_the_same() -> None:
    marker = re.compile(r"<!-- (?:run: .*?|generated: [a-z-]+|/generated) -->")
    assert marker.findall(PT_TEXT) == marker.findall(EN_TEXT)


def test_the_images_are_the_same_and_every_one_has_its_own_translated_alt_text() -> None:
    en, pt = images(EN_TEXT), images(PT_TEXT)
    assert [t for t, _ in pt] == [t for t, _ in en]
    assert len(en) >= 12
    for (target, alt_en), (_, alt_pt) in zip(en, pt):
        assert alt_pt.strip(), f"{target} has no alt text"
        assert alt_pt != alt_en or len(alt_en) <= 3, f"the alt text of {target} is not translated"


def test_every_link_goes_to_the_same_place() -> None:
    en, pt = targets(EN_TEXT), targets(PT_TEXT)
    assert pt == en, [(a, b) for a, b in zip(en, pt) if a != b][:5]
    assert sum(1 for t in en if t == "#") >= 15  # the README is full of links to its own sections


# ---- the links of the Portuguese file resolve --------------------------------------------------------------------------
ANCHORS = sorted({m["target"] for m in LINK.finditer(re.sub(r"`[^`\n]*`", "", prose(PT_TEXT))) if m["target"].startswith("#")})
FILE_LINKS = sorted({t for t in targets(PT_TEXT) if t != "#" and not re.match(r"[a-z][a-z0-9+.-]*:", t, re.IGNORECASE)})


@pytest.mark.parametrize("anchor", ANCHORS)
def test_an_in_page_link_points_at_a_heading_of_the_portuguese_file(anchor: str) -> None:
    assert anchor[1:] in slugs(PT_TEXT), f"{anchor} is not a heading of README.pt-BR.md"


@pytest.mark.parametrize("target", FILE_LINKS)
def test_a_relative_link_resolves(target: str) -> None:
    file_part, _, anchor = target.partition("#")
    destination = (ROOT / file_part) if file_part else PT
    assert destination.exists(), f"README.pt-BR.md links to {target}, which does not exist"
    if anchor and destination.suffix == ".md":
        assert anchor in slugs(read(destination)), f"{target}: {destination.name} has no heading #{anchor}"


# ---- the prose is Portuguese and keeps the style ---------------------------------------------------------------------
def test_no_long_prose_line_was_left_in_english() -> None:
    def long_lines(text: str) -> set[str]:
        out = set()
        for line in prose(body(text)).split("\n"):
            plain = re.sub(r"`[^`]*`|\[[^\]]*\]\([^)]*\)|<[^>]+>|[|*#>\-]", "", line).strip()
            if len(plain) > 60 and not line.startswith(("|", "[![")):
                out.add(line.strip())
        return out

    left = sorted(long_lines(EN_TEXT) & long_lines(PT_TEXT))
    assert not left, f"{len(left)} long lines are the same as in English, translate them: {left[:3]}"


def test_the_text_reads_as_portuguese() -> None:
    text = re.sub(r"`[^`\n]*`", "", prose(PT_TEXT))  # a quoted message of the tool stays in English
    for word in (" você ", " não ", " são ", " uma ", " para "):
        assert word in text, f"{word.strip()!r} never appears in README.pt-BR.md"
    for word in (" the ", " you ", " which "):
        assert text.count(word) <= 3, f"too much English: {word.strip()!r} appears {text.count(word)} times"


def test_the_version_is_the_one_of_the_package() -> None:
    assert f"A versão é {__version__}" in PT_TEXT, "the status line of the Limitações section names another version"
    assert f"The version is {__version__}" in EN_TEXT


def test_the_prose_has_no_emoji_and_no_long_dashes() -> None:
    text = prose(PT_TEXT)
    text = re.sub(r"`[^`\n]*`", "", text)
    assert not EMOJI.search(text), "no emoji"
    assert "—" not in text and "–" not in text, "use a comma, a colon or a full stop instead of a long dash"


def test_no_line_ends_in_spaces() -> None:
    lines = [n for n, line in enumerate(PT_TEXT.split("\n"), 1) if line != line.rstrip()]
    assert not lines, f"spaces at the end of lines {lines[:5]}"


def test_the_file_ends_with_the_license_section_and_then_only_the_support_block() -> None:
    """The shared public-repo template appends its delimited support block after the last section."""
    body = re.sub(r"\n*<!-- public-repo-support:start -->.*?<!-- public-repo-support:end -->\n*", "\n", PT_TEXT, flags=re.S)
    assert headings(body)[-1] == (2, "Licença")
    assert body.rstrip("\n").split("\n")[-1].startswith("O AgentFare é distribuído sob a licença MIT")


# ---- maintenance entry point ------------------------------------------------------------------------------------------
def synced(pt: str, en: str) -> str:
    """``pt`` with every code block and generated table taken from ``en``. The translated prose stays."""
    english, portuguese = verbatim(en), spans(pt)
    if len(english) != len(portuguese):
        raise SystemExit(
            f"README.md has {len(english)} code blocks and generated tables and README.pt-BR.md has {len(portuguese)}: "
            "add or remove the missing ones by hand first, then run this again"
        )
    out, position = [], 0
    for (first, last, _), new in zip(portuguese, english):
        out += [pt[position:first], new]
        position = last
    out.append(pt[position:])
    return "".join(out)


def main(argv: list[str]) -> int:
    if argv[1:] != ["--write"]:
        print("usage: python tests/test_readme_pt.py --write   (copies the code blocks and generated tables of README.md into README.pt-BR.md)")
        return 2
    before = read(PT)
    after = synced(before, read(EN))
    if after == before:
        print("README.pt-BR.md is up to date")
    else:
        PT.write_text(after, encoding="utf-8")
        print("README.pt-BR.md updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
