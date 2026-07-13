"""
app.py
-------
Interface Streamlit do Vibe Coding Chatbot.

Fluxo de CRIAÇÃO (primeira mensagem de uma conversa): o usuário descreve o
app -> a esteira de IA com fallback triplo gera o código -> o parser extrai
os arquivos -> se o nome do repositório ficou em branco, a própria IA sugere
um -> o publicador cria um repositório e sobe tudo no GitHub -> se um
RAILWAY_TOKEN estiver configurado, o deploy também é automático e o link
real da aplicação já volta no chat; se o deploy falhar, entra a
auto-correção (busca o log, pede pra IA corrigir, publica de novo).

Fluxo de EDIÇÃO (mensagens seguintes, na MESMA conversa onde o app já foi
criado): em vez de gerar um app novo, o pedido é tratado como um ajuste no
app existente — com uma imagem de referência opcional pra mudanças de
estilo — publicado no mesmo repositório, com redeploy automático se houver.

Conversas ficam salvas (SQLite local) e listadas na barra lateral, dá pra
trocar entre elas ou apagar — cada conversa "lembra" a qual repositório/app
ela está ligada, por isso a edição funciona na mesma aba onde o app nasceu.
"""

from __future__ import annotations

import io
import zipfile

import streamlit as st

import chat_storage
from ai_cascade import ConfiguracaoModelos, ImagemAnexada, TodasCamadasFalharamError, gerar_codigo_com_fallback
from auto_correcao import tentar_auto_correcao
from edicao import aplicar_edicao
from file_parser import NenhumArquivoEncontradoError, extrair_arquivos, sugerir_nome_repositorio
from github_deployer import PublicacaoGithubError, garantir_railway_toml, publicar_no_github
from railway_deployer import STATUS_FALHA, PublicacaoRailwayError, publicar_no_railway

RAILWAY_URL = "https://railway.app"
PADROES = ConfiguracaoModelos()

st.set_page_config(page_title="Vibe Coding Chatbot", page_icon="🛠️", layout="centered")


# ---------------------------------------------------------------------------
# Estado da sessão
# ---------------------------------------------------------------------------
if "messages" not in st.session_state:
    st.session_state.messages = []  # cada item: {"role", "content", "files"?, "repo_url"?, ...}
if "chat_id" not in st.session_state:
    st.session_state.chat_id = None  # None até a 1ª mensagem ser salva; daí vira o ID persistente


