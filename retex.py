#!/usr/bin/env python3
"""Retex pêche : lit le formulaire GitHub (variable ISSUE_BODY), ajoute la phase de la rivière, le débit,
la température de l'eau et la pluie au moment de la sortie, puis écrit une ligne dans data/retex.csv.
Le retex n'est fermé par le workflow que si la ligne a bien été enregistrée."""
import os
import re
import sys
from datetime import datetime, timedelta

import pandas as pd

import debits as D

RETEX = D.DATA / "retex.csv"
COLS = ["N", "Date", "Debut", "Fin", "Spot", "Riviere", "Resultat", "Prises", "Poste", "Bordure", "Technique", "Leurre",
        "Eau", "Sur_alerte", "Phase", "Debit", "Tendance_24h_pct", "Temp_eau", "Pluie_48h", "Remarques"]
CHAMPS = {"Date": "date", "Heures": "heures", "Spot": "spot", "Résultat": "resultat", "Prises": "prises", "Poste": "poste",
          "Poisson en bordure ou zone calme ?": "bordure", "Technique": "technique", "Leurre et couleur": "leurre",
          "Eau": "eau", "Sortie décidée par une alerte ?": "alerte", "Remarques": "remarques"}
STATION = {"Sambre": "Landelies", "Meuse": "Chooz"}


def parse_body(body: str) -> dict:
    out = {}
    for bloc in re.split(r"^### ", body.replace("\r\n", "\n"), flags=re.M)[1:]:
        titre, _, val = bloc.partition("\n")
        val = " ".join(val.split())
        if val in ("_No response_", "None"):
            val = ""
        if titre.strip() in CHAMPS:
            out[CHAMPS[titre.strip()]] = val
    return out


def parse_date(txt: str, defaut: datetime) -> datetime:
    txt = txt.strip()
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", txt)
    if m:
        return datetime(int(m[1]), int(m[2]), int(m[3]))
    m = re.search(r"(\d{1,2})[/.\- ](\d{1,2})(?:[/.\- ](\d{2,4}))?", txt)
    if m:
        y = int(m[3]) if m[3] else defaut.year
        y += 2000 if y < 100 else 0
        d = datetime(y, int(m[2]), int(m[1]))
        if not m[3] and d > defaut + timedelta(days=1):  # « 28/12 » noté en janvier : l'année d'avant
            d = d.replace(year=y - 1)
        return d
    if txt.lower() in ("hier",):
        return defaut - timedelta(days=1)
    return defaut


def parse_heures(txt: str):
    h = re.findall(r"(\d{1,2})\s*(?:[hH:]\s*(\d{2})?)?", txt)
    h = [(int(a), int(b or 0)) for a, b in h if a and int(a) < 24]
    deb = h[0] if h else (12, 0)
    fin = h[1] if len(h) > 1 else None
    return deb, fin


def contexte(riviere: str, quand: datetime) -> dict:
    """Phase, débit, tendance, température de l'eau et pluie au début de la sortie, d'après les relevés horaires."""
    vide = {"Phase": "", "Debit": "", "Tendance_24h_pct": "", "Temp_eau": "", "Pluie_48h": ""}
    rel = pd.read_csv(D.RELEVES)
    idx = pd.to_datetime(rel["Date"]).dt.tz_localize(D.TZ, ambiguous="NaT", nonexistent="NaT")
    rel = rel[idx.notna().values]
    rel.index = idx[idx.notna()]
    rel = rel[~rel.index.duplicated()].sort_index()
    t = pd.Timestamp(quand, tz=D.TZ)
    rel = rel[rel.index <= t]
    if not len(rel) or t - rel.index[-1] > pd.Timedelta(hours=6):
        return vide
    out = dict(vide)
    temp = pd.to_numeric(rel["Temp_Namur"], errors="coerce").dropna()
    if len(temp):
        out["Temp_eau"] = round(float(temp.iloc[-1]), 1)
    station = STATION.get(riviere)
    if not station:
        return out
    s = pd.to_numeric(rel[station], errors="coerce").dropna()
    if len(s) < 48:
        return out
    pluie = pd.to_numeric(rel["Pluie_Sambre" if riviere == "Sambre" else "Pluie_Meuse"], errors="coerce").fillna(0)
    out["Pluie_48h"] = round(float(pluie[pluie.index > t - pd.Timedelta(hours=48)].sum()), 1)
    out["Debit"] = round(float(s.iloc[-1]), 1)
    avant = s[s.index <= s.index[-1] - pd.Timedelta(hours=24)]
    if len(avant) and avant.iloc[-1]:
        out["Tendance_24h_pct"] = round((float(s.iloc[-1]) / float(avant.iloc[-1]) - 1) * 100)
    d0 = pd.Timestamp(quand.date())
    h = D.read_history()[station].dropna()
    h = h[h.index < d0 - pd.Timedelta(days=30)]
    out["Phase"] = D.estimation(station, s, h, {}, d0)[2]
    return out


