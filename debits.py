#!/usr/bin/env python3
"""Débits Sambre-Meuse : relevé horaire des débits (SPW KiWIS), journal en CSV dans le dépôt,
alertes crue / décrue en push ntfy avec graphiques et estimation du débit.

Usage :
    python debits.py run              relevé normal (lancé chaque heure par GitHub Actions)
    python debits.py test Chooz       notification [TEST] avec les graphiques (Chooz ou Landelies)
    python debits.py bootstrap        reconstruit l'historique journalier (5 ans) et 60 jours de relevés
"""
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE_FILE = ROOT / "state.json"
CONFIG_FILE = ROOT / "config" / "seuils.json"
RELEVES = DATA / "releves.csv"
HISTORIQUE = DATA / "historique.csv"
ALERTES = DATA / "alertes.csv"

KIWIS = "https://hydrometrie.wallonie.be/services/KiWIS/KiWIS"
TZ = ZoneInfo("Europe/Brussels")
FLOW_TS = {"Landelies": "363665010", "Chatelet": "247553010", "Salzinnes": "247060010",
           "Chooz": "301632010", "Profondeville": "347684010"}
TEMP_NAMUR_TS = "354601010"
RAIN_SAMBRE_TS = ["248435010", "248767010", "250261010", "248021010", "248835010",
                  "237665010", "249784010", "247393010", "247359010", "251171010"]
RAIN_MEUSE_TS = ["252946010", "253132010", "252370010", "250675010", "254541010", "254032010",
                 "253808010", "250008010", "252268010", "251458010", "251823010", "251424010"]
RIVER = {"Landelies": "Sambre", "Chooz": "Meuse"}
DEFAULT_RULES = {
    "Landelies": {"smooth": 3, "window": 3, "pct": 100.0, "abs": 5.0, "pause": 24, "baisse": 30.0},
    "Chooz": {"smooth": 1, "window": 6, "pct": 50.0, "abs": 20.0, "pause": 24, "baisse": 30.0},
}
ADVANCE = {"Chooz": " Arrivée à Profondeville et Namur dans environ 12 h."}
BASIN = {"Landelies": "S", "Chooz": "M"}
PAIR = {"Chooz": "Profondeville", "Landelies": None}
REPLAY_DAYS = 14
RECENT_H = 3  # relevé chaque heure : une crue détectée depuis moins de 3 h est nouvelle
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

RELEVES_COLS = ["Date", "Landelies", "Chatelet", "Salzinnes", "Chooz", "Profondeville", "Temp_Namur",
                "Pluie_Sambre", "Pluie_Meuse", "Etat_Landelies", "Etat_Chooz"]
HIST_COLS = ["Date", "Landelies", "Chatelet", "Salzinnes", "Chooz", "Profondeville", "PluieS", "PluieM"]
ALERT_COLS = ["Date", "Station", "Type", "Debit", "Message"]


class _Log:
    def info(self, msg, **kw):
        print(msg, kw if kw else "", flush=True)

    def warning(self, msg, **kw):
        print("ATTENTION :", msg, kw if kw else "", flush=True)


logger = _Log()


# ---------- Données SPW ----------

def kiwis_values(ts_ids: list[str], **period) -> dict[str, list]:
    params = dict(service="kisters", type="queryServices", datasource="0", format="json",
                  request="getTimeseriesValues", ts_id=",".join(ts_ids), returnfields="Timestamp,Value", **period)
    for attempt in range(4):
        try:
            r = httpx.get(KIWIS, params=params, timeout=180)
            r.raise_for_status()
            return {str(s["ts_id"]): s.get("data") or [] for s in r.json()}
        except Exception as e:
            logger.warning("KiWIS indisponible, nouvel essai", attempt=attempt, error=str(e))
            time.sleep(10 * (attempt + 1))
    raise RuntimeError("Le service KiWIS du SPW ne répond pas")


def to_hourly(rows: list, now_utc: datetime) -> dict[datetime, float]:
    out = {}
    for t, v in rows:
        if v is None:
            continue
        ts = datetime.fromisoformat(t).astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        if ts <= now_utc:
            out[ts] = float(v)
    return out


def basin_mean(series: list[dict[datetime, float]], ts: datetime):
    vals = [s[ts] for s in series if ts in s]
    return round(sum(vals) / len(vals), 2) if vals else None


def smooth(points, hours: int):
    if hours <= 1:
        return points
    out = []
    for i, (t, _) in enumerate(points):
        window = [v for (tt, v) in points[max(0, i - hours + 1): i + 1] if t - tt < timedelta(hours=hours)]
        if len(window) >= max(1, int(hours * 2 / 3)):
            out.append((t, sum(window) / len(window)))
    return out


def detect_moves(points, rule: dict) -> list[dict]:
    """Débuts de mouvement : hausse >= pct % ET >= abs m³/s sur la fenêtre, avec pause entre deux épisodes."""
    by_t = dict(points)
    moves, last_trig = [], None
    for t, v in points:
        ref = by_t.get(t - timedelta(hours=rule["window"]))
        if ref is None or ref <= 0:
            continue
        d = v - ref
        p = d / ref * 100
        if p >= rule["pct"] and d >= rule["abs"]:
            if last_trig is None or t - last_trig > timedelta(hours=rule["pause"]):
                moves.append({"t": t, "value": v, "ref": ref, "pct": p, "delta": d})
            last_trig = t
    return moves


def local(t: datetime) -> str:
    return t.astimezone(TZ).strftime("%Y-%m-%d %H:%M")


# ---------- Fichiers ----------

def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, cols: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def append_csv(path: Path, cols: list[str], rows: list[dict]) -> None:
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def fmt(v, nd=2):
    return "" if v is None or v == "" else round(float(v), nd)


def read_rules() -> dict:
    rules = {k: dict(v) for k, v in DEFAULT_RULES.items()}
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        for name, vals in cfg.get("stations", {}).items():
            if name in rules:
                rules[name].update({k: v for k, v in vals.items() if k in rules[name]})
    except Exception as e:
        logger.warning("Seuils illisibles, valeurs par défaut utilisées", error=str(e))
    return rules


