"""Rankers: ridge and LightGBM. Fit on rank-normalised features and rank label."""
from __future__ import annotations
import numpy as np


class Ridge:
    def __init__(self, lam: float = 200.0):
        self.lam = lam

    def fit(self, X, y):
        self.mx, self.my = X.mean(0), y.mean()
        Xc = X - self.mx
        self.w = np.linalg.solve(Xc.T @ Xc + self.lam * np.eye(X.shape[1]), Xc.T @ (y - self.my))
        return self

    def predict(self, X):
        return (X - self.mx) @ self.w + self.my


class LGB:
    def __init__(self, seed: int = 0):
        self.seed = seed

    def fit(self, X, y):
        import lightgbm as lgb
        self.m = lgb.LGBMRegressor(n_estimators=150, learning_rate=0.03, num_leaves=8, min_child_samples=300,
                                   subsample=0.7, subsample_freq=1, colsample_bytree=0.7, reg_lambda=10.0,
                                   random_state=self.seed, n_jobs=4, verbose=-1)
        self.m.fit(X, y)
        return self

    def predict(self, X):
        return self.m.predict(X)
