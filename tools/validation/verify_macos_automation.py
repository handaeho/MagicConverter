"""Check same-user IPC, durable receipts, restart recovery and watch conversion."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True)
    profile = root / "profile"
    output = root / "outputs"
    output.mkdir()

    def call(*arguments):
        result = subprocess.run([args.worker, *map(str, arguments)], capture_output=True, timeout=20)
        return json.loads(result.stdout)

    def status():
        return call("status", "--data-dir", profile)

    def start():
        log = (root / "service.log").open("ab")
        process = subprocess.Popen([args.worker, "serve", "--data-dir", profile], stdout=log, stderr=log)
        log.close()
        for _ in range(30):
            if process.poll() is not None:
                raise AssertionError((root / "service.log").read_text())
            value = status()
            if value.get("ok"):
                return process, value
            time.sleep(0.1)
        raise AssertionError("The automation service did not start")

    process, initial = start()
    try:
        assert initial["instance"]
        request = str(uuid.uuid4())
        arguments = ["send", "--data-dir", profile, "--action", "image:jpg", "--request-id", request,
                     "--output", output, args.image.resolve()]
        accepted = call(*arguments)
        assert accepted["ok"], accepted
        repeated = call(*arguments)
        assert repeated["ok"] and repeated["job_id"] == accepted["job_id"], (accepted, repeated)
        for _ in range(100):
            current = status()
            if not current["jobs"]:
                break
            time.sleep(0.05)
        assert len(list(output.glob("*.jpg"))) == 1
        history = json.loads((profile / "history.json").read_text())
        assert len(history["entries"]) == 1
        assert call("stop", "--data-dir", profile, "--instance", "wrong-instance")["ok"] is False
        # A terminated idle instance must release its profile lock on macOS.
        process.kill()
        process.wait(timeout=5)
        process, restarted = start()
        assert restarted["instance"] != initial["instance"]
        assert call(*arguments)["job_id"] == accepted["job_id"]
        assert len(list(output.glob("*.jpg"))) == 1
        stopped = call("stop", "--data-dir", profile, "--instance", restarted["instance"])
        assert stopped["ok"], stopped
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            try:
                current = status()
                call("stop", "--data-dir", profile, "--instance", current["instance"])
                process.wait(timeout=10)
            except Exception:
                process.kill()
                process.wait(timeout=5)

    watch = root / "watch"
    watch.mkdir()
    source = watch / "sample.png"
    source.write_bytes(args.image.read_bytes())
    destination = root / "watch-output"
    destination.mkdir()
    arguments = ["watch", "--data-dir", root / "watch-profile", "--to", "jpg", "--settle", "0.1", "--once", "--output", destination, watch]
    first = call(*arguments)
    assert first["ok"] and len(first["jobs"]) == 1, first
    before = sorted(path.name for path in destination.iterdir())
    second = call(*arguments)
    assert second["ok"] and not second["jobs"], second
    assert sorted(path.name for path in destination.iterdir()) == before
    print("IPC submission, idempotent replay, crash/restart, history, stop, and watch replay passed.")


if __name__ == "__main__":
    main()
