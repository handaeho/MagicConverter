"""Exercise the same CLI, files, profile and durable records across both implementations."""

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.verify_rust_full_parity import digest, native_environment, run

ACTION = "tool:copy_original"


class App:
    def __init__(self, prefix, environment=None):
        self.prefix = prefix
        self.environment = environment

    def call(self, command, profile, *args, expected=0):
        code, report = run(self.prefix, (command, "--data-dir", profile, *args), environment=self.environment)
        assert code == expected, (self.prefix, command, args, code, report)
        return report

    @contextmanager
    def server(self, profile):
        command = [*self.prefix, "serve", "--data-dir", profile, "--duration", "90"]
        process = subprocess.Popen(command, env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            yield process
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)


def finish(process, expected=0):
    out, err = process.communicate(timeout=20)
    assert process.returncode == expected, (process.returncode, out.decode("utf-8"), err.decode("utf-8", errors="replace"))
    return json.loads(out)


def contents(folder):
    return {str(path.relative_to(folder)): digest(path) for path in folder.rglob("*") if path.is_file()}


def journal(profile):
    with sqlite3.connect(profile / "watch-journal.sqlite3") as db:
        return db.execute("SELECT key, state, source, target, digest FROM events ORDER BY key").fetchall()


def watch_pair(first, second, root):
    folder = root / "incoming"
    nested = folder / "하위"
    nested.mkdir(parents=True)
    output = folder / "_converted"
    profile = root / "profile"
    fixture = {"한글 one.wav": b"abc", "하위/nested.WAV": b"def", ".hidden.wav": b"skip",
               "~$busy.wav": b"skip", "ignore.mp3": b"skip"}
    for name, data in fixture.items():
        (folder / name).write_bytes(data)
    arguments = ("--action", ACTION, "--recursive", "--include", "*.wav;*.flac",
                 "--settle", "0.1", "--once", folder)
    start = time.perf_counter()
    report = first.call("watch", profile, *arguments)
    first_s = time.perf_counter() - start
    assert report["ok"] and len(report["jobs"]) == 2, report
    outputs = [Path(job["outputs"][0]) for job in report["jobs"]]
    assert all(path.parent == output for path in outputs), outputs
    for directory in outputs:
        receipt = json.loads((directory / ".magic-receipt.json").read_text(encoding="utf-8"))
        assert len(receipt["files"]) == 1 and receipt["directories"] == [], receipt
        item = receipt["files"][0]
        assert item["sha256"] == digest(directory / item["path"])
    before = contents(output)
    previous = journal(profile)
    start = time.perf_counter()
    replay = second.call("watch", profile, *arguments)
    second_s = time.perf_counter() - start
    assert replay["ok"] and replay["jobs"] == [], (replay, previous, journal(profile))
    assert journal(profile) == previous and contents(output) == before
    (folder / "한글 one.wav").write_bytes(b"new")
    changed = second.call("watch", profile, *arguments)
    assert changed["ok"] and len(changed["jobs"]) == 1, changed
    after = journal(profile)
    assert len(after) == 3 and {row[1] for row in after} == {"done"}, after
    assert len(contents(output)) == 6
    key, _, _, target, _ = after[0]
    with sqlite3.connect(profile / "watch-journal.sqlite3") as db:
        db.execute("UPDATE events SET state='publishing' WHERE key=?", (key,))
    recovered = first.call("watch", profile, *arguments)
    assert recovered["ok"] and recovered["jobs"] == []
    assert journal(profile) == after
    assert Path(target).is_dir()
    for name in ("ignore.mp3", ".hidden.wav", "~$busy.wav"):
        assert (folder / name).read_bytes() == fixture[name]
    return {"converted": 2, "duplicate_jobs": 0, "changed": 1, "recovered_publication": True, "initial_s": first_s,
            "cross_restart_s": second_s, "outputs": before}


def wait_for_output(client, profile, output, source):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = client.call("status", profile)
        if output.is_file() and state["recent"] and not state["jobs"]:
            assert digest(output) == digest(source), state
            return
        time.sleep(0.05)
    raise AssertionError(("conversion did not finish", output, state))


