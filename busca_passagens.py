#!/usr/bin/env python3
"""
Alerta diário de passagens baratas SAO (CGH/GRU/VCP) -> AJU (ida e volta), no celular via ntfy.
Fonte: Google Flights via SerpAPI.

Variáveis de ambiente (só 2):
  SERPAPI_KEY   chave de https://serpapi.com (plano grátis: 100 buscas/mês)
  NTFY_TOPIC    um nome secreto qualquer, ex.: fabio-voos-8k3x (o mesmo que você assina no app ntfy)
"""
import csv, os, sys, requests
from datetime import date, datetime, timedelta
from pathlib import Path

# ====================== CONFIGURAÇÃO ======================
ORIGENS = "CGH,GRU,VCP"   # SAO = Congonhas, Guarulhos e Viracopos (tire algum se quiser)
DESTINO = "AJU"
JANELA_INICIO_DIAS = 14     # primeira ida: daqui a X dias
JANELA_FIM_DIAS = 90        # última ida: daqui a X dias
PASSO_DIAS = 7              # uma data de ida a cada X dias
ESTADIAS = [3, 5, 7]        # noites entre ida e volta
MAX_BUSCAS = 3              # buscas por dia (por execucao, 2 execucoes/dia: 3 x 2 x 30 = 180 de 250/mes)
MAX_ESCALAS = None          # None = qualquer; 0 = só direto; 1 = até 1 parada
HISTORICO = Path(__file__).with_name("historico.csv")
# ===========================================================

KEY = os.getenv("SERPAPI_KEY")
TOPIC = os.getenv("NTFY_TOPIC")


def combinacoes():
    hoje = date.today()
    combos, d = [], hoje + timedelta(days=JANELA_INICIO_DIAS)
    while d <= hoje + timedelta(days=JANELA_FIM_DIAS):
        combos += [(d, d + timedelta(days=n)) for n in ESTADIAS]
        d += timedelta(days=PASSO_DIAS)
    if len(combos) > MAX_BUSCAS:
        # muda as datas testadas a cada dia, para cobrir a janela inteira ao longo das semanas
        desloc = (hoje.toordinal() * 2 + datetime.now().hour // 12) % len(combos)
        combos = combos[desloc:] + combos[:desloc]
        passo = len(combos) / MAX_BUSCAS
        combos = [combos[int(i * passo)] for i in range(MAX_BUSCAS)]
    return combos


def buscar(ida, volta):
    params = {"engine": "google_flights", "departure_id": ORIGENS, "arrival_id": DESTINO,
              "outbound_date": ida.isoformat(), "return_date": volta.isoformat(), "type": 1,
              "currency": "BRL", "hl": "pt", "gl": "br", "api_key": KEY}
    if MAX_ESCALAS is not None:
        params["stops"] = MAX_ESCALAS + 1
    r = requests.get("https://serpapi.com/search.json", params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    ofertas = [o for o in (data.get("best_flights") or []) + (data.get("other_flights") or []) if o.get("price")]
    if not ofertas:
        return None
    o = min(ofertas, key=lambda x: x["price"])
    t = o.get("flights", [])
    return {"ida": ida.isoformat(), "volta": volta.isoformat(), "preco": o["price"],
            "origem": t[0]["departure_airport"]["id"] if t else "",
            "cia": ", ".join(dict.fromkeys(x.get("airline", "") for x in t)),
            "escalas": max(len(t) - 1, 0),
            "link": data.get("search_metadata", {}).get("google_flights_url", "")}


def menor_historico():
    if not HISTORICO.exists():
        return None
    with HISTORICO.open(encoding="utf-8") as f:
        p = [float(l["preco"]) for l in csv.DictReader(f)]
    return min(p) if p else None


def salvar(rs):
    novo = not HISTORICO.exists()
    with HISTORICO.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["consulta"] + list(rs[0].keys()))
        if novo:
            w.writeheader()
        for r in rs:
            w.writerow({"consulta": date.today().isoformat(), **r})


def avisar(titulo, msg, link="", prioridade="default"):
    print(titulo, "\n", msg)
    if not TOPIC:
        return
    h = {"Title": titulo, "Priority": prioridade, "Tags": "airplane"}
    if link:
        h["Click"] = link
    resp = requests.post(f"https://ntfy.sh/{TOPIC.strip()}", data=msg.encode("utf-8"), headers=h, timeout=30)
    print("ntfy:", resp.status_code, resp.text[:200])


def main():
    if not KEY:
        sys.exit("Defina SERPAPI_KEY.")
    recorde = menor_historico()
    rs = []
    for ida, volta in combinacoes():
        try:
            r = buscar(ida, volta)
        except Exception as e:
            print(f"Erro {ida}->{volta}: {e}")
            continue
        if r:
            rs.append(r)
    if not rs:
        avisar("Passagens SAO-AJU", "A busca de hoje nao retornou resultados.")
        sys.exit(1)
    rs.sort(key=lambda r: r["preco"])
    salvar(rs)

    melhor = rs[0]
    if recorde is None or melhor["preco"] < recorde:
        titulo, prio = "Menor preco ja visto! SAO-AJU", "high"
    else:
        titulo, prio = "Passagens SAO-AJU hoje", "default"
    linhas = [f"R$ {r['preco']:.0f} | {date.fromisoformat(r['ida']):%d/%m} a {date.fromisoformat(r['volta']):%d/%m} "
              f"| {r['origem']} | {r['cia']}" + (" | direto" if r["escalas"] == 0 else f" | {r['escalas']} esc.")
              for r in rs[:3]]
    if recorde:
        linhas.append(f"Menor ate agora: R$ {recorde:.0f}")
    avisar(titulo, "\n".join(linhas), melhor["link"], prio)


if __name__ == "__main__":
    main()
