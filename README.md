# RK-MIS — Multibagger Intelligence Engine

Public execution and validation repository for the **RK Multibagger Intelligence System (RK-MIS)**.

## Purpose

RK-MIS is an evidence-first research framework for ranking listed companies for long-horizon multibagger research. It is designed around nine weighted pillars:

| Pillar | Weight |
|---|---:|
| Growth Runway | 12 |
| Business Moat | 12 |
| Financial Quality | 14 |
| Growth Execution | 12 |
| Management & Governance | 12 |
| Smart Money | 10 |
| Future Catalysts | 10 |
| Valuation Feasibility | 10 |
| Technical Accumulation | 8 |

The engine also evaluates 10x / 20x / 50x feasibility and applies an evidence-coverage gate before a company can receive a decision-grade classification.

## Core principles

1. **No missing-data imputation.** Missing evidence lowers coverage; it is never silently converted into a neutral or positive score.
2. **Point-in-time discipline.** Historical predictors must be knowable at the historical anchor date. Future outcomes are joined only after score construction.
3. **Hard governance overrides.** Serious governance red flags can override a high quantitative score.
4. **No post-result tuning.** Bands, weights and admission thresholds are frozen before holdout results are inspected.
5. **Separate discovery from proof.** A high score is a research-prioritisation signal, not a return guarantee.
6. **Sparse qualitative pillars are validated prospectively.** Runway, moat, management, catalysts and similar evidence are not reconstructed with hindsight merely to improve a historical backtest.

## Classification and funnel

Default classification bands:

- DIAMOND: 90+
- A+: 85+
- A: 80+
- B+: 72+
- WATCH: 65+
- REJECT: below 65
- INSUFFICIENT_EVIDENCE: global evidence coverage below the configured minimum
- AVOID: hard governance override

Research funnel:

`Full universe → Top 100 Discovery → Top 30 High Conviction → Top 10 RK Diamond`

## Repository boundary

This public repository contains **code, frozen methodology, deterministic tests and public-safe validation workflows only**.

It intentionally excludes:

- credentials, tokens and secrets;
- private research notes;
- proprietary or personally sensitive material;
- raw exchange/provider datasets where redistribution rights are uncertain;
- private evidence archives.

Historical and live workflows should acquire permissible source data at runtime and publish only public-safe derived artifacts.

## Run locally

```bash
python -m unittest discover -s tests -v
python scripts/run_rk_mie.py --input data/input/company_intelligence.csv
```

## Status

The official 100-point methodology remains evidence-driven. Historical validation has shown the current objective slice to be useful for downside filtering but regime-dependent for upside selection. The next predictive work therefore prioritises the original differentiating pillars: **Growth Runway, Business Moat, Future Catalysts/Execution and high-quality Smart Money evidence**.

## Disclaimer

This is a research system, not investment advice. Historical validation does not guarantee future returns.
