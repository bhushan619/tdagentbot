import os
import joblib
import pandas as pd
from pathlib import Path

from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    roc_auc_score,
)

from xgboost import XGBClassifier


ROOT = Path(__file__).resolve().parent.parent

DATASET = ROOT / "storage" / "datasets" / "training_data.csv"
MODEL_DIR = ROOT / "ml" / "models"

MODEL_DIR.mkdir(parents=True, exist_ok=True)


FEATURES = [
    "rsi",
    "ema_fast",
    "ema_slow",
    "macd",
    "macd_signal",
    "confidence",
    "candle_size",
    "body_size",

    "atr",
    "ema_distance",

    "upper_wick_ratio",
    "lower_wick_ratio",
]


def load_dataset() -> pd.DataFrame:
    df = pd.read_csv(DATASET)

    print(f"Loaded rows: {len(df)}")

    # only executed trades
    df = df[df["executed"] == True]

    print(f"Executed trades: {len(df)}")

    # remove DRAW / invalid trades
    df = df[df["result"].isin([
        "WIN",
        "LOSS",
    ])]

    print(f"Labeled trades: {len(df)}")

    # target
    df["target"] = df["result"].map({
        "WIN": 1,
        "LOSS": 0,
    })

    # debug missing features
    missing = df[FEATURES].isnull().sum()

    print("\nMissing Values\n")
    print(missing)

    # remove bad rows
    df = df.dropna(subset=FEATURES)
    # remove duplicates
    df = df.drop_duplicates()

    print(f"Clean rows: {len(df)}")
    print("\nTarget Distribution\n")
    print(df["target"].value_counts(normalize=True))

    return df


def train():
    df = load_dataset()

    if len(df) < 70:
        print("Not enough clean rows for training")
        return

    X = df[FEATURES]
    y = df["target"]

    X_train, X_test, y_train, y_test = train_test_split(
        X,
        y,
        test_size=0.2,
        shuffle=True,
        random_state=42,
        stratify=y,
    )

    model = XGBClassifier(
        n_estimators=200,
        max_depth=5,
        learning_rate=0.05,
        subsample=0.9,
        colsample_bytree=0.9,
        objective="binary:logistic",
        eval_metric="logloss",
        random_state=42,
        scale_pos_weight=1,
    )

    print("\nTraining model...\n")

    model.fit(X_train, y_train)

    preds = model.predict(X_test)
    probs = model.predict_proba(X_test)[:, 1]

    acc = accuracy_score(y_test, preds)
    auc = roc_auc_score(y_test, probs)

    print("=" * 60)
    print(f"Accuracy : {acc:.4f}")
    print(f"ROC-AUC  : {auc:.4f}")
    print("=" * 60)

    print("\nClassification Report\n")
    print(classification_report(y_test, preds))

    print("\nConfusion Matrix\n")
    print(confusion_matrix(y_test, preds))

    # feature importance
    importance = pd.DataFrame({
        "feature": FEATURES,
        "importance": model.feature_importances_,
    }).sort_values("importance", ascending=False)

    print("\nFeature Importance\n")
    print(importance)

    # save model
    model_path = MODEL_DIR / "xgb_model.pkl"

    joblib.dump(model, model_path)

    # save metadata
    metadata = {
        "features": FEATURES,
        "rows": len(df),
        "accuracy": float(acc),
        "roc_auc": float(auc),
    }

    joblib.dump(
        metadata,
        MODEL_DIR / "xgb_model_meta.pkl"
    )

    print(f"\nModel saved: {model_path}")


if __name__ == "__main__":
    train()