"""
ai_cascade.py
--------------
O coração do chatbot: a esteira de IA com tripla camada de resiliência.

Ordem estrita de tentativas (try/except aninhados, conforme especificado):
  1) TENTATIVA 1 (Principal)........ API oficial do Gemini (google-genai)
  2) TENTATIVA 2 (1º Fallback)....... OpenRouter chamando o Gemini; se o Gemini
                                       do OpenRouter também falhar, tenta um
                                       modelo de apoio (DeepSeek/Claude) dentro
                                       do próprio OpenRouter.
  3) TENTATIVA 3 (Fallback final).... API oficial e individual da DeepSeek.

Qualquer falha (erro de rede, rate limit, saldo insuficiente, timeout ou
resposta vazia) empurra a execução para a próxima camada. As três camadas
recebem exatamente o mesmo SYSTEM_PROMPT, então o formato de saída nunca muda
dependendo de quem respondeu.
"""

from __future__ import annotations

import base64
import concurrent.futures
from dataclasses import dataclass
from typing import Callable, Optional

from openai import OpenAI
from google import genai
from google.genai import types as genai_types

from prompts import SYSTEM_PROMPT

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


@dataclass
class ImagemAnexada:
    """Uma imagem de referência anexada ao pedido (ex.: no modo de edição de
    estilo). Threaded como parâmetro opcional por todas as três camadas —
    ver a nota sobre suporte a visão por camada logo abaixo."""
    dados: bytes
    mime_type: str = "image/png"


# ---------------------------------------------------------------------------
# Configuração de modelos
# ---------------------------------------------------------------------------
# Nomes de modelo mudam com frequência nesse mercado. Para não deixar essa
# volatilidade enterrada dentro da lógica de chamadas, todos os nomes ficam
# centralizados aqui, com valores padrão atuais (jul/2026) e fáceis de trocar
# pela barra lateral do Streamlit sem tocar em código.
@dataclass
class ConfiguracaoModelos:
    # Alias mantido pelo próprio Google: sempre aponta para o Flash mais atual
    # (hoje resolve para gemini-3.5-flash). Reduz a chance de o nome "envelhecer".
    gemini_oficial: str = "gemini-flash-latest"
    # Usado só como rede de segurança: em cada chamada, o teto real do modelo é
    # buscado dinamicamente via client.models.get(...).output_token_limit. Esse
    # valor só entra em ação se essa busca falhar por qualquer motivo.
    gemini_max_tokens_fallback: int = 65536  # teto documentado do gemini-3.5-flash

    # No OpenRouter, tenta primeiro o Gemini mais recente...
    openrouter_gemini: str = "google/gemini-3.5-flash"
    openrouter_gemini_max_tokens: int = 65536  # teto documentado do gemini-3.5-flash
    # ...e, se ele falhar, tenta este modelo de apoio dentro do próprio OpenRouter
    # antes de escalar para a Tentativa 3. Pode ser trocado por algo como
    # "anthropic/claude-opus-4-8" se preferir Claude como segunda opção (nesse
    # caso, ajuste também openrouter_fallback_max_tokens).
    openrouter_fallback: str = "deepseek/deepseek-v4-pro"
    openrouter_fallback_max_tokens: int = 384_000  # teto documentado do deepseek-v4-pro

    # Último recurso: API individual/oficial da DeepSeek.
    deepseek_direto: str = "deepseek-v4-pro"
    deepseek_max_tokens: int = 384_000  # teto documentado do deepseek-v4-pro/-flash

    # Segundos de espera por camada antes de considerá-la "instável" e cair
    # para a próxima. Isso é reforçado manualmente (ver _executar_com_timeout)
    # porque nem todo SDK respeita o próprio parâmetro de timeout de forma
    # confiável. Um pouco mais alto que o normal porque os tetos de tokens
    # acima são bem generosos, e uma resposta grande pode levar mais tempo.
    timeout_segundos: int = 180