def read_history():
    import pandas as pd
    df = pd.read_csv(HISTORIQUE, parse_dates=["Date"]).set_index("Date").sort_index()
    return df.apply(pd.to_numeric, errors="coerce")


# ---------- Notifications ntfy ----------

def _enc(s: str) -> str:
    import base64
    return "=?UTF-8?B?" + base64.b64encode(s.encode()).decode() + "?="


def push(titre: str, message: str, tag: str, priorite: int = 4) -> None:
    if not NTFY_TOPIC:
        logger.warning("Secret NTFY_TOPIC absent : notification non envoyée", titre=titre)
        return
    r = httpx.post("https://ntfy.sh/", json={"topic": NTFY_TOPIC, "title": titre, "message": message,
                                             "tags": [tag], "priority": priorite}, timeout=30)
    r.raise_for_status()


def push_image(titre: str, message: str, tag: str, png: bytes, priorite: int = 4) -> None:
    if not NTFY_TOPIC:
        logger.warning("Secret NTFY_TOPIC absent : notification non envoyée", titre=titre)
        return
    for essai in range(3):
        r = httpx.put(f"https://ntfy.sh/{NTFY_TOPIC}", content=png, timeout=60, headers={
            "Filename": "debits.png", "Title": _enc(titre), "Message": _enc(message),
            "Tags": tag, "Priority": str(priorite)})
        if r.status_code < 400 or essai == 2:
            r.raise_for_status()
            return
        logger.warning("Envoi d'image ntfy refusé, nouvel essai", status=r.status_code)
        time.sleep(30)


# ---------- Prévisions et graphiques ----------

# Points de pluie sur tout le bassin amont de chaque station, France comprise
BASIN_PTS = {
    "Chooz": [(47.98, 5.55), (48.36, 5.70), (48.62, 5.68), (48.76, 5.59), (48.95, 5.50), (49.16, 5.38), (49.40, 5.20),
              (49.52, 5.37), (49.52, 5.76), (49.62, 4.85), (49.70, 4.94), (49.77, 4.72), (49.79, 5.07), (49.70, 5.31),
              (49.68, 5.60), (49.90, 4.75)],
    "Landelies": [(50.02, 3.78), (50.12, 3.69), (50.12, 3.93), (50.28, 3.97), (50.17, 4.09), (50.30, 4.11), (50.24, 4.24),
                  (50.34, 4.29), (50.02, 4.05), (50.06, 4.10), (50.17, 4.18)],
}
RAIN_PTS = BASIN_PTS


def rain_forecast(station: str) -> dict:
    """Pluie prévue (mm/jour, moyenne des points du bassin) sur 15 jours : icon_seamless puis ecmwf_ifs025."""
    pts = RAIN_PTS[station]
    r = httpx.get("https://api.open-meteo.com/v1/forecast", timeout=60, params={
        "latitude": ",".join(str(p[0]) for p in pts), "longitude": ",".join(str(p[1]) for p in pts),
        "daily": "precipitation_sum", "forecast_days": 15, "timezone": "Europe/Brussels",
        "models": "icon_seamless,ecmwf_ifs025"})
    r.raise_for_status()
    data = r.json()
    data = data if isinstance(data, list) else [data]
    out: dict = {}
    for loc in data:
        dl = loc["daily"]
        icon = dl.get("precipitation_sum_icon_seamless") or [None] * len(dl["time"])
        ecm = dl.get("precipitation_sum_ecmwf_ifs025") or [None] * len(dl["time"])
        for d, a, b in zip(dl["time"], icon, ecm):
            v = a if a is not None else b
            if v is not None:
                out.setdefault(d, []).append(v)
    return {d: sum(v) / len(v) for d, v in sorted(out.items())}


# Probabilité (%) d'une alerte crue dans les 3 jours selon la pluie des 2 prochains jours sur le bassin
PROBA_CRUE = {
    "Landelies": [(5, 4), (10, 7), (15, 15), (20, 13), (30, 32), (1e9, 36)],
    "Chooz": [(5, 7), (10, 5), (15, 17), (20, 20), (30, 26), (1e9, 55)],
}


def favorable_days(seq: list[float], dates: list, min_d: float) -> list:
    """Jours estimés en début de montée ou en début de décrue."""
    fav, rising = [], [False, False]
    for i in range(1, len(seq)):
        d = seq[i] - seq[i - 1]
        ch = d / seq[i - 1] if seq[i - 1] else 0
        r = ch > 0.1 and d >= min_d / 2
        f = ch < -0.1 and -d >= min_d / 2
        if (r and not rising[-1]) or (f and any(rising[-2:])):
            fav.append(dates[i])
        rising.append(r)
    return fav


# Pluie mesurée (pluviomètres SPW) / pluie Open-Meteo aux points de prévision, sur 5 ans
RAIN_SCALE = {"Landelies": 0.90, "Chooz": 0.90}  # pluviomètres / prévision sur tout le bassin (backtest 2024-2026)
# Biais médian de l'estimation (% par horizon en jours), mesuré en rejouant juin 2025 – nov. 2026 avec les vraies prévisions
EST_BIAS = {"Landelies": [(1, 0), (14, 0)], "Chooz": [(1, 0), (14, 0)]}  # ensemble : biais négligeable
# Probabilité réelle d'un début de montée ou de décrue dans la période (même backtest) :
# (si le modèle en annonce un, s'il n'en annonce pas)
P_FAV = {"Landelies": {3: (71, 13), 14: (92, 57)}, "Chooz": {3: (75, 12), 14: (90, 52)}}  # backtest juin 2024 – oct. 2026
SPECIES_LINE = "Sandre et silure : moment favorable. Brochet et perche : zones calmes, bordures, contre-courants."


def _est_feats(lq, dl, R, api, doy):
    import numpy as np
    cs, sn = np.cos(2 * np.pi * doy / 365.25), np.sin(2 * np.pi * doy / 365.25)
    r3 = R[0] + R[1] + R[2]
    return np.array([1.0, lq, dl, R[0], R[1], R[2], R[3], R[4], api, r3 * api / 10, r3 * cs, r3 * sn, cs, sn, r3 * lq])


