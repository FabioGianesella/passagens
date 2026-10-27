#!/usr/bin/env python3
"""
Alerta de passagens SAO (CGH/GRU/VCP) -> AJU -> SAO (ida e volta), aviso no celular via ntfy.
Foco: semana de 23/11/2026 a 28/11/2026. Fonte: Google Flights via SerpAPI.

Variaveis de ambiente: SERPAPI_KEY e NTFY_TOPIC.

Regras de aviso:
  - Recorde de preco, preco abaixo do teto ou queda vs. media -> aviso na hora.
  - Segunda-feira de manha -> resumo semanal com grafico.
  - Falha na busca, cota esgotada ou erro inesperado -> aviso de falha.
  - Fora disso o script so grava no historico (sem barulho).
"""
import csv
import json, os, statistics, sys, unicodedata, requests
from datetime import date, datetime, timedelta
from pathlib import Path

# ====================== CONFIGURACAO ======================
ORIGENS = "CGH,GRU,VCP"          # SAO = Congonhas, Guarulhos e Viracopos: sai daqui e volta pra ca
DESTINO = "AJU"
IDAS_PADRAO = [date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25)]    # usado se nao houver config.json valido
VOLTAS_PADRAO = [date(2026, 11, 28)]
MIN_NOITES = 2                   # minimo de noites entre ida e volta
MAX_COMBINACOES = 12             # limite de pares ida/volta aceitos no config.json
MAX_BUSCAS = 4                   # buscas por execucao (2 execucoes/dia: 4 x 2 x 30 = 240 de 250/mes)
MAX_ESCALAS = 1                  # None = qualquer; 0 = so direto; 1 = ate 1 parada
TETO_ALERTA = 1500               # avisa sempre que houver preco igual ou abaixo (R$)
QUEDA_PERCENTUAL = 10            # avisa se o preco ficar X% abaixo da media dos ultimos 30 dias
HISTORICO = Path(__file__).with_name("historico.csv")
VOOS_NOVOS = Path(__file__).with_name("voos_novos.json")
GRAFICO = Path(__file__).with_name("grafico.png")
CONFIG = Path(__file__).with_name("config.json")   # datas escolhidas e fixadas pelo painel
# ==========================================================

def carregar_datas():
    """Le as datas de config.json (gravado pelo painel). Se faltar ou for invalido, usa o padrao."""
    try:
        c = json.loads(CONFIG.read_text(encoding="utf-8"))
        idas = sorted({date.fromisoformat(x) for x in c["idas"]})
        voltas = sorted({date.fromisoformat(x) for x in c["voltas"]})
        if (idas and voltas and len(idas) * len(voltas) <= MAX_COMBINACOES
                and any((v - i).days >= MIN_NOITES for i in idas for v in voltas)):
            return idas, voltas
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return IDAS_PADRAO, VOLTAS_PADRAO


IDAS, VOLTAS = carregar_datas()

KEY = os.getenv("SERPAPI_KEY")
TOPIC = (os.getenv("NTFY_TOPIC") or "").strip()
CAMPOS = ["consulta", "ida", "volta", "preco", "origem", "cia", "escalas", "link"]
NL = chr(10)


def dm(iso):
    return f"{date.fromisoformat(iso):%d/%m}"


