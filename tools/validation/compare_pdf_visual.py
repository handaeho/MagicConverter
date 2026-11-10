"""Compare two PDFs with explicit geometry/pixel gates; exit 1 for differences."""
import argparse
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from magic_converter.core.diagnostics.visual_comparison import compare


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('reference', type=Path); parser.add_argument('candidate', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--dpi', type=int, default=96)
    parser.add_argument('--mae-limit', type=float, default=1.0)
    parser.add_argument('--changed-limit', type=float, default=.005)
    parser.add_argument('--artifacts', type=Path)
    args = parser.parse_args()
    if args.report.resolve() in {args.reference.resolve(), args.candidate.resolve()}:
        parser.error('Report must not overwrite an input')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    result = {'status':'failed', 'error':'Comparison did not complete'}
    args.report.write_text(json.dumps(result), encoding='utf-8')
    code = 2
    try:
        result = compare(args.reference, args.candidate, dpi=args.dpi, mae_limit=args.mae_limit,
                         changed_limit=args.changed_limit, artifacts=args.artifacts)
        code = 0 if result['status'] == 'matched' else 1
    except Exception as error: result = {'status':'failed', 'error':str(error)}
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status':result['status'], 'report':str(args.report)}, ensure_ascii=False))
    return code


if __name__ == '__main__': raise SystemExit(main())