def estimate_flow(station: str, hq, hr, d0, q0: float, q_prev: float, fc_vals: list[float], scenarios=None):
    """Estimation jour par jour en logarithme : variation du débit selon le débit, sa tendance, la pluie des 5 derniers
    jours, l'humidité du sol (indice de pluie antérieure) et la saison. Calé sur l'historique à chaque relevé.
    Backtest juin 2025 – nov. 2026 avec les vraies prévisions de pluie : erreur médiane à 3 jours ~7 % (Chooz),
    ~21 % (Landelies), contre 13 % et 27 % pour « le débit ne bouge pas » ; l'ancien modèle linéaire faisait pire (27 %, 67 %).
    Renvoie (estimation corrigée du biais pour l'affichage, estimation brute pour les conditions favorables)."""
    import numpy as np
    import pandas as pd
    df = pd.DataFrame({"q": hq, "r": hr})
    df = df[df.index < d0].asfreq("D")
    df["q"] = df["q"].interpolate(limit=5)
    df["r"] = df["r"].fillna(0)
    lq, R = np.log(df["q"].values), df["r"].values
    api = np.zeros(len(R))
    for i in range(1, len(R)):
        api[i] = 0.85 * api[i - 1] + R[i]
    doy = df.index.dayofyear.values
    X, y = [], []
    for t in range(4, len(lq) - 1):
        if np.isnan(lq[t - 1:t + 2]).any():
            continue
        X.append(_est_feats(lq[t], lq[t] - lq[t - 1], [R[t + 1], R[t], R[t - 1], R[t - 2], R[t - 3]], api[t], doy[t + 1]))
        y.append(lq[t + 1] - lq[t])
    coef, *_ = np.linalg.lstsq(np.array(X), np.array(y), rcond=None)
    lo, hi = np.nanmin(lq), np.nanmax(lq)
    sc = RAIN_SCALE.get(station, 1.0)

    def sim(vals):
        fc = [v * sc for v in vals]
        rs = list(R[-3:]) + [fc[0]]
        a = 0.85 * api[-1] + fc[0]
        l = float(np.log(q0))
        dl = float(np.log(q0 / q_prev)) if q_prev else 0.0
        out = []
        for h, rf in enumerate(fc[1:15], start=1):
            rs.append(rf)
            d = (d0 + pd.Timedelta(days=h)).dayofyear
            dd = float(np.clip(_est_feats(l, dl, [rs[-1], rs[-2], rs[-3], rs[-4], rs[-5]], a, d) @ coef, -0.5, 1.5))
            l = float(np.clip(l + dd, lo, hi))
            dl, a = dd, 0.85 * a + rf
            out.append(float(np.exp(l)))
        return out

    raw = sim(fc_vals)
    hb, vb = zip(*EST_BIAS.get(station, [(1, 0), (14, 0)]))
    shown = [v / (1 + float(np.interp(h, hb, vb)) / 100) for h, v in enumerate(raw, start=1)]
    if scenarios is not None:
        return shown, raw, [sim(s) for s in scenarios]
    return shown, raw


