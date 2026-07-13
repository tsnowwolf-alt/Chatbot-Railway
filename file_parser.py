"""
file_parser.py
---------------
Responsável por transformar a resposta em Markdown de qualquer uma das três IAs
em um dicionário {caminho_do_arquivo: conteudo}. Não confia cegamente na IA ter
formatado 100% certo: separa a extração dos marcadores "### FILE:" da limpeza
das cercas de código (```), então um pequeno desvio de formatação em um bloco
não derruba a extração dos demais arquivos.
"""

from __future__ import annotations

import re
import unicodedata

# Uma linha "### FILE: caminho/arquivo.ext" (aceita 1-3 '#', espaços variados,
# e opcionalmente o caminho entre crases, ex.: ### FILE: `app.py`).
_MARCADOR_ARQUIVO = re.compile(
    r"^[ \t]*#{1,3}\s*FILE:\s*`?([^\n`]+?)`?\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# A linha opcional "### PROJECT_NAME: nome-sugerido", usada para escolher o
# nome do repositório quando o usuário não preenche um na sidebar.
_MARCADOR_NOME_PROJETO = re.compile(
    r"^[ \t]*#{1,3}\s*PROJECT_NAME:\s*`?([^\n`]+?)`?\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# Uma cerca de código Markdown de abertura, capturando o identificador de linguagem
# opcional (```python, ```html, ``` etc.) para que possa ser descartado.
_CERCA_ABERTURA = re.compile(r"^[ \t]*```[a-zA-Z0-9_+\-]*[ \t]*$")
_CERCA_FECHAMENTO = re.compile(r"^[ \t]*```[ \t]*$")


class NenhumArquivoEncontradoError(Exception):
    """Levantado quando o texto da IA não contém nenhum marcador ### FILE:."""


def extrair_arquivos(texto_ia: str) -> dict[str, str]:
    """
    Extrai todos os arquivos de uma resposta em Markdown gerada pela IA.

    Args:
        texto_ia: resposta bruta (texto) devolvida por qualquer uma das três
            camadas da esteira (Gemini, OpenRouter ou DeepSeek).

    Returns:
        Dicionário ordenado {caminho_relativo: conteudo_do_arquivo}.

    Raises:
        NenhumArquivoEncontradoError: se nenhum marcador "### FILE:" for encontrado,
            o que geralmente indica que a IA respondeu fora do formato pedido.
    """
    marcadores = list(_MARCADOR_ARQUIVO.finditer(texto_ia))
    if not marcadores:
        raise NenhumArquivoEncontradoError(
            "Nenhum bloco '### FILE: caminho/arquivo' foi encontrado na resposta da IA."
        )

    arquivos: dict[str, str] = {}
    for indice, marcador in enumerate(marcadores):
        caminho = _normalizar_caminho(marcador.group(1))
        inicio = marcador.end()
        fim = marcadores[indice + 1].start() if indice + 1 < len(marcadores) else len(texto_ia)
        bloco_bruto = texto_ia[inicio:fim]
        conteudo = _remover_cerca_de_codigo(bloco_bruto)

        if not caminho or not conteudo.strip():
            continue
        arquivos[caminho] = conteudo

    if not arquivos:
        raise NenhumArquivoEncontradoError(
            "Marcadores '### FILE:' foram encontrados, mas nenhum continha conteúdo utilizável."
        )
    return arquivos


def _normalizar_caminho(caminho_bruto: str) -> str:
    """Limpa o caminho declarado pela IA e bloqueia tentativas de path traversal."""
    caminho = caminho_bruto.strip().strip("`").strip().replace("\\", "/")
    caminho = caminho.lstrip("/")
    # Remove segmentos "." e ".." para nunca escrever fora da raiz do repositório.
    partes_seguras = [parte for parte in caminho.split("/") if parte not in ("", ".", "..")]
    return "/".join(partes_seguras)


def _remover_cerca_de_codigo(bloco: str) -> str:
    """Remove a cerca de código Markdown (```linguagem ... ```) ao redor de um bloco, se existir."""
    linhas = bloco.strip("\n").split("\n")

    if linhas and _CERCA_ABERTURA.match(linhas[0]):
        linhas = linhas[1:]
        # Procura a cerca de fechamento a partir do final; se a IA esqueceu de
        # fechar o bloco, usamos o texto inteiro em vez de descartar tudo.
        for j in range(len(linhas) - 1, -1, -1):
            if _CERCA_FECHAMENTO.match(linhas[j]):
                linhas = linhas[:j]
                break

    conteudo = "\n".join(linhas).strip("\n")
    return conteudo + "\n" if conteudo else ""


def extrair_nome_projeto(texto_ia: str) -> str | None:
    """Extrai o nome sugerido pela IA na linha '### PROJECT_NAME: ...', se presente."""
    match = _MARCADOR_NOME_PROJETO.search(texto_ia)
    if not match:
        return None
    nome = match.group(1).strip().strip("`").strip()
    return nome or None


def sugerir_nome_repositorio(texto_ia: str, pedido_usuario: str) -> str:
    """
    Decide um nome de repositório quando o usuário deixou o campo em branco:
    usa o nome sugerido pela IA (### PROJECT_NAME:) se existir e for
    utilizável; caso contrário, gera um slug a partir do próprio pedido do
    usuário. Colisão de nome já existente é responsabilidade do
    github_deployer (ele tenta de novo com um sufixo), então essa função não
    se preocupa em garantir unicidade.
    """
    candidato = extrair_nome_projeto(texto_ia) or pedido_usuario
    slug = _slugificar(candidato)
    return slug or "meu-app"


def _slugificar(texto: str, tamanho_maximo: int = 40) -> str:
    """Converte um texto livre (com acentos, maiúsculas, pontuação) num slug
    seguro para nome de repositório do GitHub: minúsculo, kebab-case, ASCII."""
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    texto = texto.lower().strip()
    texto = re.sub(r"[^a-z0-9\s-]", "", texto)
    texto = re.sub(r"[\s_]+", "-", texto)
    texto = re.sub(r"-{2,}", "-", texto).strip("-")
    return texto[:tamanho_maximo].strip("-")
