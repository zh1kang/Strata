"""CLI smoke tests: in-process for speed, plus one real subprocess."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from strata.cli import main

ROOT = Path(__file__).resolve().parent.parent


def run(capsys: pytest.CaptureFixture[str], db: str, *args: str) -> object:
    code = main(["--db", db, *args])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    return json.loads(captured.out)


def test_cli_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = str(tmp_path / "cli.db")
    assert run(capsys, db, "init")["ok"] is True
    assert run(
        capsys, db, "ingest", "--session", "s", "--key", "e0", "--text", "Atlas launches Dec 10"
    ) == {
        "duplicates": 0,
        "inserted": 1,
    }
    again = run(
        capsys, db, "ingest", "--session", "s", "--key", "e0", "--text", "Atlas launches Dec 10"
    )
    assert again == {"duplicates": 1, "inserted": 0}
    version = run(capsys, db, "assert", "atlas", "launch_date", '"2026-12-10"', "--source", "e0")
    assert version == {"version_id": 1}
    run(capsys, db, "define-profile", "atlas")
    report = run(capsys, db, "maintain", "--budget", "5")
    assert report["jobs"] == 1 and report["dirty"] == 0

    recall = run(capsys, db, "recall", "launch")
    statuses = {(i["kind"], i["status"]) for i in recall["items"]}
    assert statuses == {("fact", "current"), ("profile", "current")}

    run(capsys, db, "ingest", "--session", "s", "--key", "e1", "--text", "Atlas moved to Jan 15")
    run(capsys, db, "assert", "atlas", "launch_date", '"2027-01-15"', "--source", "e1")
    stale = run(capsys, db, "recall", "launch")
    assert stale["suppressed"] == 1 and stale["fallback_used"] is True
    assert all(i["status"] != "current" or i["kind"] == "fact" for i in stale["items"])

    assert run(capsys, db, "maintain", "--policy", "fifo")["jobs"] == 1
    nodes = run(capsys, db, "nodes", "--state", "valid")
    assert [n["key"] for n in nodes] == ["atlas"]
    explained = run(capsys, db, "explain", "profile", "atlas")
    assert explained["provenance"] == ["event:e1", "fact:atlas/launch_date@2"]
    assert [f["value"] for f in run(capsys, db, "history", "atlas", "launch_date")] == [
        "2026-12-10",
        "2027-01-15",
    ]
    assert run(capsys, db, "facts", "atlas")[0]["value"] == "2027-01-15"
    assert run(capsys, db, "rebuild")["jobs"] == 1
    run(capsys, db, "retract", "atlas", "launch_date", "--source", "e1")
    assert run(capsys, db, "status")["nodes"] == {"stale": 1}


def test_cli_ingest_file_and_bare_string_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(tmp_path / "cli.db")
    lines = [
        {"key": "a", "session_id": "s1", "kind": "m", "payload": {"text": "one"}},
        {"key": "b", "session_id": "s2", "payload": {"text": "two"}},
    ]
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    assert run(capsys, db, "ingest", "--file", str(path))["inserted"] == 2
    run(
        capsys, db, "assert", "atlas", "owner", "ana", "--source", "a"
    )  # not JSON: stored as string
    assert run(capsys, db, "facts", "atlas")[0]["value"] == "ana"


def test_cli_errors_exit_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = str(tmp_path / "cli.db")
    assert main(["--db", db, "assert", "a", "b", "1", "--source", "missing"]) == 1
    assert "unknown source event" in capsys.readouterr().err
    assert main(["--db", db, "ingest", "--text", "x"]) == 1
    assert main(["--db", db, "explain", "nope", "nope"]) == 1
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        main(["--db", db, "maintain", "--policy", "lifo"])
    assert exc.value.code == 2


def test_cli_bench_matches_library_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "bench.json"
    args = [
        "--entities",
        "10",
        "--teams",
        "2",
        "--rounds",
        "4",
        "--updates-per-round",
        "3",
        "--queries-per-round",
        "5",
    ]
    code = main(["bench", "--seed", "4", *args, "--output", str(out)])
    stdout = capsys.readouterr().out
    assert code == 0 and out.read_text() == stdout
    assert json.loads(stdout)["benchmark"] == "strata-synthetic-v1"


def test_module_entry_point_subprocess(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    db = str(tmp_path / "sub.db")
    proc = subprocess.run(
        [sys.executable, "-m", "strata", "--db", db, "status"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["events"] == 0
