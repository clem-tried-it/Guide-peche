"""Rejeu quotidien d'une crue passée (Sambre et Meuse) pour le bulletin pêche de Clément.

Chaque jour, une crue passée de chaque rivière est rejouée avec ce qui était connu à l'époque : prévisions de
pluie archivées faites 3, 2 et 1 jours avant (ECMWF, Open-Meteo « previous runs »), débit observé jusque-là,
modèle actuel (ensemble statistique + GR4J de debits.py) et règles d'alerte actuelles. La pluie tombée ne sert
qu'à la comparaison. Une fois sur cinq, un jour de forte pluie annoncée SANS crue est rejoué à la place, pour
compter aussi les fausses alertes.

Seules les crues depuis juin 2024 sont rejouées : le modèle a été calé sur nov. 2021 – mai 2024.

Sorties : data/rejeu_crues.md (rejeu du jour + score, lu par le bulletin) et data/rejeu_crues.csv (historique).
Usage : python rejeu.py [jour|catalogue] [AAAA-MM-JJ]
"""
import csv
import sys
import time
from datetime import date, datetime, timedelta, timezone
from statistics import median

import httpx

import debits as D

START = date(2024, 6, 1)
STATIONS = {"Landelies": ("Sambre", "PluieS"), "Chooz": ("Meuse", "PluieM")}
PREV_CSV = D.DATA / "rejeu_pluie_prevue.csv"
CATALOGUE = D.DATA / "rejeu_catalogue.csv"
OUT_CSV = D.DATA / "rejeu_crues.csv"
OUT_MD = D.DATA / "rejeu_crues.md"
PREV_COLS = ["Date", "Station", "Obs"] + [f"P{i}" for i in range(1, 8)]
CAT_COLS = ["Station", "Type", "Date", "Heure_alerte", "Ref", "Pic", "Date_pic", "Pluie_prevue", "Rang"]
OUT_COLS = ["Date_rejeu", "Station", "Type", "Date", "Heure_alerte", "Ref", "Pic", "Pluie_tombee",
            "Pluie_J3", "Pluie_J2", "Pluie_J1", "Annonce_J3", "Annonce_J2", "Annonce_J1",
            "Pic_estime_J1", "Avance_h", "Verdict"]
CALME_TOUS_LES = 5        # un jour sur 5 : jour de forte pluie annoncée sans crue
CALME_PLUIE_MIN = 10.0    # mm prévus sur les 2 jours suivants pour être un candidat « fausse alerte »
ISSUE_HEURE = 20          # l'estimation d'un jour E est supposée disponible à 20 h (prévision du jour)
PREVRUN = "https://previous-runs-api.open-meteo.com/v1/forecast"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"


_CLIENT = None


