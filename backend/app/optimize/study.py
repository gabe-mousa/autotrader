"""The study runner: the background loop that actually optimizes
(docs/plan/14-optimizer.md §5, §8).

One `StudyRunner` owns one study. Its shape:

    fetch candles ONCE  ->  slice per fold (in memory)
    for each trial:
        sampler suggests params
        poke params into the doc, re-validate + lint  (illegal -> pruned)
        run every fold's train + test slice in the process pool
        constraints -> reject, or score
        feed the result back to the sampler
    post-hoc: stability (plateau vs spike), importance, champion
    evaluate the champion ONCE on the untouched holdout
    re-run the champion across a capital ladder (sensitivity.py)

Two design decisions dominate everything else here:

* **Candles are fetched once per study and sliced in memory.** A 400-trial x
  5-fold study is 4000 backtests; re-fetching per trial would spend all its
  time in SQLite and pandas rather than in the engine.

* **Trials do NOT write `backtest_equity` / `backtest_trades`.** Those are
  per-bar and per-trade rows; 4000 of them would add millions of rows to the
  DB for data nobody opens. Trials persist metrics only. The champion is
  re-run as a normal, fully-persisted backtest on promotion, so it lands in
  the ordinary Backtests UI.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field

import pandas as pd

from ..backtest.engine import BacktestConfig, run_backtest_serialized
from ..logging import get_logger
from ..strategy.hashing import content_hash
from ..strategy.lint import is_warning, lint
from ..strategy.schema import StrategyDocument
from ..strategy.timeframe import BAR_MS, estimate_warmup_bars
from .objective import (
    Objective,
    aggregate_oos_metrics,
    check_constraints,
    metric_value,
    score_trial,
)
from . import sensitivity
from .samplers import Observation, make_sampler, param_importance, stability_scores
from .space import Dimension, apply_params
from .walkforward import Plan

log = get_logger("optimize")


def _iso(ms: int) -> str:
    return pd.Timestamp(ms, unit="ms", tz="UTC").strftime("%Y-%m-%d")


@dataclass
class Progress:
    total: int
    done: int = 0
    rejected: int = 0
    errored: int = 0
    started_at: float = field(default_factory=time.time)
    # starting|searching|holdout|capital|done|cancelled|error
    phase: str = "starting"
    best_score: float | None = None

    def to_json(self) -> dict:
        elapsed = time.time() - self.started_at
        attempted = self.done + self.rejected + self.errored
        eta = (elapsed / attempted) * (self.total - attempted) if attempted else None
        return {"total": self.total, "done": self.done, "rejected": self.rejected,
                "errored": self.errored, "attempted": attempted, "phase": self.phase,
                "elapsed_sec": round(elapsed, 1),
                "eta_sec": round(eta, 1) if eta is not None else None,
                "best_score": self.best_score}


class StudyRunner:
    """Owns one study's execution. Registered in `app.state.optimizer`."""

    def __init__(self, app, *, study_id: str, doc: StrategyDocument, slug: str,
                 dims: list[Dimension], objective: Objective, plan: Plan,
                 base_cfg: BacktestConfig, sampler_name: str, seed: int, n_trials: int):
        self.app = app
        self.study_id = study_id
        self.doc = doc
        self.slug = slug
        self.dims = dims
        self.objective = objective
        self.plan = plan
        self.base_cfg = base_cfg
        self.sampler = make_sampler(sampler_name, dims, seed)
        self.n_trials = n_trials
        self.progress = Progress(total=n_trials)
        self.history: list[Observation] = []
        self._cancelled = False
        self._task: asyncio.Task | None = None

    def cancel(self) -> None:
        """Cooperative — the trial loop checks between trials. Partial results
        are kept; a half-finished study is still evidence, just weaker."""
        self._cancelled = True

    # -- candles -----------------------------------------------------------

    async def _load_candles(self) -> dict[str, pd.DataFrame]:
        """One read per symbol, covering the study's whole span plus warm-up.

        Deliberately goes through `marketdata.store.read` rather than
        `MarketDataService.get_candles`: the service auto-backfills from
        Schwab on a gap, and a study must never trigger network I/O (see
        walkforward.py's module docstring). The planner has already proven the
        data is on disk.
        """
        warmup_ms = estimate_warmup_bars(self.doc) * BAR_MS[self.plan.bar]
        start = self.plan.coverage_start - warmup_ms
        end = self.plan.coverage_end + BAR_MS[self.plan.bar]
        out: dict[str, pd.DataFrame] = {}
        for sym in self.doc.universe.symbols:
            df = await self.app.marketdata.store.read(sym, self.plan.bar, start, end)
            if df is not None and not df.empty:
                out[sym] = df
        if not out:
            raise RuntimeError("no candles could be read for this study's universe")
        return out

    def _slice(self, candles: dict[str, pd.DataFrame], start_ms: int,
               end_ms: int) -> dict[str, pd.DataFrame]:
        """Frames covering [start-warmup, end). Warm-up rows are kept BEFORE
        the slice start for the same reason `_fetch_candles_with_warmup` keeps
        them for single backtests: without them a fold's first bars have NaN
        indicators and silently produce no signals, which would look like "this
        parameter set doesn't trade" rather than "we didn't give it history"."""
        warmup_ms = estimate_warmup_bars(self.doc) * BAR_MS[self.plan.bar]
        lo = pd.Timestamp(start_ms - warmup_ms, unit="ms", tz="UTC")
        hi = pd.Timestamp(end_ms, unit="ms", tz="UTC")
        return {s: df.loc[(df.index >= lo) & (df.index < hi)] for s, df in candles.items()}

    def _cfg_for(self, start_ms: int, end_ms: int) -> BacktestConfig:
        return self.base_cfg.model_copy(update={
            "start": _iso(start_ms),
            # BacktestConfig.end is inclusive-by-date (`df.loc[:end]`), while
            # fold windows are half-open in ms; step back one bar so a fold's
            # last bar is never also the next fold's first.
            "end": _iso(end_ms - BAR_MS[self.plan.bar]),
        })

    # -- one backtest ------------------------------------------------------

    async def _run_slice(self, doc_data: dict, frames: dict[str, pd.DataFrame],
                         start_ms: int, end_ms: int, *,
                         capital: float | None = None) -> dict:
        """Run one window in the process pool and return metrics only.

        `capital` overrides `initial_capital` for that one run. Only the
        capital-sensitivity check uses it — every trial in the search shares
        the study's single configured capital, on purpose (see
        api/optimize._base_cfg).
        """
        cfg = self._cfg_for(start_ms, end_ms)
        if capital is not None:
            cfg = cfg.model_copy(update={"initial_capital": capital})
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            self.app.pool, run_backtest_serialized, doc_data,
            cfg.model_dump(mode="json"), frames)
        return result.metrics or {}

    # -- the loop ----------------------------------------------------------

    async def run(self) -> None:
        try:
            await self._run()
        except asyncio.CancelledError:
            await self._finish("cancelled")
            raise
        except Exception as e:  # noqa: BLE001 — a study must never take the app down
            log.error("study_failed", study_id=self.study_id, error=str(e))
            await self.app.db.execute(
                "UPDATE optimization_studies SET status='error', error=?, finished_at=? "
                "WHERE id=?", (str(e), int(time.time() * 1000), self.study_id))
            self.progress.phase = "error"

    async def _run(self) -> None:
        candles = await self._load_candles()
        base_data = self.doc.model_dump(mode="json", by_alias=True)

        # Slice every fold ONCE up front. All trials reuse these frames.
        fold_frames = [
            {"test": self._slice(candles, f.test_start, f.test_end),
             "train": self._slice(candles, f.train_start, f.train_end),
             "fold": f}
            for f in self.plan.folds
        ]
        self.progress.phase = "searching"

        for n in range(self.n_trials):
            if self._cancelled:
                break
            params = self.sampler.suggest(self.history)
            await self._run_trial(n, params, base_data, fold_frames)

        await self._finalize(candles, base_data)

    async def _run_trial(self, number: int, params: dict, base_data: dict,
                         fold_frames: list[dict]) -> None:
        trial_id = str(uuid.uuid4())
        now = int(time.time() * 1000)
        candidate_data = apply_params(base_data, params)

        # Same validate-then-lint gate the sweep and single-backtest paths
        # use, never skipped. Parameter combinations that are simply illegal
        # (fast MA above slow MA, a stop above the target) are PRUNED as
        # rejected trials rather than crashing the study -- and they feed the
        # sampler's "bad" model, so it learns the shape of the legal region.
        try:
            candidate = StrategyDocument.model_validate(candidate_data)
            errors = [p for p in lint(candidate) if not is_warning(p)]
        except Exception as e:  # noqa: BLE001
            errors = [f"schema: {e}"]
        if errors:
            await self._record_rejected(trial_id, number, params, "; ".join(errors), now)
            return

        oos: list[dict] = []
        ins: list[dict] = []
        try:
            for ff in fold_frames:
                oos.append(await self._run_slice(
                    candidate_data, ff["test"], ff["fold"].test_start, ff["fold"].test_end))
                ins.append(await self._run_slice(
                    candidate_data, ff["train"], ff["fold"].train_start, ff["fold"].train_end))
        except Exception as e:  # noqa: BLE001
            self.progress.errored += 1
            self.history.append(Observation(params=params, score=None))
            await self.app.db.execute(
                "INSERT INTO optimization_trials (id, study_id, trial_number, params_json, "
                "status, reject_reason, created_at) VALUES (?,?,?,?,?,?,?)",
                (trial_id, self.study_id, number, json.dumps(params), "error", str(e), now))
            return

        reason = check_constraints(oos, self.objective.constraints)
        if reason is None:
            scored = score_trial(oos, ins, self.objective)
            if isinstance(scored, str):
                reason = scored
        if reason is not None:
            await self._record_rejected(trial_id, number, params, reason, now,
                                        oos=oos, ins=ins)
            return

        self.progress.done += 1
        if self.progress.best_score is None or scored.score > self.progress.best_score:
            self.progress.best_score = scored.score
        self.history.append(Observation(params=params, score=scored.score))

        await self.app.db.execute(
            "INSERT INTO optimization_trials (id, study_id, trial_number, params_json, status, "
            "score, oos_mean, oos_std, is_mean, folds_json, metrics_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (trial_id, self.study_id, number, json.dumps(params), "done",
             scored.score, scored.oos_mean, scored.oos_std, scored.is_mean,
             json.dumps(self._folds_json(oos, ins)),
             json.dumps(aggregate_oos_metrics(oos)), now))

    def _folds_json(self, oos: list[dict], ins: list[dict]) -> list[dict]:
        m = self.objective.metric
        return [{"index": i,
                 "test": {"score": metric_value(o, m), "trade_count": o.get("trade_count"),
                          "total_return_pct": o.get("total_return_pct"),
                          "max_drawdown_pct": o.get("max_drawdown_pct")},
                 "train": {"score": metric_value(ins[i], m) if i < len(ins) else None,
                           "trade_count": ins[i].get("trade_count") if i < len(ins) else None}}
                for i, o in enumerate(oos)]

    async def _record_rejected(self, trial_id: str, number: int, params: dict,
                               reason: str, now: int, oos: list[dict] | None = None,
                               ins: list[dict] | None = None) -> None:
        """Rejected trials are KEPT, not discarded. The distribution of
        rejection reasons is diagnostic: a study that rejects 90% of its
        trials for "too few trades" is telling the user their search space is
        centered somewhere the strategy barely trades."""
        self.progress.rejected += 1
        self.history.append(Observation(params=params, score=None))
        await self.app.db.execute(
            "INSERT INTO optimization_trials (id, study_id, trial_number, params_json, status, "
            "reject_reason, folds_json, metrics_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (trial_id, self.study_id, number, json.dumps(params), "rejected", reason,
             json.dumps(self._folds_json(oos, ins or [])) if oos else None,
             json.dumps(aggregate_oos_metrics(oos)) if oos else None, now))

    # -- champion + holdout ------------------------------------------------

    async def _finalize(self, candles: dict, base_data: dict) -> None:
        rows = await self.app.db.fetch_all(
            "SELECT id, trial_number, params_json, score FROM optimization_trials "
            "WHERE study_id=? AND status='done'", (self.study_id,))
        if not rows:
            await self._finish("cancelled" if self._cancelled else "done",
                               diagnostics={"note": "no trial passed the constraints"})
            return

        # Stability is computed over the FULL history (scored + rejected) so
        # neighbourhood indices line up with what the sampler saw.
        stab = stability_scores(self.dims, self.history)
        by_number = {int(r["trial_number"]): r for r in rows}
        ranked: list[tuple[float, float, dict]] = []
        for i, obs in enumerate(self.history):
            if obs.score is None or i not in by_number:
                continue
            s = stab[i]
            row = by_number[i]
            await self.app.db.execute(
                "UPDATE optimization_trials SET stability=? WHERE id=?", (s, row["id"]))
            ranked.append((s if s is not None else obs.score, obs.score, dict(row)))

        # Champion by STABILITY-ADJUSTED score, not raw score
        # (docs/plan/14-optimizer.md §6.3). The single best raw score in a
        # large search is, by construction, the luckiest draw.
        ranked.sort(key=lambda t: t[0], reverse=True)
        champ_stability, champ_score, champ_row = ranked[0]
        champ_params = json.loads(champ_row["params_json"])

        self.progress.phase = "holdout"
        holdout = await self._score_holdout(candles, base_data, champ_params)

        self.progress.phase = "capital"
        capital = await self._capital_sensitivity(candles, base_data, champ_params)

        raw_best = max(ranked, key=lambda t: t[1])
        diagnostics = {
            "capital_sensitivity": capital,
            "importance": param_importance(self.dims, self.history),
            "trials_scored": len(ranked),
            "trials_rejected": self.progress.rejected,
            "trials_errored": self.progress.errored,
            "champion_stability": champ_stability,
            "champion_score": champ_score,
            "best_raw_score": raw_best[1],
            "champion_is_best_raw": raw_best[2]["id"] == champ_row["id"],
            "search_span": {"start": self.plan.search_start, "end": self.plan.search_end},
            "warnings": list(self.plan.warnings),
        }
        await self._finish("cancelled" if self._cancelled else "done",
                           champion_trial_id=champ_row["id"], holdout=holdout,
                           diagnostics=diagnostics)

    async def _score_holdout(self, candles: dict, base_data: dict,
                             params: dict) -> dict:
        """The champion's one and only look at the held-out tail.

        This is a report card, not a selection step: nothing downstream may
        re-rank on it. If it is far below the in-study out-of-sample score,
        the study found a fit to its search region, and the UI says so.
        """
        doc_data = apply_params(base_data, params)
        frames = self._slice(candles, self.plan.holdout_start, self.plan.holdout_end)
        try:
            metrics = await self._run_slice(
                doc_data, frames, self.plan.holdout_start, self.plan.holdout_end)
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
        return {
            "metrics": metrics,
            "score": metric_value(metrics, self.objective.metric),
            "metric": self.objective.metric,
            "start": self.plan.holdout_start, "end": self.plan.holdout_end,
            "bars": self.plan.holdout_bars,
        }

    async def _capital_sensitivity(self, candles: dict, base_data: dict,
                                   params: dict) -> dict:
        """Re-run the champion, unchanged, at a geometric ladder of capital
        levels (docs/plan/14-optimizer.md §1).

        Three deliberate choices:

        * **One fixed window for every level** — the search span, not the folds.
          The question is "does the same trading behaviour survive a different
          account size", so the only thing allowed to differ between the runs
          is `initial_capital`. Comparing a 0.5x run on one window against a 1x
          run on another would confound the two.

        * **The holdout is NOT used.** It gets exactly one look, on the
          champion, and that has already happened above. Five more passes over
          it would quietly turn it into another in-sample window.

        * **Candles are the same in-memory frames** the search used. Nothing is
          re-fetched; this costs one backtest per level (5 total) on top of a
          study that already ran thousands.
        """
        doc_data = apply_params(base_data, params)
        start, end = self.plan.search_start, self.plan.search_end
        frames = self._slice(candles, start, end)
        base_capital = float(self.base_cfg.initial_capital)

        # One reference price for the static sizing notes, taken over the same
        # window in a fixed symbol order so the number is reproducible.
        closes: list[float] = []
        for sym in sorted(frames):
            col = frames[sym].get("close")
            if col is not None:
                closes.extend(float(v) for v in col.to_numpy() if v == v)
        ref_px = sensitivity.reference_price(closes)

        try:
            candidate = StrategyDocument.model_validate(doc_data)
        except Exception as e:  # noqa: BLE001
            return {"error": f"champion document is invalid: {e}"}

        levels: list[dict] = []
        for mult, cap in zip(sensitivity.CAPITAL_MULTIPLIERS,
                             sensitivity.capital_levels(base_capital), strict=True):
            entry: dict = {"multiplier": mult, "capital": cap,
                           "notes": sensitivity.sizing_notes(
                               candidate, cap, ref_px,
                               self.base_cfg.commission.per_order)}
            try:
                entry["metrics"] = await self._run_slice(
                    doc_data, frames, start, end, capital=cap)
            except Exception as e:  # noqa: BLE001
                # A single level failing must not cost the user the whole
                # study; the summary reports it as an unknown level.
                entry["metrics"] = None
                entry["error"] = str(e)
            levels.append(entry)

        out = sensitivity.summarize(levels, base_capital=base_capital,
                                    metric=self.objective.metric)
        out["window"] = {"start": start, "end": end}
        out["reference_price"] = ref_px
        return out

    async def _finish(self, status: str, *, champion_trial_id: str | None = None,
                      holdout: dict | None = None, diagnostics: dict | None = None) -> None:
        self.progress.phase = status
        await self.app.db.execute(
            "UPDATE optimization_studies SET status=?, finished_at=?, champion_trial_id=?, "
            "holdout_json=?, diagnostics_json=? WHERE id=?",
            (status, int(time.time() * 1000), champion_trial_id,
             json.dumps(holdout) if holdout else None,
             json.dumps(diagnostics) if diagnostics else None, self.study_id))
        await self.app.db.audit("user", "optimize", {
            "study_id": self.study_id, "slug": self.slug, "status": status,
            "hash": content_hash(self.doc)[:8]})


class OptimizerRegistry:
    """Live studies, keyed by id — the optimizer's analogue of `RunManager`.

    Owns the background tasks so a study outlives the HTTP request that
    started it, and so `GET /studies/{id}` can report live progress without
    round-tripping through the DB on every poll.
    """

    def __init__(self):
        self._runners: dict[str, StudyRunner] = {}

    def start(self, runner: StudyRunner) -> None:
        self._runners[runner.study_id] = runner
        runner._task = asyncio.create_task(runner.run())

    def get(self, study_id: str) -> StudyRunner | None:
        return self._runners.get(study_id)

    def cancel(self, study_id: str) -> bool:
        r = self._runners.get(study_id)
        if r is None:
            return False
        r.cancel()
        return True

    async def shutdown(self) -> None:
        for r in self._runners.values():
            r.cancel()
            if r._task:
                r._task.cancel()
        self._runners.clear()