def make_chart(station: str, flows: dict, rain_series: list, hist, forecast: dict, alert_t: datetime):
    """Titre « Rivière : phase », résumé en gras, graphique journalier (avec estimation), graphique horaire, détails."""
    import io
    import textwrap
    import numpy as np
    import pandas as pd
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import matplotlib.patches as mpatches
    plt.rcParams.update({"font.size": 12})
    at = pd.Timestamp(alert_t).tz_convert(TZ)
    d0 = pd.Timestamp(at.date())
    t0 = alert_t - timedelta(hours=72)
    s_full = pd.Series(flows[station]).sort_index()
    s_full.index = pd.DatetimeIndex(s_full.index).tz_convert(TZ)
    s = s_full[s_full.index >= pd.Timestamp(t0).tz_convert(TZ)]
    h = hist[station].dropna() if hist is not None else pd.Series(dtype=float)
    h_old = h[h.index < d0 - pd.Timedelta(days=30)]
    obs = s_full.resample("1D").mean()
    obs.index = obs.index.tz_localize(None)
    obs = obs[obs.index >= d0 - pd.Timedelta(days=21)]
    est = est_raw = band = None
    col = "PluieS" if BASIN[station] == "S" else "PluieM"
    if forecast and hist is not None and col in hist and len(obs) >= 2:
        try:
            fv = list(forecast.values())
            try:
                ens = ensemble_rain(station)
            except Exception as e:
                logger.warning("Prévision d'ensemble indisponible", error=str(e))
                ens = []
            est, est_raw, ens_q = estimate_flow(station, hist[station], hist[col], d0, float(obs.iloc[-1]),
                                                float(obs.iloc[-2]), fv[:15], scenarios=ens)
            try:
                gq, ens_g = gr4j_estimate(station, d0, float(obs.iloc[-1]), fv[:15], scenarios=ens)
                est = est_raw = [(a * b) ** 0.5 for a, b in zip(est_raw, gq)]
                ens_q = [[(a * b) ** 0.5 for a, b in zip(x, y)] for x, y in zip(ens_q, ens_g)]
            except Exception as e:
                logger.warning("Modèle GR4J indisponible, modèle statistique seul", error=str(e))
            if len(ens_q) >= 10:
                arr = np.array(ens_q)
                band = (np.percentile(arr, 10, axis=0), np.percentile(arr, 90, axis=0))
        except Exception as e:
            logger.warning("Estimation du débit impossible", error=str(e))
    bold, details, phase = estimation(station, s_full, h_old, forecast, d0, hist, est_raw,
                                      float(obs.iloc[-1]) if len(obs) else None)
    if band is not None and len(band[0]) >= 7:
        def _f(v):
            return f"{v:.0f}" if v >= 10 else f"{v:.1f}".replace(".", ",")
        details.insert(1, f"Fourchette selon la pluie (80 % des 51 scénarios ECMWF) : {_f(band[0][2])}–{_f(band[1][2])} m³/s "
                          f"le {(d0 + pd.Timedelta(days=3)):%d/%m}, {_f(band[0][6])}–{_f(band[1][6])} m³/s le "
                          f"{(d0 + pd.Timedelta(days=7)):%d/%m} ; l'erreur du modèle s'y ajoute.")

    W, HGT = 8, 15
    fig = plt.figure(figsize=(W, HGT), dpi=110)
    rend = fig.canvas.get_renderer()
    fh = fig.bbox.height

    def frac(txt):
        bb = txt.get_window_extent(rend)
        return bb.y0 / fh, bb.y1 / fh

    fig.text(0.03, 0.992, f"{RIVER.get(station, station)} : {phase}", fontsize=20, fontweight="bold", va="top")
    tb = fig.text(0.03, 0.962, "\n".join(textwrap.fill(l, 74) for l in bold), fontsize=12, fontweight="bold",
                  va="top", linespacing=1.35)
    y_hi = frac(tb)[0] - 0.012
    dt = fig.text(0.035, 0.014, "\n".join(textwrap.fill(l, 94) for l in details), fontsize=11, va="bottom",
                  color="#333333", linespacing=1.3)
    bb = dt.get_window_extent(rend)
    pad = 6
    fig.patches.append(mpatches.FancyBboxPatch(
        ((bb.x0 - pad) / fig.bbox.width, (bb.y0 - pad) / fh), (0.97 * fig.bbox.width - bb.x0 + 2 * pad) / fig.bbox.width,
        (bb.height + 2 * pad) / fh, boxstyle="round,pad=0.002", transform=fig.transFigure,
        facecolor="#f4f4f4", edgecolor="#cccccc", figure=fig, zorder=-1))
    y_lo = (bb.y1 + pad) / fh + 0.012
    # Chaque graphique : titre (0.025) + courbes + dates et légende sous le graphique (0.115)
    TITLE, BELOW = 0.025, 0.115
    ah = ((y_hi - y_lo) - 2 * (TITLE + BELOW)) / 2
    a2 = fig.add_axes([0.12, y_hi - TITLE - ah, 0.76, ah])            # journalier (en premier)
    a1 = fig.add_axes([0.12, y_hi - 2 * TITLE - BELOW - 2 * ah, 0.76, ah])  # horaire

    def legend_below(axs, ax_box):
        hs, ls = [], []
        for a in axs:
            hh, ll = a.get_legend_handles_labels()
            hs += hh
            ls += ll
        fig.legend(hs, ls, loc="upper center", bbox_to_anchor=(0.5, ax_box.y0 - 0.042), ncol=2, fontsize=10,
                   frameon=False, handlelength=2.2, columnspacing=1.2)

    # --- journalier : 3 semaines avant / 2 semaines après
    rng = pd.date_range(d0 - pd.Timedelta(days=21), d0 + pd.Timedelta(days=14), freq="1D")
    tw = [a2]
    if len(h_old):
        doy = h_old.index.dayofyear
        band = np.array([h_old[abs(((doy - t.dayofyear) + 182) % 365 - 182) <= 7].quantile([.25, .5, .75]).values
                         for t in rng])
        a2.fill_between(rng, band[:, 0], band[:, 2], color="#bbbbbb", alpha=.5, label="Normale de saison (25–75 %)")
        a2.plot(rng, band[:, 1], color="#888888", lw=1)
        hd = s_full.resample("1D").mean()
        q0 = float(hd.iloc[-1]) if len(hd) else None
        rise = h_old / h_old.shift(1) - 1
        n, last = 0, None
        for t in rise[rise >= 0.4].index:
            if last is not None and t - last < pd.Timedelta(days=10):
                continue
            if abs(((t.dayofyear - d0.dayofyear) + 182) % 365 - 182) > 45 or not q0:
                continue
            seg = h_old[t: t + pd.Timedelta(days=14)]
            if len(seg) < 15 or not (0.5 <= seg.iloc[0] / q0 <= 2):
                continue
            a2.plot([d0 + pd.Timedelta(days=i) for i in range(15)], seg.values[:15], color="#e07b00", alpha=.18, lw=1)
            n, last = n + 1, t
        if n:
            a2.plot([], [], color="#e07b00", alpha=.5, lw=1, label=f"Suites de montées semblables ({n} cas)")
    a2.plot(obs.index, obs.values, color="#1f5fbf", lw=2.5, marker="o", ms=4, label=f"{station}, moyenne du jour")
    if est:
        xs = [obs.index[-1]] + [d0 + pd.Timedelta(days=i) for i in range(1, len(est) + 1)]
        a2.plot(xs, [float(obs.iloc[-1])] + est, color="#1f5fbf", lw=2, ls="--", label="Estimation (indicatif)")
    if band is not None:
        xb = [obs.index[-1]] + [d0 + pd.Timedelta(days=i) for i in range(1, len(band[0]) + 1)]
        a2.fill_between(xb, [float(obs.iloc[-1])] + list(band[0]), [float(obs.iloc[-1])] + list(band[1]),
                        color="#1f5fbf", alpha=.15, lw=0, label="Fourchette selon la pluie (80 % des scénarios)")
    a2.axvline(d0, color="#c0392b", ls="--", lw=1.5, label="Aujourd'hui")
    a2.set_ylabel("Débit (m³/s)")
    a2.set_ylim(bottom=0)
    a2.set_title("Jour par jour – 3 semaines avant, 2 semaines après", loc="left", fontweight="bold", fontsize=12)
    if forecast:
        ax2 = a2.twinx()
        ax2.bar([pd.Timestamp(d) for d in forecast], list(forecast.values()), width=0.7, color="#2e86c1",
                alpha=.35, label="Pluie prévue (mm/jour, axe de droite)")
        ax2.set_ylabel("Pluie prévue (mm/jour)")
        ax2.set_ylim(0, max(max(forecast.values()) * 3, 5))
        tw.append(ax2)
    a2.set_xlim(rng[0] - pd.Timedelta(hours=12), rng[-1] + pd.Timedelta(hours=12))
    a2.xaxis.set_major_locator(mdates.DayLocator(interval=5))
    a2.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    a2.grid(alpha=.3)
    legend_below(tw, a2.get_position())

    # --- horaire : 72 h passées, 48 h estimées
    a1.plot(s.index, s.values, color="#1f5fbf", lw=2.5, label=station)
    other = PAIR.get(station)
    o = None
    if other:
        o = pd.Series(flows[other]).sort_index()
        o.index = pd.DatetimeIndex(o.index).tz_convert(TZ)
        o = o[o.index >= pd.Timestamp(t0).tz_convert(TZ)]
        a1.plot(o.index, o.values, color="#e07b00", lw=2.5, label=other)
    hrs = sorted({t for d in rain_series for t in d if t >= t0})
    rv = [basin_mean(rain_series, t) or 0.0 for t in hrs]
    ax = a1.twinx()
    ax.bar([pd.Timestamp(t).tz_convert(TZ) for t in hrs], rv, width=1 / 24, color="#7fb3d5", alpha=.5,
           label="Pluie mesurée (mm/h, axe de droite)")
    ax.set_ylabel("Pluie (mm/h)")
    ax.set_ylim(0, max(max(rv or [0]) * 3, 1))
    if est and len(s):
        t_last = s.index[-1]
        knots_t = [t_last] + [pd.Timestamp(d0 + pd.Timedelta(days=i, hours=12)).tz_localize(TZ) for i in range(1, 4)]
        knots_v = [float(s.iloc[-1])] + list(est[:3])
        ht = pd.date_range(t_last, t_last + pd.Timedelta(hours=48), freq="1h")
        hv = np.interp([x.value for x in ht], [k.value for k in knots_t], knots_v)
        a1.plot(ht, hv, color="#1f5fbf", lw=2, ls="--", label=f"{station}, estimation")
        if o is not None and len(o):
            ratio = float(o.median() / s.median()) if s.median() else 1.0
            up = pd.concat([s, pd.Series(hv[1:], index=ht[1:])])
            ot = pd.date_range(o.index[-1], o.index[-1] + pd.Timedelta(hours=48), freq="1h")
            ov = [float(o.iloc[-1])] + [float(up.asof(t - pd.Timedelta(hours=12))) * ratio for t in ot[1:]]
            a1.plot(ot, ov, color="#e07b00", lw=2, ls="--", label=f"{other}, estimation (Chooz + 12 h)")
    a1.axvline(at, color="#c0392b", ls="--", lw=1.5, label="Maintenant")
    a1.set_ylabel("Débit (m³/s)")
    a1.set_ylim(bottom=0)
    a1.set_title("Heure par heure – 72 h passées, 48 h estimées", loc="left", fontweight="bold", fontsize=12)
    a1.set_xlim(pd.Timestamp(t0).tz_convert(TZ), at + pd.Timedelta(hours=48))
    a1.xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 12], tz=TZ))
    a1.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m\n%Hh", tz=TZ))
    a1.tick_params(axis="x", labelsize=10)
    a1.grid(alpha=.3)
    legend_below([a1, ax], a1.get_position())
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue(), bold[1] if len(bold) > 1 else bold[0]


