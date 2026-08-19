import json
import os
from datetime import datetime
# Importa le funzioni definite nei passaggi precedenti
from meteo_arpa import get_sensor_ids_for_station, download_weather_history, aggregate_daily_for_mushrooms
from modello_funghi import AnalizzatoreSiccitaPorcini, calcola_previsione_microzone

ID_STAZIONE = "1545"

def main():
    print(f"[{datetime.now()}] Avvio elaborazione modello San Siro Rescascia...")
    
    # 1. Download dati ARPA
    sensori = get_sensor_ids_for_station(ID_STAZIONE)
    df_hourly = download_weather_history(sensori, days=45)
    serie_storica = aggregate_daily_for_mushrooms(df_hourly)
    
    if len(serie_storica) < 30:
        raise RuntimeError(f"Dati insufficienti: solo {len(serie_storica)} giorni disponibili.")

    # 2. Analisi siccità e calcolo indici
    analizzatore = AnalizzatoreSiccitaPorcini()
    diagnosi_siccita = analizzatore.analizza_serie_storica(serie_storica)
    previsioni_zone = calcola_previsione_microzone(diagnosi_siccita)

    # 3. Payload JSON per l'app mobile
    output = {
        "ultimo_aggiornamento": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stazione": {
            "id": ID_STAZIONE,
            "nome": "San Siro Alpe Rescascia",
            "quota_m": 1285
        },
        "diagnosi_meteo": diagnosi_siccita,
        "zone": previsioni_zone,
        "storico_recente": serie_storica[-7:]  # Ultimi 7 giorni per grafici rapidi
    }

    # 4. Salvataggio su file
    os.makedirs("data", exist_ok=True)
    filepath = os.path.join("data", "previsioni.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Dati salvati con successo in {filepath}")

if __name__ == "__main__":
    main()
