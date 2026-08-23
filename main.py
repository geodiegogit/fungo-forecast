class AnalizzatoreSiccitaPorcini:
    def __init__(self, soglia_evento: float = 35.0):
        self.soglia_evento = soglia_evento

    def analizza(self, serie: List[Dict[str, Any]]) -> Dict[str, Any]:
        n = len(serie)
        giorno_ieri = serie[-1]
        
        eventi_trovati = []
        
        i = 2
        while i < n:
            c3 = sum(serie[k]["pioggia_mm"] for k in range(i - 2, i + 1))
            if c3 >= self.soglia_evento:
                giorni_finestra = [serie[k] for k in range(i - 2, i + 1)]
                giorno_max = max(giorni_finestra, key=lambda x: x["pioggia_mm"])
                idx = serie.index(giorno_max)
                
                if not eventi_trovati or (idx - eventi_trovati[-1]["indice"]) >= 4:
                    p_pre_30 = sum(serie[k]["pioggia_mm"] for k in range(max(0, idx - 32), max(0, idx - 2)))
                    
                    # --- NUOVA LOGICA: SENESCENZA E TERRENO IDROFUGO ---
                    if p_pre_30 < 15.0:
                        # Siccità estrema: terreno impermeabile e blocco simbiosi (piante senza foglie)
                        # Applichiamo 12 giorni di ritardo e distruggiamo il potenziale al 30% (serve solo a far bere le piante)
                        ritardo, soglia, smorz = 12, 60.0, 0.30 
                        stress_estremo = True
                    elif 15.0 <= p_pre_30 < 30.0:
                        ritardo, soglia, smorz = 7, 50.0, 0.70
                        stress_estremo = False
                    elif 30.0 <= p_pre_30 < 60.0: 
                        ritardo, soglia, smorz = 3, 40.0, 0.90
                        stress_estremo = False
                    else: 
                        ritardo, soglia, smorz = 0, 35.0, 1.00
                        stress_estremo = False

                    giorni_da_ev = (n - 1) - idx
                    
                    giorni_favonio = sum(1 for k in range(idx + 1, n) 
                                         if serie[k]["vento_max"] > 22.0 
                                         and serie[k]["rh_media"] < 60.0 
                                         and serie[k]["pioggia_mm"] < 1.0)
                    danno_favonio = max(0.1, 1.0 - (giorni_favonio * 0.25))
                    
                    eventi_trovati.append({
                        "indice": idx,
                        "data": giorno_max["data"],
                        "pioggia": c3,
                        "giorni_da_evento": giorni_da_ev,
                        "ritardo": ritardo,
                        "soglia": soglia,
                        "smorzamento": smorz * danno_favonio,
                        "stress_idrico": stress_estremo
                    })
                i += 3 
            else:
                i += 1

        eventi_trovati = [ev for ev in eventi_trovati if ev["giorni_da_evento"] <= 40]

        diag = {
            "t_max_attuale": giorno_ieri["t_max"],
            "t_min_attuale": giorno_ieri["t_min"],
            "rh_media_attuale": giorno_ieri["rh_media"],
            "vento_max_attuale": giorno_ieri["vento_max"],
            "pioggia_oggi": giorno_ieri["pioggia_mm"], 
            "eventi": eventi_trovati
        }
        
        if not eventi_trovati:
            diag.update({"evento_rilevato": False, "stato": "Nessuna pioggia rilevante"})
        else:
            ev_recente = eventi_trovati[-1]
            diag.update({
                "evento_rilevato": True,
                "data_evento": ev_recente["data"],
                "giorni_da_evento": ev_recente["giorni_da_evento"],
                "ritardo_siccita_applicato": ev_recente["ritardo"],
                "stress_estremo": ev_recente["stress_idrico"]
            })
        return diag