def estimation(station: str, s_full, h, forecast: dict, d0, hist=None, est=None, last_obs=None):
    """Résumé (lignes en gras) puis détails : conditions favorables calibrées, pluie 14 jours, ligne espèces."""
    cur = float(s_full.iloc[-1])
    prev = s_full[s_full.index <= s_full.index[-1] - timedelta(hours=24)]
    trend = (cur / float(prev.iloc[-1]) - 1) * 100 if len(prev) and prev.iloc[-1] else 0
    tend = "en hausse" if trend > 10 else "en baisse" if trend < -10 else "stable"
    niveau, p25, p50, p75, p90 = "", None, None, None, None
    if len(h):
        doy = h.index.dayofyear
        m = h[abs(((doy - d0.dayofyear) + 182) % 365 - 182) <= 7]
        p25, p50, p75, p90 = (float(m.quantile(q)) for q in (.25, .5, .75, .9))
        niveau = ("sous la normale de saison" if cur < p25 else "au-dessus de la normale de saison" if cur > p75
                  else "dans la normale de saison")
    # Phase de la rivière sur les 72 dernières heures
    last72 = s_full[s_full.index >= s_full.index[-1] - timedelta(hours=72)]
    mn, pk = float(last72.min()), float(last72.max())
    t_pk = last72.idxmax()
    min_d = DEFAULT_RULES.get(station, {"abs": 6})["abs"] / 2
    prev_v = float(prev.iloc[-1]) if len(prev) else cur
    rising = trend > 10 and cur - prev_v >= min_d
    episode = pk >= 1.3 * mn and pk - mn >= min_d
    rise_start = None
    if rising:
        above = last72[last72 > mn * 1.2]
        rise_start = above.index[0] if len(above) else None
    if rising and rise_start is not None and (s_full.index[-1] - rise_start) <= timedelta(hours=12):
        phase = "début de montée"
    elif rising:
        phase = "montée prolongée"
    elif episode and cur >= 0.9 * pk:
        phase = "autour du pic"
    elif episode and cur < 0.9 * pk and cur >= mn * 1.1 and s_full.index[-1] > t_pk:
        phase = "début de décrue"
    elif p25 is not None and cur < p25:
        phase = "étiage stable"
    elif p90 is not None and cur > p90:
        phase = "hautes eaux stables"
    else:
        phase = "débit stable, dans la normale"
    bold = []
    if est:
        import pandas as pd
        seq = [last_obs if last_obs is not None else cur] + list(est)
        dates = [d0 + pd.Timedelta(days=i) for i in range(len(seq))]
        fav = favorable_days(seq, dates, min_d)
        for n in (3, 14):
            f_n = [d for d in fav if d <= d0 + pd.Timedelta(days=n)]
            p_oui, p_non = P_FAV.get(station, {}).get(n, (None, None))
            pct = p_oui if f_n else p_non
            quand = ("estimé le " + ", ".join(d.strftime("%d/%m") for d in f_n)) if f_n else \
                "aucun début de montée ou de décrue estimé"
            bold.append(f"Conditions favorables à {n} jours : ~{pct} % ({quand}).")
    else:
        bold.append("Conditions favorables : estimation impossible (pas de prévision de pluie).")
    details = [f"Maintenant : {cur:.0f} m³/s, {tend} sur 24 h ({trend:+.0f} %), {niveau}."]
    if forecast:
        vals = list(forecast.values())
        r2 = sum(vals[1:3]) if len(vals) >= 3 else sum(vals)
        r7, r14 = sum(vals[:7]), sum(vals[:14])
        p = next(pr for lim, pr in PROBA_CRUE[station] if r2 < lim)
        risque = "peu probable" if p < 10 else "possible" if p < 25 else "probable"
        details.append(f"Statistique 5 ans : mouvement dans les 3 jours {risque} (~{p} % des situations semblables).")
        col = "PluieS" if BASIN[station] == "S" else "PluieM"
        norme = ""
        if hist is not None and col in hist and hist[col].notna().any():
            rs = hist[col].dropna().rolling(14).sum().shift(-13).dropna()
            dy = rs.index.dayofyear
            n14 = float(rs[abs(((dy - d0.dayofyear) + 182) % 365 - 182) <= 7].median())
            ecart = "sous" if r14 < 0.75 * n14 else "au-dessus de" if r14 > 1.25 * n14 else "proche de"
            norme = f", {ecart} la normale (~{n14:.0f} mm)"
        bold.append(f"Pluie prévue sur 14 jours : {r14:.0f} mm{norme}.")
        details.append(f"Pluie prévue : {r2:.0f} mm sur 2 jours, {r7:.0f} mm sur 7 jours (fiable jusqu'à ~5 jours).")
    else:
        bold.append("Prévision de pluie indisponible : pas d'estimation.")
    if phase in ("début de montée", "début de décrue"):
        bold.append(SPECIES_LINE)
    details.append("Conditions favorables : probabilité qu'un début de montée ou de décrue arrive vraiment dans la "
                   "période, mesurée en rejouant juin 2024 – oct. 2026 avec les vraies prévisions de pluie. "
                   "Tirets : moyenne d'un modèle statistique et d'un modèle hydrologique (GR4J, pluie et évaporation "
                   "sur tout le bassin, France comprise) ; erreur médiane ~7 % à 3 jours et ~22 % à 14 jours (Meuse), "
                   "~16 % et ~33 % (Sambre) ; l'ampleur des hausses est souvent sous-estimée (~20 % Meuse, ~30 % Sambre).")
    return bold, details, phase

