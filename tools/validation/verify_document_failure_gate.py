"""Prove injected native errors fail the top-level self-test, retaining evidence."""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    from magic_converter.core.diagnostics.document_diagnostics import check_documents
    from magic_converter.core.diagnostics.diagnostics import run_self_test

    def injected(root, *, test_native=False):
        pass

    with patch(
        "magic_converter.core.diagnostics.document_diagnostics.check_documents",
        side_effect=injected,
    ):
        code = run_self_test(args.report)
    report = json.loads(args.report.read_text(encoding="utf-8"))
    documents = report.get("documents", {})
    errors = [
        v.get("error")
        for v in documents.get("native", {}).values()
        if isinstance(v, dict)
    ]
    passed = (
        code == 1
        and report.get("status") == "failed"
        and documents.get("status") == "failed"
        and len(errors) == 4
        and all(e == "Injected native conversion failure" for e in errors)
    )
    print(
        json.dumps(
            {
                "gate_verification": "passed" if passed else "failed",
                "self_test_exit": code,
                "self_test_status": report.get("status"),
                "native_errors": errors,
                "report": str(args.report.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
