"""
chat_storage.py
-----------------
Persistência simples de conversas em SQLite (biblioteca padrão — nenhuma
dependência nova) para alimentar a lista de conversas na barra lateral. Cada
linha da tabela `chats` guarda uma conversa inteira, com o histórico de
mensagens serializado em JSON — incluindo os metadados do app gerado nela
(repositório, IDs do Railway), que é como o modo de edição descobre a qual
repositório uma conversa já criada está ligada.

Observação: por ser um arquivo SQLite local, as conversas persistem enquanto
o sistema de arquivos do ambiente onde o Streamlit roda persistir. Em
plataformas com filesystem efêmero (que apagam tudo a cada novo deploy), o
histórico não sobrevive a um redeploy — isso é uma limitação inerente a usar
um arquivo local em vez de um banco externo, não um bug.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

CAMINHO_BANCO = Path(__file__).parent / "vibe_coding_chats.db"


@dataclass
class ResumoChat:
    """Versão leve de uma conversa, só para listar na sidebar (sem o histórico completo)."""
    id: str
    titulo: str
    atualizado_em: str


def _conectar() -> sqlite3.Connection:
    conexao = sqlite3.connect(CAMINHO_BANCO)
    conexao.execute(
        """
        CREATE TABLE IF NOT EXISTS chats (
            id TEXT PRIMARY KEY,
            titulo TEXT NOT NULL,
            criado_em TEXT NOT NULL,
            atualizado_em TEXT NOT NULL,
            mensagens_json TEXT NOT NULL
        )
        """
    )
    return conexao


def _agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def novo_chat_id() -> str:
    return uuid.uuid4().hex[:12]


def gerar_titulo(primeira_mensagem: str, tamanho_maximo: int = 45) -> str:
    """Deriva um título de conversa a partir da primeira mensagem do usuário
    (mesma ideia do ChatGPT/Claude: normaliza espaços e trunca com reticências)."""
    texto = " ".join(primeira_mensagem.split())
    if not texto:
        return "Nova conversa"
    if len(texto) <= tamanho_maximo:
        return texto
    return texto[:tamanho_maximo].rstrip() + "…"


def salvar_chat(chat_id: str, titulo: str, mensagens: list[dict]) -> None:
    """Cria a conversa se ainda não existir, ou atualiza título/mensagens/data
    se já existir (upsert) — preserva 'criado_em' original em ambos os casos."""
    agora = _agora_iso()
    with _conectar() as conexao:
        conexao.execute(
            """
            INSERT INTO chats (id, titulo, criado_em, atualizado_em, mensagens_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                titulo = excluded.titulo,
                atualizado_em = excluded.atualizado_em,
                mensagens_json = excluded.mensagens_json
            """,
            (chat_id, titulo, agora, agora, json.dumps(mensagens, ensure_ascii=False)),
        )


def listar_chats(limite: int = 50) -> list[ResumoChat]:
    """Lista as conversas mais recentes primeiro — leve, sem carregar o histórico inteiro."""
    with _conectar() as conexao:
        linhas = conexao.execute(
            "SELECT id, titulo, atualizado_em FROM chats ORDER BY atualizado_em DESC LIMIT ?",
            (limite,),
        ).fetchall()
    return [ResumoChat(id=linha[0], titulo=linha[1], atualizado_em=linha[2]) for linha in linhas]


def carregar_mensagens(chat_id: str) -> list[dict]:
    with _conectar() as conexao:
        linha = conexao.execute("SELECT mensagens_json FROM chats WHERE id = ?", (chat_id,)).fetchone()
    if not linha:
        return []
    try:
        return json.loads(linha[0])
    except (json.JSONDecodeError, TypeError):
        return []


def excluir_chat(chat_id: str) -> None:
    with _conectar() as conexao:
        conexao.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