def _client() -> httpx.Client:
    """Client réutilisé, forcé en IPv4 : depuis GitHub Actions, la connexion IPv6 vers Open-Meteo restait bloquée
    jusqu'au délai (3 min perdues par requête le 22/11/2026)."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.Client(transport=httpx.HTTPTransport(local_address="0.0.0.0", retries=2),
                               timeout=httpx.Timeout(180, connect=20))
    return _CLIENT


def _get(url: str, params: dict, timeout: int = 180):
    """GET avec nouveaux essais : coupure réseau (pause courte), 429 ou erreur serveur (pause d'une minute)."""
    for essai in range(6):
        try:
            r = _client().get(url, params=params, timeout=httpx.Timeout(timeout, connect=20))
            if r.status_code not in (429, 500, 502, 503, 504):
                r.raise_for_status()
                return r
            D.logger.warning("Open-Meteo refuse ou sature, pause", status=r.status_code, essai=essai)
            time.sleep(65)
        except httpx.TransportError as e:
            D.logger.warning("Coupure réseau, nouvel essai", error=str(e), essai=essai)
            time.sleep(5)
    raise RuntimeError(f"{url} ne répond pas")


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def _fr(d: date) -> str:
    return d.strftime("%d/%m/%Y")


# ---------- Pluie prévue archivée (moyenne du bassin, mm/jour) ----------

def fetch_prev_rain(station: str, start: date, end: date) -> dict:
    """{date: [obs, P1..P7]} : Obs = pluie du jour (run le plus récent), Pk = prévision faite k jours avant."""
    pts = D.BASIN_PTS[station]
    vars_ = ["precipitation"] + [f"precipitation_previous_day{i}" for i in range(1, 8)]
    acc: dict = {}
    for i in range(0, len(pts), 2):
        grp = pts[i:i + 2]
        params = {"latitude": ",".join(str(p[0]) for p in grp), "longitude": ",".join(str(p[1]) for p in grp),
                  "hourly": ",".join(vars_), "models": "ecmwf_ifs025", "timezone": "Europe/Brussels",
                  "start_date": str(start), "end_date": str(end)}
        r = _get(PREVRUN, params)
        time.sleep(3)  # rester sous la limite par minute d'Open-Meteo
        js = r.json()
        for loc in js if isinstance(js, list) else [js]:
            h = loc["hourly"]
            for k, t in enumerate(h["time"]):
                day = t[:10]
                row = acc.setdefault(day, [[0.0, 0] for _ in vars_])
                for j, v in enumerate(vars_):
                    x = h[v][k]
                    if x is not None:
                        row[j][0] += x
                        row[j][1] += 1
    n_pts = len(pts)
    out = {}
    for day, row in acc.items():
        # somme horaire par point puis moyenne des points ; jour incomplet (< 20 h par point) = absent
        out[_d(day)] = [round(s / n_pts, 2) if n >= 20 * n_pts else None for s, n in row]
    return out


CHUNK_DAYS = 45          # téléchargement par tranches : Open-Meteo compte le volume demandé
BUDGET_S = 30 * 60       # au-delà, on enregistre et on reprendra au prochain lancement


def _save_prev(table: dict) -> None:
    rows = [{"Date": str(d), "Station": st, **{c: ("" if v is None else v) for c, v in zip(PREV_COLS[2:], vals)}}
            for st in STATIONS for d, vals in sorted(table[st].items())]
    D.write_csv(PREV_CSV, PREV_COLS, rows)


def update_prev_rain() -> tuple[dict, bool]:
    """Table {station: {date: [obs, P1..P7]}} tenue à jour dans data/rejeu_pluie_prevue.csv, téléchargée par
    tranches et enregistrée après chacune. Renvoie (table, complète jusqu'à hier ?)."""
    t0 = time.monotonic()
    table = {s: {} for s in STATIONS}
    for r in D.read_csv(PREV_CSV):
        table[r["Station"]][_d(r["Date"])] = [float(r[c]) if r[c] not in ("", None) else None for c in PREV_COLS[2:]]
    hier = date.today() - timedelta(days=1)
    complete = True
    for st in STATIONS:
        known = [d for d, v in table[st].items() if None not in v]
        start = max(known) - timedelta(days=3) if known else START - timedelta(days=10)
        while start <= hier:
            if time.monotonic() - t0 > BUDGET_S:
                complete = False
                break
            end = min(start + timedelta(days=CHUNK_DAYS - 1), hier)
            table[st].update(fetch_prev_rain(st, start, end))
            _save_prev(table)
            D.logger.info("Prévisions archivées téléchargées", station=st, jusqu_au=str(end))
            start = end + timedelta(days=1)
    return table, complete


# ---------- Catalogue des crues et des jours « fausse alerte possible » ----------

def build_catalogue(table: dict) -> list[dict]:
    now = datetime.now(timezone.utc)
    rules = D.read_rules()
    rows = []
    for st, (riv, _) in STATIONS.items():
        pts = {}
        a = datetime(START.year, START.month, START.day, tzinfo=timezone.utc) - timedelta(days=5)
        while a < now:
            b = min(a + timedelta(days=180), now)
            data = D.kiwis_values([D.FLOW_TS[st]], **{"from": a.strftime("%Y-%m-%dT%H:%M:%S"),
                                                       "to": b.strftime("%Y-%m-%dT%H:%M:%S")})
            pts.update(D.to_hourly(data[D.FLOW_TS[st]], now))
            a = b
        rule = rules[st]
        sm = D.smooth(D.fill_gaps(sorted(pts.items())), rule["smooth"])
        moves = D.detect_moves(sm, rule)
        fin = now - timedelta(days=5)  # crue trop récente : pic pas encore connu
        crues = []
        for i, m in enumerate(moves):
            if m["t"].date() < START + timedelta(days=4) or m["t"] > fin:
                continue
            # seulement le début d'un épisode : une vague qui suit une autre de moins de 5 jours n'est pas
            # une crue « à prévoir », la rivière monte déjà (même règle que le point quotidien)
            if i and m["t"] - moves[i - 1]["t"] <= timedelta(hours=120):
                continue
            fin_ep = m["t"] + timedelta(hours=120)
            for n in moves[i + 1:]:
                if n["t"] - fin_ep <= timedelta(0):
                    fin_ep = n["t"] + timedelta(hours=120)
            # pic mesuré sur les 7 jours qui suivent l'alerte, la même fenêtre que le pic estimé la veille
            # (sinon une 2e vague, une semaine plus tard, passe pour le pic de la crue : 02/01/2025, 114 → 154 m³/s)
            after = [(t, v) for t, v in sm if m["t"] <= t <= min(fin_ep, m["t"] + timedelta(days=7))]
            tp, vp = max(after, key=lambda p: p[1])
            crues.append({"Station": st, "Type": "crue", "Date": str(m["t"].astimezone(D.TZ).date()),
                          "Heure_alerte": m["t"].isoformat(), "Ref": round(m["ref"], 1), "Pic": round(vp, 1),
                          "Date_pic": tp.astimezone(D.TZ).strftime("%Y-%m-%d %H:%M"), "Pluie_prevue": ""})
        crues.sort(key=lambda r: -r["Pic"])
        for i, r in enumerate(crues, 1):
            r["Rang"] = i
        # Jours de forte pluie annoncée (2 jours suivants, prévision du jour) sans aucune crue dans les 5 jours
        dates_crue = [_d(r["Date"]) for r in crues] + [m["t"].astimezone(D.TZ).date() for m in moves]
        calmes = []
        hq = D.read_history()[st]
        for E, vals in table[st].items():
            nxt = [table[st].get(E + timedelta(days=h)) for h in (1, 2)]
            if E < START or any(v is None or v[h] is None for h, v in zip((1, 2), nxt)) or E > fin.date():
                continue
            p2 = nxt[0][1] + nxt[1][2]
            if p2 < CALME_PLUIE_MIN or any(E - timedelta(days=5) <= dc <= E + timedelta(days=5) for dc in dates_crue):
                continue
            calmes.append({"Station": st, "Type": "calme", "Date": str(E + timedelta(days=1)), "Heure_alerte": "",
                           "Ref": round(float(hq.get(str(E), float("nan"))), 1), "Pic": "",
                           "Date_pic": "", "Pluie_prevue": round(p2, 1)})
        calmes.sort(key=lambda r: -r["Pluie_prevue"])
        # un seul jour par épisode pluvieux : écarter les candidats à moins de 4 jours d'un candidat mieux classé
        gard = []
        for r in calmes:
            if all(abs((_d(r["Date"]) - _d(g["Date"])).days) >= 4 for g in gard):
                gard.append(r)
        for i, r in enumerate(gard, 1):
            r["Rang"] = i
        rows += crues + gard
    D.write_csv(CATALOGUE, CAT_COLS, rows)
    return rows


# ---------- Rejeu d'un événement ----------

_ARCH: dict = {}


def _basin_inputs_archive(station: str, d0):
    """Remplace debits.basin_inputs pour le passé : pluie et ET0 de l'archive Open-Meteo (pas de prévision).
    Un seul téléchargement par bassin (depuis START − 460 jours), puis découpe."""
    import pandas as pd
    if station not in _ARCH:
        _ARCH[station] = _archive_full(station)
    df = _ARCH[station]
    return df[(df.index >= d0 - pd.Timedelta(days=450)) & (df.index <= d0 + pd.Timedelta(days=15))]


def _archive_full(station: str):
    import pandas as pd
    pts = D.BASIN_PTS[station]
    params = {"latitude": ",".join(str(p[0]) for p in pts), "longitude": ",".join(str(p[1]) for p in pts),
              "daily": "precipitation_sum,et0_fao_evapotranspiration", "timezone": "Europe/Brussels",
              "start_date": str(START - timedelta(days=460)), "end_date": str(date.today() - timedelta(days=6))}
    r = _get(ARCHIVE, params)
    js = r.json()
    acc: dict = {}
    for loc in js if isinstance(js, list) else [js]:
        dl = loc["daily"]
        for d, p, e in zip(dl["time"], dl["precipitation_sum"], dl["et0_fao_evapotranspiration"]):
            a = acc.setdefault(d, ([], []))
            if p is not None:
                a[0].append(p)
            if e is not None:
                a[1].append(e)
    df = pd.DataFrame([(pd.Timestamp(d), sum(p) / len(p) if p else None, sum(e) / len(e) if e else None)
                       for d, (p, e) in sorted(acc.items())], columns=["d", "P", "E"]).set_index("d")
    df["P"] = df["P"].fillna(0.0)
    df["E"] = df["E"].interpolate().bfill().ffill()
    return df


D.basin_inputs = _basin_inputs_archive


def _clim(table_st: dict, d: date) -> float:
    """Pluie moyenne de saison (± 15 jours) sur l'archive, pour les jours 8 à 14 (au-delà des prévisions)."""
    v = [x[0] for k, x in table_st.items() if x[0] is not None
         and abs(((k.timetuple().tm_yday - d.timetuple().tm_yday) + 182) % 365 - 182) <= 15]
    return sum(v) / len(v) if v else 2.0


def estimate_at(station: str, E: date, table: dict, hist):
    """Estimation faite le soir du jour E (d0 = E), avec la seule pluie prévue ce jour-là. Renvoie les débits
    estimés pour E+1 … E+14, ou None si une donnée manque."""
    import pandas as pd
    col = STATIONS[station][1]
    tst = table[station]
    q0, qp = hist[station].get(pd.Timestamp(E)), hist[station].get(pd.Timestamp(E - timedelta(days=1)))
    if tst.get(E) is None or tst[E][0] is None or q0 is None or q0 != q0 or qp is None or qp != qp:
        return None
    fc = [tst[E][0]]
    for h in range(1, 15):
        t = tst.get(E + timedelta(days=h))
        if h <= 7:
            if t is None or t[h] is None:
                return None
            fc.append(t[h])
        else:
            fc.append(_clim(tst, E + timedelta(days=h)))
    d0 = pd.Timestamp(E)
    _, raw = D.estimate_flow(station, hist[station], hist[col], d0, float(q0), float(qp), fc)
    try:
        g = D.gr4j_estimate(station, d0, float(q0), fc)
        return [(a * b) ** 0.5 for a, b in zip(raw, g)], fc
    except Exception as e:  # pragma: no cover
        D.logger.warning("GR4J indisponible pour le rejeu", error=str(e))
        return raw, fc


def rises(station: str, q0: float, est: list[float], horizon: int = 4) -> list[int]:
    """Jours (1 … horizon) où l'estimation annonce une montée : même critère que l'image d'alerte
    (hausse > 10 % et d'au moins le quart du seuil absolu d'alerte)."""
    min_d = D.DEFAULT_RULES[station]["abs"] / 2
    seq = [q0] + list(est)
    return [i for i in range(1, min(horizon, len(est)) + 1)
            if seq[i - 1] and (seq[i] - seq[i - 1]) / seq[i - 1] > 0.1 and seq[i] - seq[i - 1] >= min_d / 2]


def rejeu(ev: dict, table: dict, hist) -> dict:
    import pandas as pd
    st = ev["Station"]
    tst = table[st]
    Dv = _d(ev["Date"])  # jour de l'alerte réelle (crue) ou jour visé par la pluie annoncée (calme)
    window = [Dv - timedelta(days=2), Dv - timedelta(days=1), Dv] if ev["Type"] == "crue" else [Dv, Dv + timedelta(days=1)]
    tombe = sum((tst.get(d) or [0])[0] or 0 for d in window)
    res = {"Date_rejeu": str(date.today()), "Station": st, "Type": ev["Type"], "Date": ev["Date"],
           "Heure_alerte": ev["Heure_alerte"], "Ref": ev["Ref"], "Pic": ev["Pic"], "Pluie_tombee": round(tombe, 1)}
    annonces = {}
    for k in (3, 2, 1):
        E = Dv - timedelta(days=k)
        # pluie de la fenêtre connue le soir de E : observée pour les jours <= E, prévue ensuite
        prev = 0.0
        ok = True
        for d in window:
            h = (d - E).days
            if h <= 0:
                prev += (tst.get(d) or [0])[0] or 0
            else:
                v = tst.get(d)
                if v is None or v[h] is None:
                    ok = False
                    break
                prev += v[h]
        res[f"Pluie_J{k}"] = round(prev, 1) if ok else ""
        out = estimate_at(st, E, table, hist)
        if out is None:
            res[f"Annonce_J{k}"] = "donnée manquante"
            continue
        est, _ = out
        q0 = float(hist[st].get(pd.Timestamp(E)))
        if ev["Type"] == "crue":
            # montée annoncée autour de l'alerte réelle (la veille, le jour même ou le lendemain)
            hs = [h for h in rises(st, q0, est, horizon=k + 1) if abs((E + timedelta(days=h) - Dv).days) <= 1]
        else:
            hs = rises(st, q0, est, horizon=3)
        annonces[k] = E + timedelta(days=hs[0]) if hs else None
        res[f"Annonce_J{k}"] = str(annonces[k]) if annonces[k] else "non"
        if k == 1:
            res["Pic_estime_J1"] = round(max([q0] + est[:7]), 1)
    # Verdict
    if ev["Type"] == "crue":
        t_al = datetime.fromisoformat(ev["Heure_alerte"])
        prem = [k for k in (3, 2, 1) if annonces.get(k)]
        if prem:
            k = max(prem)
            E = Dv - timedelta(days=k)
            t_iss = datetime(E.year, E.month, E.day, ISSUE_HEURE, tzinfo=D.TZ)
            av = (t_al - t_iss).total_seconds() / 3600
            res["Avance_h"] = round(av)
            res["Verdict"] = (f"Prévenu ~{av:.0f} h avant l'alerte (dès J-{k})" if av >= 6
                              else "Pas mieux que l'alerte (annonce trop tardive)")
        else:
            res["Avance_h"] = ""
            p1 = res.get("Pluie_J1")
            if p1 not in ("", None) and tombe > 0:
                res["Verdict"] = f"Raté : pluie prévue la veille {p1:.0f} mm pour {tombe:.0f} tombés ({(p1 / tombe - 1) * 100:+.0f} %)"
            else:
                res["Verdict"] = "Raté : aucune montée annoncée"
    else:
        fa = [k for k in (3, 2, 1) if annonces.get(k)]
        res["Avance_h"] = ""
        res["Verdict"] = (f"Fausse alerte : montée annoncée à J-{max(fa)} qui n'est pas venue" if fa
                          else "Bien vu : pluie annoncée mais pas de montée estimée")
    return res


# ---------- Choix du jour, texte du bulletin ----------

def pick(cat: list[dict], done: list[dict], st: str, typ: str) -> dict | None:
    vus = {(r["Station"], r["Type"], r["Date"]) for r in done}
    cands = [r for r in cat if r["Station"] == st and r["Type"] == typ]
    cands.sort(key=lambda r: int(r["Rang"]))
    for r in cands:
        if (st, typ, r["Date"]) not in vus:
            return r
    if not cands:
        return None
    # catalogue épuisé : reprendre l'événement rejoué il y a le plus longtemps
    last = {(r["Station"], r["Type"], r["Date"]): r["Date_rejeu"] for r in done}
    return min(cands, key=lambda r: last.get((st, typ, r["Date"]), ""))


def _h(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(D.TZ).strftime("%d/%m à %H h") if iso else ""


def lines_for(r: dict) -> list[str]:
    riv = STATIONS[r["Station"]][0]
    pj = [r.get(f"Pluie_J{k}") for k in (3, 2, 1)]
    pj_txt = ", ".join(f"{'?' if v in ('', None) else f'{float(v):.0f}'} mm à J-{k}" for v, k in zip(pj, (3, 2, 1)))
    ann = ", ".join(f"J-{k} : {'oui (' + _d(r[f'Annonce_J{k}']).strftime('%d/%m') + ')' if r[f'Annonce_J{k}'][:2] == '20' else r[f'Annonce_J{k}']}"
                    for k in (3, 2, 1))
    if r["Type"] == "crue":
        tete = (f"**{riv} – crue du {_fr(_d(r['Date']))}** (alerte le {_h(r['Heure_alerte'])}, "
                f"{float(r['Ref']):.0f} → {float(r['Pic']):.0f} m³/s)")
        pic = (f" Pic estimé la veille : {float(r['Pic_estime_J1']):.0f} m³/s (mesuré {float(r['Pic']):.0f})."
               if r.get("Pic_estime_J1") not in ("", None) else "")
    else:
        tete = f"**{riv} – jour de pluie annoncée sans crue, {_fr(_d(r['Date']))}**"
        pic = ""
    return [tete,
            f"Pluie {'des 3 jours avant l’alerte' if r['Type'] == 'crue' else 'des 2 jours visés'} : attendue {pj_txt} ; "
            f"tombée {float(r['Pluie_tombee']):.0f} mm.",
            f"Montée annoncée : {ann}.{pic}",
            f"Verdict : {r['Verdict']}."]


def score(done: list[dict], st: str) -> str:
    cr = [r for r in done if r["Station"] == st and r["Type"] == "crue"]
    ca = [r for r in done if r["Station"] == st and r["Type"] == "calme"]
    prev = [r for r in cr if r["Verdict"].startswith("Prévenu")]
    av = [float(r["Avance_h"]) for r in prev if r["Avance_h"] not in ("", None)]
    fa = [r for r in ca if r["Verdict"].startswith("Fausse")]
    txt = f"{STATIONS[st][0]} : {len(prev)} crue(s) prévenue(s) à l'avance sur {len(cr)}"
    if av:
        txt += f" (avance médiane ~{median(av):.0f} h)"
    txt += f" ; {len(fa)} fausse(s) alerte(s) sur {len(ca)} jour(s) de pluie sans crue."
    return txt


def write_md(today_rows: list[dict], done: list[dict]) -> None:
    out = [f"# Rejeu d'une crue – {_fr(date.today())}", "",
           "Avec la météo prévue à l'époque (ECMWF archivé), le modèle et les règles d'alerte actuels : "
           "le système aurait-il prévenu, et combien de temps avant l'alerte ?", ""]
    for r in today_rows:
        out += lines_for(r) + [""]
    out += ["**Score cumulé**", ""] + [f"- {score(done, st)}" for st in STATIONS] + [
        "", "Rejeu sur les crues depuis juin 2024 (le modèle a appris sur 2021 – mai 2024). "
            "« Prévenu » = début de montée annoncé au plus tard le lendemain de l'alerte réelle, la prévision du "
            "soir (20 h) servant d'heure de départ. Seule l'évaporation (ET0) est prise après coup, effet faible."]
    OUT_MD.write_text("\n".join(out) + "\n", encoding="utf-8")


def jour(force_date: str | None = None) -> None:
    table, complete = update_prev_rain()
    if not complete:
        n = sum(len(v) for v in table.values())
        total = 2 * (date.today() - START).days
        OUT_MD.write_text(f"# Rejeu d'une crue – {_fr(date.today())}\n\nPremier téléchargement des prévisions archivées "
                          f"en cours ({min(100, n * 100 // max(total, 1))} %) : pas de rejeu aujourd'hui.\n", encoding="utf-8")
        D.logger.info("Prévisions archivées incomplètes, rejeu reporté", pct=n * 100 // max(total, 1))
        return
    cat = D.read_csv(CATALOGUE)
    if not cat or date.today().day in (1, 15):  # nouvelles crues ajoutées deux fois par mois
        cat = build_catalogue(table)
    done = D.read_csv(OUT_CSV)
    if any(r["Date_rejeu"] == str(date.today()) for r in done) and not force_date:
        D.logger.info("Rejeu déjà fait aujourd'hui")
        return
    jours = sorted({r["Date_rejeu"] for r in done})
    typ = "calme" if (len(jours) + 1) % CALME_TOUS_LES == 0 else "crue"
    hist = D.read_history()
    rows = []
    for st in STATIONS:
        ev = pick(cat, done, st, typ) or pick(cat, done, st, "crue")
        if force_date:
            ev = next((r for r in cat if r["Station"] == st and r["Date"] == force_date), ev)
        if ev is None:
            continue
        try:
            rows.append(rejeu(ev, table, hist))
        except Exception as e:
            D.logger.warning("Rejeu impossible", station=st, date=ev["Date"], error=str(e))
    D.append_csv(OUT_CSV, OUT_COLS, rows)
    write_md(rows, done + rows)
    D.logger.info("Rejeu terminé", rejeux=[(r["Station"], r["Date"], r["Verdict"]) for r in rows])


def tout() -> None:
    """Contrôle : rejoue tout le catalogue d'un coup (data/rejeu_tout.csv et .md), sans toucher au rejeu du jour."""
    table, complete = update_prev_rain()
    if not complete:
        D.logger.info("Prévisions archivées incomplètes : relancer")
        return
    cat = build_catalogue(table)
    hist = D.read_history()
    out = []
    for ev in cat:
        try:
            out.append(rejeu(ev, table, hist))
        except Exception as e:
            D.logger.warning("Rejeu impossible", station=ev["Station"], date=ev["Date"], error=str(e))
    D.write_csv(D.DATA / "rejeu_tout.csv", OUT_COLS, out)
    lignes = [f"# Rejeu de tout le catalogue – {_fr(date.today())}", ""] + [f"- {score(out, st)}" for st in STATIONS]
    (D.DATA / "rejeu_tout.md").write_text("\n".join(lignes) + "\n", encoding="utf-8")
    D.logger.info("Rejeu complet terminé", n=len(out))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "jour"
    if mode == "catalogue":
        tb, ok = update_prev_rain()
        if ok:
            build_catalogue(tb)
    elif mode == "tout":
        tout()
    else:
        jour(sys.argv[2] if len(sys.argv) > 2 else None)
