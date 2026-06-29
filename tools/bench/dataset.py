#!/usr/bin/env python3
"""SWE-bench instance loading.

Loads instances from a local JSON/JSONL file (no dependency) OR, if the
`datasets` package is installed, straight from Hugging Face. Local-first so the
plumbing is fully usable and testable without pulling heavyweight deps onto a
machine that can't yet grade (no Docker).

Only the fields Pincer needs to PRODUCE a patch are surfaced; the gold `patch`
and `test_patch` are deliberately NOT exposed to the worker (that would be
leakage) — they live only in the official grader's copy of the dataset.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import List, Optional

LITE = "princeton-nlp/SWE-bench_Lite"
VERIFIED = "princeton-nlp/SWE-bench_Verified"


@dataclasses.dataclass(frozen=True)
class Instance:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    hints_text: str = ""
    version: str = ""
    environment_setup_commit: str = ""
    fail_to_pass: tuple = ()
    pass_to_pass: tuple = ()

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.repo}.git"

    @classmethod
    def from_row(cls, row: dict) -> "Instance":
        return cls(
            instance_id=row["instance_id"],
            repo=row["repo"],
            base_commit=row["base_commit"],
            problem_statement=row.get("problem_statement", ""),
            hints_text=row.get("hints_text", "") or "",
            version=str(row.get("version", "") or ""),
            environment_setup_commit=row.get("environment_setup_commit", "") or "",
            fail_to_pass=tuple(_as_list(row.get("FAIL_TO_PASS"))),
            pass_to_pass=tuple(_as_list(row.get("PASS_TO_PASS"))),
        )


def _as_list(v) -> list:
    """SWE-bench encodes FAIL_TO_PASS / PASS_TO_PASS as a JSON string in the HF
    parquet, but as a real list in hand-authored JSON. Accept both."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, list) else [v]
        except (json.JSONDecodeError, ValueError):
            return [v]
    return list(v)


def load_local(path: str, limit: Optional[int] = None) -> List[Instance]:
    """Load instances from a .jsonl (one object per line) or .json (a list)."""
    text = Path(path).read_text()
    rows: List[dict]
    if path.endswith(".jsonl"):
        rows = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    else:
        data = json.loads(text)
        rows = data if isinstance(data, list) else [data]
    insts = [Instance.from_row(r) for r in rows]
    return insts[:limit] if limit else insts


def load_hf(dataset_name: str = LITE, split: str = "test",
            limit: Optional[int] = None) -> List[Instance]:
    """Load from Hugging Face. Requires the optional `datasets` package."""
    try:
        from datasets import load_dataset
    except ImportError as e:  # pragma: no cover - exercised only without datasets
        raise RuntimeError(
            "the 'datasets' package is required to load from Hugging Face; "
            "either `pip install datasets` or pass a local --dataset-file"
        ) from e
    ds = load_dataset(dataset_name, split=split)
    rows = ds.select(range(limit)) if limit else ds
    return [Instance.from_row(dict(r)) for r in rows]


def load(source: Optional[str] = None, *, dataset_name: str = LITE,
         limit: Optional[int] = None) -> List[Instance]:
    """Unified entry: a local file path if `source` looks like one, else HF."""
    if source and (source.endswith(".json") or source.endswith(".jsonl")):
        return load_local(source, limit=limit)
    return load_hf(dataset_name=dataset_name, limit=limit)
