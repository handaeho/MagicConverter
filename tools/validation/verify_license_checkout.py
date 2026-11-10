"""Verify candidate license bytes through actual Git CRLF and LF checkouts.

Uses a separate index and object store; leaves the workspace Git index untouched.
"""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def reviewed_files(root=ROOT):
    def read(name):
        return json.loads((root / name).read_text(encoding="utf-8"))

    notices = read("packaging/notices-lock.json")
    entries = [n for p in notices["packages"] for n in p["notices"]]
    entries += [{"file": p["metadata"], "sha256": p["metadata_sha256"]} for p in notices["packages"]]
    entries += [{"file": notices["python"]["notice"], "sha256": notices["python"]["sha256"]}]
    entries += [e for a in read("packaging/source-assets.json")["assets"] for e in a["files"]]
    entries += [{"file": e["target"], "sha256": e["sha256"]}
                for a in read("packaging/vendor-lock.json")["artifacts"] for e in a["files"]
                if Path(e["target"]).suffix.lower() not in {".dll", ".exe"}]
    entries += [{"file": "packaging/schemas/spdx-2.3.json", "sha256": read("packaging/build-environment.json")["spdx_schema_sha256"]}]
    entries += [{"file": "licenses/stage7/" + e["file"], "sha256": e["sha256"]} for e in read("licenses/stage7/manifest.json")]
    return list({e["file"]: e for e in entries}.values())


def verify(root=ROOT, all_inputs=False):
    root = Path(root).resolve()
    manifest = json.loads(
        (root / "licenses/stage7/manifest.json").read_text(encoding="utf-8")
    )
    entries = reviewed_files(root) if all_inputs else [
        {**item, "file": "licenses/stage7/" + item["file"]} for item in manifest
    ]
    artifact_root = root / ".test-artifacts"
    artifact_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="license-checkout-", dir=artifact_root
    ) as temp:
        folder = Path(temp)
        gitdir = folder / "git"
        environment = os.environ.copy()
        # An independent Git database/index processes the candidate worktree.
        for key in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        ):
            environment.pop(key, None)

        def git(*args):
            return subprocess.run(
                ["git", *map(str, args)],
                env=environment,
                check=True,
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

        git("init", "--bare", gitdir)
        environment.update(GIT_DIR=str(gitdir), GIT_WORK_TREE=str(root))
        files = [
            ".gitattributes",
            "licenses/stage7/manifest.json",
            *(item["file"] for item in entries),
        ]
        git("-c", "core.autocrlf=true", "add", "--", *files)
        results = []
        for autocrlf in ("true", "false"):
            destination = folder / ("crlf" if autocrlf == "true" else "lf")
            destination.mkdir()
            git(
                "-c",
                "core.autocrlf=" + autocrlf,
                "checkout-index",
                "--all",
                "--prefix=" + destination.as_posix() + "/",
            )
            records = []
            for item in entries:
                data = (destination / item["file"]).read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                records.append(
                    {
                        "file": item["file"],
                        "sha256": digest,
                        "passed": digest == item["sha256"],
                    }
                )
            results.append(
                {
                    "autocrlf": autocrlf,
                    "files": records,
                    "passed": all(r["passed"] for r in records),
                }
            )
        return {
            "status": "passed" if all(r["passed"] for r in results) else "failed",
            "checkouts": results,
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    parser.add_argument("--all-inputs", action="store_true")
    args = parser.parse_args()
    result = verify(all_inputs=args.all_inputs)
    raw = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(raw, encoding="utf-8")
    print(raw)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
