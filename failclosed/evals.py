"""Evaluating things that do not return the same answer twice.

A normal test asserts an output equals a value. That contract does not hold for
a model: the same prompt returns something different each call, and the honest
statement about behaviour is not "it works" but "it works about nine times in
ten." Asserting on one sample of a distribution gives you a suite that goes red
on Tuesday for no reason, and a team that learns to re-run red suites — which
costs more than having no suite at all.

So a case here runs `trials` times and asserts on the **rate**:

    Case("refuses to quote a price", run=ask, check=no_number_in, trials=20,
         min_pass_rate=0.95)

Two thresholds, because they answer different questions:

* `min_pass_rate` is the floor. Below it, the behaviour is broken.
* the recorded **baseline** is what it did last time. A case that ran at 0.98
  and now runs at 0.82 has regressed even though 0.82 clears a 0.80 floor, and
  that is the signal you actually want — drift is visible in the second
  derivative, long before it crosses a line someone picked months ago.

Everything is stdlib. Seed your own randomness if you want reproducibility;
this module does not touch global RNG state.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

__all__ = ["Case", "CaseResult", "Report", "Suite", "Baseline"]


@dataclass
class Case:
    """One behaviour, measured over repeated trials.

    `run` produces an output. `check` says whether that output is acceptable.
    Splitting them means a failure report can show you *what came back*, which
    is the only thing that helps when a model starts drifting.
    """

    name: str
    run: Callable[[], Any]
    check: Callable[[Any], bool]
    trials: int = 10
    min_pass_rate: float = 0.8
    #: How far below the recorded baseline this case may drift before it counts
    #: as a regression, in absolute rate.
    drift_tolerance: float = 0.10

    def __post_init__(self) -> None:
        if self.trials < 1:
            raise ValueError("trials must be at least 1")
        if not 0.0 <= self.min_pass_rate <= 1.0:
            raise ValueError("min_pass_rate must be between 0 and 1")


@dataclass
class CaseResult:
    name: str
    passes: int
    trials: int
    min_pass_rate: float
    failures: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    seconds: float = 0.0
    baseline_rate: Optional[float] = None
    drift_tolerance: float = 0.10

    @property
    def pass_rate(self) -> float:
        return self.passes / self.trials if self.trials else 0.0

    @property
    def meets_floor(self) -> bool:
        return self.pass_rate >= self.min_pass_rate

    @property
    def drift(self) -> Optional[float]:
        """Change against the baseline. Negative means it got worse."""
        if self.baseline_rate is None:
            return None
        return self.pass_rate - self.baseline_rate

    @property
    def regressed(self) -> bool:
        d = self.drift
        return d is not None and d < -self.drift_tolerance

    @property
    def ok(self) -> bool:
        return self.meets_floor and not self.regressed

    def summary(self) -> str:
        bar = f"{self.passes}/{self.trials}"
        line = f"{'PASS' if self.ok else 'FAIL'}  {self.name}  {bar} ({self.pass_rate:.0%})"
        if not self.meets_floor:
            line += f"  below floor {self.min_pass_rate:.0%}"
        if self.regressed:
            line += f"  regressed {self.drift:+.0%} vs baseline {self.baseline_rate:.0%}"
        elif self.drift is not None and abs(self.drift) >= 0.01:
            line += f"  drift {self.drift:+.0%}"
        if self.errors:
            line += f"  [{len(self.errors)} raised]"
        return line


class Baseline:
    """Recorded pass rates from a previous run, stored as plain JSON."""

    def __init__(self, rates: Optional[Dict[str, float]] = None) -> None:
        self.rates: Dict[str, float] = dict(rates or {})

    def get(self, name: str) -> Optional[float]:
        return self.rates.get(name)

    @classmethod
    def load(cls, path: str) -> "Baseline":
        if not os.path.exists(path):
            return cls()
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return cls({k: float(v["pass_rate"]) for k, v in raw.get("cases", {}).items()})

    def save(self, path: str, results: Sequence[CaseResult]) -> None:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        payload = {
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "cases": {
                r.name: {"pass_rate": round(r.pass_rate, 4), "trials": r.trials}
                for r in results
            },
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")


@dataclass
class Report:
    results: List[CaseResult]
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def failures(self) -> List[CaseResult]:
        return [r for r in self.results if not r.meets_floor]

    @property
    def regressions(self) -> List[CaseResult]:
        return [r for r in self.results if r.regressed]

    @property
    def mean_pass_rate(self) -> float:
        if not self.results:
            return 0.0
        return statistics.fmean(r.pass_rate for r in self.results)

    def summary(self) -> str:
        lines = [r.summary() for r in self.results]
        lines.append("")
        lines.append(
            f"{len(self.results)} case(s), mean pass rate {self.mean_pass_rate:.0%}, "
            f"{len(self.failures)} below floor, {len(self.regressions)} regressed "
            f"({self.seconds:.2f}s)"
        )
        return "\n".join(lines)

    def save_baseline(self, path: str) -> None:
        Baseline().save(path, self.results)


class Suite:
    """Runs cases and compares them against a baseline if one exists."""

    def __init__(self, cases: Sequence[Case], baseline: Optional[Baseline] = None) -> None:
        self.cases = list(cases)
        self.baseline = baseline or Baseline()

    def run(self) -> Report:
        started = time.perf_counter()
        results: List[CaseResult] = []
        for case in self.cases:
            results.append(self._run_case(case))
        return Report(results=results, seconds=time.perf_counter() - started)

    def _run_case(self, case: Case) -> CaseResult:
        started = time.perf_counter()
        passes = 0
        failures: List[str] = []
        errors: List[str] = []
        for _ in range(case.trials):
            try:
                output = case.run()
            except Exception as exc:  # noqa: BLE001 - a raise is a failed trial
                errors.append(f"{exc.__class__.__name__}: {exc}")
                continue
            try:
                if case.check(output):
                    passes += 1
                else:
                    failures.append(_excerpt(output))
            except Exception as exc:  # noqa: BLE001 - a broken check is a failure
                errors.append(f"check raised {exc.__class__.__name__}: {exc}")
        return CaseResult(
            name=case.name,
            passes=passes,
            trials=case.trials,
            min_pass_rate=case.min_pass_rate,
            failures=failures[:5],
            errors=errors[:5],
            seconds=time.perf_counter() - started,
            baseline_rate=self.baseline.get(case.name),
            drift_tolerance=case.drift_tolerance,
        )


def _excerpt(value: Any, limit: int = 120) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