# GR4J calé sur nov. 2021 – mai 2024 (KGE test 2024-2026 : 0,94 Chooz, 0,92 Landelies) : x1, x2, x3, x4, surface effective km²
GR4J = {"Chooz": (293.9, 2.000, 131.0, 2.351, 10120 * 0.7906),
        "Landelies": (352.0, -0.583, 41.35, 2.062, 1250 * 1.2427)}


def basin_inputs(station: str, d0):
    """Pluie et évapotranspiration (ET0) journalières moyennes sur le bassin : archive (450 jours) + 14 jours récents
    et 15 jours de prévision (Open-Meteo)."""
    import pandas as pd
    pts = BASIN_PTS[station]
    base = {"latitude": ",".join(str(p[0]) for p in pts), "longitude": ",".join(str(p[1]) for p in pts),
            "daily": "precipitation_sum,et0_fao_evapotranspiration", "timezone": "Europe/Brussels"}
    with httpx.Client(timeout=90) as c:
        a = c.get("https://archive-api.open-meteo.com/v1/archive", params={**base,
                  "start_date": str((d0 - pd.Timedelta(days=450)).date()), "end_date": str((d0 - pd.Timedelta(days=15)).date())})
        a.raise_for_status()
        f = c.get("https://api.open-meteo.com/v1/forecast", params={**base, "past_days": 14, "forecast_days": 15})
        f.raise_for_status()

    def mean(js):
        js = js if isinstance(js, list) else [js]
        acc: dict = {}
        for loc in js:
            dl = loc["daily"]
            for d, p, e in zip(dl["time"], dl["precipitation_sum"], dl["et0_fao_evapotranspiration"]):
                acc.setdefault(d, ([], []))
                if p is not None:
                    acc[d][0].append(p)
                if e is not None:
                    acc[d][1].append(e)
        return {d: (sum(p) / len(p) if p else None, sum(e) / len(e) if e else None) for d, (p, e) in acc.items()}

    m = {**mean(a.json()), **mean(f.json())}
    df = pd.DataFrame([(pd.Timestamp(d), p, e) for d, (p, e) in sorted(m.items())], columns=["d", "P", "E"]).set_index("d")
    df["P"] = df["P"].fillna(0.0)
    df["E"] = df["E"].interpolate().bfill().ffill()
    return df


def gr4j_estimate(station: str, d0, q0: float, fc_vals: list[float], scenarios=None):
    """Modèle hydrologique GR4J (INRAE, base des prévisions Vigicrues) : réservoir de sol + réservoir de routage.
    Tourne 450 jours sur la pluie et l'ET0 du bassin, recale le réservoir de routage sur le débit observé du jour,
    puis avance 14 jours avec la pluie prévue (et chaque scénario d'ensemble si fourni)."""
    import math
    x1, x2, x3, x4, area = GR4J[station]
    conv = area / 86.4
    df = basin_inputs(station, d0)
    past, fut = df[df.index <= d0], df[df.index > d0]
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
    b1, b2 = [0.0] * L, [0.0] * (2 * L)

    def step(p, e, S, R):
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

    S, R, qd = 0.6 * x1, 0.5 * x3, 0.0
    for p, e in zip(past["P"].values, past["E"].values):
        S, R, _, qd = step(float(p), float(e), S, R)
    # Recalage sur le débit observé (comme les modèles de prévision opérationnels)
    qobs = q0 / conv
    tgt = max(qobs - qd, 0.05 * qobs)
    lo, hi = 0.0, 20 * x3
    for _ in range(60):
        mid = (lo + hi) / 2
        if mid * (1 - (1 + (mid / x3) ** 4) ** -0.25) < tgt:
            lo = mid
        else:
            hi = mid
    R = max((lo + hi) / 2 - tgt, 0.0)
    ef = list(fut["E"].values[:14])
    ef += [ef[-1] if ef else 1.0] * (14 - len(ef))
    S0, R0, s1, s2 = S, R, list(b1), list(b2)

    def sim(vals):
        S, R = S0, R0
        b1[:] = s1
        b2[:] = s2
        out = []
        for h, p in enumerate(vals[1:15]):
            S, R, q, _ = step(float(p), float(ef[h]), S, R)
            out.append(q * conv)
        return out

    out = sim(fc_vals)
    if scenarios is not None:
        return out, [sim(s) for s in scenarios]
    return out


