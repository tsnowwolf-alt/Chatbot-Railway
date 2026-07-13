"""
auto_correcao.py
------------------
Loop de auto-correção: quando o deploy no Railway falha (status FAILED ou
CRASHED), tenta buscar o log real do erro, pede pra esteira de IA corrigir o
código mantendo o mesmo formato de sempre, publica a correção no MESMO
repositório (atualizando em vez de recriar) e dispara um novo deploy — tudo
automaticamente, até um número máximo de tentativas configurável.

Se a busca automática de log falhar (ver o aviso em
railway_deployer.buscar_logs_deployment sobre a instabilidade conhecida da
query deploymentLogs da API do Railway), o loop para nessa hora e devolve um
resultado pedindo o log manualmente — o mesmo fluxo que já funciona hoje
(colar/anexar o .txt exportado do painel do Railway), em vez de tentar
"corrigir no escuro" sem nenhuma evidência do que quebrou.
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_cascade import ConfiguracaoModelos, TodasCamadasFalharamError, gerar_codigo_com_fallback
from file_parser import NenhumArquivoEncontradoError, extrair_arquivos
from github_deployer import PublicacaoGithubError, atualizar_arquivos_no_github, garantir_railway_toml
from prompts import montar_prompt_correcao
from railway_deployer import (
    STATUS_FALHA,
    STATUS_SUCESSO,
    BuscaLogsError,
    PublicacaoRailwayError,
    ResultadoDeployRailway,
    buscar_logs_deployment,
    redisparar_deploy,
)


@dataclass
class TentativaCorrecao:
    numero: int
    log_erro: str
    status_resultante: str


@dataclass
class ResultadoAutoCorrecao:
    corrigido: bool
    tentativas: list[TentativaCorrecao]
    arquivos_finais: dict[str, str]
    resultado_railway_final: ResultadoDeployRailway
    motivo_parada: str
    # "sem_falha" | "sucesso" | "esgotou_tentativas" | "busca_log_falhou" |
    # "ia_falhou_na_correcao" | "parser_falhou_na_correcao" |
    # "github_falhou_na_correcao" | "redeploy_falhou"


def tentar_auto_correcao(
    pedido_original: str,
    chaves: dict[str, str],
    modelos: ConfiguracaoModelos,
    arquivos_atuais: dict[str, str],
    repo_full_name: str,
    resultado_railway_inicial: ResultadoDeployRailway,
    url_app: str,
    max_tentativas: int = 2,
    on_status=None,
) -> ResultadoAutoCorrecao:
    """
    Executa até `max_tentativas` rodadas de: buscar log -> IA corrige -> sobe
    no GitHub -> redeploy -> checa status. Para assim que um redeploy terminar
    em SUCCESS, ou na primeira etapa que falhar (busca de log, IA, parser,
    GitHub ou o próprio redeploy) — nesses casos, para e devolve o motivo,
    sem tentar "adivinhar" uma correção sem evidência de causa.
    """
    avisar = on_status or (lambda _mensagem: None)

    resultado_railway = resultado_railway_inicial
    arquivos = arquivos_atuais
    tentativas: list[TentativaCorrecao] = []

    if resultado_railway.status_final not in STATUS_FALHA:
        return ResultadoAutoCorrecao(
            corrigido=resultado_railway.status_final in STATUS_SUCESSO,
            tentativas=tentativas,
            arquivos_finais=arquivos,
            resultado_railway_final=resultado_railway,
            motivo_parada="sem_falha",
        )

    numero = 0
    while numero < max_tentativas and resultado_railway.status_final in STATUS_FALHA:
        numero += 1
        avisar(f"🔁 Auto-correção {numero}/{max_tentativas} — status atual: {resultado_railway.status_final}")

        # 1) Busca o log real (best-effort — ver aviso no docstring do módulo)
        try:
            log_erro = buscar_logs_deployment(chaves.get("railway", ""), resultado_railway.deployment_id)
        except BuscaLogsError as erro:
            avisar(f"⚠️ Não consegui buscar os logs automaticamente: {erro}")
            return ResultadoAutoCorrecao(
                corrigido=False,
                tentativas=tentativas,
                arquivos_finais=arquivos,
                resultado_railway_final=resultado_railway,
                motivo_parada="busca_log_falhou",
            )

        # 2) Pede a correção pra esteira de IA (mesmo cascade tripla, prompt diferente)
        prompt_correcao = montar_prompt_correcao(pedido_original, arquivos, log_erro)
        try:
            resultado_ia = gerar_codigo_com_fallback(prompt_correcao, chaves, modelos, on_status=avisar)
        except TodasCamadasFalharamError as erro:
            avisar(f"❌ A esteira de IA falhou tentando gerar a correção: {erro}")
            return ResultadoAutoCorrecao(
                corrigido=False,
                tentativas=tentativas,
                arquivos_finais=arquivos,
                resultado_railway_final=resultado_railway,
                motivo_parada="ia_falhou_na_correcao",
            )

        # 3) Parser — mesmo formato de sempre, então reaproveita sem mudanças
        try:
            arquivos_corrigidos = extrair_arquivos(resultado_ia.texto)
        except NenhumArquivoEncontradoError as erro:
            avisar(f"❌ A IA respondeu fora do formato esperado na correção: {erro}")
            return ResultadoAutoCorrecao(
                corrigido=False,
                tentativas=tentativas,
                arquivos_finais=arquivos,
                resultado_railway_final=resultado_railway,
                motivo_parada="parser_falhou_na_correcao",
            )
        arquivos_corrigidos = garantir_railway_toml(arquivos_corrigidos)

        # 4) Publica a correção no MESMO repositório (atualiza, não recria)
        try:
            atualizar_arquivos_no_github(
                token=chaves.get("github", ""),
                full_name=repo_full_name,
                arquivos=arquivos_corrigidos,
                mensagem_commit=f"fix: auto-correção #{numero} após falha no deploy",
                on_status=avisar,
            )
        except PublicacaoGithubError as erro:
            avisar(f"⚠️ Não consegui publicar a correção no GitHub: {erro}")
            return ResultadoAutoCorrecao(
                corrigido=False,
                tentativas=tentativas,
                arquivos_finais=arquivos_corrigidos,
                resultado_railway_final=resultado_railway,
                motivo_parada="github_falhou_na_correcao",
            )
        arquivos = arquivos_corrigidos

        # 5) Redeploy e espera pelo novo status
        try:
            resultado_railway = redisparar_deploy(
                token=chaves.get("railway", ""),
                project_id=resultado_railway.project_id,
                service_id=resultado_railway.service_id,
                environment_id=resultado_railway.environment_id,
                on_status=avisar,
            )
            resultado_railway.url_app = url_app  # a URL pública não muda entre redeploys
        except PublicacaoRailwayError as erro:
            avisar(f"⚠️ Falha ao redisparar o deploy: {erro}")
            return ResultadoAutoCorrecao(
                corrigido=False,
                tentativas=tentativas,
                arquivos_finais=arquivos,
                resultado_railway_final=resultado_railway,
                motivo_parada="redeploy_falhou",
            )

        tentativas.append(
            TentativaCorrecao(numero=numero, log_erro=log_erro, status_resultante=resultado_railway.status_final)
        )

        if resultado_railway.status_final in STATUS_SUCESSO:
            avisar(f"✅ Corrigido na tentativa {numero}!")
            return ResultadoAutoCorrecao(
                corrigido=True,
                tentativas=tentativas,
                arquivos_finais=arquivos,
                resultado_railway_final=resultado_railway,
                motivo_parada="sucesso",
            )

    return ResultadoAutoCorrecao(
        corrigido=resultado_railway.status_final in STATUS_SUCESSO,
        tentativas=tentativas,
        arquivos_finais=arquivos,
        resultado_railway_final=resultado_railway,
        motivo_parada="esgotou_tentativas",
    )