def ascii_safe(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def fmt(r):
    esc = "direto" if r["escalas"] == 0 else f"{r['escalas']} esc."
    return f"R$ {r['preco']:.0f} | {dm(r['ida'])} a {dm(r['volta'])} | {r['origem']} | {r['cia']} | {esc}"


# ---------------------------- avisos ----------------------------
def avisar(titulo, msg, link="", prioridade="default", acoes="", anexo=None):
    print(titulo, NL, msg)
    if not TOPIC:
        print("NTFY_TOPIC nao definido: sem notificacao.")
        return
    url = f"https://ntfy.sh/{TOPIC}"
    h = {"Title": titulo, "Priority": prioridade, "Tags": "airplane"}
    if link:
        h["Click"] = link
    try:
        if anexo:
            h["Filename"] = "grafico.png"
            h["Message"] = ascii_safe(msg.replace(NL, " | "))
            resp = requests.put(url, data=anexo, headers=h, timeout=60)
        else:
            if acoes:
                h["Actions"] = acoes
            resp = requests.post(url, data=msg.encode("utf-8"), headers=h, timeout=30)
            if resp.status_code >= 400 and acoes:
                print("ntfy recusou os botoes de link, reenviando sem eles:", resp.status_code)
                h.pop("Actions")
                resp = requests.post(url, data=msg.encode("utf-8"), headers=h, timeout=30)
        print("ntfy:", resp.status_code, resp.text[:150])
    except Exception as e:
        print("ntfy erro:", type(e).__name__)


# ---------------------------- SerpAPI ----------------------------
def cota_restante():
    """Buscas que ainda restam no mes (consulta gratuita). None se nao der para saber."""
    try:
        r = requests.get("https://serpapi.com/account.json", params={"api_key": KEY}, timeout=30)
        r.raise_for_status()
        d = r.json()
        v = d.get("total_searches_left", d.get("plan_searches_left"))
        return int(v) if v is not None else None
    except Exception as e:
        print("Nao consegui consultar a cota:", type(e).__name__)
        return None


def combinacoes(n):
    """Pares (ida, volta) da janela; cada execucao pega um bloco diferente, cobrindo tudo ao longo do dia."""
    if n <= 0:
        return []
    hoje = date.today()
    todos = [(i, v) for i in IDAS for v in VOLTAS if (v - i).days >= MIN_NOITES and i > hoje]
    if not todos:
        return []
    slot = hoje.toordinal() * 2 + datetime.now().hour // 12
    ini = (slot * MAX_BUSCAS) % len(todos)
    rot = todos[ini:] + todos[:ini]
    return rot[:min(n, len(todos))]


def resumo(o, ida, volta, link):
    t = o.get("flights", [])
    return {"ida": ida.isoformat(), "volta": volta.isoformat(), "preco": o["price"],
            "origem": t[0]["departure_airport"]["id"] if t else "",
            "cia": ", ".join(dict.fromkeys(x.get("airline", "") for x in t)),
            "escalas": max(len(t) - 1, 0), "link": link}


def detalhe(o):
    """Dados de um voo de ida para exibir no painel (horarios, duracao, conexoes)."""
    t = o.get("flights", [])
    if not t:
        return None
    return {"preco": int(o["price"]),
            "cia": ", ".join(dict.fromkeys(x.get("airline", "") for x in t)),
            "origem": t[0].get("departure_airport", {}).get("id", ""),
            "saida": t[0].get("departure_airport", {}).get("time", ""),
            "chegada": t[-1].get("arrival_airport", {}).get("time", ""),
            "duracao": o.get("total_duration") or sum(x.get("duration", 0) for x in t),
            "escalas": max(len(t) - 1, 0),
            "paradas": [(l.get("id") or "") for l in (o.get("layovers") or [])]}


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
    link = data.get("search_metadata", {}).get("google_flights_url", "")
    res = resumo(min(ofertas, key=lambda x: x["price"]), ida, volta, link)
    diretos = [o for o in ofertas if len(o.get("flights", [])) == 1]
    voos, vistos = [], set()
    for o in sorted(ofertas, key=lambda x: x["price"]):
        d = detalhe(o)
        k = (d["saida"], d["chegada"], d["cia"]) if d else None
        if d and k not in vistos:
            vistos.add(k); voos.append(d)
        if len(voos) >= 5:
            break
    res["voos"] = voos
    res["direto"] = resumo(min(diretos, key=lambda x: x["price"]), ida, volta, link) if diretos else None
    return res


# ---------------------------- historico ----------------------------
def ler_historico():
    if not HISTORICO.exists():
        return []
    rows, vistos = [], set()
    with HISTORICO.open(encoding="utf-8") as f:
        for l in csv.DictReader(f):
            try:
                chave = (l["consulta"], l["ida"], l["volta"], int(float(l["preco"])), l["origem"], l["cia"])
                if chave in vistos:
                    continue  # ignora linhas repetidas (mesma busca no mesmo dia)
                vistos.add(chave)
                rows.append({**l, "preco": float(l["preco"])})
            except (KeyError, ValueError, TypeError):
                pass
    return rows


def na_janela(l):
    try:
        return date.fromisoformat(l["ida"]) in IDAS and date.fromisoformat(l["volta"]) in VOLTAS
    except (KeyError, ValueError, TypeError):
        return False


def salvar(rs, existentes):
    hoje = date.today().isoformat()
    chaves = {(l.get("consulta"), l.get("ida"), l.get("volta"), str(int(l["preco"])), l.get("origem"), l.get("cia"))
              for l in existentes}
    novos = [r for r in rs
             if (hoje, r["ida"], r["volta"], str(int(r["preco"])), r["origem"], r["cia"]) not in chaves]
    if not novos:
        return
    novo_arquivo = not HISTORICO.exists() or HISTORICO.stat().st_size == 0
    with HISTORICO.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS, extrasaction="ignore")
        if novo_arquivo:
            w.writeheader()
        for r in novos:
            w.writerow({"consulta": hoje, **r})