def main() -> int:
    f = parse_body(os.environ.get("ISSUE_BODY", ""))
    if not f.get("spot") or not f.get("resultat"):
        print("Formulaire incomplet : spot ou résultat manquant", file=sys.stderr)
        return 1
    cree = os.environ.get("ISSUE_DATE")
    maintenant = (pd.Timestamp(cree).tz_convert(D.TZ) if cree else pd.Timestamp.now(D.TZ)).to_pydatetime().replace(tzinfo=None)
    jour = parse_date(f.get("date", ""), maintenant.replace(hour=0, minute=0, second=0, microsecond=0))
    deb, fin = parse_heures(f.get("heures", ""))
    quand = jour.replace(hour=deb[0], minute=deb[1])
    riviere = "Sambre" if "Sambre" in f["spot"] else "Meuse" if "Meuse" in f["spot"] else "Eau d'Heure"
    ctx = contexte(riviere, quand)
    ligne = {"N": os.environ.get("ISSUE_NUMBER", ""), "Date": jour.strftime("%Y-%m-%d"), "Debut": f"{deb[0]:02d}:{deb[1]:02d}",
             "Fin": f"{fin[0]:02d}:{fin[1]:02d}" if fin else "", "Spot": re.sub(r"\s*\(.*\)", "", f["spot"]), "Riviere": riviere,
             "Resultat": f["resultat"], "Prises": f.get("prises", ""), "Poste": f.get("poste", ""),
             "Bordure": f.get("bordure", ""), "Technique": f.get("technique", ""), "Leurre": f.get("leurre", ""),
             "Eau": f.get("eau", ""), "Sur_alerte": f.get("alerte", ""), "Remarques": f.get("remarques", ""), **ctx}
    D.append_csv(RETEX, COLS, [ligne])
    n = len(D.read_csv(RETEX))
    auto = []
    if ctx["Phase"]:
        auto.append(f"phase « {ctx['Phase']} »")
    if ctx["Debit"] != "":
        auto.append(f"{ctx['Debit']} m³/s à {STATION[riviere]} ({ctx['Tendance_24h_pct']:+} % sur 24 h)"
                    if ctx["Tendance_24h_pct"] != "" else f"{ctx['Debit']} m³/s à {STATION[riviere]}")
    if ctx["Temp_eau"] != "":
        auto.append(f"eau à {ctx['Temp_eau']} °C (Namur)")
    if ctx["Pluie_48h"] != "":
        auto.append(f"{ctx['Pluie_48h']} mm de pluie en 48 h")
    txt = (f"Retex n° {n} enregistré : {ligne['Spot']}, {jour:%d/%m/%Y} à {ligne['Debut']}, {f['resultat'].lower()}.\n\n"
           + ("Ajouté automatiquement : " + ", ".join(auto) + "." if auto else
              "Pas de relevé de débit pour ce moment : phase et débit laissés vides.")
           + ("\n\nAu bout d'une dizaine de retex, on pourra comparer vos résultats par phase." if n < 10 else ""))
    (D.ROOT / "retex_reponse.md").write_text(txt + "\n", encoding="utf-8")
    print(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
