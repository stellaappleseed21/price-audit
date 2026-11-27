"""Shared helpers for the analysis scripts: read the database and clean data."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from core import ROOT, load_config


def load_mode(mode: str, db: str | None = None, cfg: dict | None = None) -> pd.DataFrame:
    cfg = cfg or load_config()
    path = Path(db) if db else ROOT / cfg["storage"]["db_path"]
    con = sqlite3.connect(path)
    df = pd.read_sql_query("SELECT * FROM loads WHERE mode = ?", con, params=(mode,))
    con.close()
    if df.empty:
        raise SystemExit(f"No data with mode={mode} in database {path}")
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True, format="ISO8601")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["price_c"] = df["price"].round(2)                   # compare in cents to avoid floating-point error
    df["valid"] = (df["blocked"] == 0) & df["price"].notna()
    df["log_price"] = np.log(df["price"])
    return df


def header(t: str):
    print("\n" + "=" * 70 + f"\n{t}\n" + "=" * 70)
