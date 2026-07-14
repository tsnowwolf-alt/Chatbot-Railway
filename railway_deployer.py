"""
railway_deployer.py
---------------------
Automação do deploy no Railway via API pública GraphQL — não a CLI, para que
o app inteiro continue sendo Python puro, sem depender de um binário externo
instalado no ambiente onde o Streamlit roda (o que quebraria em plataformas
como o Streamlit Community Cloud).

Fluxo do deploy inicial (cada etapa é uma chamada GraphQL separada, nesta ordem):
  1) projectCreate            -> cria um projeto novo (já tenta trazer o
                                   environment padrão "production" na mesma resposta)
  2) serviceCreate             -> cria o serviço ligado ao repositório do GitHub
  3) serviceInstanceDeployV2    -> garante que um deploy seja disparado
  4) serviceDomainCreate        -> gera o domínio público gratuito (*.up.railway.app)
  5) polling de deployments(...).status, com um teto de tempo, só para dar um
     retorno confiável (sucesso/falha/ainda buildando) ao usuário

Também expõe, para uso pela auto-correção (ver auto_correcao.py):
  - buscar_logs_deployment(...) — best-effort; a query deploymentLogs da API
    do Railway tem relatos consistentes de falhar com "Problem processing
    request" mesmo com token válido, então isso é tratado como uma exceção
    própria (BuscaLogsError) em vez de um erro genérico.
  - redisparar_deploy(...)      — dispara e espera um novo deploy num serviço
    já existente, sem recriar projeto/serviço/domínio.

Referência: https://docs.railway.com/integrations/api
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

import requests

RAILWAY_API_URL = "https://backboard.railway.com/graphql/v2"

# Status possíveis de um deployment, conforme a documentação da API do Railway.
# Públicos (sem "_") porque auto_correcao.py precisa checar esses conjuntos também.
STATUS_SUCESSO = {"SUCCESS"}
STATUS_FALHA = {"FAILED", "CRASHED", "REMOVED"}


class PublicacaoRailwayError(Exception):
    """Erro amigável para qualquer falha ao criar o projeto/serviço/domínio no Railway."""


class BuscaLogsError(Exception):
    """
    Erro específico para falhas ao buscar logs. É separado de PublicacaoRailwayError
    de propósito: segundo relatos consistentes da comunidade (e não só um caso
    isolado), as queries deploymentLogs/buildLogs da API do Railway às vezes
    devolvem "Problem processing request" mesmo com um token válido — uma
    limitação conhecida do lado deles, não necessariamente um problema de
    configuração. Separar a exceção deixa quem chama decidir: parar e pedir o
    log manualmente (como o usuário já fez com sucesso antes) em vez de tratar
    como uma falha genérica de API.
    """


@dataclass
class ResultadoDeployRailway:
    url_app: str  # https://xxxx.up.railway.app — a URL pública da aplicação
    url_projeto: str  # link do dashboard do Railway para esse projeto
    status_final: str  # "SUCCESS" | ainda em andamento (ex.: "BUILDING") | "FAILED" etc.
    project_id: str
    service_id: str
    environment_id: str
    deployment_id: str  # necessário para buscar logs e para o redeploy da auto-correção


# ---------------------------------------------------------------------------
# Cliente GraphQL mínimo — só o necessário, sem trazer uma lib de GraphQL a
# mais como dependência. É só um POST com query + variables, como a própria
# documentação do Railway recomenda.
# ---------------------------------------------------------------------------
def _chamar_graphql(token: str, query: str, variables: dict, timeout: int = 30) -> dict:
    try:
        resposta = requests.post(
            RAILWAY_API_URL,
            json={"query": query, "variables": variables},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as erro:
        raise PublicacaoRailwayError(f"Falha de rede ao falar com a API do Railway: {erro}") from erro

    if resposta.status_code == 401:
        raise PublicacaoRailwayError(
            "RAILWAY_TOKEN inválido/expirado (HTTP 401). Precisa ser um token de Account ou "
            "Workspace — um token de Project não tem permissão para criar projetos novos."
        )
    if resposta.status_code >= 500:
        raise PublicacaoRailwayError(f"A API do Railway está instável no momento (HTTP {resposta.status_code}).")

    try:
        corpo = resposta.json()
    except ValueError as erro:
        raise PublicacaoRailwayError(f"A API do Railway devolveu uma resposta que não é JSON válido: {erro}") from erro

    if corpo.get("errors"):
        mensagens = "; ".join(e.get("message", "erro desconhecido") for e in corpo["errors"])
        raise PublicacaoRailwayError(f"A API do Railway recusou a operação: {mensagens}")

    return corpo.get("data") or {}


# ---------------------------------------------------------------------------
# Queries e mutations (nomes e formatos conforme docs.railway.com/integrations/api)
# ---------------------------------------------------------------------------
_MUTATION_PROJECT_CREATE = """
mutation projectCreate($input: ProjectCreateInput!) {
  projectCreate(input: $input) {
    id
    environments { edges { node { id name } } }
  }
}
"""

_QUERY_PROJECT_ENVIRONMENTS = """
query project($id: String!) {
  project(id: $id) {
    id
    environments { edges { node { id name } } }
  }
}
"""

_MUTATION_SERVICE_CREATE = """
mutation serviceCreate($input: ServiceCreateInput!) {
  serviceCreate(input: $input) {
    id
    name
  }
}
"""

_MUTATION_SERVICE_DEPLOY = """
mutation serviceInstanceDeployV2($serviceId: String!, $environmentId: String!, $commitSha: String) {
  serviceInstanceDeployV2(serviceId: $serviceId, environmentId: $environmentId, commitSha: $commitSha)
}
"""

_MUTATION_SERVICE_DOMAIN_CREATE = """
mutation serviceDomainCreate($input: ServiceDomainCreateInput!) {
  serviceDomainCreate(input: $input) {
    domain
  }
}
"""

_QUERY_DEPLOYMENTS = """
query deployments($input: DeploymentListInput!, $first: Int) {
  deployments(input: $input, first: $first) {
    edges { node { id status } }
  }
}
"""

_QUERY_DEPLOYMENT_LOGS = """
query deploymentLogs($deploymentId: String!, $limit: Int) {
  deploymentLogs(deploymentId: $deploymentId, limit: $limit) {
    timestamp
    severity
    message
  }
}
"""


def _primeiro_environment_id(projeto: dict) -> str:
    edges = ((projeto or {}).get("environments") or {}).get("edges") or []
    return edges[0]["node"]["id"] if edges else ""


# ---------------------------------------------------------------------------
# Orquestrador
# ---------------------------------------------------------------------------
def publicar_no_railway(
    token: str,
    nome_projeto: str,
    repositorio_github: str,  # formato "dono/repo", vindo de ResultadoPublicacaoGithub.full_name
    on_status: Optional[Callable[[str], None]] = None,
    tempo_maximo_espera_segundos: int = 150,
    intervalo_polling_segundos: int = 6,
    commit_sha: Optional[str] = None,
) -> ResultadoDeployRailway:
    """
    Cria um projeto no Railway, liga um serviço ao repositório do GitHub recém-criado,
    dispara o build via Nixpacks e gera o domínio público — tudo via API, sem UI.

    Args:
        commit_sha: SHA do commit a buildar (ex.: de github_deployer.obter_commit_sha_atual).
            Opcional aqui porque, na criação inicial, o serviceCreate já associa o
            commit mais recente automaticamente — mas passar explicitamente
            remove qualquer ambiguidade. Em um REDEPLOY (ver redisparar_deploy),
            isso deixa de ser opcional na prática: sem isso, o Railway reusa o
            commit antigo e ignora qualquer coisa nova publicada no GitHub.

    Raises:
        PublicacaoRailwayError: para qualquer falha em qualquer etapa. A mensagem
            já vem pronta para mostrar ao usuário.
    """
    avisar = on_status or (lambda _m: None)

    if not token:
        raise PublicacaoRailwayError("RAILWAY_TOKEN não foi configurado na barra lateral.")
    if not repositorio_github:
        raise PublicacaoRailwayError("Repositório do GitHub (dono/repo) não informado.")

    # 1) Cria o projeto
    avisar(f"🚂 Criando o projeto '{nome_projeto}' no Railway...")
    dados = _chamar_graphql(token, _MUTATION_PROJECT_CREATE, {"input": {"name": nome_projeto}})
    projeto = dados.get("projectCreate") or {}
    project_id = projeto.get("id")
    if not project_id:
        raise PublicacaoRailwayError("O Railway não devolveu um ID de projeto válido.")

    environment_id = _primeiro_environment_id(projeto)
    if not environment_id:
        # Rede de segurança: busca de novo, caso o environment padrão não tenha
        # vindo junto na resposta da criação (pequena corrida entre serviços internos do Railway).
        dados_projeto = _chamar_graphql(token, _QUERY_PROJECT_ENVIRONMENTS, {"id": project_id})
        environment_id = _primeiro_environment_id(dados_projeto.get("project") or {})
    if not environment_id:
        raise PublicacaoRailwayError(
            f"Projeto criado (id={project_id}), mas não encontrei o environment padrão dele. "
            f"Veja em https://railway.com/project/{project_id}."
        )

    # 2) Cria o serviço ligado ao repositório do GitHub
    avisar(f"🔧 Criando o serviço a partir de `{repositorio_github}`...")
    dados_servico = _chamar_graphql(
        token,
        _MUTATION_SERVICE_CREATE,
        {"input": {"projectId": project_id, "name": nome_projeto, "source": {"repo": repositorio_github}}},
    )
    servico = dados_servico.get("serviceCreate") or {}
    service_id = servico.get("id")
    if not service_id:
        raise PublicacaoRailwayError(
            f"O Railway não conseguiu criar o serviço a partir de '{repositorio_github}'. Isso costuma "
            "acontecer quando o GitHub App do Railway ainda não tem acesso a esse repositório "
            "(Settings do repo no GitHub → Integrations → Railway → conceder acesso)."
        )

    # 3) Garante que um deploy seja disparado, já apontando pro commit certo
    # (ver a nota em commit_sha) — o serviceCreate normalmente já dispara um
    # automaticamente quando a origem é um repositório, mas essa chamada é uma
    # rede de segurança; se já havia um em andamento, ela simplesmente
    # falha/repete sem causar problema, então o erro é ignorado.
    avisar("🏗️ Disparando o build (o Nixpacks detecta o railway.toml automaticamente)...")
    try:
        _chamar_graphql(
            token,
            _MUTATION_SERVICE_DEPLOY,
            {"serviceId": service_id, "environmentId": environment_id, "commitSha": commit_sha},
        )
    except PublicacaoRailwayError as erro:
        avisar(f"   (aviso ignorável: {erro})")

    # 4) Gera o domínio público gratuito
    avisar("🌐 Gerando o domínio público (*.up.railway.app)...")
    dados_dominio = _chamar_graphql(
        token, _MUTATION_SERVICE_DOMAIN_CREATE, {"input": {"serviceId": service_id, "environmentId": environment_id}}
    )
    dominio = (dados_dominio.get("serviceDomainCreate") or {}).get("domain") or ""
    if not dominio:
        raise PublicacaoRailwayError("O Railway criou o serviço, mas não devolveu um domínio público.")
    url_app = f"https://{dominio}"
    url_projeto = f"https://railway.com/project/{project_id}"

    # 5) Espera (com teto de tempo) o primeiro build terminar, só para dar um
    # status confiável — a URL acima já é válida mesmo antes disso terminar.
    status_final, deployment_id = _esperar_deployment(
        token=token,
        project_id=project_id,
        service_id=service_id,
        environment_id=environment_id,
        tempo_maximo_segundos=tempo_maximo_espera_segundos,
        intervalo_segundos=intervalo_polling_segundos,
        avisar=avisar,
    )

    return ResultadoDeployRailway(
        url_app=url_app,
        url_projeto=url_projeto,
        status_final=status_final,
        project_id=project_id,
        service_id=service_id,
        environment_id=environment_id,
        deployment_id=deployment_id,
    )


# ---------------------------------------------------------------------------
# Busca de logs (best-effort) — usada pela auto-correção
# ---------------------------------------------------------------------------
def buscar_logs_deployment(token: str, deployment_id: str, limite: int = 300) -> str:
    """
    Busca as linhas de log de um deployment específico.

    ATENÇÃO: a query `deploymentLogs` da API do Railway tem relatos consistentes
    na comunidade de devolver "Problem processing request" mesmo com token
    válido — não é exclusivo deste projeto. Por isso essa função levanta um
    tipo de erro PRÓPRIO (BuscaLogsError), pensado para quem chama tratar como
    "busca automática indisponível agora" e cair para pedir o log manualmente,
    em vez de misturar com os outros erros de PublicacaoRailwayError.
    """
    if not token:
        raise BuscaLogsError("RAILWAY_TOKEN não foi configurado.")
    if not deployment_id:
        raise BuscaLogsError("Nenhum deployment_id disponível para buscar logs.")

    try:
        dados = _chamar_graphql(token, _QUERY_DEPLOYMENT_LOGS, {"deploymentId": deployment_id, "limit": limite})
    except PublicacaoRailwayError as erro:
        raise BuscaLogsError(
            f"A busca automática de logs falhou (limitação conhecida da API do Railway): {erro}"
        ) from erro

    linhas = dados.get("deploymentLogs") or []
    if not linhas:
        raise BuscaLogsError("A API do Railway não devolveu nenhuma linha de log para esse deployment.")

    texto = "\n".join(f"[{linha.get('severity', '?')}] {linha.get('message', '')}" for linha in linhas)
    if not texto.strip():
        raise BuscaLogsError("As linhas de log vieram vazias.")

    # Trunca mantendo o FINAL do log — é onde o traceback/crash normalmente
    # aparece (o mesmo padrão usado ao ler manualmente um log grande).
    limite_caracteres = 8000
    if len(texto) > limite_caracteres:
        texto = "...(log truncado, mostrando só o final)...\n" + texto[-limite_caracteres:]
    return texto


# ---------------------------------------------------------------------------
# Redeploy (usado pela auto-correção e pela edição, depois de atualizar os
# arquivos no GitHub)
# ---------------------------------------------------------------------------
def redisparar_deploy(
    token: str,
    project_id: str,
    service_id: str,
    environment_id: str,
    commit_sha: str,
    on_status: Optional[Callable[[str], None]] = None,
    tempo_maximo_espera_segundos: int = 150,
    intervalo_polling_segundos: int = 6,
) -> ResultadoDeployRailway:
    """
    Dispara um novo deploy num serviço já existente e espera o resultado —
    usado depois que a auto-correção ou uma edição sobe uma versão nova no GitHub.

    IMPORTANTE: commit_sha é obrigatório (não tem valor padrão) de propósito.
    A mutation serviceInstanceDeployV2, sem esse argumento, redisparar o deploy
    usando o commit que já estava associado ao serviço — SEM checar o GitHub por
    commits novos. Passar o SHA do commit mais recente (via
    github_deployer.obter_commit_sha_atual, chamado logo depois de subir os
    arquivos) é o que garante que o Railway realmente builda o código atualizado,
    em vez de re-buildar silenciosamente a versão antiga.
    """
    avisar = on_status or (lambda _m: None)

    avisar(f"🔁 Disparando um novo deploy com o commit `{commit_sha[:7]}`...")
    _chamar_graphql(
        token,
        _MUTATION_SERVICE_DEPLOY,
        {"serviceId": service_id, "environmentId": environment_id, "commitSha": commit_sha},
    )

    status_final, deployment_id = _esperar_deployment(
        token=token,
        project_id=project_id,
        service_id=service_id,
        environment_id=environment_id,
        tempo_maximo_segundos=tempo_maximo_espera_segundos,
        intervalo_segundos=intervalo_polling_segundos,
        avisar=avisar,
    )

    return ResultadoDeployRailway(
        url_app="",  # preenchido pelo chamador, que já sabe a URL do deploy anterior (não muda)
        url_projeto=f"https://railway.com/project/{project_id}",
        status_final=status_final,
        project_id=project_id,
        service_id=service_id,
        environment_id=environment_id,
        deployment_id=deployment_id,
    )


def _esperar_deployment(
    token: str,
    project_id: str,
    service_id: str,
    environment_id: str,
    tempo_maximo_segundos: int,
    intervalo_segundos: int,
    avisar: Callable[[str], None],
) -> tuple[str, str]:
    """Faz polling de deployments(...).status até SUCCESS/FAILED ou o tempo máximo acabar.
    Devolve (status_final, deployment_id) — o deployment_id pode vir vazio se o
    polling nunca encontrou nenhum deployment (bem incomum, mas possível logo
    após o serviceCreate)."""
    decorrido = 0
    ultimo_status = "QUEUED"
    ultimo_deployment_id = ""
    variables = {"input": {"projectId": project_id, "serviceId": service_id, "environmentId": environment_id}, "first": 1}

    while decorrido < tempo_maximo_segundos:
        time.sleep(intervalo_segundos)
        decorrido += intervalo_segundos
        try:
            dados = _chamar_graphql(token, _QUERY_DEPLOYMENTS, variables)
            edges = ((dados.get("deployments") or {}).get("edges")) or []
            if edges:
                ultimo_status = edges[0]["node"]["status"]
                ultimo_deployment_id = edges[0]["node"]["id"]
        except PublicacaoRailwayError:
            continue  # uma falha pontual de polling não deve derrubar o processo inteiro

        if ultimo_status in STATUS_SUCESSO:
            avisar("✅ Build concluído — a aplicação já está no ar.")
            return ultimo_status, ultimo_deployment_id
        if ultimo_status in STATUS_FALHA:
            avisar(f"⚠️ O deploy terminou com status '{ultimo_status}'. Confira os logs no painel do Railway.")
            return ultimo_status, ultimo_deployment_id
        avisar(f"⏳ Status atual: {ultimo_status} ({decorrido}s)...")

    avisar("⏳ Ainda buildando depois do tempo de espera configurado — a URL deve funcionar em instantes.")
    return ultimo_status, ultimo_deployment_id