def calcola_microzone(diag: Dict[str, Any], quota_stazione: int = 1285) -> List[Dict[str, Any]]:
    zone_cfg = [
        {"nome": "Camnasco", "quota": 750, "essenza": "castagno", "esposizione": "SE", "giorni_base": 6},
        {"nome": "Betulle SE", "quota": 1222, "essenza": "betulla", "esposizione": "SE", "giorni_base": 9},
        {"nome": "Betulle NE", "quota": 1144, "essenza": "betulla", "esposizione": "NE", "giorni_base": 8},
        {"nome": "Faggi Ovest", "quota": 1561, "essenza": "faggio", "esposizione": "OVEST_OMBRA", "giorni_base": 12},
        {"nome": "Abeti Nord", "quota": 1478, "essenza": "pino", "esposizione": "NORD", "giorni_base": 13}
    ]

    if not diag.get("evento_rilevato"):
        return [{"zona": z["nome"], "indice_buttata": 0.0, "stato": "In attesa", "giorni_mancanti_al_picco": None, "onde": []} for z in zone_cfg]

    res = []
    for z in zone_cfg:
        gradiente = 0.0065 * (z["quota"] - quota_stazione)
        t_min_b = diag["t_min_attuale"] - gradiente
        t_max_b = diag["t_max_attuale"] - gradiente
        
        rh_eff = diag["rh_media_attuale"]
        if z["esposizione"] == "SE":
            t_max_eff, t_min_eff, rh_eff = t_max_b + 2.5, t_min_b, max(0.0, diag["rh_media_attuale"] - 10.0)
        elif z["esposizione"] == "NE":
            t_max_eff, t_min_eff, rh_eff = t_max_b - 1.0, t_min_b, min(100.0, diag["rh_media_attuale"] + 12.0)
        elif z["esposizione"] == "OVEST_OMBRA":
            t_max_eff, t_min_eff, rh_eff = t_max_b - 1.5, t_min_b - 0.5, min(100.0, diag["rh_media_attuale"] + 15.0)
        elif z["esposizione"] == "NORD":
            t_max_eff, t_min_eff, rh_eff = t_max_b - 2.5, t_min_b - 1.5, min(100.0, diag["rh_media_attuale"] + 20.0)
        else:
            t_max_eff, t_min_eff = t_max_b, t_min_b
            
        if z["nome"] == "Camnasco":
            rh_eff = max(60.0, rh_eff) 

        t_opt = 16.5 if z["essenza"] not in ["pino", "faggio"] else 14.5
        t_media_eff = (t_max_eff + t_min_eff) / 2
        f_T_media = math.exp(- ((t_media_eff - t_opt) ** 2) / (2 * (3.5 ** 2)))
        f_T_freddo = 0.0 if t_min_eff < 3.0 else ((t_min_eff - 3.0) / 4.0 if t_min_eff < 7.0 else 1.0)
        
        # --- NUOVA LOGICA: IL GRILLETTO TERMICO ---
        # Se la temperatura minima è tra 8 e 13 gradi, si innesca il corpo fruttifero (Boost del 30%)
        # Se fa ancora troppo caldo (sopra i 17 gradi), il fungo si impigrisce (Penalità)
        if 8.0 <= t_min_eff <= 13.0:
            f_grilletto = 1.3
        elif t_min_eff > 17.0:
            f_grilletto = 0.7
        else:
            f_grilletto = 1.0

        f_H = 1.0 if rh_eff >= 85 else (0.0 if rh_eff < 40 else ((rh_eff - 40) / 45) ** 1.2)
        
        vento = diag["vento_max_attuale"]
        is_favonio = (vento > 20 and diag["rh_media_attuale"] < 60 and diag["pioggia_oggi"] < 1.0)
        phi_vento = max(0.1, 1.0 - 0.04 * (vento - 20)) if is_favonio else 1.0
        
        indice_totale = 0.0
        onde = []
        
        for ev in diag["eventi"]:
            f_R = 1.0 / (1.0 + math.exp(-0.12 * (ev["pioggia"] - ev["soglia"])))
            
            ritardo_applicato = ev["ritardo"]
            if z["nome"] == "Camnasco":
                # Camnasco (sorgente) non va mai in stress idrico profondo
                ritardo_applicato = min(1, ritardo_applicato) 
                
            picco_eff = z["giorni_base"] + ritardo_applicato
            f_L = math.exp(- ((ev["giorni_da_evento"] - picco_eff) ** 2) / (2 * (2.2 ** 2)))
            
            ind_pieno = 100.0 * (f_R * (f_T_media * f_T_freddo) * 1.0 * f_H) * phi_vento * ev["smorzamento"] * f_grilletto
            ind_oggi = ind_pieno * f_L
            
            indice_totale += ind_oggi
            onde.append({
                "giorni_mancanti_da_ieri": round(picco_eff - ev["giorni_da_evento"], 1),
                "indice_picco": round(ind_pieno, 1)
            })

        indice_totale = min(100.0, indice_totale)
        
        futuri = [onda["giorni_mancanti_da_ieri"] for onda in onde if onda["giorni_mancanti_da_ieri"] > 0]
        giorni_mancanti = min(futuri) if futuri else (min([o["giorni_mancanti_da_ieri"] for o in onde]) if onde else 0)
        
        if f_T_freddo == 0.0: stato = "Blocco da freddo notturno"
        elif diag.get("stress_estremo") and z["nome"] != "Camnasco": stato = "Alberi in stress: Simbiosi debole"
        elif indice_totale > 65: stato = "Buttata in corso (Onde multiple)" if len(onde)>1 else "Buttata in corso"
        elif giorni_mancanti > 0: stato = f"Incubazione (prossimo picco in {giorni_mancanti} gg)"
        else: stato = "In esaurimento"

        res.append({
            "zona": z["nome"],
            "indice_buttata": round(indice_totale, 1),
            "t_min_stimata": round(t_min_eff, 1),
            "t_max_stimata": round(t_max_eff, 1),
            "giorni_mancanti_al_picco": giorni_mancanti,
            "stato": stato,
            "onde": onde
        })
    return res