# ---------------------------------------------------------------------------
# Leitura de st.secrets (opcional — nunca quebra o app se não existir)
# ---------------------------------------------------------------------------
def _obter_secret(chave: str) -> str:
    """Lê uma chave de st.secrets, se existir. Se não houver secrets.toml
    configurado (ou a chave não existir nele), devolve string vazia em vez de
    deixar o FileNotFoundError/KeyError do Streamlit derrubar o app."""
    try:
        return str(st.secrets[chave])
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Barra lateral — conversas salvas, chaves de API, repositório e config avançada
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("💬 Conversas")
    if st.button("➕ Nova conversa", use_container_width=True, type="primary"):
        st.session_state.messages = []
        st.session_state.chat_id = None
        st.rerun()

    conversas_salvas = chat_storage.listar_chats()
    if not conversas_salvas:
        st.caption("Suas conversas aparecem aqui assim que você mandar a primeira mensagem.")
    for conversa in conversas_salvas:
        col_abrir, col_excluir = st.columns([5, 1])
        with col_abrir:
            eh_a_atual = conversa.id == st.session_state.chat_id
            if st.button(
                conversa.titulo,
                key=f"abrir_{conversa.id}",
                use_container_width=True,
                type="primary" if eh_a_atual else "secondary",
                disabled=eh_a_atual,
            ):
                st.session_state.chat_id = conversa.id
                st.session_state.messages = chat_storage.carregar_mensagens(conversa.id)
                st.rerun()
        with col_excluir:
            if st.button("🗑️", key=f"excluir_{conversa.id}", help="Excluir esta conversa"):
                chat_storage.excluir_chat(conversa.id)
                if st.session_state.chat_id == conversa.id:
                    st.session_state.messages = []
                    st.session_state.chat_id = None
                st.rerun()

    st.divider()
    st.header("🔑 Chaves de API")

    secrets_das_chaves = {
        "gemini": _obter_secret("GEMINI_API_KEY"),
        "openrouter": _obter_secret("OPENROUTER_API_KEY"),
        "deepseek": _obter_secret("DEEPSEEK_API_KEY"),
        "github": _obter_secret("GITHUB_TOKEN"),
        "railway": _obter_secret("RAILWAY_TOKEN"),
    }
    total_chaves = len(secrets_das_chaves)
    total_carregado = sum(1 for v in secrets_das_chaves.values() if v)
    if total_carregado == total_chaves:
        st.success(f"As {total_chaves} chaves foram carregadas de `st.secrets`.", icon="🔒")
    elif total_carregado > 0:
        st.info(f"🔒 {total_carregado}/{total_chaves} chaves carregadas de `st.secrets`. Preencha as que faltam abaixo.")
    else:
        st.caption("Nenhuma chave encontrada em `st.secrets` — preencha manualmente abaixo.")

    # Pré-preenchidos com o valor de st.secrets quando existir, mas ainda
    # editáveis: dá pra sobrescrever na hora sem precisar mexer no secrets.toml.
    gemini_key = st.text_input(
        "GEMINI_API_KEY", value=secrets_das_chaves["gemini"], type="password", help="Tentativa 1 — API oficial do Gemini."
    )
    openrouter_key = st.text_input(
        "OPENROUTER_API_KEY",
        value=secrets_das_chaves["openrouter"],
        type="password",
        help="Tentativa 2 — fallback via OpenRouter (tenta Gemini, depois um modelo de apoio).",
    )
    deepseek_key = st.text_input(
        "DEEPSEEK_API_KEY",
        value=secrets_das_chaves["deepseek"],
        type="password",
        help="Tentativa 3 — API individual/oficial da DeepSeek (último recurso).",
    )
    github_token = st.text_input(
        "GITHUB_TOKEN", value=secrets_das_chaves["github"], type="password", help="Personal Access Token com escopo 'repo'."
    )
    railway_token = st.text_input(
        "RAILWAY_TOKEN",
        value=secrets_das_chaves["railway"],
        type="password",
        help=(
            "Opcional. Token de Account/Workspace (não de Project) do Railway — "
            "https://railway.com/account/tokens. Se preenchido, o deploy é feito "
            "automaticamente e o link real da aplicação aparece no chat. Se deixar em "
            "branco, você recebe as instruções pra fazer isso manualmente."
        ),
    )

    st.divider()
    st.header("📦 Repositório")
    nome_repo = st.text_input(
        "Nome do repositório",
        placeholder="deixe em branco pra eu escolher",
        help="Se ficar em branco, a própria IA sugere um nome curto baseado no app pedido.",
    )

    with st.expander("⚙️ Configuração avançada de modelos"):
        st.caption(
            "Nomes de modelo mudam com frequência — ajuste aqui sem tocar no código. "
            "Os tokens de saída ficam sempre no teto máximo de cada modelo automaticamente "
            "(inclusive buscando esse teto ao vivo na API do Gemini), então não há um limite "
            "configurável aqui de propósito."
        )
        modelo_gemini = st.text_input("Gemini oficial", value=PADROES.gemini_oficial)
        modelo_or_gemini = st.text_input("OpenRouter — Gemini", value=PADROES.openrouter_gemini)
        modelo_or_apoio = st.text_input("OpenRouter — apoio (2ª opção)", value=PADROES.openrouter_fallback)
        modelo_deepseek = st.text_input("DeepSeek direto", value=PADROES.deepseek_direto)
        timeout_s = st.number_input(
            "Timeout por camada de IA (segundos)", min_value=15, max_value=600, value=PADROES.timeout_segundos, step=15
        )
        timeout_railway_s = st.number_input(
            "Tempo máx. esperando o build no Railway (segundos)",
            min_value=0,
            max_value=600,
            value=150,
            step=15,
            help="0 = não espera nada; devolve a URL assim que ela é gerada, sem checar o status do build.",
        )
        max_tentativas_correcao = st.number_input(
            "Tentativas de auto-correção se o deploy falhar",
            min_value=0,
            max_value=5,
            value=2,
            step=1,
            help=(
                "0 desliga a auto-correção. Cada tentativa busca o log do erro, pede pra IA corrigir, "
                "publica de novo no GitHub e redisparar o deploy. A busca de log pode falhar por uma "
                "limitação conhecida da API do Railway — nesse caso a auto-correção para e o chat "
                "pede o log manualmente."
            ),
        )