@dataclass
class ResultadoGeracao:
    texto: str
    camada: str  # "gemini_oficial" | "openrouter" | "deepseek_direto"
    modelo: str  # nome exato do modelo que efetivamente respondeu


class CamadaFalhouError(Exception):
    """Guarda a causa original da falha de uma camada específica da esteira."""

    def __init__(self, camada: str, causa: Exception):
        self.camada = camada
        self.causa = causa
        super().__init__(f"[{camada}] {type(causa).__name__}: {causa}")


class TodasCamadasFalharamError(Exception):
    """Levantado quando Gemini oficial, OpenRouter e DeepSeek direto falham em sequência."""

    def __init__(self, erros: list[CamadaFalhouError]):
        self.erros = erros
        resumo = " | ".join(str(erro) for erro in erros)
        super().__init__(f"As três camadas da esteira de IA falharam -> {resumo}")


# ---------------------------------------------------------------------------
# Utilitário de timeout manual (independe do SDK respeitar ou não seu próprio
# parâmetro de timeout — alguns clientes HTTP internos ignoram isso).
# ---------------------------------------------------------------------------
def _executar_com_timeout(func: Callable, *args, timeout: int, **kwargs):
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    futuro = executor.submit(func, *args, **kwargs)
    try:
        return futuro.result(timeout=timeout)
    except concurrent.futures.TimeoutError as erro:
        raise TimeoutError(
            f"tempo limite de {timeout}s excedido (possível instabilidade ou lentidão do serviço)"
        ) from erro
    finally:
        # wait=False: não bloqueia esperando a thread perdida terminar; ela
        # morre sozinha em segundo plano quando a chamada HTTP finalmente cair.
        executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# TENTATIVA 1 — API oficial do Gemini
# ---------------------------------------------------------------------------
def _chamar_gemini_oficial(
    pedido_usuario: str, api_key: str, modelo: str, max_tokens_fallback: int, imagem: Optional[ImagemAnexada] = None
) -> str:
    if not api_key:
        raise ValueError("GEMINI_API_KEY não foi configurada na barra lateral.")

    cliente = genai.Client(api_key=api_key)

    # Busca dinâmica do teto REAL de tokens de saída do modelo configurado, em
    # vez de confiar em um número fixo que pode ficar desatualizado assim que
    # o modelo mudar. Se a busca falhar por qualquer motivo (SDK antigo,
    # instabilidade momentânea), cai para o valor fixo de segurança.
    max_tokens = max_tokens_fallback
    try:
        info_modelo = cliente.models.get(model=modelo)
        limite_real = getattr(info_modelo, "output_token_limit", None)
        if limite_real:
            max_tokens = limite_real
    except Exception:
        pass

    # O Gemini é nativamente multimodal: uma imagem entra como mais um item
    # na lista de "contents", junto do texto do pedido.
    conteudo = pedido_usuario
    if imagem is not None:
        conteudo = [pedido_usuario, genai_types.Part.from_bytes(data=imagem.dados, mime_type=imagem.mime_type)]

    resposta = cliente.models.generate_content(
        model=modelo,
        contents=conteudo,
        config=genai_types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.4,
            max_output_tokens=max_tokens,
        ),
    )
    texto = (getattr(resposta, "text", None) or "").strip()
    if not texto:
        raise RuntimeError("Gemini oficial devolveu uma resposta vazia (possível bloqueio de safety ou corte de tokens).")
    return texto


# ---------------------------------------------------------------------------
# TENTATIVA 2 — OpenRouter (Gemini primeiro, depois um modelo de apoio)
# ---------------------------------------------------------------------------
def _montar_conteudo_usuario_openai(pedido_usuario: str, imagem: Optional[ImagemAnexada]):
    """Monta o campo 'content' de uma mensagem no formato OpenAI-compatível
    (usado por OpenRouter e DeepSeek). Com imagem, vira uma lista de blocos
    text + image_url em base64; sem imagem, continua sendo uma string simples
    como sempre foi — mantém 100% de compatibilidade com as chamadas antigas.

    NOTA: nem todo modelo por trás do OpenRouter/DeepSeek entende blocos de
    imagem (o Gemini via OpenRouter entende; os modelos de texto puro da
    DeepSeek, historicamente, não) — se um modelo não suportar, a chamada
    tende a falhar, e a esteira de fallback já trata isso como qualquer
    outra falha de camada, escalando pra próxima.
    """
    if imagem is None:
        return pedido_usuario
    b64 = base64.b64encode(imagem.dados).decode("ascii")
    return [
        {"type": "text", "text": pedido_usuario},
        {"type": "image_url", "image_url": {"url": f"data:{imagem.mime_type};base64,{b64}"}},
    ]


