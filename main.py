import os
import json
import math
import requests
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Any

# ==========================================
# 1. DOWNLOAD DATI METEO OPEN DATA ARPA
# ==========================================
ENDPOINT_SENSORS_ANAGRAFICA = "https://www.dati.lombardia.it/resource/nf78-nj6b.json"
ENDPOINT_METEO_DATA = "https://www.dati.lombardia.it/resource/647i-nhxk.json"
ID_STAZIONE = "1545"  # San Siro Alpe Rescascia (1285 m)

def get_sensor_ids_for_station(id_stazione: str) -> Dict[str, str]:
    """Recupera gli ID dei sensori attivi per la stazione."""
    params = {
        "$where": f"idstazione = '{id_stazione}' AND idstatosensore = 'A'",
        "$limit": 50
    }
    response = requests.get(ENDPOINT_SENSORS_ANAGRAFICA, params=params, timeout=20)
    response.raise_for_status()
    sensori = response.json()
    
    mappa = {}
    for s in sensori:
        tipo = s.get("tipologia", "").lower()
        idsensore = s.get("idsensore")
        if "precipitazione" in tipo:
            mappa["pioggia"] = idsensore
        elif "temperatura" in tipo:
            mappa["temperatura"] = idsensore
        elif "umidit" in tipo:
            mappa["umidita"] = idsensore
        elif "vento" in tipo or "velocit" in tipo:
            mappa["vento"] = idsensore
    return mappa

def download_weather_history(mappa_sensori: Dict[str, str], days: int = 45) -> pd.DataFrame:
    """Scarica le misurazioni orarie degli ultimi N giorni."""
    if not mappa_sensori:
        raise ValueError("Nessun sensore individuato per la stazione.")

    start_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    id_list = "','".join(mappa_sensori.values())
    
    params = {
        "$where": f"idsensore in ('{id_list}') AND data >= '{start_date}' AND valore != -9999",
        "$limit": 50000,
        "$order": "data ASC"
    }
    response = requests.get(ENDPOINT_METEO_DATA, params=params, timeout=30)
    response.raise_for_status()
    records = response.json()

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df["data"] = pd.to_datetime(df["data"])
    df["valore"] = pd.to_numeric(df["valore"], errors="coerce")

    inv_map = {v: k for k, v in mappa_sensori.items()}
    df["tipo"] = df["idsensore"].map(inv_map)
    return df.pivot_table(index="data", columns="tipo", values="valore", aggfunc="mean").reset_index()

def aggregate_daily(df_hourly: pd.DataFrame) -> List[Dict[str, Any]]:
    """Aggrega le letture orarie in valori giornalieri."""
    if df_hourly.empty:
        return []

    df_hourly["giorno"] = df_hourly["data"].dt.strftime("%Y-%m-%d")
    agg_rules = {}
    if "pioggia" in df_hourly.columns: agg_rules["pioggia"] = "sum"
    if "temperatura" in df_hourly.columns: agg_rules["temperatura"] = "mean"
    if "umidita" in df_hourly.columns: agg_rules["umidita"] = "mean"
    if "vento" in df_hourly.columns: agg_rules["vento"] = "max"

    df_daily = df_hourly.groupby("giorno").agg(agg_rules).reset_index()
    serie = []
    for _, row in df_daily.iterrows():
        vento_kmh = row.get("vento", 0.0) * 3.6 if pd.notna(row.get("vento")) else 10.0
        serie.append({
            "data": row["giorno"],
            "pioggia_mm": round(float(row.get("pioggia", 0.0)), 1),
            "t_media": round(float(row.get("temperatura", 15.0)), 1),
            "rh_media": round(float(row.get("umidita", 70.0)), 1),
            "vento_max": round(float(vento_kmh), 1)
        })
    return serie

# ==========================================
# 2. ANALISI SICCITA' PREGRESSA & RITARDO
# ==========================================
class AnalizzatoreSiccitaPorcini:
    def __init__(self, soglia_evento: float = 35.0):
        self.soglia_evento = soglia_evento

    def analizza(self, serie: List[Dict[str, Any]]) -> Dict[str, Any]:
        n = len(serie)
        giorno_oggi = serie[-1]

        indice_ev = None
        pioggia_ev = 0.0
        for i in range(n - 1, 2, -1):
            c3 = sum(serie[k]["pioggia_mm"] for k in range(i - 2, i + 1))
            if c3 >= self.soglia_evento:
                indice_ev = i
                pioggia_ev = c3
                break

        if indice_ev is None:
            return {
                "evento_rilevato": False,
                "stato": "Nessuna pioggia significativa recente",
                "pioggia_30gg_totale": sum(d["pioggia_mm"] for d in serie[-30:]),
                "t_media_attuale": giorno_oggi["t_media"],
                "rh_media_attuale": giorno_oggi["rh_media"],
                "vento_max_attuale": giorno_oggi["vento_max"]
            }

        giorni_da_ev = (n - 1) - indice_ev
        in_pre = max(0, indice_ev - 32)
        fi_pre = max(0, indice_ev - 2)
        p_pre_30 = sum(serie[k]["pioggia_mm"] for k in range(in_pre, fi_pre))

        if p_pre_30 >= 100.0:
            livello, ritardo, soglia, smorz = "Idratato", 0, 40.0, 1.00
        elif 60.0 <= p_pre_30 < 100.0:
            livello, ritardo, soglia, smorz = "Lieve", 2, 45.0, 0.95
        elif 30.0 <= p_pre_30 < 60.0:
            livello, ritardo, soglia, smorz = "Moderata", 4, 55.0, 0.85
        else:
            livello, ritardo, soglia, smorz = "Severa (Terreno arido)", 6, 65.0, 0.70

        return {
            "evento_rilevato": True,
            "data_evento": serie[indice_ev]["data"],
            "pioggia_evento_mm": round(pioggia_ev, 1),
            "giorni_da_evento": giorni_da_ev,
            "pioggia_precedente_30gg": round(p_pre_30, 1),
            "livello_siccita": livello,
            "ritardo_siccita_applicato": ritardo,
            "soglia_efficace_richiesta": soglia,
            "fattore_smorzamento_resa": smorz,
            "t_media_attuale": giorno_oggi["t_media"],
            "rh_media_attuale": giorno_oggi["rh_media"],
            "vento_max_attuale": giorno_oggi["vento_max"]
        }

