"""
github_deployer.py
--------------------
Automação com a API do GitHub via PyGithub:
  1) cria um repositório novo e público na conta dona do token;
  2) garante que railway.toml (builder = "nixpacks") exista na raiz, mesmo que
     a IA tenha esquecido de gerá-lo;
  3) faz upload em lote de todos os arquivos do projeto gerado.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass

from github import Auth, Github, GithubException

RAILWAY_TOML_PADRAO = """[build]
builder = "NIXPACKS"

[deploy]
startCommand = "python app.py"
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 3
"""


class PublicacaoGithubError(Exception):
    """Erro amigável para qualquer falha ao criar o repositório ou subir arquivos."""


@dataclass
class ResultadoPublicacaoGithub:
    html_url: str  # https://github.com/dono/repo
    full_name: str  # dono/repo — é isso que a API do Railway espera em `source.repo`
    nome_repositorio: str  # só o nome do repo, sem o dono (pode ter ganhado sufixo por colisão)


def _slug_disponivel(nome: str) -> str:
    """Anexa um sufixo curto e aleatório para evitar colisão de nomes de repositório."""
    sufixo = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
    return f"{nome}-{sufixo}"


def garantir_railway_toml(arquivos: dict[str, str]) -> dict[str, str]:
    """Garante que exista um railway.toml na raiz, sem sobrescrever um já gerado pela IA."""
    if "railway.toml" not in arquivos:
        arquivos = {**arquivos, "railway.toml": RAILWAY_TOML_PADRAO}
    return arquivos


def publicar_no_github(
    token: str,
    nome_repositorio: str,
    arquivos: dict[str, str],
    descricao: str = "Aplicação gerada automaticamente pelo Vibe Coding Chatbot",
    on_status=None,
) -> ResultadoPublicacaoGithub:
    """
    Cria um repositório público e sobe todos os arquivos gerados.

    Args:
        token: GITHUB_TOKEN (Personal Access Token) com escopo 'repo'.
        nome_repositorio: nome desejado para o novo repositório.
        arquivos: dicionário {caminho: conteúdo} vindo do file_parser.
        descricao: descrição do repositório no GitHub.
        on_status: callback opcional para reportar progresso.

    Returns:
        ResultadoPublicacaoGithub com a URL, o "dono/repo" e o nome final do repositório.

    Raises:
        PublicacaoGithubError: para qualquer falha de autenticação, criação ou upload.
    """
    avisar = on_status or (lambda _mensagem: None)

    if not token:
        raise PublicacaoGithubError("GITHUB_TOKEN não foi configurado na barra lateral.")
    if not nome_repositorio or not nome_repositorio.strip():
        raise PublicacaoGithubError("Informe um nome de repositório na barra lateral.")
    if not arquivos:
        raise PublicacaoGithubError("Nenhum arquivo para publicar (o parser não extraiu nada).")

    arquivos = garantir_railway_toml(arquivos)
    nome_repositorio = nome_repositorio.strip()

    try:
        cliente = Github(auth=Auth.Token(token))
        usuario = cliente.get_user()
    except GithubException as erro:
        raise PublicacaoGithubError(
            f"Não foi possível autenticar no GitHub com o token informado (HTTP {erro.status})."
        ) from erro
    except Exception as erro:
        raise PublicacaoGithubError(f"Não foi possível autenticar no GitHub: {erro}") from erro

    avisar(f"📦 Criando o repositório '{nome_repositorio}'...")
    repo = None
    try:
        repo = usuario.create_repo(
            name=nome_repositorio,
            description=descricao,
            private=False,
            auto_init=False,
        )
    except GithubException as erro:
        ja_existe = erro.status == 422
        if ja_existe:
            nome_alternativo = _slug_disponivel(nome_repositorio)
            avisar(f"⚠️ O nome '{nome_repositorio}' já existe nessa conta. Tentando '{nome_alternativo}'...")
            try:
                repo = usuario.create_repo(
                    name=nome_alternativo,
                    description=descricao,
                    private=False,
                    auto_init=False,
                )
                nome_repositorio = nome_alternativo
            except GithubException as segundo_erro:
                raise PublicacaoGithubError(
                    f"Falha ao criar o repositório mesmo com nome alternativo (HTTP {segundo_erro.status})."
                ) from segundo_erro
        else:
            raise PublicacaoGithubError(
                f"Falha ao criar o repositório no GitHub (HTTP {erro.status}): {erro.data}"
            ) from erro

    avisar(f"⬆️ Enviando {len(arquivos)} arquivo(s) para '{repo.full_name}'...")
    falhas_de_upload: list[str] = []
    for caminho, conteudo in arquivos.items():
        try:
            # Sem "branch=" explícito: o primeiro create_file em um repositório
            # vazio cria automaticamente a branch padrão configurada na conta
            # (normalmente "main"), evitando qualquer suposição fixa aqui.
            repo.create_file(
                path=caminho,
                message=f"feat: adiciona {caminho}",
                content=conteudo,
            )
            avisar(f"   • {caminho} ✔")
        except GithubException as erro:
            falhas_de_upload.append(f"{caminho} (HTTP {erro.status})")
        except Exception as erro:
            falhas_de_upload.append(f"{caminho} ({erro})")

    if falhas_de_upload:
        avisar(f"⚠️ {len(falhas_de_upload)} arquivo(s) não subiram: {', '.join(falhas_de_upload)}")

    if len(falhas_de_upload) == len(arquivos):
        raise PublicacaoGithubError(
            "O repositório foi criado, mas nenhum arquivo pôde ser enviado. "
            "Verifique se o token tem o escopo 'repo' habilitado."
        )

    return ResultadoPublicacaoGithub(
        html_url=repo.html_url,
        full_name=repo.full_name,
        nome_repositorio=nome_repositorio,
    )


def atualizar_arquivos_no_github(
    token: str,
    full_name: str,  # "dono/repo" — ResultadoPublicacaoGithub.full_name
    arquivos: dict[str, str],
    mensagem_commit: str = "fix: auto-correção após falha no deploy",
    on_status=None,
) -> None:
    """
    Sobe uma versão corrigida dos arquivos num repositório JÁ EXISTENTE — usada
    pela auto-correção depois de uma falha de deploy. Atualiza arquivos que já
    existem (precisa do sha atual, por isso o get_contents antes) e cria os que
    forem novos, em vez de assumir que é sempre a mesma lista de arquivos de antes.

    Raises:
        PublicacaoGithubError: se o repositório não puder ser aberto ou se
            NENHUM arquivo puder ser atualizado/criado.
    """
    avisar = on_status or (lambda _mensagem: None)

    if not token:
        raise PublicacaoGithubError("GITHUB_TOKEN não foi configurado.")
    if not full_name:
        raise PublicacaoGithubError("Repositório (dono/repo) não informado.")
    if not arquivos:
        raise PublicacaoGithubError("Nenhum arquivo para atualizar.")

    try:
        cliente = Github(auth=Auth.Token(token))
        repo = cliente.get_repo(full_name)
    except GithubException as erro:
        raise PublicacaoGithubError(
            f"Não foi possível abrir o repositório '{full_name}' (HTTP {erro.status})."
        ) from erro
    except Exception as erro:
        raise PublicacaoGithubError(f"Não foi possível abrir o repositório '{full_name}': {erro}") from erro

    falhas: list[str] = []
    for caminho, conteudo in arquivos.items():
        try:
            try:
                conteudo_atual = repo.get_contents(caminho)
                repo.update_file(caminho, mensagem_commit, conteudo, conteudo_atual.sha)
                avisar(f"   • {caminho} atualizado ✔")
            except GithubException as erro_leitura:
                if erro_leitura.status == 404:
                    # Arquivo novo (não existia na versão anterior) — cria em vez de atualizar.
                    repo.create_file(caminho, mensagem_commit, conteudo)
                    avisar(f"   • {caminho} criado ✔")
                else:
                    raise
        except GithubException as erro:
            falhas.append(f"{caminho} (HTTP {erro.status})")
        except Exception as erro:
            falhas.append(f"{caminho} ({erro})")

    if falhas:
        avisar(f"⚠️ {len(falhas)} arquivo(s) não puderam ser atualizados: {', '.join(falhas)}")
    if len(falhas) == len(arquivos):
        raise PublicacaoGithubError(
            "Nenhum arquivo pôde ser atualizado durante a auto-correção. "
            "Verifique se o token ainda tem o escopo 'repo' habilitado."
        )
