#!/usr/bin/env python3
"""Gera docs/data.json (usado pelo dashboard) a partir do historico.csv."""
import json
from datetime import datetime, timezone
from pathlib import Path

import busca_passagens as b


def main():
    linhas = []
    for l in b.ler_historico():
        linhas.append({
            "consulta": l["consulta"], "ida": l["ida"], "volta": l["volta"],
            "preco": int(l["preco"]), "origem": l.get("origem", ""), "cia": l.get("cia", ""),
            "escalas": int(l.get("escalas") or 0), "link": l.get("link", ""),
        })
    linhas.sort(key=lambda x: (x["consulta"], x["ida"], x["volta"]))
    linhas = linhas[-800:]   # historico completo (limitado); o painel filtra pelas datas escolhidas
    # voos detalhados: junta os desta rodada aos da ultima publicacao (mantem so datas futuras)
    out = Path(__file__).with_name("docs") / "data.json"
    voos = {}
    try:
        voos = json.loads(out.read_text(encoding="utf-8")).get("voos") or {}
    except Exception:
        pass
    try:
        voos.update(json.loads(Path(__file__).with_name("voos_novos.json").read_text(encoding="utf-8")))
    except Exception:
        pass
    hoje = datetime.now(timezone.utc).date().isoformat()
    voos = {k: v for k, v in voos.items() if k.split("|")[0] >= hoje}
    dados = {
        "voos": voos,
        "atualizado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rota": {"origens": b.ORIGENS, "destino": b.DESTINO},
        "janela": {"idas": [d.isoformat() for d in b.IDAS], "voltas": [d.isoformat() for d in b.VOLTAS],
                   "min_noites": b.MIN_NOITES},
        "teto": b.TETO_ALERTA,
        "linhas": linhas,
    }
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(dados, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"data.json gerado com {len(linhas)} linhas")


if __name__ == "__main__":
    main()