def _chamar_openrouter(
    pedido_usuario: str,
    api_key: str,
    modelos_em_ordem: list[tuple[str, int]],
    imagem: Optional[ImagemAnexada] = None,
) -> tuple[str, str]:
    """
    modelos_em_ordem: lista de (nome_do_modelo, teto_de_tokens_desse_modelo).
    Um max_tokens único para todos não funciona aqui — o Gemini e o modelo de
    apoio (ex.: DeepSeek) têm tetos de saída bem diferentes entre si, e enviar
    um valor acima do teto real de um modelo pode ser rejeitado pela API.
    """
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY não foi configurada na barra lateral.")

    cliente = OpenAI(api_key=api_key, base_url=OPENROUTER_BASE_URL)
    conteudo_usuario = _montar_conteudo_usuario_openai(pedido_usuario, imagem)
    falhas: list[str] = []

    for modelo, max_tokens in modelos_em_ordem:
        try:
            resposta = cliente.chat.completions.create(
                model=modelo,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": conteudo_usuario},
                ],
                temperature=0.4,
                max_tokens=max_tokens,
                extra_headers={
                    "HTTP-Referer": "https://github.com",
                    "X-Title": "Vibe Coding Chatbot",
                },
            )
            texto = (resposta.choices[0].message.content or "").strip()
            if texto:
                return texto, modelo
            falhas.append(f"{modelo}: resposta vazia")
        except Exception as erro:  # qualquer provedor dentro do OpenRouter pode falhar de formas diferentes
            falhas.append(f"{modelo}: {type(erro).__name__}: {erro}")
            continue  # tenta o próximo modelo de apoio antes de desistir do OpenRouter

    raise RuntimeError("Todos os modelos tentados no OpenRouter falharam -> " + " | ".join(falhas))


# ---------------------------------------------------------------------------
# TENTATIVA 3 — API oficial e individual da DeepSeek (último recurso)
# ---------------------------------------------------------------------------
def _chamar_deepseek_direto(
    pedido_usuario: str, api_key: str, modelo: str, max_tokens: int, imagem: Optional[ImagemAnexada] = None
) -> str:
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY não foi configurada na barra lateral.")

    cliente = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
    conteudo_usuario = _montar_conteudo_usuario_openai(pedido_usuario, imagem)
    resposta = cliente.chat.completions.create(
        model=modelo,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": conteudo_usuario},
        ],
        temperature=0.4,
        max_tokens=max_tokens,
        stream=False,
    )
    texto = (resposta.choices[0].message.content or "").strip()
    if not texto:
        raise RuntimeError("DeepSeek direto devolveu uma resposta vazia.")
    return texto


