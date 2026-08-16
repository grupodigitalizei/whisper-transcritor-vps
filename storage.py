#!/usr/bin/env python3
"""Inventário e limpeza do que o app guarda em disco.

Por que existe
──────────────
O app acumula coisas em lugares diferentes (mídia original, resultados de
transcrição, datasets de coleta, miniaturas, planilhas exportadas) e não havia
uma tela para ver o total nem para apagar seletivamente. Quem quisesse
recuperar espaço tinha que ir pelo Finder — sem saber o que era seguro remover.

Como está organizado
────────────────────
Cada CATEGORIA declara onde mora, o que é um "item" ali e o quanto aquilo é
recuperável se for apagado:

  regeneravel=True  → o app refaz sozinho quando precisar (cache, planilhas)
  regeneravel=False → é conteúdo do usuário; apagar é definitivo

Quem consome isto decide o tom do aviso a partir desse campo, em vez de manter
uma lista de exceções na interface.

O módulo não importa o whisper-app: quem sabe dizer se um arquivo está em uso
agora é injetado por `configure()` — do contrário a limpeza poderia apagar o
arquivo de uma transcrição em andamento.
"""
from __future__ import annotations

import os
import re
import shutil
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, ".whisper_data")

UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
RESULTS_DIR = os.path.join(DATA_DIR, "results")
SOCIAL_DIR = os.path.join(DATA_DIR, "social")
SOCIAL_DATA_DIR = os.path.join(SOCIAL_DIR, "data")
SOCIAL_CACHE_DIR = os.path.join(SOCIAL_DIR, "cache", "thumbs")
SOCIAL_EXPORT_DIR = os.path.join(SOCIAL_DIR, "exports")
SOCIAL_MEDIA_DIR = os.path.join(SOCIAL_DIR, "media")

# Injetado pelo app: devolve o task_id se o arquivo estiver em uso agora.
_em_uso = lambda filename: None  # noqa: E731


def configure(*, em_uso=None) -> None:
    global _em_uso
    if em_uso is not None:
        _em_uso = em_uso


# ── util ───────────────────────────────────────────────────────────────────
def _tamanho_de(caminho: str) -> int:
    """Bytes ocupados por um arquivo ou por uma pasta inteira."""
    try:
        if os.path.isfile(caminho):
            return os.path.getsize(caminho)
        total = 0
        for raiz, _, arquivos in os.walk(caminho):
            for f in arquivos:
                try:
                    total += os.path.getsize(os.path.join(raiz, f))
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def _mtime(caminho: str) -> float:
    try:
        return os.path.getmtime(caminho)
    except OSError:
        return 0.0


def _listar(pasta: str) -> list:
    try:
        return sorted(os.listdir(pasta))
    except OSError:
        return []


# ── categorias ─────────────────────────────────────────────────────────────
# `id` é o que a interface manda de volta nas chamadas de exclusão.
CATEGORIAS = {
    "uploads": {
        "titulo": "Mídias originais",
        "descricao": "Áudios e vídeos baixados ou enviados. As transcrições "
                     "continuam salvas mesmo se você apagar a mídia.",
        "pasta": UPLOAD_DIR,
        "regeneravel": False,
    },
    "results": {
        "titulo": "Transcrições",
        "descricao": "O texto de cada transcrição (.txt, .srt, .json, .md). "
                     "Apagar aqui remove o conteúdo em si.",
        "pasta": RESULTS_DIR,
        "regeneravel": False,
    },
    "social_data": {
        "titulo": "Coletas de redes sociais",
        "descricao": "As listas de posts que aparecem em “Coletas anteriores”.",
        "pasta": SOCIAL_DATA_DIR,
        "regeneravel": False,
    },
    "social_media": {
        "titulo": "Mídia de redes sociais",
        "descricao": "Vídeos e imagens baixados pela aba Redes Sociais.",
        "pasta": SOCIAL_MEDIA_DIR,
        "regeneravel": False,
    },
    "social_exports": {
        "titulo": "Planilhas exportadas",
        "descricao": "Excel e CSV gerados nas coletas. Podem ser exportados de novo.",
        "pasta": SOCIAL_EXPORT_DIR,
        "regeneravel": True,
    },
    "social_cache": {
        "titulo": "Miniaturas em cache",
        "descricao": "Capas baixadas para o mosaico. O app rebaixa quando precisar.",
        "pasta": SOCIAL_CACHE_DIR,
        "regeneravel": True,
    },
}


def _item_de(cat_id: str, nome: str, caminho: str) -> dict:
    item = {
        "id": nome,
        "nome": nome,
        "bytes": _tamanho_de(caminho),
        "modificado": _mtime(caminho),
        "em_uso": bool(_em_uso(nome)) if cat_id == "uploads" else False,
    }
    if cat_id == "social_data":
        # ds_id costuma ser "<perfil>_<data>_<hora>.json" — mostra bonito.
        base = nome[:-5] if nome.endswith(".json") else nome
        m = re.match(r"^(.*)_(\d{4}-\d{2}-\d{2})_(\d{4})$", base)
        if m:
            item["nome"] = f"@{m.group(1)}"
            item["detalhe"] = f"{m.group(2)} às {m.group(3)[:2]}:{m.group(3)[2:]}"
        item["ds_id"] = base
    return item