def ipc_pair(first, second, root):
    profile = root / "profile"
    output = root / "output"
    output.mkdir(parents=True)
    source = root / "보낸 파일.txt"
    source.write_text("원본 파일을 그대로 보존합니다.", encoding="utf-8")
    destination = output / "보낸 파일_copy.txt"
    arguments = ("--action", ACTION, "--output", output, "--request-id", "migration-id", source)
    with first.server(profile) as server:
        start = time.perf_counter()
        state = second.call("status", profile)
        startup_s = time.perf_counter() - start
        assert state["ok"] and not state["watch_running"] and not state["jobs"], state
        instance = state["instance"]
        rejected = first.call("serve", profile, "--duration", "0.2", expected=1)
        assert "이미 실행" in rejected["error"], rejected
        second.call("cancel", profile, "--instance", "stale-id", expected=1)
        idle = second.call("cancel", profile, "--instance", instance, "999")
        assert idle["cancelled"] is False, idle
        sent = second.call("send", profile, *arguments)
        assert sent["ok"] and sent["instance"] == instance, sent
        repeated = first.call("send", profile, *arguments)
        assert repeated["replayed"] and not repeated["previous_instance"], repeated
        assert repeated["job_id"] == sent["job_id"]
        conflict = second.call("send", profile, *arguments[:-1], source.with_name("other.txt"), expected=1)
        assert "동일한 IPC 요청 ID" in conflict["error"], conflict
        wait_for_output(second, profile, destination, source)
        assert second.call("stop", profile, "--instance", instance)["stopping"]
        completed = finish(server)
        assert completed["ok"] and len(completed["jobs"]) == 1, completed

    with second.server(profile) as server:
        state = first.call("status", profile)
        assert state["instance"] != instance and len(state["recent"]) == 1, state
        replay = first.call("send", profile, *arguments)
        assert replay["replayed"] and replay["previous_instance"], replay
        assert replay["instance"] == instance and replay["job_id"] == sent["job_id"], replay
        assert list(output.iterdir()) == [destination]
        assert first.call("stop", profile, "--instance", state["instance"])["stopping"]
        completed = finish(server)
        assert completed["ok"] and completed["jobs"] == [], completed
    return {"startup_and_status_s": startup_s, "cross_client": True, "cross_restart": True,
            "duplicate_jobs": 0, "sha256": digest(destination)}


def cancel_job(server_app, client, source, root):
    profile = root / "profile"
    output = root / "output"
    output.mkdir(parents=True)
    before = digest(source)
    with server_app.server(profile) as server:
        instance = client.call("status", profile)["instance"]
        sent = client.call("send", profile, "--action", "media:webm", "--output", output, source)
        active = client.call("status", profile)["jobs"]
        assert any(job["job_id"] == sent["job_id"] for job in active), active
        assert client.call("cancel", profile, "--instance", instance, sent["job_id"])["cancelled"]
        assert client.call("stop", profile, "--instance", instance)["stopping"]
        completed = finish(server, 1)
        job, = completed["jobs"]
        assert job["cancelled"] and job["files"][0]["state"] == "cancelled" and not job["outputs"], job
    assert digest(source) == before and list(output.iterdir()) == []
    return {"cancelled": True, "source_sha256": before, "unpublished": True}


def main():
    parser = argparse.ArgumentParser()
    reference = parser.add_mutually_exclusive_group(required=True)
    reference.add_argument("--baseline", type=Path)
    reference.add_argument("--python-cli", type=Path)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scope", choices=("all", "watch", "ipc"), default="all")
    parser.add_argument("--cancel-media", type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True)
    python = (App([args.python_cli.resolve(strict=True)], native_environment()) if args.python_cli else
              App([sys.executable, "-B", args.baseline.resolve() / "run_magic_converter.py"]))
    rust = App([args.worker.resolve()], native_environment())
    evidence = {"status": "running", "worker_sha256": digest(args.worker)}
    try:
        for name, check in (("watch", watch_pair), ("ipc", ipc_pair)):
            if args.scope not in ("all", name):
                continue
            for order, first, second in (("python-rust", python, rust), ("rust-python", rust, python)):
                print(name, order, flush=True)
                evidence[f"{name}-{order}"] = check(first, second, root / name / order)
        if args.cancel_media:
            for name, server in (("python", python), ("rust", rust)):
                print("cancel", name, flush=True)
                evidence[f"cancel-{name}"] = cancel_job(server, rust, args.cancel_media.resolve(), root / "cancel" / name)
        evidence["status"] = "passed"
    finally:
        (root / "report.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
