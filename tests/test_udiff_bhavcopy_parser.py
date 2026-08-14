from __future__ import annotations

import importlib.util
import io
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("technical_replay", ROOT / "scripts/run_technical_replay.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def zip_csv(name: str, text: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, text)
    return out.getvalue()


class BhavcopyParserTests(unittest.TestCase):
    def test_udiff_cm_aliases(self) -> None:
        raw = zip_csv(
            "BhavCopy_NSE_CM_0_0_0_20250131_F_0000.csv",
            "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd\n"
            "2025-01-31,2025-01-31,CM,NSE,STK,12345,INE062A01020,SBIN,EQ,750.00,760.00,745.00,755.00,754.50,749.00,1000000,755000000,50000\n",
        )
        rows = MODULE.parse_bhavcopy(raw)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["symbol"], "SBIN")
        self.assertEqual(row["series"], "EQ")
        self.assertEqual(row["isin"], "INE062A01020")
        self.assertEqual(row["instrument_type"], "STK")
        self.assertEqual(row["close"], 755.0)
        self.assertEqual(row["volume"], 1000000.0)
        self.assertEqual(row["turnover"], 755000000.0)

    def test_legacy_cm_aliases_remain_supported(self) -> None:
        raw = zip_csv(
            "cm31JAN2022bhav.csv",
            "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,TOTTRDQTY,TOTTRDVAL,ISIN\n"
            "SBIN,EQ,500,510,495,505,900000,454500000,INE062A01020\n",
        )
        rows = MODULE.parse_bhavcopy(raw)
        self.assertEqual(rows[0]["symbol"], "SBIN")
        self.assertEqual(rows[0]["close"], 505.0)
        self.assertEqual(rows[0]["isin"], "INE062A01020")


if __name__ == "__main__":
    unittest.main()