# ---------------------------- resumo semanal ----------------------------
def grafico(jan):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        por_dia = {}
        for l in jan:
            por_dia[l["consulta"]] = min(por_dia.get(l["consulta"], l["preco"]), l["preco"])
        dias = sorted(por_dia)
        if len(dias) < 2:
            return None
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot([dm(d) for d in dias], [por_dia[d] for d in dias], marker="o")
        ax.set_title(f"Menor preco por dia - SAO > AJU ({IDAS[0]:%d/%m} a {VOLTAS[-1]:%d/%m})")
        ax.set_ylabel("R$")
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(GRAFICO, dpi=120)
        return GRAFICO.read_bytes()
    except Exception as e:
        print("Grafico nao gerado:", type(e).__name__)
        return None


def resumo_semanal(hoje):
    jan = [l for l in ler_historico() if na_janela(l)]
    corte = (hoje - timedelta(days=7)).isoformat()
    corte2 = (hoje - timedelta(days=14)).isoformat()
    sem = [l for l in jan if l["consulta"] >= corte]
    ant = [l for l in jan if corte2 <= l["consulta"] < corte]
    if not sem:
        return
    melhor = min(sem, key=lambda l: l["preco"])
    m_sem = statistics.mean(l["preco"] for l in sem)
    linhas = [f"Menor da semana: R$ {melhor['preco']:.0f} | {dm(melhor['ida'])} a {dm(melhor['volta'])}"
              f" | {melhor['origem']} | {melhor['cia']}",
              f"Media da semana: R$ {m_sem:.0f}"]
    if ant:
        m_ant = statistics.mean(l["preco"] for l in ant)
        linhas.append(f"Vs semana anterior: {(m_sem / m_ant - 1) * 100:+.0f}%")
    linhas.append(f"Recorde geral: R$ {min(l['preco'] for l in jan):.0f}")
    avisar("Resumo semanal SAO-AJU", NL.join(linhas), link=melhor.get("link", ""), anexo=grafico(jan))


