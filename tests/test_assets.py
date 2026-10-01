"""The images of the README (docs/images, written by scripts/make_assets.py).

* every image the README and the docs will show exists, has the size it should and is under 2.5 MB, and the folder holds
  exactly the files the script's table says it makes (so nobody leaves an old image behind or forgets to commit a new one);
* every image the Markdown files of the repository point to exists;
* the pieces of the script that need no browser work: how a command line is shown, how colours are read, how a long line
  breaks, and that a path of this machine is replaced before anything is drawn (and stops the script when it cannot be);
* with Chromium, ffmpeg and Pillow installed, the script builds a screenshot, the diagram and the GIF into a scratch folder.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
import typer.main

from agentfare.cli import app

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "make_assets.py"
IMAGES = ROOT / "docs" / "images"
pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="needs a checkout with scripts/ (an installed copy has none)")
MAX_BYTES = 2_500_000

# What the README and the repository page use. A new image the README shows goes here, and in the table of the script.
README_IMAGES = (
    "hero.png", "social-preview.png", "summary.png", "models.png", "advise.png", "run.png", "run-light.png", "sessions.png", "prices.png",
    "init.png", "doctor.png", "report-dark.png", "report-light.png", "report-mobile.png", "how-it-works.svg", "how-it-works.png", "demo.gif",
)


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("make_assets", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_assets"] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


assets = load_script()


# ---- reading the files without an imaging library ---------------------------------------------------------------------------
def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    assert data[-12:-8] == b"\x00\x00\x00\x00" and data[-8:-4] == b"IEND", f"{path.name} is cut short"
    return struct.unpack(">II", data[16:24])  # type: ignore[return-value]


def gif_info(path: Path) -> dict[str, Any]:
    """Size, number of frames, running time in seconds and whether it loops, from the blocks of the file."""
    data = path.read_bytes()
    assert data[:6] in (b"GIF87a", b"GIF89a"), f"{path.name} is not a GIF"
    width, height, flags = struct.unpack("<HHB", data[6:11])
    pos = 13 + (3 * (2 << (flags & 7)) if flags & 0x80 else 0)
    frames = centiseconds = delay = 0
    loops = False
    while pos < len(data) and data[pos] != 0x3B:
        if data[pos] == 0x21:  # extension
            label = data[pos + 1]
            pos += 2
            if label == 0xF9:
                delay = struct.unpack("<H", data[pos + 2:pos + 4])[0]
            elif label == 0xFF and data[pos + 1:pos + 12] == b"NETSCAPE2.0":
                loops = True
            while data[pos] != 0:
                pos += 1 + data[pos]
            pos += 1
        elif data[pos] == 0x2C:  # frame
            frames += 1
            centiseconds += delay
            packed = data[pos + 9]
            pos += 10 + (3 * (2 << (packed & 7)) if packed & 0x80 else 0) + 1
            while data[pos] != 0:
                pos += 1 + data[pos]
            pos += 1
        else:
            raise AssertionError(f"unknown block {data[pos]:#x} in {path.name}")
    return {"size": (width, height), "frames": frames, "seconds": centiseconds / 100, "loops": loops}


def expected_files() -> set[str]:
    return {name for files in assets.OUTPUTS.values() for name in files}


# ---- the folder ------------------------------------------------------------------------------------------------------------
class TestFolder:
    def test_the_scripts_table_matches_the_files_on_disk(self) -> None:
        on_disk = {p.name for p in IMAGES.iterdir() if p.is_file()}
        assert on_disk == expected_files(), (sorted(on_disk - expected_files()), sorted(expected_files() - on_disk))

    def test_every_image_the_readme_will_show_is_made_by_the_script(self) -> None:
        assert set(README_IMAGES) <= expected_files()
        for name in README_IMAGES:
            assert (IMAGES / name).is_file(), name

    def test_every_terminal_screenshot_is_one_line_of_the_table(self) -> None:
        names = [shot.name for shot in assets.SHOTS]
        assert len(names) == len(set(names)) and {"summary", "models", "advise", "run", "sessions", "prices"} <= set(names)
        assert all(assets.OUTPUTS[name] == (f"{name}.png",) for name in names)
        commands = set(typer.main.get_command(app).commands)  # type: ignore[attr-defined]
        for shot in assets.SHOTS:
            assert shot.argv[0] in commands, shot
        for scene in assets.GIF_SCENES:
            assert scene.argv[0] in commands, scene

    def test_no_image_is_over_the_limit(self) -> None:
        assert assets.MAX_BYTES == MAX_BYTES
        for path in IMAGES.iterdir():
            assert 0 < path.stat().st_size <= MAX_BYTES, path.name

    def test_the_whole_folder_stays_small(self) -> None:
        assert sum(p.stat().st_size for p in IMAGES.iterdir()) < 6_000_000  # every clone downloads it, and the history keeps every version


class TestDimensions:
    def test_the_banner_and_the_social_preview_are_1280_by_640(self) -> None:
        assert png_size(IMAGES / "hero.png") == (1280, 640)
        assert png_size(IMAGES / "social-preview.png") == (1280, 640)

    def test_the_terminal_screenshots_are_1000_wide(self) -> None:
        for shot in assets.SHOTS:
            width, height = png_size(IMAGES / f"{shot.name}.png")
            assert width == 1000 and 200 <= height <= 1500, (shot.name, width, height)

    def test_the_report_is_the_card_alone_on_a_desktop_and_on_a_phone(self) -> None:
        for name in ("report-dark.png", "report-light.png"):
            width, height = png_size(IMAGES / name)
            assert width == 1080 and 1500 <= height <= 2600, (name, width, height)
        width, height = png_size(IMAGES / "report-mobile.png")
        assert width == 390 and height > 2000

    def test_the_diagram_is_a_vector_file_and_a_png_of_the_same_shape(self) -> None:
        root = ET.fromstring((IMAGES / "how-it-works.svg").read_text(encoding="utf-8"))
        _, _, w, h = (float(v) for v in root.attrib["viewBox"].split())
        width, height = png_size(IMAGES / "how-it-works.png")
        assert width >= 1600 and abs(width / height - w / h) < 0.01

    def test_the_gif_is_900_wide_loops_and_runs_6_to_12_seconds_under_2_5_mb(self) -> None:
        info = gif_info(IMAGES / "demo.gif")
        assert info["size"][0] == 900 and info["size"][1] <= 800, info
        assert info["loops"] and info["frames"] > 20
        assert 6 <= info["seconds"] <= 12, info
        assert (IMAGES / "demo.gif").stat().st_size <= MAX_BYTES


class TestDiagram:
    def svg(self) -> str:
        return (IMAGES / "how-it-works.svg").read_text(encoding="utf-8")

    def test_it_says_what_the_readme_says_it_says(self) -> None:
        text = self.svg()
        for words in ("Nothing leaves your machine", "Claude Code", "Codex CLI", "Gemini CLI", "Parsers", "SQLite", "pricing.yaml",
                      "summary", "advise", "run", "report", "no prompts, replies"):
            assert words in text, words
        assert "<title>" in text and "aria-label" in text

    def test_it_is_a_plain_picture(self) -> None:
        text = self.svg()
        assert "<script" not in text and "javascript:" not in text and "<image" not in text and "<foreignObject" not in text
        assert re.findall(r"https?://[^\"' ]+", text) == ["http://www.w3.org/2000/svg"]
        assert "/home/" not in text and "/Users/" not in text

    def test_it_matches_what_the_script_draws(self) -> None:
        assert self.svg() == assets.diagram_svg()


class TestMarkdownLinks:
    def test_every_image_the_markdown_files_point_to_exists(self) -> None:
        files = [ROOT / "README.md", ROOT / "README.pt-BR.md", ROOT / "CONTRIBUTING.md", *sorted((ROOT / "docs").glob("*.md"))]
        pointed = 0
        for md in files:
            if not md.is_file():
                continue
            text = re.sub(r"```.*?```|`[^`\n]*`", "", md.read_text(encoding="utf-8"), flags=re.S)  # an example in code is not a link
            targets = re.findall(r"!\[[^\]]*\]\(([^)\s]+)", text) + re.findall(r"<img[^>]+src=\"([^\"]+)\"", text) \
                + re.findall(r"\]\(([^)\s#]*images/[^)\s#]+)", text)
            for target in targets:
                if target.startswith(("http://", "https://", "data:")):
                    continue
                pointed += 1
                assert (md.parent / target.split("#")[0]).is_file(), f"{md.name} points to {target}, which is not there"
        assert pointed >= 25  # both READMEs show the same images


# ---- the pieces of the script that need no browser ---------------------------------------------------------------------------
class TestScript:
    def test_a_command_is_shown_as_it_is_typed(self) -> None:
        assert assets.typed(("run", "--dry-run", "--", "claude", "-p", "refactor the auth module")) == 'run --dry-run -- claude -p "refactor the auth module"'
        assert assets.typed(("summary", "--top", "5")) == "summary --top 5"

    def test_colours_and_weights_are_read_from_the_codes_the_commands_write(self) -> None:
        [line] = assets.parse_ansi("\x1b[1mbold\x1b[0m plain \x1b[2mdim\x1b[0m \x1b[1;33myellow\x1b[0m\n")
        assert line == [("bold", True, False, None), (" plain ", False, False, None), ("dim", False, True, None), (" ", False, False, None),
                        ("yellow", True, False, assets.PALETTE[33])]
        assert assets.parse_ansi("a\n\nb\n") == [[("a", False, False, None)], [], [("b", False, False, None)]]
        [rgb] = assets.parse_ansi("\x1b[38;2;10;20;30mx\x1b[0m")
        assert rgb == [("x", False, False, "#0a141e")]

    def test_a_line_longer_than_the_window_breaks_like_a_terminal_does(self) -> None:
        wrapped = assets.wrap_runs([[("abcdef", True, False, None), ("ghij", False, False, "#fff")]], 4)
        assert ["".join(t for t, *_ in row) for row in wrapped] == ["abcd", "efgh", "ij"]
        assert wrapped[1] == [("ef", True, False, None), ("gh", False, False, "#fff")]

    def test_a_path_of_this_machine_is_replaced_and_what_cannot_be_replaced_stops_the_script(self, tmp_path: Path) -> None:
        ws = object.__new__(assets.Workspace)
        ws.root = tmp_path / "agentfare-assets-x"
        scrubbed = ws.scrub(f"price table: built-in table ({ROOT}/src/agentfare/data/pricing.yaml)\nreport in {ws.root}/week.html\n")
        assert "(…/agentfare/data/pricing.yaml)" in scrubbed and "~/agentfare-demo/week.html" in scrubbed
        assert str(ROOT) not in scrubbed and str(ws.root) not in scrubbed
        with pytest.raises(SystemExit, match="names this machine"):
            ws.scrub(f"cache in {Path.home()}/.cache/x")
        with pytest.raises(SystemExit, match="names this machine"):
            ws.scrub("log in /tmp/somewhere/else.log")

    def test_an_unknown_name_is_refused_and_the_list_prints_every_asset(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as refused:
            assets.main(["--only", "nonsense"])
        assert refused.value.code == 2
        assert assets.main(["--list"]) == 0
        listed = capsys.readouterr().out
        assert all(name in listed for name in assets.OUTPUTS)

    def test_the_numbers_on_the_banner_are_whole_cents_from_integers(self) -> None:
        assert assets.dollars(19396) == "$193.96" and assets.dollars(5) == "$0.05" and assets.dollars(123456789) == "$1,234,567.89"


# ---- building them (when the tools are installed) -------------------------------------------------------------------------------
needs_chromium = pytest.mark.skipif(not any(shutil.which(b) for b in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")),
                                    reason="needs Chromium")


@pytest.fixture
def scratch_images(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    out = tmp_path / "images"
    monkeypatch.setattr(assets, "IMAGES", out)
    return out


@needs_chromium
class TestBuild:
    def test_a_terminal_screenshot_is_the_real_output_in_a_window(self, scratch_images: Path) -> None:
        assert assets.main(["--only", "models"]) == 0
        width, height = png_size(scratch_images / "models.png")
        assert width == 1000 and 200 < height < 600

    def test_a_new_line_in_the_table_makes_a_new_screenshot(self, scratch_images: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        shot = assets.Shot("sessions-short", ("sessions", "-n", "3"))
        monkeypatch.setattr(assets, "SHOTS", (*assets.SHOTS, shot))
        monkeypatch.setitem(assets.OUTPUTS, "sessions-short", ("sessions-short.png",))
        assert assets.main(["--only", "sessions-short"]) == 0
        assert png_size(scratch_images / "sessions-short.png")[0] == 1000

    def test_the_diagram_is_written_as_svg_and_png(self, scratch_images: Path) -> None:
        if not (shutil.which("rsvg-convert") or shutil.which("magick") or shutil.which("convert")):
            pytest.skip("needs rsvg-convert or ImageMagick")
        assert assets.main(["--only", "how-it-works"]) == 0
        assert (scratch_images / "how-it-works.svg").read_text(encoding="utf-8") == assets.diagram_svg()
        assert png_size(scratch_images / "how-it-works.png")[0] >= 1600

    def test_the_gif_is_built_from_the_real_output(self, scratch_images: Path) -> None:
        pytest.importorskip("PIL")
        if not shutil.which("ffmpeg"):
            pytest.skip("needs ffmpeg")
        assert assets.main(["--only", "demo"]) == 0
        info = gif_info(scratch_images / "demo.gif")
        assert info["size"][0] == 900 and 6 <= info["seconds"] <= 12 and info["loops"]
