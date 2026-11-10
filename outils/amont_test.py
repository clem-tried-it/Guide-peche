#!/usr/bin/env python3
"""Test : les débits mesurés en amont (France, Hub'Eau) améliorent-ils l'estimation à Chooz et Landelies ?
Lancé par GitHub Actions (workflow amont.yml). Écrit data/amont/*.csv et data/amont/resultat.md."""
import json
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "amont"
OUT.mkdir(parents=True, exist_ok=True)
HUB = "https://hubeau.eaufrance.fr/api/v2/hydrometrie"
AMONT = {
    "Chooz": {"Montherme": "B700001002", "Aiglemont": "B560010101", "Charleville": "B540001001",
              "Sedan": "B502001001", "Stenay": "B315002001"},
    "Landelies": {"Marpent": "D019223001", "Maubeuge": "D019801101", "Berlaimont": "D016221001",
                  "HelpeMaj_Taisnieres": "D015652001", "HelpeMin_Maroilles": "D013702001", "Solre_Ferriere": "D020601001"},
}
L = []


def say(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    L.append(s)


def hub(path, **params):
    rows, url, p = [], f"{HUB}/{path}", dict(params, size=20000)
    for _ in range(30):
        for essai in range(4):
            try:
                r = httpx.get(url, params=p, timeout=120)
                if r.status_code in (200, 206):
                    break
            except Exception as e:
                print("essai", essai, e, flush=True)
            time.sleep(5 * (essai + 1))
        else:
            raise RuntimeError(f"Hub'Eau ne répond pas : {url}")
        js = r.json()
        rows += js.get("data", [])
        url, p = js.get("next"), None
        if not url:
            break
    return rows


def journalier(code):
    rows = hub("obs_elab", code_entite=code, grandeur_hydro_elab="QmnJ", date_debut_obs_elab="2021-11-01")
    if not rows:
        return pd.Series(dtype=float), None
    df = pd.DataFrame(rows)
    s = df.groupby("date_obs_elab")["resultat_obs_elab"].mean() / 1000.0
    s.index = pd.to_datetime(s.index)
    return s.sort_index(), list(df.columns)


def temps_reel(code):
    rows = hub("observations_tr", code_entite=code, grandeur_hydro="Q", timestep=60,
               date_debut_obs=(pd.Timestamp.utcnow() - pd.Timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows)
    s = pd.Series(df["resultat_obs"].values / 1000.0, index=pd.to_datetime(df["date_obs"], utc=True))
    return s[~s.index.duplicated()].sort_index().resample("1h").mean()


def feats(lq, R, api, doy, t, h, up):
    cs, sn = np.cos(2 * np.pi * doy[t] / 365.25), np.sin(2 * np.pi * doy[t] / 365.25)
    fut = R[t + 1:t + h + 1].sum()
    r3 = R[t] + R[t - 1] + R[t - 2]
    f = [1.0, lq[t], lq[t] - lq[t - 1], R[t], R[t - 1], R[t - 2], R[t - 3], R[t - 4], api[t], fut, fut * api[t] / 10,
         fut * lq[t], r3 * lq[t], cs, sn, fut * cs, fut * sn]
    for u in up:
        f += [u[t] - u[t - 1], u[t - 1] - u[t - 2], u[t] - lq[t]]
    return f


def backtest(station, hist, ups):
    col = "PluieS" if station == "Landelies" else "PluieM"
    df = pd.DataFrame({"q": hist[station], "r": hist[col]}).asfreq("D")
    df["q"] = df["q"].interpolate(limit=5)
    df["r"] = df["r"].fillna(0)
    for n, s in ups.items():
        df[n] = np.log(s.reindex(df.index).interpolate(limit=3).clip(lower=0.01))
    lq, R, doy = np.log(df["q"].values), df["r"].values, df.index.dayofyear.values
    api = np.zeros(len(R))
    for i in range(1, len(R)):
        api[i] = 0.85 * api[i - 1] + R[i]
    split = df.index.get_indexer([pd.Timestamp("2024-06-01")])[0]
    variantes = {"sans amont": []} | {f"+ {n}": [n] for n in ups} | ({"+ toutes": list(ups)} if len(ups) > 1 else {})
    say(f"\n### {station} — erreur médiane (%), test juin 2024 → aujourd'hui, pluie réelle connue pour toutes les variantes")
    say("\n| Variante | J+1 | J+2 | J+3 | J+5 | J+7 | J+1 montées | J+2 montées | J+3 montées | biais montées J+2 | jours test |")
    say("|---|---|---|---|---|---|---|---|---|---|---|")
    res = {}
    for nom, cols in variantes.items():
        up = [df[c].values for c in cols]
        line, n_te = [], 0
        ext = {}
        for h in (1, 2, 3, 5, 7):
            Xtr, ytr, Xte, yte = [], [], [], []
            for t in range(5, len(lq) - h):
                x = feats(lq, R, api, doy, t, h, up)
                y = lq[t + h] - lq[t]
                if not np.isfinite(x).all() or not np.isfinite(y):
                    continue
                (Xtr if t + h < split else Xte).append(x)
                (ytr if t + h < split else yte).append(y)
            Xtr, ytr, Xte, yte = map(np.array, (Xtr, ytr, Xte, yte))
            if len(Xtr) < 200 or len(Xte) < 100:
                line.append("n/d")
                continue
            lam = 1e-3 * np.eye(Xtr.shape[1])
            coef = np.linalg.solve(Xtr.T @ Xtr + lam, Xtr.T @ ytr)
            pred = np.clip(Xte @ coef, -1.0, 2.5)
            err = np.abs(np.exp(pred - yte) - 1) * 100
            line.append(f"{np.median(err):.1f}")
            n_te = len(yte)
            m = yte > np.log(1.2)
            if h <= 3 and m.sum() >= 10:
                ext[h] = (np.median(err[m]), np.median(np.exp(pred[m] - yte[m]) - 1) * 100, int(m.sum()))
            res[(nom, h)] = float(np.median(err))
        mont = [f"{ext[h][0]:.0f} (n={ext[h][2]})" if h in ext else "n/d" for h in (1, 2, 3)]
        biais = f"{ext[2][1]:+.0f} %" if 2 in ext else "n/d"
        say(f"| {nom} | " + " | ".join(line) + " | " + " | ".join(mont) + f" | {biais} | {n_te} |")
    return res


def decalage(station, rel, tr):
    """Avance de l'amont sur l'aval : décalage (h) qui maximise la corrélation des variations sur 6 h (30 derniers jours)."""
    a = rel[station].dropna()
    say(f"\n### {station} — avance horaire de l'amont (30 derniers jours)")
    say("\n| Station amont | heures de données | dernier relevé (UTC) | meilleur décalage (h) | corrélation | rapport médian aval/amont |")
    say("|---|---|---|---|---|---|")
    for n, s in tr.items():
        s = s.dropna()
        if len(s) < 100:
            say(f"| {n} | {len(s)} | {s.index.max() if len(s) else '-'} | n/d | n/d | n/d |")
            continue
        da = np.log(a).diff(6)
        best = (None, -9)
        for lag in range(0, 73):
            du = np.log(s.clip(lower=0.01)).diff(6).shift(lag, freq="h")
            j = pd.concat([da, du], axis=1, join="inner").dropna()
            if len(j) > 100:
                c = j.corr().iloc[0, 1]
                if c > best[1]:
                    best = (lag, c)
        j = pd.concat([a, s], axis=1, join="inner").dropna()
        ratio = (j.iloc[:, 0] / j.iloc[:, 1]).median() if len(j) else float("nan")
        say(f"| {n} | {len(s)} | {s.index.max():%d/%m %H:%M} | {best[0]} | {best[1]:.2f} | {ratio:.2f} |")


def main():
    hist = pd.read_csv(ROOT / "data" / "historique.csv", parse_dates=["Date"]).set_index("Date").sort_index()
    hist = hist.apply(pd.to_numeric, errors="coerce")
    rel = pd.read_csv(ROOT / "data" / "releves.csv")
    rel.index = pd.to_datetime(rel["Date"]).dt.tz_localize("Europe/Brussels", ambiguous="NaT", nonexistent="NaT").dt.tz_convert("UTC")
    rel = rel[rel.index.notna()]
    rel = rel[~rel.index.duplicated()].apply(pd.to_numeric, errors="coerce")
    say(f"# Test des débits amont (Hub'Eau) — {pd.Timestamp.utcnow():%d/%m/%Y %H:%M} UTC")
    jour, heure = {}, {}
    say("\n## Disponibilité des données\n")
    say("| Aval | Station amont | code | jours (QmnJ) | de | à | médiane m³/s | heures temps réel (31 j) |")
    say("|---|---|---|---|---|---|---|---|")
    for st, d in AMONT.items():
        for n, code in d.items():
            try:
                s, cols = journalier(code)
            except Exception as e:
                s, cols = pd.Series(dtype=float), None
                print("ERREUR journalier", n, e, flush=True)
            try:
                h = temps_reel(code)
            except Exception as e:
                h = pd.Series(dtype=float)
                print("ERREUR temps réel", n, e, flush=True)
            jour[(st, n)], heure[(st, n)] = s, h
            say(f"| {st} | {n} | {code} | {len(s)} | {s.index.min():%Y-%m-%d} | {s.index.max():%Y-%m-%d} | {s.median():.1f} | {h.notna().sum()} |"
                if len(s) else f"| {st} | {n} | {code} | 0 | - | - | - | {h.notna().sum()} |")
    pd.DataFrame({f"{a}|{b}": s for (a, b), s in jour.items()}).to_csv(OUT / "journalier.csv", float_format="%.3f")
    pd.DataFrame({f"{a}|{b}": s for (a, b), s in heure.items()}).to_csv(OUT / "horaire_31j.csv", float_format="%.3f")
    say("\n## Backtest journalier\n")
    say("Même modèle statistique direct par horizon, avec et sans les variations de débit amont de la veille et du jour. "
        "« Montées » = jours où le débit a réellement pris plus de 20 % sur l'horizon. Biais négatif = hausse sous-estimée.")
    out = {}
    for st in AMONT:
        ups = {n: s for (a, n), s in jour.items() if a == st and len(s) > 900}
        out[st] = {f"{k[0]}|{k[1]}": v for k, v in backtest(st, hist, ups).items()}
    say("\n## Avance horaire\n")
    for st in AMONT:
        decalage(st, rel, {n: s for (a, n), s in heure.items() if a == st})
    (OUT / "resultat.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    (OUT / "resultat.json").write_text(json.dumps(out, indent=1), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