# ---------------------------------------------------------------------------
# Orquestrador — a esteira de fallback em cascata (try/except aninhados)
# ---------------------------------------------------------------------------
def gerar_codigo_com_fallback(
    pedido_usuario: str,
    chaves: dict[str, str],
    modelos: Optional[ConfiguracaoModelos] = None,
    on_status: Optional[Callable[[str], None]] = None,
    imagem: Optional[ImagemAnexada] = None,
) -> ResultadoGeracao:
    """
    Executa a cascata Gemini oficial -> OpenRouter (Gemini + apoio) -> DeepSeek direto.

    Args:
        pedido_usuario: descrição do app (ou instrução de edição) digitada no chat.
        chaves: dicionário com as chaves "gemini", "openrouter" e "deepseek".
        modelos: nomes de modelo e limites de tokens/timeout de cada camada.
        on_status: callback opcional chamado a cada passo (ex.: para atualizar
            um st.status() na interface) — recebe uma string legível.
        imagem: imagem de referência opcional (ex.: modo de edição de estilo).
            O Gemini (oficial e via OpenRouter) é multimodal nativo; a DeepSeek
            pode não suportar — ver nota em _montar_conteudo_usuario_openai.

    Returns:
        ResultadoGeracao com o texto Markdown bruto e qual camada respondeu.

    Raises:
        TodasCamadasFalharamError: se as três camadas falharem.
    """
    modelos = modelos or ConfiguracaoModelos()
    avisar = on_status or (lambda _mensagem: None)

    try:
        # ---- TENTATIVA 1 (Principal): Gemini oficial ----
        avisar(f"🥇 Tentativa 1/3 — API oficial do Gemini (`{modelos.gemini_oficial}`)...")
        texto = _executar_com_timeout(
            _chamar_gemini_oficial,
            pedido_usuario,
            chaves.get("gemini", ""),
            modelos.gemini_oficial,
            modelos.gemini_max_tokens_fallback,
            imagem,
            timeout=modelos.timeout_segundos,
        )
        avisar("✅ Gemini oficial respondeu com sucesso.")
        return ResultadoGeracao(texto=texto, camada="gemini_oficial", modelo=modelos.gemini_oficial)

    except Exception as erro_gemini:
        avisar(f"⚠️ Gemini oficial falhou ({erro_gemini}). Acionando o 1º fallback: OpenRouter...")

        try:
            # ---- TENTATIVA 2 (1º Fallback): OpenRouter chamando o Gemini, com apoio ----
            modelos_openrouter = [
                (modelos.openrouter_gemini, modelos.openrouter_gemini_max_tokens),
                (modelos.openrouter_fallback, modelos.openrouter_fallback_max_tokens),
            ]
            nomes_em_ordem = [nome for nome, _ in modelos_openrouter]
            avisar(f"🥈 Tentativa 2/3 — OpenRouter, na ordem {nomes_em_ordem}...")
            texto, modelo_usado = _executar_com_timeout(
                _chamar_openrouter,
                pedido_usuario,
                chaves.get("openrouter", ""),
                modelos_openrouter,
                imagem,
                timeout=modelos.timeout_segundos,
            )
            avisar(f"✅ OpenRouter respondeu com sucesso via `{modelo_usado}`.")
            return ResultadoGeracao(texto=texto, camada="openrouter", modelo=modelo_usado)

        except Exception as erro_openrouter:
            avisar(f"⚠️ OpenRouter falhou ({erro_openrouter}). Acionando o fallback de emergência: DeepSeek direto...")

            try:
                # ---- TENTATIVA 3 (Fallback de emergência): DeepSeek oficial/direto ----
                avisar(f"🥉 Tentativa 3/3 — API oficial da DeepSeek (`{modelos.deepseek_direto}`)...")
                texto = _executar_com_timeout(
                    _chamar_deepseek_direto,
                    pedido_usuario,
                    chaves.get("deepseek", ""),
                    modelos.deepseek_direto,
                    modelos.deepseek_max_tokens,
                    imagem,
                    timeout=modelos.timeout_segundos,
                )
                avisar("✅ DeepSeek direto respondeu com sucesso.")
                return ResultadoGeracao(texto=texto, camada="deepseek_direto", modelo=modelos.deepseek_direto)

            except Exception as erro_deepseek:
                avisar("❌ As três camadas da esteira falharam. Veja os detalhes no resumo abaixo.")
                raise TodasCamadasFalharamError(
                    [
                        CamadaFalhouError("gemini_oficial", erro_gemini),
                        CamadaFalhouError("openrouter", erro_openrouter),
                        CamadaFalhouError("deepseek_direto", erro_deepseek),
                    ]
                ) from erro_deepseek
