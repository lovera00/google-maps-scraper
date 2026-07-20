#!/usr/bin/env python3
"""Entrena y compara modelos que predicen precio USD/m2 de un terreno a partir
de su ubicacion (features espaciales de public.terreno_features).

v2 - mejoras sobre el baseline v1:
    - + superficie_m2 (log) y uso comercial/habitacional como features
    - + feature KNN "precio de vecinos": media del log(usd_m2) de los k listados
      mas cercanos y distancia media a ellos. Se calcula FOLD-SAFE: dentro de
      cada fold solo se usan vecinos del train (nunca del test), y en train se
      excluye el propio punto. Sin fuga.

Compara, bajo la MISMA validacion cruzada ESPACIAL por bloques (~11km):
    - baseline (mediana global)
    - RandomForest (imputacion mediana + flag)
    - HistGradientBoosting (NaN nativo)

Guarda el mejor modelo (+ BallTree de vecinos para prediccion) en
models/terreno_usd_m2.joblib.

Uso:  python scripts/entrenar_modelo.py
"""
import asyncio
import os
from pathlib import Path

import numpy as np
import pandas as pd
import asyncpg
import joblib
from sklearn.ensemble import RandomForestRegressor, HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.neighbors import BallTree
from sklearn.pipeline import Pipeline
from sklearn.model_selection import GroupKFold, KFold
from sklearn.inspection import permutation_importance
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = PROJECT_ROOT / "models"
R_TIERRA_KM = 6371.0
K_VECINOS = 10

FEATURES_BASE = [
    "lat", "lng", "dist_asuncion_km",
    "dist_via_principal_m", "dist_parque_m", "dist_agua_m",
    "dist_hospital_m", "dist_universidad_m",
    "frente_avenida", "cerca_agua",
    "pop_500m", "pop_1km", "pop_2km",
    "biz_500m", "biz_1km", "biz_2km",
    "log_superficie", "uso_comercial",
]
FEATURES_VECINOS = ["vecinos_log_usd_m2", "vecinos_dist_km"]
FEATURES = FEATURES_BASE + FEATURES_VECINOS


