#!/usr/bin/env python3
"""Test : le débit de Sedan (Meuse française) améliore-t-il l'estimation retenue à Chooz
(ensemble modèle statistique + GR4J) ?

Rejoue juin 2024 → oct. 2026 jour par jour, avec les prévisions de pluie réellement émises
(Open-Meteo « previous runs », échéances 0 à 7 jours) puis la pluie observée de J+8 à J+14
(identique pour toutes les variantes, donc sans effet sur la comparaison).
Variantes du modèle statistique : sans Sedan / Sedan, futur inconnu = 0 / Sedan, futur = même variation que Chooz.
GR4J est identique dans toutes les variantes. Écrit data/sedan/resultat.md et resultat.json.
Lancé par GitHub Actions (.github/workflows/sedan.yml)."""
import json
import math
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "data" / "sedan"
OUT.mkdir(parents=True, exist_ok=True)

from debits import BASIN_PTS, GR4J, RAIN_SCALE, _est_feats  # noqa: E402

ST = "Chooz"
START, END = pd.Timestamp("2024-06-01"), pd.Timestamp("2026-10-31")
H_LIST = (1, 2, 3, 5, 7, 14)
LOG = []


def say(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    LOG.append(s)


def get(url, params, tries=5):
    for i in range(tries):
        try:
            r = httpx.get(url, params=params, timeout=180)
            if r.status_code == 200:
                return r.json()
            print("HTTP", r.status_code, r.text[:300], flush=True)
        except Exception as e:  # noqa: BLE001
            print("essai", i, e, flush=True)
        time.sleep(10 * (i + 1))
    raise RuntimeError(f"Pas de réponse : {url}")


# ---------- Données ----------

def sedan_series():
    s = pd.read_csv(OUT / "amont_journalier.csv", index_col=0, parse_dates=True)["Chooz|Sedan"]
    try:
        rows, url, p = [], "https://hubeau.eaufrance.fr/api/v2/hydrometrie/obs_elab", dict(
            code_entite="B502001001", grandeur_hydro_elab="QmnJ", date_debut_obs_elab="2021-11-01", size=20000)
        for _ in range(10):
            js = get(url, p)
            rows += js.get("data", [])
            url, p = js.get("next"), None
            if not url:
                break
        if rows:
            df = pd.DataFrame(rows)
            s2 = df.groupby("date_obs_elab")["resultat_obs_elab"].mean() / 1000.0
            s2.index = pd.to_datetime(s2.index)
            s = s2.combine_first(s)
            say(f"Sedan : Hub'Eau à jour jusqu'au {s.index.max():%d/%m/%Y}")
    except Exception as e:  # noqa: BLE001
        say(f"Sedan : Hub'Eau indisponible ({e}), fichier du 10/11 utilisé")
    return s.sort_index()


def basin_mean_daily(js, var):
    js = js if isinstance(js, list) else [js]
    acc = {}
    for loc in js:
        for d, v in zip(loc["daily"]["time"], loc["daily"][var]):
            if v is not None:
                acc.setdefault(d, []).append(v)
    return pd.Series({pd.Timestamp(d): sum(v) / len(v) for d, v in acc.items()}).sort_index()


def archive_PE():
    pts = BASIN_PTS[ST]
    js = get("https://archive-api.open-meteo.com/v1/archive", {
        "latitude": ",".join(str(p[0]) for p in pts), "longitude": ",".join(str(p[1]) for p in pts),
        "daily": "precipitation_sum,et0_fao_evapotranspiration", "timezone": "Europe/Brussels",
        "start_date": "2020-08-01", "end_date": str(min(END + pd.Timedelta(days=20), pd.Timestamp.today().normalize() - pd.Timedelta(days=2)).date())})
    P = basin_mean_daily(js, "precipitation_sum")
    E = basin_mean_daily(js, "et0_fao_evapotranspiration")
    return P.fillna(0.0), E.interpolate().bfill().ffill()


def forecast_archive():
    """Pluie prévue émise chaque jour d0 pour d0..d0+7 : variable precipitation_previous_dayN à la date d0+N."""
    pts = BASIN_PTS[ST]
    hourly = ["precipitation"] + [f"precipitation_previous_day{n}" for n in range(1, 8)]
    for model in ("ecmwf_ifs025", "best_match"):
        frames, ok = [], True
        a = START - pd.Timedelta(days=2)
        while a <= END + pd.Timedelta(days=8):
            b = min(a + pd.Timedelta(days=150), END + pd.Timedelta(days=8), pd.Timestamp.today().normalize())
            try:
                js = get("https://previous-runs-api.open-meteo.com/v1/forecast", {
                    "latitude": ",".join(str(p[0]) for p in pts), "longitude": ",".join(str(p[1]) for p in pts),
                    "hourly": ",".join(hourly), "models": model, "timezone": "Europe/Brussels",
                    "start_date": str(a.date()), "end_date": str(b.date())}, tries=3)
            except RuntimeError:
                ok = False
                break
            js = js if isinstance(js, list) else [js]
            per = []
            for loc in js:
                df = pd.DataFrame(loc["hourly"])
                df["time"] = pd.to_datetime(df["time"])
                per.append(df.set_index("time"))
            m = pd.concat(per).groupby(level=0).mean()
            frames.append(m.resample("D").sum(min_count=18))
            a = b + pd.Timedelta(days=1)
        if ok and frames:
            d = pd.concat(frames)
            d = d[~d.index.duplicated()]
            cov = {c: float(d[c].notna().mean()) for c in hourly}
            say(f"Prévisions archivées : modèle {model}, couverture " +
                ", ".join(f"J+{i}:{cov[c]:.0%}" for i, c in enumerate(hourly)))
            if cov["precipitation_previous_day3"] > 0.8:
                return d, model
    raise RuntimeError("Archive des prévisions indisponible")


# ---------- GR4J (copie du modèle en production, état continu) ----------

def gr4j_runs(P, E, q_obs, days, fc_by_day):
    x1, x2, x3, x4, area = GR4J[ST]
    conv = area / 86.4
    L = 20

    def sh1(t):
        return 0.0 if t <= 0 else 1.0 if t >= x4 else (t / x4) ** 2.5

    def sh2(t):
        if t <= 0:
            return 0.0
        if t < x4:
            return 0.5 * (t / x4) ** 2.5
        if t < 2 * x4:
            return 1 - 0.5 * (2 - t / x4) ** 2.5
        return 1.0

    u1 = [sh1(i + 1) - sh1(i) for i in range(L)]
    u2 = [sh2(i + 1) - sh2(i) for i in range(2 * L)]

    def step(p, e, S, R, b1, b2):
        if p >= e:
            pn = p - e
            tw = math.tanh(pn / x1)
            ps, es = x1 * (1 - (S / x1) ** 2) * tw / (1 + S / x1 * tw), 0.0
        else:
            pn = 0.0
            tw = math.tanh((e - p) / x1)
            ps, es = 0.0, S * (2 - S / x1) * tw / (1 + (1 - S / x1) * tw)
        S = S - es + ps
        perc = S * (1 - (1 + (4.0 / 9.0 * S / x1) ** 4) ** -0.25)
        S -= perc
        pr = perc + (pn - ps)
        for i in range(L - 1):
            b1[i] = b1[i + 1] + u1[i] * pr * 0.9
        b1[L - 1] = u1[L - 1] * pr * 0.9
        for i in range(2 * L - 1):
            b2[i] = b2[i + 1] + u2[i] * pr * 0.1
        b2[2 * L - 1] = u2[2 * L - 1] * pr * 0.1
        F = x2 * (max(R, 0.0) / x3) ** 3.5
        R = max(0.0, R + b1[0] + F)
        qr = R * (1 - (1 + (R / x3) ** 4) ** -0.25)
        R -= qr
        qd = max(0.0, b2[0] + F)
        return S, R, qr + qd, qd

    S, R, b1, b2 = 0.6 * x1, 0.5 * x3, [0.0] * L, [0.0] * (2 * L)
    out = {}
    want = set(days)
    for d in P.index:
        if d > days[-1]:
            break
        S, R, _, qd = step(float(P[d]), float(E[d]), S, R, b1, b2)
        if d in want:
            q0 = q_obs[d]
            tgt = max(q0 / conv - qd, 0.05 * q0 / conv)
            lo, hi = 0.0, 20 * x3
            for _ in range(60):
                mid = (lo + hi) / 2
                if mid * (1 - (1 + (mid / x3) ** 4) ** -0.25) < tgt:
                    lo = mid
                else:
                    hi = mid
            Rr = max((lo + hi) / 2 - tgt, 0.0)
            Ss, c1, c2, res = S, list(b1), list(b2), []
            for h in range(1, 15):
                dd = d + pd.Timedelta(days=h)
                Ss, Rr, q, _ = step(float(fc_by_day[d][h]), float(E.get(dd, E.iloc[-1])), Ss, Rr, c1, c2)
                res.append(q * conv)
            out[d] = res
    return out


# ---------- Modèle statistique (production + variantes Sedan) ----------

def stat_runs(hq, hr, sed, days, fc_by_day, variant):
    sc = RAIN_SCALE.get(ST, 1.0)
    df = pd.DataFrame({"q": hq, "r": hr}).asfreq("D")
    df["q"] = df["q"].interpolate(limit=5)
    df["r"] = df["r"].fillna(0)
    lq_all, R_all = np.log(df["q"].values), df["r"].values
    ls = np.log(sed.reindex(df.index).interpolate(limit=3).clip(lower=0.01)).values
    ds_all = np.r_[np.nan, np.diff(ls)]
    api_all = np.zeros(len(R_all))
    for i in range(1, len(R_all)):
        api_all[i] = 0.85 * api_all[i - 1] + R_all[i]
    doy_all = df.index.dayofyear.values
    use_sed = variant != "sans Sedan"

    def feats(l, dl, rs, a, d, s0, s1):
        f = _est_feats(l, dl, rs, a, d)
        return np.r_[f, s0, s1] if use_sed else f

    # Lignes d'apprentissage (une fois), filtrées ensuite par date
    rows = []
    for t in range(4, len(lq_all) - 1):
        if np.isnan(lq_all[t - 1:t + 2]).any() or (use_sed and np.isnan(ds_all[t - 1:t + 1]).any()):
            continue
        x = feats(lq_all[t], lq_all[t] - lq_all[t - 1], [R_all[t + 1], R_all[t], R_all[t - 1], R_all[t - 2], R_all[t - 3]],
                  api_all[t], doy_all[t + 1], ds_all[t], ds_all[t - 1])
        rows.append((t + 1, x, lq_all[t + 1] - lq_all[t]))
    tgt_idx = np.array([r[0] for r in rows])
    X_all = np.array([r[1] for r in rows])
    y_all = np.array([r[2] for r in rows])
    idx = {d: i for i, d in enumerate(df.index)}
    out = {}
    for d0 in days:
        i0 = idx[d0]
        m = tgt_idx <= i0  # comme en production : historique jusqu'à d0 inclus (q0 = débit du jour)
        coef, *_ = np.linalg.lstsq(X_all[m], y_all[m], rcond=None)
        lo, hi = np.nanmin(lq_all[:i0 + 1]), np.nanmax(lq_all[:i0 + 1])
        fc = [v * sc for v in fc_by_day[d0]]
        rs = list(R_all[i0 - 3:i0]) + [fc[0]]
        a = 0.85 * api_all[i0 - 1] + fc[0]
        l = float(lq_all[i0])
        dl = float(lq_all[i0] - lq_all[i0 - 1])
        s0, s1 = (float(ds_all[i0]) if np.isfinite(ds_all[i0]) else 0.0,
                  float(ds_all[i0 - 1]) if np.isfinite(ds_all[i0 - 1]) else 0.0)
        res = []
        for h, rf in enumerate(fc[1:15], start=1):
            rs.append(rf)
            dday = (d0 + pd.Timedelta(days=h)).dayofyear
            dd = float(np.clip(feats(l, dl, [rs[-1], rs[-2], rs[-3], rs[-4], rs[-5]], a, dday, s0, s1) @ coef, -0.5, 1.5))
            l = float(np.clip(l + dd, lo, hi))
            dl, a = dd, 0.85 * a + rf
            # Sedan des jours futurs : inconnu
            s1 = s0
            s0 = dd if variant == "Sedan, futur = variation de Chooz" else 0.0
            res.append(float(np.exp(l)))
        out[d0] = res
    return out


# ---------- Évaluation ----------

def scores(est, obs, days):
    r = {}
    for h in H_LIST:
        e, b = [], []
        for d in days:
            dd = d + pd.Timedelta(days=h)
            if dd not in obs.index or not np.isfinite(obs[dd]):
                continue
            ratio = est[d][h - 1] / obs[dd] - 1
            e.append(abs(ratio))
            if obs[dd] > 1.2 * obs[d]:
                b.append(ratio)
        r[h] = {"err": float(np.median(e) * 100), "n": len(e),
                "biais_montees": float(np.median(b) * 100) if len(b) >= 10 else None, "n_montees": len(b)}
    return r


def main():
    hist = pd.read_csv(ROOT / "data" / "historique.csv", parse_dates=["Date"], index_col="Date")
    q = hist[ST].astype(float)
    sed = sedan_series()
    P, E = archive_PE()
    fca, model = forecast_archive()
    days = [d for d in pd.date_range(START, END) if d in q.index and np.isfinite(q[d])]
    fc_by_day, skipped = {}, 0
    cols = ["precipitation"] + [f"precipitation_previous_day{n}" for n in range(1, 8)]
    for d in days:
        vals = []
        for h in range(15):
            dd = d + pd.Timedelta(days=h)
            v = fca[cols[h]].get(dd, np.nan) if h <= 7 else P.get(dd, np.nan)
            vals.append(v)
        if not np.isfinite(vals).all():
            skipped += 1
            continue
        fc_by_day[d] = vals
    days = [d for d in days if d in fc_by_day]
    say(f"Jours testés : {len(days)} (écartés faute de prévision : {skipped})")

    g = gr4j_runs(P, E, q, days, fc_by_day)
    say("GR4J rejoué")
    res = {}
    const = {d: [q[d]] * 14 for d in days}
    res["Débit constant"] = scores(const, q, days)
    res["GR4J seul"] = scores(g, q, days)
    for v in ("sans Sedan", "Sedan, futur inconnu = 0", "Sedan, futur = variation de Chooz"):
        s = stat_runs(q, hist["PluieM"].astype(float), sed, days, fc_by_day, v)
        res[f"Statistique {v}"] = scores(s, q, days)
        ens = {d: [(a * b) ** 0.5 for a, b in zip(s[d], g[d])] for d in days}
        res[f"Ensemble {v}"] = scores(ens, q, days)
        say(f"Variante rejouée : {v}")

    lines = [f"# Test Sedan dans l'ensemble GR4J + statistique — Chooz ({pd.Timestamp.utcnow():%d/%m/%Y %H:%M} UTC)", "",
             f"Rejeu jour par jour du {days[0]:%d/%m/%Y} au {days[-1]:%d/%m/%Y} ({len(days)} jours). "
             f"Pluie : prévisions réellement émises (Open-Meteo previous runs, modèle {model}) de J+0 à J+7, "
             "pluie observée de J+8 à J+14 (identique pour toutes les variantes). Modèle statistique recalé chaque jour "
             "sur l'historique disponible ce jour-là, comme en production. Erreur = écart médian au débit moyen mesuré.",
             "", "« Biais montées » : jours où le débit a réellement pris plus de 20 % ; négatif = hausse sous-estimée.", "",
             "| Variante | " + " | ".join(f"J+{h}" for h in H_LIST) + " | biais montées J+2 | biais montées J+3 |",
             "|---|" + "---|" * (len(H_LIST) + 2)]
    for k, r in res.items():
        bm = [f"{r[h]['biais_montees']:+.0f} % (n={r[h]['n_montees']})" if r[h]["biais_montees"] is not None else "n/d"
              for h in (2, 3)]
        lines.append(f"| {k} | " + " | ".join(f"{r[h]['err']:.1f} %" for h in H_LIST) + " | " + " | ".join(bm) + " |")
    base = res["Ensemble sans Sedan"]
    lines += ["", "## Verdict", "",
              "Règle fixée avant le test : intégrer seulement si aucun horizon ne se dégrade (tolérance 0,2 point) "
              "et si l'erreur baisse à J+1 à J+3.", ""]
    best, best_gain = None, 0.0
    for v in ("Sedan, futur inconnu = 0", "Sedan, futur = variation de Chooz"):
        r = res[f"Ensemble {v}"]
        delta = {h: r[h]["err"] - base[h]["err"] for h in H_LIST}
        ok = all(dv <= 0.2 for dv in delta.values()) and all(delta[h] < 0 for h in (1, 2, 3))
        gain = -sum(delta[h] for h in (1, 2, 3))
        lines.append(f"- {v} : " + ", ".join(f"J+{h} {delta[h]:+.1f} pt" for h in H_LIST) +
                     (" → **conforme**" if ok else " → non conforme"))
        if ok and gain > best_gain:
            best, best_gain = v, gain
    lines += ["", f"**Décision : {'intégrer « ' + best + ' »' if best else 'ne pas intégrer'}.**", "",
              "## Journal", "", *[f"- {x}" for x in LOG]]
    (OUT / "resultat.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "resultat.json").write_text(json.dumps({"modele_pluie": model, "jours": len(days), "decision": best,
                                                   "resultats": {k: {str(h): v for h, v in r.items()} for k, r in res.items()}},
                                                  ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