def overview() -> dict:
    """Resumo de todas as categorias, para a tela principal."""
    cats = []
    total = 0
    for cat_id, cfg in CATEGORIAS.items():
        pasta = cfg["pasta"]
        nomes = _listar(pasta)
        tamanho = _tamanho_de(pasta)
        total += tamanho
        cats.append({
            "id": cat_id,
            "titulo": cfg["titulo"],
            "descricao": cfg["descricao"],
            "regeneravel": cfg["regeneravel"],
            "itens": len(nomes),
            "bytes": tamanho,
            "existe": os.path.isdir(pasta),
        })
    cats.sort(key=lambda c: c["bytes"], reverse=True)
    return {"categorias": cats, "total_bytes": total,
            "livre_bytes": _espaco_livre()}


def _espaco_livre() -> int:
    try:
        st = os.statvfs(DATA_DIR)
        return st.f_bavail * st.f_frsize
    except OSError:
        return 0


def listar_itens(cat_id: str, limite: int = 500) -> dict:
    """Itens de uma categoria, do maior para o menor."""
    cfg = CATEGORIAS.get(cat_id)
    if not cfg:
        raise KeyError("categoria desconhecida")
    pasta = cfg["pasta"]
    itens = [_item_de(cat_id, nome, os.path.join(pasta, nome))
             for nome in _listar(pasta)]
    itens.sort(key=lambda i: i["bytes"], reverse=True)
    truncado = len(itens) > limite
    return {"id": cat_id, "titulo": cfg["titulo"],
            "regeneravel": cfg["regeneravel"],
            "itens": itens[:limite], "total": len(itens), "truncado": truncado}


def _dentro_de(caminho: str, pasta: str) -> bool:
    """Garante que o alvo não escapou da pasta da categoria."""
    try:
        return os.path.commonpath([os.path.realpath(caminho),
                                   os.path.realpath(pasta)]) == os.path.realpath(pasta)
    except (ValueError, OSError):
        return False


def apagar(cat_id: str, ids: list) -> dict:
    """Apaga itens de uma categoria. Devolve o que saiu e o que foi poupado."""
    cfg = CATEGORIAS.get(cat_id)
    if not cfg:
        raise KeyError("categoria desconhecida")
    pasta = cfg["pasta"]

    apagados, liberados, falhas, em_uso = 0, 0, [], []
    for bruto in ids:
        nome = os.path.basename((bruto or "").strip())
        if not nome or nome in (".", ".."):
            continue
        caminho = os.path.join(pasta, nome)
        if not _dentro_de(caminho, pasta) or not os.path.exists(caminho):
            falhas.append(nome)
            continue
        # Arquivo que uma transcrição/download está usando agora fica de fora:
        # apagá-lo quebraria a tarefa em andamento.
        if cat_id == "uploads" and _em_uso(nome):
            em_uso.append(nome)
            continue
        tamanho = _tamanho_de(caminho)
        try:
            if os.path.isdir(caminho):
                shutil.rmtree(caminho)
            else:
                os.remove(caminho)
            apagados += 1
            liberados += tamanho
        except OSError:
            falhas.append(nome)
    return {"apagados": apagados, "liberados_bytes": liberados,
            "falhas": falhas, "em_uso": em_uso}


def limpar_categoria(cat_id: str) -> dict:
    """Esvazia uma categoria inteira (usado nas regeneráveis)."""
    cfg = CATEGORIAS.get(cat_id)
    if not cfg:
        raise KeyError("categoria desconhecida")
    return apagar(cat_id, _listar(cfg["pasta"]))


# ── sobras que não são de nenhuma categoria ────────────────────────────────
def sobras() -> list:
    """Arquivos soltos em .whisper_data que não deveriam estar ali.

    O caso concreto: o iCloud cria cópias de conflito ("history 2.json") e o
    app deixou backups antigos de auth. Não somem sozinhos e ninguém sabe que
    existem — mas também não podem ser apagados às cegas, então aparecem
    listados à parte, com o motivo.
    """
    esperados = {"history.json", "media.json", "folders.json", "settings.json",
                 "auth.json", "sessions.json", "subscriptions.json",
                 "public_folders.json", "uploads", "results", "social"}
    out = []
    for nome in _listar(DATA_DIR):
        if nome in esperados or nome.startswith("."):
            continue
        caminho = os.path.join(DATA_DIR, nome)
        if re.search(r" \d+\.json$", nome):
            motivo = "cópia criada pelo iCloud ao sincronizar"
        elif ".bak" in nome:
            motivo = "backup antigo"
        else:
            motivo = "não faz parte dos arquivos do app"
        out.append({"id": nome, "nome": nome, "bytes": _tamanho_de(caminho),
                    "modificado": _mtime(caminho), "motivo": motivo})
    out.sort(key=lambda i: i["bytes"], reverse=True)
    return out


def apagar_sobras(ids: list) -> dict:
    """Apaga arquivos listados por sobras(). Nunca toca nos esperados."""
    validos = {s["id"] for s in sobras()}
    apagados, liberados, falhas = 0, 0, []
    for bruto in ids:
        nome = os.path.basename((bruto or "").strip())
        if nome not in validos:
            falhas.append(nome)
            continue
        caminho = os.path.join(DATA_DIR, nome)
        tamanho = _tamanho_de(caminho)
        try:
            if os.path.isdir(caminho):
                shutil.rmtree(caminho)
            else:
                os.remove(caminho)
            apagados += 1
            liberados += tamanho
        except OSError:
            falhas.append(nome)
    return {"apagados": apagados, "liberados_bytes": liberados, "falhas": falhas}