def load_env():
    for line in (PROJECT_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def cargar_datos():
    conn = await asyncpg.connect(os.environ["PG_DSN"], timeout=20)
    try:
        rows = await conn.fetch("""
            SELECT id, lat, lng, usd_m2, superficie_m2, uso,
                   dist_asuncion_km, dist_via_principal_m, dist_parque_m,
                   dist_agua_m, dist_hospital_m, dist_universidad_m,
                   frente_avenida, cerca_agua,
                   pop_500m, pop_1km, pop_2km, biz_500m, biz_1km, biz_2km
            FROM public.terreno_features""")
    finally:
        await conn.close()
    df = pd.DataFrame([dict(r) for r in rows])
    for col in df.columns:
        if col in ("id", "uso"):
            continue
        df[col] = pd.to_numeric(df[col].astype(object).where(df[col].notna(), np.nan),
                                errors="coerce")
    df["log_superficie"] = np.log1p(df["superficie_m2"])
    df["uso_comercial"] = df["uso"].map({"comercial": 1.0, "habitacional": 0.0})
    return df


def vecinos_feats(lat_tr, lng_tr, y_log_tr, lat_q, lng_q, k=K_VECINOS, excluir_self=False):
    """Media de log(usd_m2) y distancia media (km) a los k vecinos mas cercanos
    del set de ENTRENAMIENTO. Si excluir_self, descarta el vecino a distancia 0
    (el propio punto, cuando query == train)."""
    tree = BallTree(np.radians(np.c_[lat_tr, lng_tr]), metric="haversine")
    kk = k + 1 if excluir_self else k
    dist, idx = tree.query(np.radians(np.c_[lat_q, lng_q]), k=min(kk, len(lat_tr)))
    if excluir_self:
        dist, idx = dist[:, 1:], idx[:, 1:]
    return y_log_tr[idx].mean(axis=1), dist.mean(axis=1) * R_TIERRA_KM


def metricas(y_true, y_pred):
    return {
        "MAE": mean_absolute_error(y_true, y_pred),
        "MedAE": float(np.median(np.abs(y_true - y_pred))),
        "MAPE%": float(np.mean(np.abs((y_true - y_pred) / y_true)) * 100),
        "RMSE": float(np.sqrt(mean_squared_error(y_true, y_pred))),
        "R2": r2_score(y_true, y_pred),
    }


def cv_eval(model, df, y_log, y_true, splitter, groups=None, con_vecinos=True):
    accum = []
    for tr, te in splitter.split(df, y_log, groups):
        Xtr = df.iloc[tr][FEATURES_BASE].copy()
        Xte = df.iloc[te][FEATURES_BASE].copy()
        if con_vecinos:
            lat_tr, lng_tr = df["lat"].values[tr], df["lng"].values[tr]
            v, d = vecinos_feats(lat_tr, lng_tr, y_log[tr], lat_tr, lng_tr, excluir_self=True)
            Xtr["vecinos_log_usd_m2"], Xtr["vecinos_dist_km"] = v, d
            v, d = vecinos_feats(lat_tr, lng_tr, y_log[tr],
                                 df["lat"].values[te], df["lng"].values[te])
            Xte["vecinos_log_usd_m2"], Xte["vecinos_dist_km"] = v, d
        model.fit(Xtr, y_log[tr])
        pred = np.clip(np.expm1(model.predict(Xte)), 0, None)
        accum.append(metricas(y_true[te], pred))
    return {k: float(np.mean([a[k] for a in accum])) for k in accum[0]}


def fmt(m):
    return (f"MAE ${m['MAE']:6.1f}  MedAE ${m['MedAE']:6.1f}  "
            f"MAPE {m['MAPE%']:5.1f}%  RMSE ${m['RMSE']:6.1f}  R2 {m['R2']:.3f}")


def main():
    load_env()
    MODELS_DIR.mkdir(exist_ok=True)
    df = asyncio.run(cargar_datos())
    y_true = df["usd_m2"].values
    y_log = np.log1p(y_true)
    print(f"Filas: {len(df)} | uso conocido: {df['uso_comercial'].notna().sum()}")

    blk = (np.floor(df["lat"] / 0.1).astype(int).astype(str) + "_" +
           np.floor(df["lng"] / 0.1).astype(int).astype(str))
    groups = blk.values
    print(f"Bloques espaciales: {blk.nunique()}\n")

    spatial = GroupKFold(n_splits=5)
    aleatoria = KFold(n_splits=5, shuffle=True, random_state=42)

    def rf_new():
        return Pipeline([
            ("imp", SimpleImputer(strategy="median", add_indicator=True)),
            ("rf", RandomForestRegressor(n_estimators=400, min_samples_leaf=2,
                                         n_jobs=-1, random_state=42)),
        ])

    def hgb_new():
        return HistGradientBoostingRegressor(max_iter=500, learning_rate=0.05,
                                             random_state=42)

    base = np.full_like(y_true, np.median(y_true))
    print("== BASELINE (mediana global) ==")
    print("  ", fmt(metricas(y_true, base)), "\n")

    print("== CV ESPACIAL sin feature de vecinos (= v1 + superficie/uso) ==")
    print("  RandomForest      ", fmt(cv_eval(rf_new(), df, y_log, y_true, spatial, groups, con_vecinos=False)))
    print("  HistGradientBoost ", fmt(cv_eval(hgb_new(), df, y_log, y_true, spatial, groups, con_vecinos=False)), "\n")

    print("== CV ESPACIAL con feature de vecinos (fold-safe) ==")
    rf_sp = cv_eval(rf_new(), df, y_log, y_true, spatial, groups)
    hgb_sp = cv_eval(hgb_new(), df, y_log, y_true, spatial, groups)
    print("  RandomForest      ", fmt(rf_sp))
    print("  HistGradientBoost ", fmt(hgb_sp), "\n")

    print("== CV ALEATORIA con vecinos (referencia inflada) ==")
    print("  RandomForest      ", fmt(cv_eval(rf_new(), df, y_log, y_true, aleatoria)))
    print("  HistGradientBoost ", fmt(cv_eval(hgb_new(), df, y_log, y_true, aleatoria)), "\n")

    ganador, make = (("RandomForest", rf_new) if rf_sp["MAE"] <= hgb_sp["MAE"]
                     else ("HistGradientBoosting", hgb_new))
    print(f"Ganador (MAE espacial): {ganador}")

    # Importancia en un holdout espacial
    tr, te = next(GroupKFold(5).split(df, y_log, groups))
    Xtr = df.iloc[tr][FEATURES_BASE].copy()
    Xte = df.iloc[te][FEATURES_BASE].copy()
    lat_tr, lng_tr = df["lat"].values[tr], df["lng"].values[tr]
    v, d = vecinos_feats(lat_tr, lng_tr, y_log[tr], lat_tr, lng_tr, excluir_self=True)
    Xtr["vecinos_log_usd_m2"], Xtr["vecinos_dist_km"] = v, d
    v, d = vecinos_feats(lat_tr, lng_tr, y_log[tr], df["lat"].values[te], df["lng"].values[te])
    Xte["vecinos_log_usd_m2"], Xte["vecinos_dist_km"] = v, d
    modelo = make()
    modelo.fit(Xtr, y_log[tr])
    imp = permutation_importance(modelo, Xte, y_log[te], n_repeats=10,
                                 random_state=42, n_jobs=-1)
    orden = np.argsort(imp.importances_mean)[::-1]
    print("\nImportancia de features (permutacion, top 10):")
    for i in orden[:10]:
        print(f"  {FEATURES[i]:22} {imp.importances_mean[i]:.3f}")

    # Modelo final con TODOS los datos (vecinos sin self) + arbol para predecir
    Xall = df[FEATURES_BASE].copy()
    v, d = vecinos_feats(df["lat"].values, df["lng"].values, y_log,
                         df["lat"].values, df["lng"].values, excluir_self=True)
    Xall["vecinos_log_usd_m2"], Xall["vecinos_dist_km"] = v, d
    modelo = make()
    modelo.fit(Xall, y_log)
    out = MODELS_DIR / "terreno_usd_m2.joblib"
    joblib.dump({
        "modelo": modelo, "features": FEATURES, "target": "log1p(usd_m2)",
        "algoritmo": ganador, "n_train": len(df), "k_vecinos": K_VECINOS,
        "vecinos_lat": df["lat"].values, "vecinos_lng": df["lng"].values,
        "vecinos_y_log": y_log,
        "metricas_cv_espacial": rf_sp if ganador.startswith("Random") else hgb_sp,
    }, out)
    print(f"\nModelo guardado en {out}")


if __name__ == "__main__":
    main()