def ensemble_rain(station: str) -> list[list[float]]:
    """51 scénarios de pluie journalière (prévision d'ensemble ECMWF via Open-Meteo), moyenne sur le bassin, 15 jours."""
    import pandas as pd
    pts = BASIN_PTS[station][::2]
    r = httpx.get("https://ensemble-api.open-meteo.com/v1/ensemble", timeout=90, params={
        "latitude": ",".join(str(p[0]) for p in pts), "longitude": ",".join(str(p[1]) for p in pts),
        "hourly": "precipitation", "models": "ecmwf_ifs025", "forecast_days": 15, "timezone": "Europe/Brussels"})
    r.raise_for_status()
    js = r.json()
    js = js if isinstance(js, list) else [js]
    per_loc = []
    for loc in js:
        h = pd.DataFrame(loc["hourly"])
        h["time"] = pd.to_datetime(h["time"])
        per_loc.append(h.set_index("time").resample("D").sum(min_count=1))
    m = sum(per_loc) / len(per_loc)
    cols = [c for c in m.columns if c.startswith("precipitation")]
    return [list(m[c].fillna(0.0).values[:15]) for c in cols if len(m) >= 15]



# ---------- Relevé, alertes, journal ----------

def send_alert(station: str, titre: str, txt: str, tag: str, flows: dict, rain_series: list,
               alert_t: datetime, hist) -> None:
    """Envoie l'alerte avec graphiques ; repli sur un texte seul si le graphique échoue."""
    try:
        try:
            fc = rain_forecast(station)
        except Exception as e:
            logger.warning("Prévision de pluie indisponible", error=str(e))
            fc = {}
        png, _ = make_chart(station, flows, rain_series, hist, fc, alert_t)
        push_image(titre, txt, tag, png)
    except Exception as e:
        logger.warning("Graphique impossible, envoi du texte seul", error=str(e))
        push(titre, txt, tag)


def fetch_all(now: datetime, flow_period="P22D", rain_period="P4D"):
    fr = kiwis_values(list(FLOW_TS.values()) + [TEMP_NAMUR_TS], period=flow_period)
    rr = kiwis_values(RAIN_SAMBRE_TS + RAIN_MEUSE_TS, period=rain_period)
    flows = {n: to_hourly(fr.get(ts, []), now) for n, ts in FLOW_TS.items()}
    temp = to_hourly(fr.get(TEMP_NAMUR_TS, []), now)
    rain_s = [to_hourly(rr.get(ts, []), now) for ts in RAIN_SAMBRE_TS]
    rain_m = [to_hourly(rr.get(ts, []), now) for ts in RAIN_MEUSE_TS]
    return flows, temp, rain_s, rain_m


def releve_rows(hours, flows, temp, rain_s, rain_m, states=None, last_ts=None) -> list[dict]:
    rows = []
    for t in hours:
        row = {"Date": local(t), "Temp_Namur": fmt(temp.get(t)),
               "Pluie_Sambre": fmt(basin_mean(rain_s, t)), "Pluie_Meuse": fmt(basin_mean(rain_m, t)),
               "Etat_Landelies": states.get("Landelies", "") if states and t == last_ts else "",
               "Etat_Chooz": states.get("Chooz", "") if states and t == last_ts else ""}
        for n in FLOW_TS:
            row[n] = fmt(flows[n].get(t))
        rows.append(row)
    return rows


