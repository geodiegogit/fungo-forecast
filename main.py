import os
import json
import math
import requests
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Any

# ==========================================
# 1. DATI PASSATI (ARPA LOMBARDIA)
# ==========================================
ENDPOINT_SENSORS_ANAGRAFICA = "https://www.dati.lombardia.it/resource/nf78-nj6b.json"
ENDPOINT_METEO_DATA = "https://www.dati.lombardia.it/resource/647i-nhxk.json"
ID_STAZIONE = "1545"

def get_sensor_ids_for_station(id_stazione: str) -> Dict[str, str]:
    params = {"$where": f"idstazione = '{id_stazione}'", "$limit": 50}
    response = requests.get(ENDPOINT_SENSORS_ANAGRAFICA, params=params, timeout=20)
    response.raise_for_status()
    sensori = response.json()
    
    mappa = {}
    for s in sensori:
        tipo = s.get("tipologia", "").lower()
        ids = s.get("idsensore")
        if "precipitazione" in tipo: mappa["pioggia"] = ids
        elif "temperatura" in tipo: mappa["temperatura"] = ids
        elif "umidit" in tipo: mappa["umidita"] = ids
        elif "vento" in tipo or "velocit" in tipo: mappa["vento"] = ids
    return mappa

def download_weather_history(mappa_sensori: Dict[str, str], days: int = 45) -> pd.DataFrame:
    start_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    id_list = "','".join(mappa_sensori.values())
    params = {
        "$where": f"idsensore in ('{id_list}') AND data >= '{start_date}' AND valore != -9999",
        "$limit": 50000, "$order": "data ASC"
    }
    response = requests.get(ENDPOINT_METEO_DATA, params=params, timeout=30)
    response.raise_for_status()
    records = response.json()
    if not records: return pd.DataFrame()
    df = pd.DataFrame(records)
    df["data"] = pd.to_datetime(df["data"])
    df["valore"] = pd.to_numeric(df["valore"], errors="coerce")
    inv_map = {v: k for k, v in mappa_sensori.items()}
    df["tipo"] = df["idsensore"].map(inv_map)
    return df.pivot_table(index="data", columns="tipo", values="valore", aggfunc="mean").reset_index()

def aggregate_daily(df_hourly: pd.DataFrame) -> List[Dict[str, Any]]:
    if df_hourly.empty: return []
    df_hourly["giorno"] = df_hourly["data"].dt.strftime("%Y-%m-%d")
    agg_rules = {}
    if "pioggia" in df_hourly.columns: agg_rules["pioggia"] = "sum"
    if "umidita" in df_hourly.columns: agg_rules["umidita"] = "mean"
    if "vento" in df_hourly.columns: agg_rules["vento"] = "max"
    if "temperatura" in df_hourly.columns: agg_rules["temperatura"] = ["mean", "max", "min"]
    df_daily = df_hourly.groupby("giorno").agg(agg_rules)
    df_daily.columns = ['_'.join(col).strip() for col in df_daily.columns.values]
    df_daily = df_daily.reset_index()

    serie = []
    for _, row in df_daily.iterrows():
        vento_kmh = row.get("vento_max", 0.0) * 3.6 if pd.notna(row.get("vento_max")) else 10.0
        serie.append({
            "data": row["giorno"],
            "pioggia_mm": round(float(row.get("pioggia_sum", 0.0)), 1),
            "t_media": round(float(row.get("temperatura_mean", 15.0)), 1),
            "t_max": round(float(row.get("temperatura_max", 15.0)), 1),
            "t_min": round(float(row.get("temperatura_min", 15.0)), 1),
            "rh_media": round(float(row.get("umidita_mean", 70.0)), 1),
            "vento_max": round(float(vento_kmh), 1)
        })
    return serie

