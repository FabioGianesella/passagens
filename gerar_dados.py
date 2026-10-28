#!/usr/bin/env python3
"""Gera docs/data.json (usado pelo dashboard) a partir do historico.csv e do config.json."""
import json
from datetime import datetime, timezone
from pathlib import Path

import busca_passagens as b


def main():
    ids = {t["id"] for t in b.TRAJETOS}
    por_t = {i: [] for i in ids}
    for l in b.ler_historico():
        tid = l.get("trajeto") or b.ID_PADRAO
        if tid in por_t:
            por_t[tid].append({
                "trajeto": tid, "consulta": l["consulta"], "ida": l["ida"], "volta": l["volta"],
                "preco": int(l["preco"]), "origem": l.get("origem", ""), "cia": l.get("cia", ""),
                "escalas": int(l.get("escalas") or 0), "link": l.get("link", ""),
            })
    linhas = []
    for tid, ls in por_t.items():
        ls.sort(key=lambda x: (x["consulta"], x["ida"], x["volta"]))
        linhas += ls[-800:]   # historico completo (limitado) por trajeto; o painel filtra pelas datas escolhidas

    out = Path(__file__).with_name("docs") / "data.json"
    voos = {}
    try:
        voos = json.loads(out.read_text(encoding="utf-8")).get("voos") or {}
    except Exception:
        pass
    voos = {(k if k.count("|") == 2 else b.ID_PADRAO + "|" + k): v for k, v in voos.items()}  # formato antigo
    try:
        voos.update(json.loads(Path(__file__).with_name("voos_novos.json").read_text(encoding="utf-8")))
    except Exception:
        pass
    hoje = datetime.now(timezone.utc).date().isoformat()
    voos = {k: v for k, v in voos.items() if k.split("|")[0] in ids and k.split("|")[1] >= hoje}

    trajetos = [{"id": t["id"], "nome": t["nome"], "origens": t["origens"], "destino": t["destino"], "teto": t["teto"],
                 "idas": [d.isoformat() for d in t["idas"]], "voltas": [d.isoformat() for d in t["voltas"]]}
                for t in b.TRAJETOS]
    total = sum(1 for t in b.TRAJETOS for i in t["idas"] for v in t["voltas"] if (v - i).days >= b.MIN_NOITES)
    t0 = trajetos[0]
    dados = {
        "voos": voos,
        "atualizado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "trajetos": trajetos,
        "cobertura": {"combinacoes": total, "buscas_por_dia": b.MAX_BUSCAS * 2, "max_trajetos": b.MAX_TRAJETOS},
        "min_noites": b.MIN_NOITES,
        # campos antigos (paineis em cache): primeiro trajeto
        "rota": {"origens": t0["origens"], "destino": t0["destino"]},
        "janela": {"idas": t0["idas"], "voltas": t0["voltas"], "min_noites": b.MIN_NOITES},
        "teto": t0["teto"],
        "linhas": linhas,
    }
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(dados, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"data.json gerado com {len(linhas)} linhas, {len(trajetos)} trajeto(s)")


if __name__ == "__main__":
    main()