# ---------------------------- principal ----------------------------
def main():
    if not KEY:
        avisar("Falha no alerta SAO-AJU", "SERPAPI_KEY nao definida.", prioridade="high")
        sys.exit("Defina SERPAPI_KEY.")
    hoje = date.today()
    left = cota_restante()
    if left is not None and left <= 0:
        avisar("Falha no alerta SAO-AJU", "A cota de buscas do SerpAPI acabou neste mes.", prioridade="high")
        sys.exit(1)
    n = MAX_BUSCAS if left is None else min(MAX_BUSCAS, left)
    combos = combinacoes(n)
    if not combos:
        print("Sem datas futuras na janela: busca encerrada.")
        if hoje <= IDAS[-1] + timedelta(days=1):
            avisar("Alerta SAO-AJU encerrado", "A janela de datas terminou. Configure novas datas para continuar.")
        return

    rs, erros = [], []
    for ida, volta in combos:
        try:
            r = buscar(ida, volta)
        except requests.HTTPError as e:
            cod = e.response.status_code if e.response is not None else "?"
            erros.append(f"HTTP {cod}")
            print(f"Erro {ida}->{volta}: HTTP {cod}")
        except Exception as e:
            erros.append(type(e).__name__)
            print(f"Erro {ida}->{volta}: {type(e).__name__}")
        else:
            if r:
                rs.append(r)
    if not rs:
        motivo = ", ".join(sorted(set(erros))) if erros else "sem ofertas para as datas"
        avisar("Falha no alerta SAO-AJU", f"A busca de hoje nao trouxe resultados ({motivo}).", prioridade="high")
        sys.exit(1)

    hist = ler_historico()
    jan = [l for l in hist if na_janela(l)]
    ant = [l["preco"] for l in jan]
    recorde = min(ant) if ant else None
    lim = (hoje - timedelta(days=30)).isoformat()
    rec30 = [l for l in jan if l.get("consulta", "") >= lim]
    dias30 = {l["consulta"] for l in rec30}
    media = statistics.mean(l["preco"] for l in rec30) if len(rec30) >= 6 and len(dias30) >= 3 else None
    salvar(rs, hist)
    try:  # voos detalhados desta rodada; o gerar_dados.py junta ao painel
        VOOS_NOVOS.write_text(json.dumps(
            {f"{r['ida']}|{r['volta']}": {"consulta": hoje.isoformat(), "link": r.get("link", ""), "voos": r.get("voos", [])}
             for r in rs}, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print("Nao salvei os voos detalhados:", type(e).__name__)

    rs.sort(key=lambda r: r["preco"])
    melhor = rs[0]
    p = melhor["preco"]
    if recorde is None:
        titulo, prio = "Monitoramento SAO-AJU iniciado", "default"
    elif p < recorde:
        titulo, prio = "Menor preco ja visto! SAO-AJU", "high"
    elif p <= TETO_ALERTA:
        titulo, prio = f"Abaixo de R$ {TETO_ALERTA}! SAO-AJU", "high"
    elif media and p <= media * (1 - QUEDA_PERCENTUAL / 100):
        titulo, prio = "Queda de preco SAO-AJU", "default"
    else:
        titulo = None

    if titulo:
        linhas = [fmt(r) for r in rs[:3]]
        diretos = [r["direto"] for r in rs if r.get("direto")]
        if diretos:
            d = min(diretos, key=lambda r: r["preco"])
            if d["preco"] > melhor["preco"]:
                linhas.append("Melhor direto: " + fmt(d))
        if media:
            linhas.append(f"Media 30 dias: R$ {media:.0f} ({(p / media - 1) * 100:+.0f}% hoje)")
        if recorde:
            linhas.append(f"Recorde anterior: R$ {recorde:.0f}")
        if left is not None and left - len(combos) <= 20:
            linhas.append(f"Atencao: restam ~{max(left - len(combos), 0)} buscas no SerpAPI")
        acoes = "; ".join(f"view, Opcao {i + 1} R$ {r['preco']:.0f}, '{r['link']}'"
                          for i, r in enumerate(rs[:3]) if r.get("link"))
        avisar(titulo, NL.join(linhas), melhor["link"], prio, acoes)
    else:
        print(f"Sem alerta: menor preco R$ {p:.0f} (recorde R$ {recorde:.0f}, teto R$ {TETO_ALERTA}).")

    if hoje.weekday() == 0 and datetime.now().hour < 12:
        resumo_semanal(hoje)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        avisar("Erro no alerta SAO-AJU", f"Falha inesperada: {type(e).__name__}", prioridade="high")
        raise
