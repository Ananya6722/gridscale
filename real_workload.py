#!/usr/bin/env python3
"""Self-contained demo workload: pure-Python batch gradient descent.

Generates 50,000 synthetic grid-telemetry rows, fits a linear model of carbon
intensity over 6 epochs, then saves the learned weights and the region the
job ran in to trained_grid_model.json. Standard library only.
"""
import json
import os
import random
import time

N_ROWS = 50_000
EPOCHS = 6
LEARNING_RATE = 0.2
OUTPUT_FILE = "trained_grid_model.json"
FEATURES = ["demand_gw", "wind_share", "solar_share"]


def generate_rows(n, seed=42):
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        demand = rng.uniform(20.0, 60.0)
        wind = rng.uniform(0.0, 0.6)
        solar = rng.uniform(0.0, 0.3)
        intensity = (450 + 4.0 * (demand - 40.0) - 600.0 * wind - 400.0 * solar
                     + rng.gauss(0.0, 12.0))
        rows.append(([demand, wind, solar], intensity))
    return rows


def standardize(rows):
    k = len(FEATURES)
    n = len(rows)
    means = [sum(r[0][j] for r in rows) / n for j in range(k)]
    stds = [(sum((r[0][j] - means[j]) ** 2 for r in rows) / n) ** 0.5 for j in range(k)]
    y_mean = sum(r[1] for r in rows) / n
    y_std = (sum((r[1] - y_mean) ** 2 for r in rows) / n) ** 0.5
    xs = [[(r[0][j] - means[j]) / stds[j] for j in range(k)] for r in rows]
    ys = [(r[1] - y_mean) / y_std for r in rows]
    return xs, ys


def train(xs, ys, epochs=EPOCHS, lr=LEARNING_RATE):
    k = len(xs[0])
    n = len(xs)
    w = [0.0] * k
    b = 0.0
    history = []
    for epoch in range(1, epochs + 1):
        grad_w = [0.0] * k
        grad_b = 0.0
        sq_err = 0.0
        for x, y in zip(xs, ys):
            err = sum(wj * xj for wj, xj in zip(w, x)) + b - y
            sq_err += err * err
            for j in range(k):
                grad_w[j] += err * x[j]
            grad_b += err
        w = [wj - lr * 2.0 * g / n for wj, g in zip(w, grad_w)]
        b -= lr * 2.0 * grad_b / n
        mse = sq_err / n
        history.append(mse)
        print(f"epoch {epoch}/{epochs}  mse={mse:.5f}")
    return w, b, history


def main():
    region = os.environ.get("GRIDSCALE_TARGET_REGION", "local-default (Unscheduled)")
    print(f"Running in region: {region}")
    start = time.time()
    rows = generate_rows(N_ROWS)
    print(f"Generated {len(rows):,} synthetic telemetry rows")
    xs, ys = standardize(rows)
    w, b, history = train(xs, ys)
    result = {
        "active_region": region,
        "rows": len(rows),
        "epochs": EPOCHS,
        "features": FEATURES,
        "weights": dict(zip(FEATURES, w)),
        "bias": b,
        "final_mse": history[-1],
        "runtime_seconds": round(time.time() - start, 3),
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    print(f"Saved model to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()