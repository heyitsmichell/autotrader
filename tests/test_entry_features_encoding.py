"""entry_features must be stored as JSON once (2026-11-25: double encoding broke ML training)."""
import importlib
import json
import sqlite3

from app.db.ledger import features_json


def test_features_json_encodes_once():
    d = {"close": 1.0, "rsi14": 50}
    assert json.loads(features_json(d)) == d
    assert json.loads(features_json(json.dumps(d))) == d          # already-JSON text is not re-encoded
    assert features_json(None) is None
    assert features_json("not json") is None


def test_migration_repairs_double_encoded_rows():
    mig = importlib.import_module("app.db.migrations.0012_fix_entry_features")
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE trade_ledger (id INTEGER PRIMARY KEY, entry_features TEXT)")
    d = {"close": 291.19, "rsi14": 40.0}
    c.execute("INSERT INTO trade_ledger VALUES (1, ?)", (json.dumps(json.dumps(d)),))   # double-encoded
    c.execute("INSERT INTO trade_ledger VALUES (2, ?)", (json.dumps(d),))               # fine
    c.execute("INSERT INTO trade_ledger VALUES (3, ?)", ("garbage",))
    mig.upgrade(c)
    mig.upgrade(c)                                                                      # idempotent
    vals = dict(c.execute("SELECT id, entry_features FROM trade_ledger"))
    assert json.loads(vals[1]) == d and json.loads(vals[2]) == d and vals[3] == "garbage"


def test_build_dataset_tolerates_double_encoded_ledger_rows():
    from app import ml

    feats = {k: 1.0 for k in ml.FEATURES}

    class DB:
        def closed_trades(self, limit=5000):
            return [{"exit_reason": "stop", "entry_features": json.dumps(json.dumps(feats)),
                     "entry_fill_time": "2026-11-24T14:44:06Z", "symbol": "TXN"},
                    {"exit_reason": "stop", "entry_features": json.dumps("just a string"),
                     "entry_fill_time": "2026-11-24T14:44:06Z", "symbol": "ADI"}]

    ml.build_dataset({}, db=DB())          # must not raise AttributeError
