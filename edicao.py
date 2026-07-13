"""
edicao.py
----------
Orquestra um pedido de EDIÇÃO numa aplicação já gerada NESTA conversa (a
mesma aba de chat onde o app foi criado, conforme pedido): manda pra esteira
de IA aplicar só o ajuste pedido — com uma imagem de referência opcional,
para mudanças de estilo — publica a atualização no MESMO repositório e, se
essa conversa tiver um deploy no Railway associado, redisparar o deploy
automaticamente.

Reaproveita a maior parte da infraestrutura já construída para a
auto-correção (atualizar_arquivos_no_github, redisparar_deploy) — a
diferença é só o prompt (edição pedida pelo usuário, não correção de um
erro) e o gatilho (mensagem nova do usuário, não uma falha de deploy).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ai_cascade import ConfiguracaoModelos, ImagemAnexada, TodasCamadasFalharamError, gerar_codigo_com_fallback
from file_parser import NenhumArquivoEncontradoError, extrair_arquivos
from github_deployer import PublicacaoGithubError, atualizar_arquivos_no_github, garantir_railway_toml
from prompts import montar_prompt_edicao
from railway_deployer import PublicacaoRailwayError, ResultadoDeployRailway, redisparar_deploy


@dataclass
class ResultadoEdicao:
    sucesso: bool
    arquivos_finais: dict[str, str]
    motivo_parada: str
    # "sucesso" | "ia_falhou" | "parser_falhou" | "github_falhou" | "redeploy_falhou"
    resultado_railway: Optional[ResultadoDeployRailway] = None


def aplicar_edicao(
    pedido_edicao: str,
    chaves: dict[str, str],
    modelos: ConfiguracaoModelos,
    arquivos_atuais: dict[str, str],
    repo_full_name: str,
    imagem: Optional[ImagemAnexada] = None,
    info_railway: Optional[dict] = None,
    on_status=None,
) -> ResultadoEdicao:
    """
    Args:
        pedido_edicao: a nova mensagem do usuário nesta conversa (pedido de ajuste).
        arquivos_atuais: o snapshot mais recente dos arquivos do app nesta conversa.
        repo_full_name: "dono/repo" do app já publicado.
        imagem: referência visual opcional (ex.: print de um estilo desejado).
        info_railway: se essa conversa tem deploy associado, um dict com
            "project_id", "service_id", "environment_id" e "url_app" — usado
            para redisparar o deploy depois de publicar a edição. None
            (padrão) significa "essa conversa não tem deploy no Railway
            ainda", e a etapa de redeploy é simplesmente pulada.
    """
    avisar = on_status or (lambda _mensagem: None)

    # 1) Pede a edição pra esteira de IA (mesmo cascade tripla, com imagem se houver)
    prompt = montar_prompt_edicao(pedido_edicao, arquivos_atuais, tem_imagem=imagem is not None)
    avisar("✏️ Aplicando a edição pedida...")
    try:
        resultado_ia = gerar_codigo_com_fallback(prompt, chaves, modelos, on_status=avisar, imagem=imagem)
    except TodasCamadasFalharamError as erro:
        avisar(f"❌ A esteira de IA falhou tentando aplicar a edição: {erro}")
        return ResultadoEdicao(sucesso=False, arquivos_finais=arquivos_atuais, motivo_parada="ia_falhou")

    # 2) Parser — mesmo formato de sempre
    try:
        arquivos_editados = extrair_arquivos(resultado_ia.texto)
    except NenhumArquivoEncontradoError as erro:
        avisar(f"❌ A IA respondeu fora do formato esperado na edição: {erro}")
        return ResultadoEdicao(sucesso=False, arquivos_finais=arquivos_atuais, motivo_parada="parser_falhou")
    arquivos_editados = garantir_railway_toml(arquivos_editados)

    # 3) Publica no MESMO repositório (atualiza os arquivos existentes, cria os novos)
    try:
        atualizar_arquivos_no_github(
            token=chaves.get("github", ""),
            full_name=repo_full_name,
            arquivos=arquivos_editados,
            mensagem_commit=f"edit: {pedido_edicao[:72]}",
            on_status=avisar,
        )
    except PublicacaoGithubError as erro:
        avisar(f"⚠️ Não consegui publicar a edição no GitHub: {erro}")
        return ResultadoEdicao(sucesso=False, arquivos_finais=arquivos_editados, motivo_parada="github_falhou")

    # 4) Redeploy — só roda se essa conversa já tem um deploy associado no Railway
    resultado_railway = None
    if info_railway and chaves.get("railway"):
        try:
            resultado_railway = redisparar_deploy(
                token=chaves["railway"],
                project_id=info_railway["project_id"],
                service_id=info_railway["service_id"],
                environment_id=info_railway["environment_id"],
                on_status=avisar,
            )
            resultado_railway.url_app = info_railway.get("url_app") or resultado_railway.url_app
        except PublicacaoRailwayError as erro:
            avisar(f"⚠️ Publiquei a edição no GitHub, mas não consegui redisparar o deploy: {erro}")
            return ResultadoEdicao(
                sucesso=False,
                arquivos_finais=arquivos_editados,
                motivo_parada="redeploy_falhou",
                resultado_railway=None,
            )

    avisar("✅ Edição aplicada.")
    return ResultadoEdicao(
        sucesso=True, arquivos_finais=arquivos_editados, motivo_parada="sucesso", resultado_railway=resultado_railway
    )