# ==========================================
# 3. PREVISIONE PER LE 4 MICROZONE
# ==========================================
def calcola_microzone(diag: Dict[str, Any], quota_stazione: int = 1285) -> List[Dict[str, Any]]:
    zone_cfg = [
        {"nome": "Betulle Sud-Est", "quota": 1100, "essenza": "betulla", "esposizione": "SE", "giorni_base": 9},
        {"nome": "Betulle Nord-Est", "quota": 1100, "essenza": "betulla", "esposizione": "NE", "giorni_base": 8},
        {"nome": "Faggeta Alta", "quota": 1350, "essenza": "faggio", "esposizione": "SE", "giorni_base": 12},
        {"nome": "Pini & Retroverso Svizzera", "quota": 1450, "essenza": "pino", "esposizione": "OMBRA_SVIZZERA", "giorni_base": 13}
    ]

    if not diag["evento_rilevato"]:
        return [{
            "zona": z["nome"],
            "indice_buttata": 0.0,
            "stato": "In attesa di pioggia",
            "giorni_mancanti_al_picco": None
        } for z in zone_cfg]

    res = []
    delta_t = diag["giorni_da_evento"]
    r_cum = diag["pioggia_evento_mm"]
    soglia_req = diag["soglia_efficace_richiesta"]
    ritardo = diag["ritardo_siccita_applicato"]
    smorz = diag["fattore_smorzamento_resa"]

    for z in zone_cfg:
        t_base = diag["t_media_attuale"] - 0.0065 * (z["quota"] - quota_stazione)
        if z["esposizione"] == "SE":
            t_eff = t_base + 1.2
            rh_eff = max(0.0, diag["rh_media_attuale"] - 8.0)
            v_eff = diag["vento_max_attuale"]
        elif z["esposizione"] == "NE":
            t_eff = t_base - 0.5
            rh_eff = min(100.0, diag["rh_media_attuale"] + 12.0)
            v_eff = diag["vento_max_attuale"] * 0.8
        else:
            t_eff = t_base - 1.2
            rh_eff = min(100.0, diag["rh_media_attuale"] + 18.0)
            v_eff = diag["vento_max_attuale"] * 0.45

        # Formule
        f_R = 1.0 / (1.0 + math.exp(-0.09 * (r_cum - soglia_req)))
        picco_eff = z["giorni_base"] + ritardo
        f_L = math.exp(- ((delta_t - picco_eff) ** 2) / (2 * (2.2 ** 2)))
        t_opt = 16.5 if z["essenza"] != "pino" else 14.5
        f_T = math.exp(- ((t_eff - t_opt) ** 2) / (2 * (3.5 ** 2)))
        f_H = 1.0 if rh_eff >= 85 else (0.0 if rh_eff < 40 else ((rh_eff - 40) / 45) ** 1.2)
        phi_vento = 1.0 if v_eff <= 20 else max(0.1, 1.0 - 0.03 * (v_eff - 20))

        indice = 100.0 * (f_R * f_T * f_L * f_H) * phi_vento * smorz
        giorni_mancanti = picco_eff - delta_t

        if indice > 65:
            stato = "Buttata in corso"
        elif giorni_mancanti > 0:
            stato = f"In incubazione (picco tra {giorni_mancanti} gg)"
        else:
            stato = "In esaurimento"

        res.append({
            "zona": z["nome"],
            "indice_buttata": round(indice, 1),
            "temp_stimata": round(t_eff, 1),
            "umidita_stimata": round(rh_eff, 1),
            "giorno_picco_calcolato": f"+{picco_eff} gg da pioggia",
            "giorni_mancanti_al_picco": giorni_mancanti,
            "stato": stato
        })
    return res

# ==========================================
# 4. ESECUZIONE PRINCIPALE
# ==========================================
def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Connessione Open Data ARPA...")
    
    sensori = get_sensor_ids_for_station(ID_STAZIONE)
    df_orari = download_weather_history(sensori, days=45)
    serie_storica = aggregate_daily(df_orari)
    
    if not serie_storica:
        print("Attenzione: nessun dato restituito da ARPA. Genero un modello vuoto.")
        serie_storica = []
        diagnosi = {"evento_rilevato": False, "stato": "Dati non disponibili"}
        previsioni = []
    else:
        analizzatore = AnalizzatoreSiccitaPorcini()
        diagnosi = analizzatore.analizza(serie_storica)
        previsioni = calcola_microzone(diagnosi)

    output = {
        "ultimo_aggiornamento": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stazione": {"id": ID_STAZIONE, "nome": "San Siro Alpe Rescascia", "quota_m": 1285},
        "diagnosi_meteo": diagnosi,
        "zone": previsioni,
        "storico_recente": serie_storica[-7:] if serie_storica else []
    }

    os.makedirs("data", exist_ok=True)
    percorso_file = os.path.join("data", "previsioni.json")
    with open(percorso_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Completato! File generato con successo: {percorso_file}")

if __name__ == "__main__":
    main()