# ==========================================
# 2. DATI FUTURI (OPEN-METEO 15 GIORNI)
# ==========================================
def scarica_previsioni_future() -> List[Dict[str, Any]]:
    # Coordinate San Siro
    url = "https://api.open-meteo.com/v1/forecast?latitude=46.06&longitude=9.27&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max&timezone=Europe/Rome&forecast_days=15"
    try:
        res = requests.get(url, timeout=15)
        res.raise_for_status()
        data = res.json()["daily"]
        futuro = []
        oggi_str = datetime.now().strftime("%Y-%m-%d")
        
        for i in range(len(data["time"])):
            giorno = data["time"][i]
            if giorno <= oggi_str:
                continue # Saltiamo oggi e passato, usiamo ARPA per quelli
                
            pioggia = float(data["precipitation_sum"][i] or 0.0)
            t_max = float(data["temperature_2m_max"][i])
            t_min = float(data["temperature_2m_min"][i])
            vento = float(data["wind_speed_10m_max"][i] or 10.0)
            
            # Stima Umidità se non disponibile nativamente
            rh_est = 85.0 if pioggia > 1.0 else 55.0
            
            futuro.append({
                "data": giorno,
                "pioggia_mm": round(pioggia, 1),
                "t_max": round(t_max, 1),
                "t_min": round(t_min, 1),
                "t_media": round((t_max + t_min) / 2, 1),
                "vento_max": round(vento, 1),
                "rh_media": rh_est
            })
        return futuro
    except Exception as e:
        print("Errore Open-Meteo:", e)
        return []

# ==========================================
# 3. MODELLO LOGICO (SICCITA' + MICROZONE)
# ==========================================
def analizza_giorno(serie_fino_a_oggi: List[Dict[str, Any]], quota_stazione: int = 1285) -> Dict[str, Any]:
    # --- LOGICA SICCITA' ---
    n = len(serie_fino_a_oggi)
    giorno_target = serie_fino_a_oggi[-1]
    indice_ev, pioggia_ev = None, 0.0
    
    for i in range(n - 1, max(2, n - 45), -1):
        c3 = sum(serie_fino_a_oggi[k]["pioggia_mm"] for k in range(i - 2, i + 1))
        if c3 >= 35.0:
            indice_ev, pioggia_ev = i, c3
            break

    if indice_ev is None:
        diag = {"evento_rilevato": False, "stato": "Nessuna pioggia"}
    else:
        giorni_da_ev = (n - 1) - indice_ev
        p_pre_30 = sum(serie_fino_a_oggi[k]["pioggia_mm"] for k in range(max(0, indice_ev - 32), max(0, indice_ev - 2)))
        
        if p_pre_30 >= 100.0: ritardo, soglia, smorz = 0, 40.0, 1.00
        elif 60.0 <= p_pre_30 < 100.0: ritardo, soglia, smorz = 2, 45.0, 0.95
        elif 30.0 <= p_pre_30 < 60.0: ritardo, soglia, smorz = 4, 55.0, 0.85
        else: ritardo, soglia, smorz = 6, 65.0, 0.70

        giorni_favonio = sum(1 for k in range(indice_ev + 1, n) 
                           if serie_fino_a_oggi[k]["vento_max"] > 22.0 
                           and serie_fino_a_oggi[k]["rh_media"] < 60.0 
                           and serie_fino_a_oggi[k]["pioggia_mm"] < 1.0)
        
        diag = {
            "evento_rilevato": True,
            "data_evento": serie_fino_a_oggi[indice_ev]["data"],
            "pioggia_evento_mm": pioggia_ev,
            "giorni_da_evento": giorni_da_ev,
            "ritardo_siccita_applicato": ritardo,
            "soglia_efficace_richiesta": soglia,
            "fattore_smorzamento_resa": smorz * max(0.1, 1.0 - (giorni_favonio * 0.25)),
            "t_max_attuale": giorno_target["t_max"],
            "t_min_attuale": giorno_target["t_min"],
            "rh_media_attuale": giorno_target["rh_media"],
            "vento_max_attuale": giorno_target["vento_max"],
            "pioggia_oggi": giorno_target["pioggia_mm"]
        }

    # --- CALCOLO ZONE ---
    zone_cfg = [
        {"nome": "Betulle Sud-Est", "quota": 1100, "essenza": "betulla", "esposizione": "SE", "giorni_base": 9},
        {"nome": "Betulle Nord-Est", "quota": 1100, "essenza": "betulla", "esposizione": "NE", "giorni_base": 8},
        {"nome": "Faggeta Alta", "quota": 1350, "essenza": "faggio", "esposizione": "SE", "giorni_base": 12},
        {"nome": "Pini/Svizzera", "quota": 1450, "essenza": "pino", "esposizione": "OMBRA", "giorni_base": 13}
    ]

    risultato_zone = []
    if not diag.get("evento_rilevato"):
        return {"diagnosi": diag, "zone": [{"zona": z["nome"], "indice": 0} for z in zone_cfg]}

    for z in zone_cfg:
        gradiente = 0.0065 * (z["quota"] - quota_stazione)
        t_min_b = diag["t_min_attuale"] - gradiente
        t_max_b = diag["t_max_attuale"] - gradiente
        
        if z["esposizione"] == "SE": t_max_eff, t_min_eff, rh_eff = t_max_b + 2.5, t_min_b, max(0.0, diag["rh_media_attuale"] - 10.0)
        elif z["esposizione"] == "NE": t_max_eff, t_min_eff, rh_eff = t_max_b - 1.0, t_min_b, min(100.0, diag["rh_media_attuale"] + 12.0)
        else: t_max_eff, t_min_eff, rh_eff = t_max_b - 2.5, t_min_b - 1.0, min(100.0, diag["rh_media_attuale"] + 18.0)

        f_R = 1.0 / (1.0 + math.exp(-0.09 * (diag["pioggia_evento_mm"] - diag["soglia_efficace_richiesta"])))
        picco = z["giorni_base"] + diag["ritardo_siccita_applicato"]
        f_L = math.exp(- ((diag["giorni_da_evento"] - picco) ** 2) / (2 * (2.2 ** 2)))
        
        t_opt = 16.5 if z["essenza"] != "pino" else 14.5
        f_T_media = math.exp(- ((((t_max_eff + t_min_eff)/2) - t_opt) ** 2) / (2 * (3.5 ** 2)))
        f_T_freddo = 0.0 if t_min_eff < 3.0 else ((t_min_eff - 3.0)/4.0 if t_min_eff < 7.0 else 1.0)
        f_H = 1.0 if rh_eff >= 85 else (0.0 if rh_eff < 40 else ((rh_eff - 40) / 45) ** 1.2)
        
        vento = diag["vento_max_attuale"]
        is_favonio = (vento > 20 and diag["rh_media_attuale"] < 60 and diag["pioggia_oggi"] < 1.0)
        phi_vento = max(0.1, 1.0 - 0.04 * (vento - 20)) if is_favonio else 1.0

        indice = 100.0 * (f_R * (f_T_media * f_T_freddo) * f_L * f_H) * phi_vento * diag["fattore_smorzamento_resa"]
        
        risultato_zone.append({
            "zona": z["nome"],
            "indice": round(indice, 1),
            "t_min_stimata": round(t_min_eff, 1),
            "t_max_stimata": round(t_max_eff, 1)
        })
    return {"diagnosi": diag, "zone": risultato_zone}

