#!/usr/bin/env python3
"""
Alerta de passagens (ida e volta) para ate 5 trajetos configurados no painel, aviso no celular via ntfy.
Fonte: Google Flights via SerpAPI.

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
ID_PADRAO = "sp-aju"
IDAS_PADRAO = [date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25)]    # usado se nao houver config.json valido
VOLTAS_PADRAO = [date(2026, 11, 28)]
MAX_TRAJETOS = 5                 # trajetos monitorados ao mesmo tempo
MIN_NOITES = 2                   # minimo de noites entre ida e volta
MAX_COMBINACOES = 12             # limite de pares ida/volta por trajeto
MAX_BUSCAS = 4                   # buscas por execucao (2 execucoes/dia: 4 x 2 x 30 = 240 de 250/mes), divididas entre os trajetos
MAX_ESCALAS = 1                  # None = qualquer; 0 = so direto; 1 = ate 1 parada
TETO_PADRAO = 1500               # teto de alerta (R$) de um trajeto sem teto proprio
QUEDA_PERCENTUAL = 10            # avisa se o preco ficar X% abaixo da media dos ultimos 30 dias
HISTORICO = Path(__file__).with_name("historico.csv")
VOOS_NOVOS = Path(__file__).with_name("voos_novos.json")
GRAFICO = Path(__file__).with_name("grafico.png")
CONFIG = Path(__file__).with_name("config.json")   # trajetos, datas e tetos escolhidos e fixados pelo painel
# ==========================================================

def _cods(txt, maximo):
    """'CGH, GRU' -> ['CGH','GRU'] (codigos IATA de 3 letras, sem repetir); invalido -> None."""
    cods = []
    for x in str(txt or "").replace(";", ",").replace(" ", ",").split(","):
        x = x.strip().upper()
        if not x:
            continue
        if not (len(x) == 3 and x.isalpha() and x.isascii()):
            return None
        if x not in cods:
            cods.append(x)
    return cods if 1 <= len(cods) <= maximo else None


def _datas(lst):
    return sorted({date.fromisoformat(x) for x in lst})


def _trajeto(c, i):
    """Valida um trajeto vindo do config.json; devolve dict ou None se invalido."""
    try:
        tid = str(c.get("id") or ("t%d" % (i + 1)))
        if not (1 <= len(tid) <= 24 and all(ch.isalnum() or ch == "-" for ch in tid) and tid.isascii()):
            return None
        org, dst = _cods(c.get("origens"), 3), _cods(c.get("destino"), 1)
        idas, voltas = _datas(c["idas"]), _datas(c["voltas"])
        if not org or not dst or dst[0] in org or not idas or not voltas:
            return None
        if len(idas) * len(voltas) > MAX_COMBINACOES or not any((v - i_).days >= MIN_NOITES for i_ in idas for v in voltas):
            return None
        teto = int(c.get("teto") or TETO_PADRAO)
        if not 100 <= teto <= 50000:
            teto = TETO_PADRAO
        nome = str(c.get("nome") or "").strip()[:40] or ("%s > %s" % ("/".join(org), dst[0]))
        return {"id": tid, "nome": nome, "origens": ",".join(org), "destino": dst[0], "idas": idas, "voltas": voltas, "teto": teto}
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


TRAJETO_PADRAO = {"id": ID_PADRAO, "nome": "Sao Paulo > Aracaju", "origens": "CGH,GRU,VCP", "destino": "AJU",
                  "idas": IDAS_PADRAO, "voltas": VOLTAS_PADRAO, "teto": TETO_PADRAO}


def carregar_trajetos():
    """Le os trajetos de config.json (gravado pelo painel). Aceita o formato antigo (um trajeto so)."""
    try:
        c = json.loads(CONFIG.read_text(encoding="utf-8"))
        if isinstance(c.get("trajetos"), list):
            res, ids = [], set()
            for i, t in enumerate(c["trajetos"][:MAX_TRAJETOS]):
                v = _trajeto(t, i)
                if v and v["id"] not in ids:
                    ids.add(v["id"]); res.append(v)
            if res:
                return res
        else:  # formato antigo: idas/voltas/teto na raiz = Sao Paulo > Aracaju
            v = _trajeto({"id": ID_PADRAO, "nome": TRAJETO_PADRAO["nome"], "origens": "CGH,GRU,VCP", "destino": "AJU",
                          "idas": c["idas"], "voltas": c["voltas"], "teto": c.get("teto")}, 0)
            if v:
                return [v]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return [dict(TRAJETO_PADRAO)]


TRAJETOS = carregar_trajetos()

KEY = os.getenv("SERPAPI_KEY")
TOPIC = (os.getenv("NTFY_TOPIC") or "").strip()
CAMPOS = ["consulta", "ida", "volta", "preco", "origem", "cia", "escalas", "link", "trajeto"]
NL = chr(10)


def rotulo(t):
    o = t["origens"]
    return ("SAO" if o == "CGH,GRU,VCP" else o.replace(",", "/")) + "-" + t["destino"]


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
    h = {"Title": ascii_safe(titulo), "Priority": prioridade, "Tags": "airplane"}
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
    """Triplas (trajeto, ida, volta) de todos os trajetos; cada execucao pega um bloco diferente (rodizio),
    entao a cota de buscas e dividida igualmente entre trajetos e datas."""
    if n <= 0:
        return []
    hoje = date.today()
    todos = [(t, i, v) for t in TRAJETOS for i in t["idas"] for v in t["voltas"]
             if (v - i).days >= MIN_NOITES and i > hoje]
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


def buscar(t, ida, volta):
    params = {"engine": "google_flights", "departure_id": t["origens"], "arrival_id": t["destino"],
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
    res["trajeto"] = t["id"]
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
    if res["direto"]:
        res["direto"]["trajeto"] = t["id"]
    return res


# ---------------------------- historico ----------------------------
def ler_historico():
    if not HISTORICO.exists():
        return []
    rows, vistos = [], set()
    with HISTORICO.open(encoding="utf-8") as f:
        for l in csv.DictReader(f):
            try:
                l["trajeto"] = l.get("trajeto") or ID_PADRAO   # historico antigo = Sao Paulo > Aracaju
                chave = (l["consulta"], l["ida"], l["volta"], int(float(l["preco"])), l["origem"], l["cia"], l["trajeto"])
                if chave in vistos:
                    continue  # ignora linhas repetidas (mesma busca no mesmo dia)
                vistos.add(chave)
                rows.append({**l, "preco": float(l["preco"])})
            except (KeyError, ValueError, TypeError):
                pass
    return rows


def na_janela(l, t):
    try:
        return (l.get("trajeto") or ID_PADRAO) == t["id"] and date.fromisoformat(l["ida"]) in t["idas"] and date.fromisoformat(l["volta"]) in t["voltas"]
    except (KeyError, ValueError, TypeError):
        return False


def _linha(l):
    return {c: l.get(c, "") for c in CAMPOS} | {"preco": int(float(l["preco"]))}


def salvar(rs, existentes):
    hoje = date.today().isoformat()
    chaves = {(l.get("consulta"), l.get("ida"), l.get("volta"), str(int(l["preco"])), l.get("origem"), l.get("cia"), l.get("trajeto") or ID_PADRAO)
              for l in existentes}
    novos = [r for r in rs
             if (hoje, r["ida"], r["volta"], str(int(r["preco"])), r["origem"], r["cia"], r.get("trajeto") or ID_PADRAO) not in chaves]
    if not novos:
        return
    cab_ok = False
    if HISTORICO.exists() and HISTORICO.stat().st_size > 0:
        with HISTORICO.open(encoding="utf-8") as f:
            cab_ok = f.readline().strip().split(",") == CAMPOS
    if not cab_ok:   # arquivo novo ou no formato antigo (sem coluna trajeto): reescreve tudo uma vez
        with HISTORICO.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CAMPOS, extrasaction="ignore")
            w.writeheader()
            for l in existentes:
                w.writerow(_linha(l))
    with HISTORICO.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS, extrasaction="ignore")
        for r in novos:
            w.writerow({"consulta": hoje, **r, "trajeto": r.get("trajeto") or ID_PADRAO})


# ---------------------------- resumo semanal ----------------------------
def grafico(jan, t):
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
        ax.set_title(f"Menor preco por dia - {rotulo(t)} ({t['idas'][0]:%d/%m} a {t['voltas'][-1]:%d/%m})")
        ax.set_ylabel("R$")
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(GRAFICO, dpi=120)
        return GRAFICO.read_bytes()
    except Exception as e:
        print("Grafico nao gerado:", type(e).__name__)
        return None


def resumo_semanal(hoje, t, hist):
    jan = [l for l in hist if na_janela(l, t)]
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
    avisar(f"Resumo semanal {rotulo(t)}", NL.join(linhas), link=melhor.get("link", ""), anexo=grafico(jan, t))


def alertar(t, rs, hist, hoje, left, usadas):
    """Decide e envia o aviso de UM trajeto, com base nas buscas desta rodada (rs) e no historico dele."""
    jan = [l for l in hist if na_janela(l, t)]
    ant = [l["preco"] for l in jan]
    recorde = min(ant) if ant else None
    lim = (hoje - timedelta(days=30)).isoformat()
    rec30 = [l for l in jan if l.get("consulta", "") >= lim]
    dias30 = {l["consulta"] for l in rec30}
    media = statistics.mean(l["preco"] for l in rec30) if len(rec30) >= 6 and len(dias30) >= 3 else None
    rs = sorted(rs, key=lambda r: r["preco"])
    melhor = rs[0]
    p, teto, tag = melhor["preco"], t["teto"], rotulo(t)
    if recorde is None:
        titulo, prio = f"Monitoramento {tag} iniciado", "default"
    elif p < recorde:
        titulo, prio = f"Menor preco ja visto! {tag}", "high"
    elif p <= teto:
        titulo, prio = f"Abaixo de R$ {teto}! {tag}", "high"
    elif media and p <= media * (1 - QUEDA_PERCENTUAL / 100):
        titulo, prio = f"Queda de preco {tag}", "default"
    else:
        titulo = None
    if not titulo:
        rec = f"{recorde:.0f}" if recorde else "-"
        print(f"[{tag}] Sem alerta: menor preco R$ {p:.0f} (recorde R$ {rec}, teto R$ {teto}).")
        return
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
    if left is not None and left - usadas <= 20:
        linhas.append(f"Atencao: restam ~{max(left - usadas, 0)} buscas no SerpAPI")
    acoes = "; ".join(f"view, Opcao {i + 1} R$ {r['preco']:.0f}, '{r['link']}'"
                      for i, r in enumerate(rs[:3]) if r.get("link"))
    avisar(titulo, NL.join(linhas), melhor["link"], prio, acoes)


# ---------------------------- principal ----------------------------
def main():
    if not KEY:
        avisar("Falha no alerta de passagens", "SERPAPI_KEY nao definida.", prioridade="high")
        sys.exit("Defina SERPAPI_KEY.")
    hoje = date.today()
    left = cota_restante()
    if left is not None and left <= 0:
        avisar("Falha no alerta de passagens", "A cota de buscas do SerpAPI acabou neste mes.", prioridade="high")
        sys.exit(1)
    n = MAX_BUSCAS if left is None else min(MAX_BUSCAS, left)
    combos = combinacoes(n)
    if not combos:
        print("Sem datas futuras em nenhum trajeto: busca encerrada.")
        for t in TRAJETOS:
            if hoje <= t["idas"][-1] + timedelta(days=1):
                avisar(f"Alerta {rotulo(t)} encerrado", "A janela de datas terminou. Configure novas datas para continuar.")
        return

    rs, erros = [], []
    for t, ida, volta in combos:
        try:
            r = buscar(t, ida, volta)
        except requests.HTTPError as e:
            cod = e.response.status_code if e.response is not None else "?"
            erros.append(f"HTTP {cod}")
            print(f"Erro {rotulo(t)} {ida}->{volta}: HTTP {cod}")
        except Exception as e:
            erros.append(type(e).__name__)
            print(f"Erro {rotulo(t)} {ida}->{volta}: {type(e).__name__}")
        else:
            if r:
                rs.append(r)
    if not rs:
        motivo = ", ".join(sorted(set(erros))) if erros else "sem ofertas para as datas"
        avisar("Falha no alerta de passagens", f"A busca de hoje nao trouxe resultados ({motivo}).", prioridade="high")
        sys.exit(1)

    hist = ler_historico()
    salvar(rs, hist)   # (alertas comparam com o historico de ANTES desta rodada)
    try:  # voos detalhados desta rodada; o gerar_dados.py junta ao painel
        VOOS_NOVOS.write_text(json.dumps(
            {f"{r['trajeto']}|{r['ida']}|{r['volta']}": {"consulta": hoje.isoformat(), "link": r.get("link", ""), "voos": r.get("voos", [])}
             for r in rs}, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print("Nao salvei os voos detalhados:", type(e).__name__)

    for t in TRAJETOS:
        rt = [r for r in rs if r.get("trajeto") == t["id"]]
        if rt:
            alertar(t, rt, hist, hoje, left, len(combos))

    if hoje.weekday() == 0 and datetime.now().hour < 12:
        hist2 = ler_historico()
        for t in TRAJETOS:
            resumo_semanal(hoje, t, hist2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        avisar("Erro no alerta de passagens", f"Falha inesperada: {type(e).__name__}", prioridade="high")
        raise