def update_history(days: int = 3) -> None:
    """Recalcule les derniers jours de l'historique journalier à partir des relevés horaires."""
    rel = read_csv(RELEVES)
    if not rel:
        return
    last_day = rel[-1]["Date"][:10]
    first = (datetime.fromisoformat(last_day) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    acc: dict = {}
    for r in rel:
        d = r["Date"][:10]
        if d < first:
            continue
        a = acc.setdefault(d, {k: [] for k in HIST_COLS[1:]})
        for n in FLOW_TS:
            if r.get(n) not in ("", None):
                a[n].append(float(r[n]))
        for col, key in (("PluieS", "Pluie_Sambre"), ("PluieM", "Pluie_Meuse")):
            if r.get(key) not in ("", None):
                a[col].append(float(r[key]))
    new = {}
    for d, a in acc.items():
        row = {"Date": d}
        for n in FLOW_TS:
            row[n] = fmt(sum(a[n]) / len(a[n])) if a[n] else ""
        row["PluieS"] = fmt(sum(a["PluieS"])) if a["PluieS"] else ""
        row["PluieM"] = fmt(sum(a["PluieM"])) if a["PluieM"] else ""
        new[d] = row
    hist = {r["Date"]: r for r in read_csv(HISTORIQUE)}
    hist.update(new)
    write_csv(HISTORIQUE, HIST_COLS, [hist[d] for d in sorted(hist)])


def run() -> None:
    now = datetime.now(timezone.utc)
    rules = read_rules()
    flows, temp, rain_s, rain_m = fetch_all(now)
    hours = sorted(set(flows["Profondeville"]) | set(flows["Landelies"]))
    hours = [t for t in hours if t >= now - timedelta(days=REPLAY_DAYS)]
    if not hours:
        raise RuntimeError("Aucune donnée de débit reçue")
    last_ts = hours[-1]
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

    def rain_48h(series, t):
        return sum((basin_mean(series, t - timedelta(hours=h)) or 0.0) for h in range(48))

    states, notes, alert_rows = {}, [], []
    for station, rule in rules.items():
        pts = smooth(sorted(flows[station].items()), rule["smooth"])
        moves = detect_moves(pts, rule)
        c = moves[-1] if moves else None
        etat, en_decrue, pic, actuel = "normal", False, None, None
        if c:
            after = [(t, v) for t, v in pts if t >= c["t"]]
            pic = max(after, key=lambda p: p[1])
            actuel = after[-1]
            en_decrue = actuel[1] <= pic[1] * (1 - rule["baisse"] / 100)
            if last_ts - c["t"] <= timedelta(hours=72):
                etat = "décrue" if en_decrue else "crue"
        states[station] = etat
        seen_c, seen_d = state.get(f"crue:{station}"), state.get(f"decrue:{station}")
        mark = c["t"].isoformat() if c else "aucune"
        if seen_c is None:  # tout premier passage : on mémorise sans alerter
            state[f"crue:{station}"] = mark
            state[f"decrue:{station}"] = mark if en_decrue else ""
            continue
        if c and seen_c != mark and last_ts - c["t"] <= timedelta(hours=RECENT_H):
            r48 = rain_48h(rain_s if BASIN[station] == "S" else rain_m, c["t"])
            cause = f"avec pluie ({r48:.0f} mm en 48 h)" if r48 >= 5 else "sans pluie (barrage, écluse ou pluie en France ?)"
            txt = (f"{station} ({RIVER[station]}) : {c['ref']:.0f} → {c['value']:.0f} m³/s en {rule['window']} h "
                   f"(+{c['pct']:.0f} %), {cause}, détectée {local(c['t'])}.{ADVANCE.get(station, '')}")
            notes.append((f"Alerte crue – {RIVER[station]}", txt, "ocean", station, c["t"]))
            alert_rows.append({"Date": local(c["t"]), "Station": station, "Type": "crue",
                               "Debit": round(c["value"], 1), "Message": txt})
            seen_c, seen_d = mark, ""
        if c and en_decrue and seen_d != mark:
            h_pic = (actuel[0] - pic[0]).total_seconds() / 3600
            txt = (f"{station} ({RIVER[station]}) : {actuel[1]:.0f} m³/s, -{(1 - actuel[1] / pic[1]) * 100:.0f} % "
                   f"depuis le pic de {pic[1]:.0f} m³/s (il y a {h_pic:.0f} h).")
            notes.append((f"Alerte décrue – {RIVER[station]}", txt, "arrow_down", station, actuel[0]))
            alert_rows.append({"Date": local(actuel[0]), "Station": station, "Type": "décrue",
                               "Debit": round(actuel[1], 1), "Message": txt})
            seen_d = mark
        state[f"crue:{station}"] = seen_c
        state[f"decrue:{station}"] = seen_d or ""

    prev = state.get("last_row")
    prev_t = datetime.fromisoformat(prev) if prev else None
    new_hours = [t for t in hours if prev_t is None or t > prev_t]
    if RELEVES.exists() and prev_t is None:
        known = {r["Date"] for r in read_csv(RELEVES)}
        new_hours = [t for t in new_hours if local(t) not in known]
    append_csv(RELEVES, RELEVES_COLS, releve_rows(new_hours, flows, temp, rain_s, rain_m, states, last_ts))
    state["last_row"] = last_ts.isoformat()
    update_history()
    rel = read_csv(RELEVES)
    write_csv(DATA / "releves_10j.csv", RELEVES_COLS, rel[-240:])  # extrait léger pour le guide de pêche

    if notes:
        hist = read_history()
        for titre, txt, tag, st, t_al in notes:
            send_alert(st, titre, txt, tag, flows, rain_s if BASIN[st] == "S" else rain_m, t_al, hist)
        append_csv(ALERTES, ALERT_COLS, alert_rows)
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n")
    logger.info("Relevé terminé", heure=local(last_ts), nouvelles_heures=len(new_hours), alertes=len(notes),
                etats=states)


def test(station: str) -> None:
    now = datetime.now(timezone.utc)
    flows, _, rain_s, rain_m = fetch_all(now)
    last = max(flows[station])
    send_alert(station, f"[TEST] Graphiques – {RIVER.get(station, station)}",
               f"Situation au {local(last)} : {flows[station][last]:.0f} m³/s. Exemple de graphiques joints aux alertes.",
               "chart_with_upwards_trend", flows, rain_s if BASIN[station] == "S" else rain_m, last, read_history())
    logger.info("Notification de test envoyée", station=station)


def bootstrap(years: int = 5) -> None:
    """Historique journalier sur `years` ans (moyennes de débit, cumuls de pluie) et 60 jours de relevés horaires."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=365 * years + 1)
    chunks, a = [], start
    while a < end:
        b = min(a + timedelta(days=366), end)
        chunks.append((a, b))
        a = b
    all_ts = list(FLOW_TS.values()) + RAIN_SAMBRE_TS + RAIN_MEUSE_TS
    series: dict = {ts: {} for ts in all_ts}

    def one(job):
        ts, a, b = job
        data = kiwis_values([ts], **{"from": a.strftime("%Y-%m-%dT%H:%M:%S"), "to": b.strftime("%Y-%m-%dT%H:%M:%S")})
        return ts, to_hourly(data.get(ts, []), end)

    with ThreadPoolExecutor(4) as ex:
        for ts, vals in ex.map(one, [(ts, a, b) for ts in all_ts for a, b in chunks]):
            series[ts].update(vals)

    def daily(ts_list, how):
        acc: dict = {}
        for ts in ts_list:
            per_day: dict = {}
            for t, v in series[ts].items():
                per_day.setdefault(t.astimezone(TZ).strftime("%Y-%m-%d"), []).append(v)
            for d, vals in per_day.items():
                acc.setdefault(d, []).append(sum(vals) / len(vals) if how == "mean" else sum(vals))
        return {d: round(sum(v) / len(v), 2) for d, v in acc.items()}

    cols = {n: daily([ts], "mean") for n, ts in FLOW_TS.items()}
    cols["PluieS"], cols["PluieM"] = daily(RAIN_SAMBRE_TS, "sum"), daily(RAIN_MEUSE_TS, "sum")
    days = sorted(set().union(*[c.keys() for c in cols.values()]))
    write_csv(HISTORIQUE, HIST_COLS, [{"Date": d, **{k: cols[k].get(d, "") for k in HIST_COLS[1:]}} for d in days])
    logger.info("Historique reconstruit", jours=len(days))
    flows, temp, rain_s, rain_m = fetch_all(end, "P60D", "P60D")
    hours = sorted(set(flows["Profondeville"]) | set(flows["Landelies"]))
    write_csv(RELEVES, RELEVES_COLS, releve_rows(hours, flows, temp, rain_s, rain_m))
    if not ALERTES.exists():
        write_csv(ALERTES, ALERT_COLS, [])
    logger.info("Relevés horaires reconstruits", heures=len(hours))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "test":
        test(sys.argv[2] if len(sys.argv) > 2 else "Chooz")
    elif mode == "bootstrap":
        bootstrap()
    else:
        run()
