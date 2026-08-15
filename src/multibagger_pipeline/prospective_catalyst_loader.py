from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from .prospective_catalysts import ProspectiveCatalystStore


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_reviewed_catalyst_ledgers(
    store: ProspectiveCatalystStore,
    ledger_paths: Iterable[str | Path],
    *,
    scoring_as_of: str | None = None,
) -> dict[str, Any]:
    loaded: list[dict[str, Any]] = []
    order_rows = 0
    capex_rows = 0
    seen = set()
    for raw in ledger_paths:
        path = Path(raw)
        resolved = str(path.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("status") != "REVIEWED_PRIMARY_SOURCE_CATALYST_EVIDENCE":
            raise ValueError(f"ledger is not reviewed catalyst evidence: {path}")
        as_of = str(payload.get("as_of_date") or "")[:10]
        if not as_of:
            raise ValueError(f"catalyst ledger missing as_of_date: {path}")
        if scoring_as_of and as_of > str(scoring_as_of)[:10]:
            raise ValueError(f"catalyst ledger {as_of} is after scoring date {str(scoring_as_of)[:10]}: {path}")
        orders = payload.get("orders") or []
        capex = payload.get("capex") or []
        if not isinstance(orders, list) or not isinstance(capex, list):
            raise ValueError(f"orders and capex must be lists: {path}")
        for row in orders:
            clean = {k: v for k, v in row.items() if k != "review_note"}
            store.add_order(clean)
            order_rows += 1
        for row in capex:
            clean = {k: v for k, v in row.items() if k != "review_note"}
            store.add_capex(clean)
            capex_rows += 1
        loaded.append({
            "path": str(path), "sha256": _sha256(path), "as_of_date": as_of,
            "orders": len(orders), "capex": len(capex),
        })
    chain = hashlib.sha256(
        "\n".join(f"{x['path']}|{x['sha256']}" for x in loaded).encode("utf-8")
    ).hexdigest()
    return {
        "ledgers": loaded,
        "ledger_count": len(loaded),
        "order_rows": order_rows,
        "capex_rows": capex_rows,
        "ledger_chain_sha256": chain,
        "scoring_as_of": None if scoring_as_of is None else str(scoring_as_of)[:10],
        "missing_data_imputed": False,
    }