# ---------------------------------------------------------------------------
# Utilitários de interface
# ---------------------------------------------------------------------------
def _gerar_zip(arquivos: dict[str, str]) -> bytes:
    """Empacota os arquivos gerados em um .zip em memória, para download de segurança."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zip_arquivo:
        for caminho, conteudo in arquivos.items():
            zip_arquivo.writestr(caminho, conteudo)
    return buffer.getvalue()


def _renderizar_extras(mensagem: dict, indice: int) -> None:
    """Redesenha os botões (zip / Railway) de uma mensagem, na tela inicial ou no histórico."""
    if mensagem.get("files"):
        nome_para_zip = mensagem.get("repo_nome") or (nome_repo or "projeto").strip() or "projeto"
        st.download_button(
            "⬇️ Baixar projeto (.zip)",
            data=_gerar_zip(mensagem["files"]),
            file_name=f"{nome_para_zip}.zip",
            mime="application/zip",
            key=f"download_{indice}",
        )
    if mensagem.get("railway_app_url"):
        st.link_button("🌐 Abrir a aplicação no ar", mensagem["railway_app_url"], key=f"app_{indice}")
        if mensagem.get("railway_project_url"):
            st.link_button("🚂 Abrir projeto no Railway", mensagem["railway_project_url"], key=f"railwayproj_{indice}")
    elif mensagem.get("repo_url"):
        st.link_button("🚀 Abrir Railway e fazer o deploy", RAILWAY_URL, key=f"railway_{indice}")


def _extras_railway(resultado_railway) -> dict:
    """Campos padronizados guardados na mensagem a partir de um ResultadoDeployRailway
    — inclui os IDs (não só a URL de exibição) para que uma EDIÇÃO futura, nesta
    mesma conversa, saiba como redisparar o deploy sem precisar recriar nada."""
    return {
        "railway_app_url": resultado_railway.url_app,
        "railway_project_url": resultado_railway.url_projeto,
        "railway_project_id": resultado_railway.project_id,
        "railway_service_id": resultado_railway.service_id,
        "railway_environment_id": resultado_railway.environment_id,
    }


def _info_app_existente(mensagens: list[dict]) -> dict | None:
    """Procura, do fim pro começo, a mensagem mais recente que já tem um app
    publicado nesta conversa. Se achar, a próxima mensagem do usuário vira
    uma EDIÇÃO desse app em vez de criar um app novo — é assim que o modo de
    edição "mora" na mesma aba de chat onde o app nasceu."""
    for msg in reversed(mensagens):
        if msg.get("role") == "assistant" and msg.get("repo_full_name") and msg.get("files"):
            info = {
                "arquivos": msg["files"],
                "repo_full_name": msg["repo_full_name"],
                "repo_url": msg.get("repo_url"),
                "repo_nome": msg.get("repo_nome"),
            }
            if msg.get("railway_project_id"):
                info["railway"] = {
                    "project_id": msg["railway_project_id"],
                    "service_id": msg["railway_service_id"],
                    "environment_id": msg["railway_environment_id"],
                    "url_app": msg.get("railway_app_url"),
                    "url_projeto": msg.get("railway_project_url"),
                }
            return info
    return None


# ---------------------------------------------------------------------------
# Pipeline de CRIAÇÃO (primeira mensagem de uma conversa)
# ---------------------------------------------------------------------------
def _executar_pipeline(
    pedido: str,
    chaves: dict[str, str],
    modelos: ConfiguracaoModelos,
    timeout_railway_segundos: int,
    max_tentativas_correcao: int,
    imagem: ImagemAnexada | None = None,
) -> dict:
    """
    Roda a esteira completa: geração com fallback -> parser -> (nome do repo,
    escolhido pela IA se a sidebar ficou em branco) -> publicação no GitHub ->
    (se houver RAILWAY_TOKEN) deploy automático -> (se falhar) auto-correção.
    """
    with st.status("🚀 Gerando sua aplicação...", expanded=True) as status:

        def log(texto: str) -> None:
            status.write(texto)

        try:
            resultado = gerar_codigo_com_fallback(pedido, chaves, modelos, on_status=log, imagem=imagem)
        except TodasCamadasFalharamError as erro:
            status.update(label="❌ Falha na geração de código", state="error")
            detalhes = "\n".join(f"- **{e.camada}**: {e.causa}" for e in erro.erros)
            return {
                "role": "assistant",
                "content": (
                    "Não consegui gerar o código — as três camadas da esteira de IA falharam:\n\n"
                    f"{detalhes}\n\n"
                    "Confira as chaves de API na barra lateral (ou os limites de uso/saldo de cada "
                    "provedor) e peça novamente."
                ),
            }

        log(f"🧠 Código gerado pela camada **{resultado.camada}** (modelo `{resultado.modelo}`).")

        try:
            arquivos = extrair_arquivos(resultado.texto)
        except NenhumArquivoEncontradoError as erro:
            status.update(label="❌ Falha ao interpretar a resposta da IA", state="error")
            return {
                "role": "assistant",
                "content": (
                    f"A IA (`{resultado.camada}`) respondeu, mas o parser não conseguiu extrair arquivos "
                    f"dela: {erro}\n\nTente reformular o pedido ou peça novamente."
                ),
            }

        log(f"📄 {len(arquivos)} arquivo(s) extraído(s): {', '.join(sorted(arquivos))}")

        if "railway.toml" not in arquivos:
            arquivos = garantir_railway_toml(arquivos)
            log("➕ railway.toml não veio da IA — adicionando a versão padrão (Nixpacks).")

        # Nome do repositório: usa o que o usuário digitou; se ficou em
        # branco, deixa a própria IA escolher (### PROJECT_NAME:), com um
        # slug do pedido como rede de segurança se nem isso vier.
        nome_repo_efetivo = nome_repo.strip() if nome_repo and nome_repo.strip() else ""
        if not nome_repo_efetivo:
            nome_repo_efetivo = sugerir_nome_repositorio(resultado.texto, pedido)
            log(f"📛 Nome do repositório não informado — usando o sugerido: `{nome_repo_efetivo}`")

        try:
            resultado_github = publicar_no_github(
                token=chaves.get("github", ""),
                nome_repositorio=nome_repo_efetivo,
                arquivos=arquivos,
                descricao=f"Gerado via Vibe Coding Chatbot a partir de: {pedido[:200]}",
                on_status=log,
            )
        except PublicacaoGithubError as erro:
            status.update(label="⚠️ Código gerado, mas não publicado no GitHub", state="error")
            return {
                "role": "assistant",
                "content": (
                    f"O código foi gerado com sucesso ({len(arquivos)} arquivos, via **{resultado.camada}**), "
                    f"mas não consegui publicar no GitHub: {erro}\n\n"
                    "Baixe o projeto abaixo e suba manualmente, ou corrija o GITHUB_TOKEN / nome do "
                    "repositório na barra lateral e peça novamente."
                ),
                "files": arquivos,
            }

        resultado_railway = None
        erro_railway = None
        resultado_correcao = None
        if chaves.get("railway"):
            try:
                resultado_railway = publicar_no_railway(
                    token=chaves["railway"],
                    nome_projeto=resultado_github.nome_repositorio,
                    repositorio_github=resultado_github.full_name,
                    on_status=log,
                    tempo_maximo_espera_segundos=timeout_railway_segundos,
                )
            except PublicacaoRailwayError as erro:
                erro_railway = str(erro)
                log(f"⚠️ Deploy automático no Railway falhou: {erro}")

            if (
                resultado_railway is not None
                and resultado_railway.status_final in STATUS_FALHA
                and max_tentativas_correcao > 0
            ):
                resultado_correcao = tentar_auto_correcao(
                    pedido_original=pedido,
                    chaves=chaves,
                    modelos=modelos,
                    arquivos_atuais=arquivos,
                    repo_full_name=resultado_github.full_name,
                    resultado_railway_inicial=resultado_railway,
                    url_app=resultado_railway.url_app,
                    max_tentativas=max_tentativas_correcao,
                    on_status=log,
                )
                arquivos = resultado_correcao.arquivos_finais
                resultado_railway = resultado_correcao.resultado_railway_final

        if resultado_correcao is not None and resultado_correcao.corrigido:
            status.update(label="✅ Aplicação gerada, corrigida automaticamente e no ar!", state="complete")
        elif resultado_railway is not None:
            status.update(label="✅ Aplicação gerada, publicada e no ar!", state="complete")
        else:
            status.update(label="✅ Aplicação gerada e publicada!", state="complete")

    return _montar_mensagem_final(pedido, resultado, arquivos, resultado_github, resultado_railway, erro_railway, resultado_correcao)


def _montar_mensagem_final(pedido, resultado, arquivos, resultado_github, resultado_railway, erro_railway, resultado_correcao=None) -> dict:
    """Monta o dicionário de mensagem final, com os desfechos possíveis para Railway + auto-correção."""
    base = (
        "### ✅ Pronto! Sua aplicação foi gerada e publicada no GitHub\n\n"
        f"- **Camada da IA usada:** `{resultado.camada}` (modelo `{resultado.modelo}`)\n"
        f"- **Arquivos gerados:** {len(arquivos)}\n"
        f"- **Repositório:** {resultado_github.html_url}\n\n"
    )
    extras = {
        "files": arquivos,
        "repo_url": resultado_github.html_url,
        "repo_nome": resultado_github.nome_repositorio,
        "repo_full_name": resultado_github.full_name,
    }

    bloco_correcao = ""
    if resultado_correcao is not None and resultado_correcao.tentativas:
        linhas_tentativas = "\n".join(
            f"  {t.numero}. redeploy terminou com status `{t.status_resultante}`" for t in resultado_correcao.tentativas
        )
        if resultado_correcao.corrigido:
            bloco_correcao = (
                f"\n### 🔧 Auto-correção\nO primeiro deploy falhou, mas corrigi automaticamente em "
                f"{len(resultado_correcao.tentativas)} tentativa(s):\n{linhas_tentativas}\n"
            )
        else:
            motivo_para_texto = {
                "busca_log_falhou": "não consegui buscar o log do erro automaticamente (limitação conhecida da API do Railway) — me manda o log exportado do painel, como você já fez antes, que eu corrijo.",
                "ia_falhou_na_correcao": "a esteira de IA falhou tentando gerar a correção.",
                "parser_falhou_na_correcao": "a IA respondeu fora do formato esperado ao tentar corrigir.",
                "github_falhou_na_correcao": "consegui gerar uma correção, mas não consegui publicá-la no GitHub.",
                "redeploy_falhou": "consegui publicar uma correção, mas não consegui redisparar o deploy.",
                "esgotou_tentativas": f"tentei corrigir automaticamente {len(resultado_correcao.tentativas)} vez(es), mas o deploy continua falhando.",
            }.get(resultado_correcao.motivo_parada, "a auto-correção parou por um motivo inesperado.")
            bloco_correcao = (
                f"\n### 🔧 Auto-correção não conseguiu resolver\n"
                f"Tentativas feitas:\n{linhas_tentativas if linhas_tentativas else '  (nenhuma completou)'}\n\n"
                f"O que houve: {motivo_para_texto}\n"
            )

    if resultado_railway is not None:
        status_para_texto = {
            "SUCCESS": "🟢 no ar (build concluído com sucesso)",
            "FAILED": "🔴 o build falhou — confira os logs no painel do Railway",
            "CRASHED": "🔴 a aplicação buildou mas travou ao iniciar — confira os logs",
        }.get(resultado_railway.status_final, f"🟡 ainda em andamento ({resultado_railway.status_final}) — a URL deve responder em instantes")
        conteudo = (
            base + "### 🚂 Deploy automático no Railway\n"
            f"- **Status:** {status_para_texto}\n"
            f"- **URL da aplicação:** {resultado_railway.url_app}\n"
            f"- **Painel do projeto:** {resultado_railway.url_projeto}\n"
            + bloco_correcao
        )
        extras.update(_extras_railway(resultado_railway))
    elif erro_railway is not None:
        conteudo = (
            base + "### ⚠️ Deploy automático no Railway não completou\n"
            f"{erro_railway}\n\n"
            "O repositório no GitHub está pronto — você pode terminar o deploy manualmente: "
            "**New Project → Deploy from GitHub repo** no [railway.app](https://railway.app)."
        )
    else:
        conteudo = (
            base + "### 🚂 Deploy no Railway\n"
            "O repositório já tem um `railway.toml` configurado com Nixpacks. No Railway, clique em "
            "**New Project → Deploy from GitHub repo** e selecione o repositório que acabei de criar.\n\n"
            "💡 _Dica: preencha um `RAILWAY_TOKEN` na barra lateral para que eu faça esse passo "
            "automaticamente da próxima vez e já te devolva o link da aplicação no ar._"
        )

    return {"role": "assistant", "content": conteudo, **extras}


# ---------------------------------------------------------------------------
# Pipeline de EDIÇÃO (mensagens seguintes, na mesma conversa de um app já criado)
# ---------------------------------------------------------------------------
def _executar_pipeline_edicao(
    pedido_edicao: str,
    imagem: ImagemAnexada | None,
    chaves: dict[str, str],
    modelos: ConfiguracaoModelos,
    info_existente: dict,
) -> dict:
    with st.status("✏️ Aplicando a edição...", expanded=True) as status:

        def log(texto: str) -> None:
            status.write(texto)

        resultado = aplicar_edicao(
            pedido_edicao=pedido_edicao,
            chaves=chaves,
            modelos=modelos,
            arquivos_atuais=info_existente["arquivos"],
            repo_full_name=info_existente["repo_full_name"],
            imagem=imagem,
            info_railway=info_existente.get("railway"),
            on_status=log,
        )
        status.update(
            label="✅ Edição aplicada!" if resultado.sucesso else "⚠️ Não consegui aplicar a edição",
            state="complete" if resultado.sucesso else "error",
        )

    return _montar_mensagem_edicao(pedido_edicao, resultado, info_existente)


def _montar_mensagem_edicao(pedido_edicao: str, resultado, info_existente: dict) -> dict:
    extras = {
        "files": resultado.arquivos_finais,
        "repo_url": info_existente.get("repo_url"),
        "repo_nome": info_existente.get("repo_nome"),
        "repo_full_name": info_existente["repo_full_name"],
    }
    # Preserva os IDs do Railway pra próxima edição continuar funcionando,
    # mesmo quando ESTA edição em particular não chegou a redisparar o deploy.
    if info_existente.get("railway"):
        r = info_existente["railway"]
        extras.update(
            {
                "railway_app_url": r.get("url_app"),
                "railway_project_url": r.get("url_projeto"),
                "railway_project_id": r["project_id"],
                "railway_service_id": r["service_id"],
                "railway_environment_id": r["environment_id"],
            }
        )

    if not resultado.sucesso:
        motivo_para_texto = {
            "ia_falhou": "a esteira de IA falhou tentando aplicar essa edição.",
            "parser_falhou": "a IA respondeu fora do formato esperado.",
            "github_falhou": "não consegui publicar a edição no GitHub.",
            "redeploy_falhou": "publiquei a edição no GitHub, mas não consegui redisparar o deploy no Railway — o app antigo ainda está no ar, a edição deve refletir assim que um novo deploy rodar.",
        }.get(resultado.motivo_parada, "algo deu errado ao aplicar a edição.")
        return {
            "role": "assistant",
            "content": f"### ⚠️ Não consegui aplicar a edição\n{motivo_para_texto}\n\nO app publicado anteriormente continua como estava — pode tentar reformular o pedido.",
            **extras,
        }

    conteudo = f"### ✏️ Edição aplicada\n\n- **Pedido:** {pedido_edicao}\n- **Repositório:** {info_existente.get('repo_url', '')}\n"
    if resultado.resultado_railway is not None:
        rr = resultado.resultado_railway
        status_txt = {
            "SUCCESS": "🟢 no ar (redeploy concluído)",
            "FAILED": "🔴 o novo build falhou",
            "CRASHED": "🔴 travou ao iniciar",
        }.get(rr.status_final, f"🟡 ainda em andamento ({rr.status_final})")
        conteudo += f"- **Status do redeploy:** {status_txt}\n- **URL:** {rr.url_app}\n"
        extras.update(_extras_railway(rr))
    elif info_existente.get("railway"):
        conteudo += f"- **URL da aplicação:** {info_existente['railway'].get('url_app', '')} _(deploy anterior — configure o RAILWAY_TOKEN pra eu redisparar automaticamente)_\n"

    return {"role": "assistant", "content": conteudo, **extras}


# ---------------------------------------------------------------------------
# Persistência da conversa atual
# ---------------------------------------------------------------------------
def _salvar_conversa_atual() -> None:
    if not st.session_state.messages:
        return
    if not st.session_state.chat_id:
        st.session_state.chat_id = chat_storage.novo_chat_id()
    primeira_mensagem_usuario = next(
        (m["content"] for m in st.session_state.messages if m["role"] == "user"), "Nova conversa"
    )
    titulo = chat_storage.gerar_titulo(primeira_mensagem_usuario)
    chat_storage.salvar_chat(st.session_state.chat_id, titulo, st.session_state.messages)


# ---------------------------------------------------------------------------
# Cabeçalho
# ---------------------------------------------------------------------------
st.title("🛠️ Vibe Coding Chatbot")
st.caption(
    "Descreva o app que você quer construir. Eu gero o código full-stack, publico no GitHub e, "
    "com um RAILWAY_TOKEN configurado, já faço o deploy. Depois de criado, continue nesta mesma "
    "conversa para pedir ajustes — anexe uma imagem para pedir mudanças de estilo."
)

# ---------------------------------------------------------------------------
# Histórico do chat
# ---------------------------------------------------------------------------
for indice, mensagem in enumerate(st.session_state.messages):
    with st.chat_message(mensagem["role"]):
        st.markdown(mensagem["content"])
        _renderizar_extras(mensagem, indice)

# ---------------------------------------------------------------------------
# Nova mensagem do usuário (texto + imagem opcional)
# ---------------------------------------------------------------------------
info_app_existente = _info_app_existente(st.session_state.messages)

if info_app_existente:
    st.caption(f"✏️ Editando **{info_app_existente.get('repo_nome', 'o app desta conversa')}** — anexe uma imagem para pedir mudança de estilo.")
    placeholder_input = "Peça um ajuste (ex.: 'deixa o botão verde') ou anexe uma imagem de referência de estilo"
else:
    placeholder_input = "Descreva o app que você quer gerar (ex.: 'um app de lista de tarefas com prioridades')"

entrada = st.chat_input(placeholder_input, accept_file=True, file_type=["png", "jpg", "jpeg", "webp"])

pedido = ""
imagem_anexada = None
if entrada:
    pedido = (entrada.text or "").strip()
    arquivos_anexados = entrada.files or []
    if arquivos_anexados:
        arquivo = arquivos_anexados[0]
        imagem_anexada = ImagemAnexada(dados=arquivo.getvalue(), mime_type=arquivo.type or "image/png")
        if not pedido:
            pedido = "Ajuste o estilo visual da aplicação para combinar com a imagem anexada."

if entrada and (pedido or imagem_anexada):
    st.session_state.messages.append({"role": "user", "content": pedido})
    with st.chat_message("user"):
        st.markdown(pedido)
        if imagem_anexada:
            st.image(imagem_anexada.dados, caption="Imagem de referência anexada", width=240)

    chaves = {
        "gemini": gemini_key,
        "openrouter": openrouter_key,
        "deepseek": deepseek_key,
        "github": github_token,
        "railway": railway_token,
    }
    modelos = ConfiguracaoModelos(
        gemini_oficial=modelo_gemini or PADROES.gemini_oficial,
        openrouter_gemini=modelo_or_gemini or PADROES.openrouter_gemini,
        openrouter_fallback=modelo_or_apoio or PADROES.openrouter_fallback,
        deepseek_direto=modelo_deepseek or PADROES.deepseek_direto,
        timeout_segundos=int(timeout_s),
    )

    with st.chat_message("assistant"):
        if info_app_existente:
            nova_mensagem = _executar_pipeline_edicao(pedido, imagem_anexada, chaves, modelos, info_app_existente)
        else:
            nova_mensagem = _executar_pipeline(
                pedido, chaves, modelos, int(timeout_railway_s), int(max_tentativas_correcao), imagem=imagem_anexada
            )
        st.markdown(nova_mensagem["content"])
        _renderizar_extras(nova_mensagem, len(st.session_state.messages))

    st.session_state.messages.append(nova_mensagem)
    _salvar_conversa_atual()
    st.rerun()
