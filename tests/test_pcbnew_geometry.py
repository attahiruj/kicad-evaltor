"""Tier 3: verify KiCad's SWIG `pcbnew` can supply real geometry headlessly.

These tests are the evidence behind the architecture decision: the library needs
accurate footprint and text bounding boxes, and must get them from KiCad's own
geometry engine rather than a reimplementation. Each case shells out to KiCad's
bundled CPython (pcbnew is a compiled extension, so it cannot be imported into
the interpreter running the suite) and asserts against known-good values.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FOOTPRINT_LIB = Path(
    os.environ.get("KICAD_FOOTPRINT_LIB")
    or r"C:\Program Files\KiCad\10.0\share\kicad\footprints\Resistor_SMD.pretty"
)
FOOTPRINT_NAME = "R_0805_2012Metric"

# KiCad's own footprint: courtyard is 3.45 x 1.99 mm.
EXPECTED_COURTYARD_W_MM = 3.45
EXPECTED_COURTYARD_H_MM = 1.99

NM_PER_MM = 1_000_000


def find_kicad_python() -> Path | None:
    """Locate the CPython that KiCad ships, which is where pcbnew is importable.

    KiCad's own interpreter is searched first: under a virtualenv the
    co-located python.exe is the venv's, which has no pcbnew.
    """
    candidates: list[Path] = []
    if sys.platform == "win32":
        for var in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(var)
            if not base:
                continue
            root = Path(base) / "KiCad"
            if not root.is_dir():
                continue
            candidates.extend(sorted(root.glob("*/bin/python.exe"), reverse=True))
    else:
        candidates.extend(
            Path(p)
            for p in (
                "/usr/lib/kicad/bin/python3",
                "/usr/bin/python3",
                "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3",
            )
        )

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def run_pcbnew(code: str, tmp_path: Path) -> dict:
    """Execute a snippet under KiCad's Python and parse its RESULT_JSON line."""
    kicad_python = find_kicad_python()
    script = tmp_path / "snippet.py"
    script.write_text(code, encoding="utf-8")

    result = subprocess.run(
        [str(kicad_python), str(script)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    for line in result.stdout.splitlines():
        if line.startswith("RESULT_JSON"):
            return json.loads(line.removeprefix("RESULT_JSON"))
    raise AssertionError(
        f"snippet produced no RESULT_JSON\nrc={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def pcbnew_board(tmp_path_factory):
    """Generate a board with two 0805s on a 4 mm pitch, plus a silk text item."""
    out = tmp_path_factory.mktemp("pcbnew")
    board = out / "gen.kicad_pcb"

    build = f"""
import pcbnew
b = pcbnew.CreateEmptyBoard()
for x, name in [(10000000, 'R1'), (14000000, 'R2')]:
    fp = pcbnew.FootprintLoad(r'{FOOTPRINT_LIB}', r'{FOOTPRINT_NAME}')
    b.Add(fp)
    fp.SetPosition(pcbnew.VECTOR2I(x, 10000000))
    fp.SetReference(name)
t = pcbnew.PCB_TEXT(b)
t.SetText('R1')
t.SetPosition(pcbnew.VECTOR2I(20000000, 10000000))
t.SetTextSize(pcbnew.VECTOR2I(1000000, 1000000))
t.SetTextThickness(150000)
b.Add(t)
b.Save(r'{board}')
print('RESULT_JSON' + '{{}}')
"""
    run_pcbnew(build, out)
    assert board.exists(), "pcbnew did not write the board"
    return board


@pytest.fixture(scope="module")
def footprint_geometry(pcbnew_board, tmp_path_factory):
    out = tmp_path_factory.mktemp("pcbnew_measure")
    code = f"""
import json, pcbnew
b = pcbnew.LoadBoard(r'{pcbnew_board}')
data = {{}}
for fp in b.GetFootprints():
    bb = fp.GetBoundingBox()
    crt = fp.GetCourtyard(pcbnew.F_CrtYd)
    cb = crt.BBox()
    data[fp.GetReference()] = {{
        'bbox': [bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom()],
        'courtyard': [cb.GetLeft(), cb.GetTop(), cb.GetRight(), cb.GetBottom()],
        'courtyard_outlines': crt.OutlineCount(),
    }}
print('RESULT_JSON' + json.dumps(data))
"""
    return run_pcbnew(code, out)


@pytest.fixture(scope="module")
def text_geometry(pcbnew_board, tmp_path_factory):
    out = tmp_path_factory.mktemp("pcbnew_text")
    code = f"""
import json, pcbnew
b = pcbnew.LoadBoard(r'{pcbnew_board}')
out = []
for t in b.GetDrawings():
    if isinstance(t, pcbnew.PCB_TEXT):
        bb = t.GetBoundingBox()
        out.append({{
            'text': t.GetText(),
            'bbox': [bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom()],
        }})
print('RESULT_JSON' + json.dumps(out))
"""
    return run_pcbnew(code, out)


def test_pcbnew_is_importable_from_the_bundled_python(tmp_path):
    code = "import json, pcbnew; print('RESULT_JSON' + json.dumps(pcbnew.GetBuildVersion()))"
    assert run_pcbnew(code, tmp_path)


def test_courtyard_bbox_matches_the_footprint_geometry(footprint_geometry):
    for reference, geometry in footprint_geometry.items():
        left, _top, right, bottom = geometry["courtyard"]
        width_mm = (right - left) / NM_PER_MM
        height_mm = (bottom - _top) / NM_PER_MM

        assert width_mm == pytest.approx(EXPECTED_COURTYARD_W_MM, abs=0.01), reference
        assert height_mm == pytest.approx(EXPECTED_COURTYARD_H_MM, abs=0.01), reference


def test_courtyard_has_one_outline(footprint_geometry):
    assert {r: g["courtyard_outlines"] for r, g in footprint_geometry.items()} == {
        "R1": 1,
        "R2": 1,
    }


def test_placement_is_reflected_in_the_courtyard(footprint_geometry):
    # The 4 mm pitch between the two footprints must show up in the coordinates.
    r1_right = footprint_geometry["R1"]["courtyard"][2]
    r2_left = footprint_geometry["R2"]["courtyard"][0]

    assert r1_right < r2_left
    assert (r2_left - r1_right) / NM_PER_MM == pytest.approx(0.55, abs=0.01)


def test_text_bbox_is_non_degenerate_and_ordered(text_geometry):
    assert len(text_geometry) == 1
    entry = text_geometry[0]
    left, top, right, bottom = entry["bbox"]

    assert entry["text"] == "R1"
    assert left < right
    assert top < bottom
    # A 1 mm "R1" with a 0.15 mm stroke is roughly 2.2 x 1.7 mm; this is a
    # sanity bound, not an exact value, so it does not pin KiCad's font metrics.
    assert 1.0 < (right - left) / NM_PER_MM < 4.0
    assert 1.0 < (bottom - top) / NM_PER_MM < 3.0


def test_geometry_is_available_without_a_running_kicad(tmp_path):
    """The whole point: no GUI, no IPC server, no kicad-cli."""
    code = f"""
import json, pcbnew
b = pcbnew.CreateEmptyBoard()
fp = pcbnew.FootprintLoad(r'{FOOTPRINT_LIB}', r'{FOOTPRINT_NAME}')
b.Add(fp)
cb = fp.GetCourtyard(pcbnew.F_CrtYd).BBox()
print('RESULT_JSON' + json.dumps({{'w_nm': cb.GetWidth()}}))
"""
    data = run_pcbnew(code, tmp_path)

    assert data["w_nm"] / NM_PER_MM == pytest.approx(EXPECTED_COURTYARD_W_MM, abs=0.01)
