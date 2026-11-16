"""Exercise the local MCP server with framed JSON-RPC requests."""

import argparse
import json
from pathlib import Path
from queue import Queue
import subprocess
import sys
from threading import Thread
from time import perf_counter, sleep

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.verify_rust_full_parity import digest, native_environment, run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--server", type=Path, required=True)
    parser.add_argument("--media", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True)
    source = root / "한글 원본.txt"
    source.write_text("source\n한글", encoding="utf-8")
    evidence = {"server_sha256": digest(args.server), "worker_sha256": digest(args.worker), "checks": {}}
    env = native_environment()
    log = root / "stderr.log"
    launch = perf_counter()
    with log.open("wb") as errors, subprocess.Popen(
        [args.server.resolve(), "--data-dir", root / "mcp-profile"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=errors, text=True, encoding="utf-8", env=env,
    ) as server:
        responses = Queue()

        def receive():
            for line in server.stdout:
                try:
                    responses.put(json.loads(line))
                except Exception as error:
                    responses.put(error)
            responses.put(None)

        reader = Thread(target=receive, daemon=True)
        reader.start()

        def send(method, params=None, request_id=None):
            message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
            if request_id is not None:
                message["id"] = request_id
            server.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            server.stdin.flush()

        def read(request_id):
            response = responses.get(timeout=30)
            assert isinstance(response, dict) and response.get("id") == request_id and response["jsonrpc"] == "2.0", response
            return response

        def call(number, action, paths, target):
            send("tools/call", {"name": "convert_files", "arguments": {
                "action": action, "paths": [str(path) for path in paths], "output": str(target),
            }}, number)

        try:
            server.stdin.write("\ufeff")
            send("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                "clientInfo": {"name": "MagicConverter verification", "version": "1"}}, 1)
            assert read(1)["result"]["protocolVersion"] == "2025-11-25"
            timings = evidence["seconds"] = {"launch_to_initialize": perf_counter() - launch}
            send("notifications/initialized")
            send("tools/list", request_id=2)
            assert [tool["name"] for tool in read(2)["result"]["tools"]] == ["convert_files"]
            evidence["checks"]["initialize_and_discover"] = True

            started = perf_counter()
            call(3, "tool:copy_original", [source], root / "mcp")
            mcp = read(3)["result"]["structuredContent"]
            timings["copy_request"] = perf_counter() - started
            code, cli = run([args.worker.resolve()], ("convert", "--action", "tool:copy_original", "--output",
                            root / "cli", "--data-dir", root / "cli-profile", source), environment=env)
            assert code == 0 and mcp["ok"] is True
            assert [digest(Path(result["jobs"][0]["outputs"][0])) for result in (mcp, cli)] == [digest(source)] * 2
            evidence["checks"]["copy_receipt_and_bytes"] = True

            call(4, "unknown:route", [source], root / "invalid")
            result = read(4)["result"]
            code, cli = run([args.worker.resolve()], ("convert", "--action", "unknown:route", "--output",
                            root / "invalid-cli", "--data-dir", root / "cli-profile", source), environment=env)
            assert code != 0 and result["isError"] and result["structuredContent"] == cli
            evidence["checks"]["same_error_receipt"] = True

            call(5, "media:webm", [args.media.resolve()], root / "cancelled")
            send("ping", request_id=6)
            assert "result" in read(6)
            sleep(0.4)
            started = perf_counter()
            send("notifications/cancelled", {"requestId": 5, "reason": "verification"})
            call(7, "tool:copy_original", [source], root / "after-cancel")
            assert read(7)["result"]["structuredContent"]["ok"]
            timings["cancel_to_next_copy"] = perf_counter() - started
            assert not list((root / "cancelled").glob("*"))
            history = json.loads((root / "mcp-profile/history.json").read_text(encoding="utf-8"))
            assert any(entry.get("cancelled") or any(file.get("state") == "cancelled" for file in entry.get("files", []))
                       for entry in history.get("entries", [])), history
            evidence["checks"]["cancel_running_request_and_publish_next"] = True
            server.stdin.close()
            assert server.wait(timeout=10) == 0
            evidence["status"] = "passed"
        except BaseException as error:
            evidence["status"] = "failed"
            evidence["error"] = repr(error)
            server.kill()
            raise
        finally:
            (root / "report.json").write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
