#!/usr/bin/env python3
"""Gera docs/data.json (usado pelo dashboard) a partir do historico.csv."""
import json
from datetime import datetime, timezone
from pathlib import Path

import busca_passagens as b


def main():
    linhas = []
    for l in b.ler_historico():
        if not b.na_janela(l):
            continue
        linhas.append({
            "consulta": l["consulta"], "ida": l["ida"], "volta": l["volta"],
            "preco": int(l["preco"]), "aeroporto": l.get("aeroporto", ""), "cia": l.get("cia", ""),
            "escalas": int(l.get("escalas") or 0), "link": l.get("link", ""),
        })
    linhas.sort(key=lambda x: (x["consulta"], x["ida"], x["volta"]))
    dados = {
        "atualizado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rota": {"origem": b.ORIGEM, "destinos": b.DESTINOS},
        "janela": {"idas": [d.isoformat() for d in b.IDAS], "voltas": [d.isoformat() for d in b.VOLTAS],
                   "min_noites": b.MIN_NOITES},
        "teto": b.TETO_ALERTA,
        "linhas": linhas,
    }
    out = Path(__file__).with_name("docs") / "data.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(dados, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"data.json gerado com {len(linhas)} linhas")


if __name__ == "__main__":
    main()
