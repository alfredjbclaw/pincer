#!/usr/bin/env python3
"""Run Pincer over a SWE-bench subset and (optionally) grade it.

    python3 -m tools.bench.run_lite \
        --dataset-name princeton-nlp/SWE-bench_Lite --limit 5 \
        --work-root /tmp/pincer-bench --out preds.jsonl \
        --samples 3 --max-revise-iters 3

Add --grade to invoke the official harness afterward (preflight-gated: it will
refuse / warn loudly without Docker, and warn that arm64 numbers are
non-canonical). Always runs the gold sanity check before grading unless
--skip-gold.

Predictions are written incrementally so a long run is resumable-by-hand and a
crash never loses completed instances.
"""
from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS.parent))

import parallel_orchestrator as po  # for SelectionTuning
from . import dataset as ds
from . import predictions as pr
from . import grade as gr
from . import runner as rn


def _tuning(args) -> po.SelectionTuning:
    base = po.SelectionTuning.load()
    return dataclasses.replace(
        base,
        samples=args.samples if args.samples is not None else base.samples,
        max_revise_iters=(args.max_revise_iters if args.max_revise_iters is not None
                          else base.max_revise_iters),
        repro_tests=args.repro_tests if args.repro_tests is not None else base.repro_tests,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Run Pincer over SWE-bench + grade")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--dataset-file", help="local .json/.jsonl of instances")
    src.add_argument("--dataset-name", default=ds.LITE, help="HF dataset (needs `datasets`)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--work-root", default="/tmp/pincer-bench")
    ap.add_argument("--out", default="preds.jsonl")
    ap.add_argument("--samples", type=int, default=None)
    ap.add_argument("--max-revise-iters", type=int, default=None)
    ap.add_argument("--repro-tests", dest="repro_tests", action="store_true", default=None)
    ap.add_argument("--no-repro-tests", dest="repro_tests", action="store_false")
    ap.add_argument("--no-sandbox", action="store_true",
                    help="skip Pincer's internal sandbox ranking (faster, weaker selection)")
    ap.add_argument("--use-hints", action="store_true",
                    help="feed dataset hints_text to the worker (borderline; off by default)")
    ap.add_argument("--grade", action="store_true", help="grade with the official harness after")
    ap.add_argument("--skip-gold", action="store_true", help="skip the gold sanity check")
    ap.add_argument("--run-id", default="pincer_lite")
    ap.add_argument("--max-workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true", help="print grade commands, don't execute")
    args = ap.parse_args()

    instances = ds.load(args.dataset_file, dataset_name=args.dataset_name, limit=args.limit)
    print(f"[bench] {len(instances)} instance(s) loaded")
    tuning = _tuning(args)
    work_root = Path(args.work_root)
    work_root.mkdir(parents=True, exist_ok=True)

    preds = []
    for i, inst in enumerate(instances, 1):
        try:
            p = rn.run_instance(inst, work_root=work_root, tuning=tuning,
                                use_sandbox=not args.no_sandbox, use_hints=args.use_hints)
        except Exception as e:
            print(f"[bench] {inst.instance_id}: ERROR {e}")
            p = pr.Prediction(inst.instance_id, "", "pincer")
        preds.append(p)
        pr.write_jsonl(preds, args.out)  # incremental
        ok = "patch" if p.model_patch.strip() else "EMPTY"
        print(f"[bench] [{i}/{len(instances)}] {inst.instance_id}: {ok}")

    print(f"[bench] wrote {len(preds)} prediction(s) -> {args.out}")

    if not args.grade:
        print("[bench] grading skipped (pass --grade). Plumbing done.")
        return 0

    problems = gr.preflight()
    for prob in problems:
        print(f"[bench][preflight] {prob}")
    if any("Docker" in p for p in problems) and not args.dry_run:
        print("[bench] cannot grade without Docker — aborting grade (predictions still written).")
        return 1

    if not args.skip_gold:
        print("[bench] gold sanity run (must resolve ~100%) ...")
        gr.run_evaluation(gr.gold_sanity_argv(args.dataset_name, max_workers=args.max_workers),
                          dry_run=args.dry_run)
    print("[bench] grading predictions ...")
    rc = gr.run_evaluation(
        gr.grade_argv(args.out, dataset_name=args.dataset_name, run_id=args.run_id,
                      max_workers=args.max_workers),
        dry_run=args.dry_run)
    return rc


if __name__ == "__main__":
    sys.exit(main())
