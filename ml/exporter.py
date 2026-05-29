import csv
from pathlib import Path

DATASET_FILE = Path(
    "storage/datasets/training_data.csv"
)

HEADER = [

    "timestamp",

    "open",
    "high",
    "low",
    "close",
    "volume",

    "rsi",
    "ema_fast",
    "ema_slow",

    "macd",
    "macd_signal",

    "signal",
    "confidence",

    "executed",
    "rejection_reason",

    "result",
    "pnl",

    "future_close",
    "future_move",

    "candle_size",
    "body_size",

    "atr",
    "ema_distance",

    "upper_wick_ratio",
    "lower_wick_ratio",
    
    "momentum_accel",
]

def append_training_row(row: dict):

    exists = DATASET_FILE.exists()

    with open(
        DATASET_FILE,
        "a",
        newline="",
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=HEADER,
        )

        if not exists:
            writer.writeheader()

        writer.writerow(row)