# ==========================================
# 4. ESECUZIONE PRINCIPALE
# ==========================================
def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Avvio Motore 3.0 (ARPA + OpenMeteo)...")
    
    sensori = get_sensor_ids_for_station(ID_STAZIONE)
    df_orari = download_weather_history(sensori, days=45)
    serie_storica_arpa = aggregate_daily(df_orari)
    serie_futura_meteo = scarica_previsioni_future()
    
    # Serie combinata per scorrere nel tempo
    serie_completa = serie_storica_arpa + serie_futura_meteo
    oggi_str = datetime.now().strftime("%Y-%m-%d")
    
    # 1. Calcola stato attuale (Oggi)
    indice_oggi = next((i for i, d in enumerate(serie_completa) if d["data"] == oggi_str), len(serie_storica_arpa)-1)
    dati_oggi = analizza_giorno(serie_completa[:indice_oggi+1])

    # 2. Calcola le proiezioni REALI dei prossimi 15 giorni
    calendario_futuro = []
    for i in range(indice_oggi + 1, min(indice_oggi + 16, len(serie_completa))):
        giorno_futuro = serie_completa[i]
        calcolo_giorno = analizza_giorno(serie_completa[:i+1])
        
        calendario_futuro.append({
            "data": giorno_futuro["data"],
            "meteo": giorno_futuro,
            "zone": calcolo_giorno["zone"]
        })

    output = {
        "ultimo_aggiornamento": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stazione": {"id": ID_STAZIONE, "nome": "San Siro Alpe Rescascia", "quota_m": 1285},
        "diagnosi_meteo": dati_oggi["diagnosi"],
        "zone": dati_oggi["zone"],
        "storico_completo": serie_storica_arpa,
        "calendario_futuro": calendario_futuro
    }

    os.makedirs("data", exist_ok=True)
    with open("data/previsioni.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print("Finito! Dati meteo futuri elaborati con successo.")

if __name__ == "__main__":
    main()
