# 🛠️ Vibe Coding Chatbot

Chatbot em Streamlit que transforma uma descrição em linguagem natural em uma
aplicação full-stack (frontend + backend Python + SQLite), publicada
automaticamente em um novo repositório no GitHub e pronta para deploy no
Railway via Nixpacks.

## Como rodar localmente

```bash
pip install -r requirements.txt
streamlit run app.py
```

Abra a URL local que o Streamlit mostrar no terminal, preencha as chaves na
barra lateral e descreva o app que você quer no campo de chat.

## Chaves necessárias (barra lateral ou `st.secrets`)

| Chave | Onde conseguir |
|---|---|
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) |
| `DEEPSEEK_API_KEY` | [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) |
| `GITHUB_TOKEN` | GitHub → Settings → Developer settings → Personal access tokens (escopo `repo`) |
| `RAILWAY_TOKEN` *(opcional)* | [railway.com/account/tokens](https://railway.com/account/tokens) — precisa ser um token de **Account** ou **Workspace**, não de Project (um token de Project não tem permissão para criar projetos novos) |

O app lê essas chaves automaticamente de `st.secrets` (local: `.streamlit/secrets.toml`;
Streamlit Community Cloud: console de Secrets do app) usando exatamente esses nomes. Se
alguma não estiver em `st.secrets`, o campo correspondente na sidebar fica em branco e
editável normalmente — o texto digitado ali sempre pode sobrescrever o valor do secrets.
Nenhuma chave é obrigatória para o app abrir, mas cada uma que faltar reduz o que é feito
automaticamente (ex.: sem `GEMINI_API_KEY`, a Tentativa 1 é pulada; sem `RAILWAY_TOKEN`, o
deploy vira manual).

Exemplo de `.streamlit/secrets.toml`:
```toml
GEMINI_API_KEY = "..."
OPENROUTER_API_KEY = "..."
DEEPSEEK_API_KEY = "..."
GITHUB_TOKEN = "..."
RAILWAY_TOKEN = "..."
```

## Deploy automático no Railway (opcional)

Se `RAILWAY_TOKEN` estiver configurado, depois de publicar no GitHub o app também:
1. Cria um projeto novo no Railway (`projectCreate`).
2. Cria um serviço ligado ao repositório recém-criado (`serviceCreate`).
3. Dispara o build via Nixpacks, **apontando explicitamente pro commit que acabou de subir**
   (`serviceInstanceDeployV2` com `commitSha`).
4. Gera o domínio público gratuito `*.up.railway.app` (`serviceDomainCreate`).
5. Espera até 150s (configurável) o build terminar, só para confirmar o status.

Tudo isso via [API GraphQL pública do Railway](https://docs.railway.com/integrations/api) —
não a CLI, para não depender de um binário externo no ambiente onde o Streamlit roda. Sem
`RAILWAY_TOKEN`, o app volta ao comportamento manual: um link para railway.app e instruções
de "New Project → Deploy from GitHub repo".

**⚠️ Detalhe de correção importante — `commitSha` não é opcional na prática.** A mutation
`serviceInstanceDeployV2`, sem esse argumento, redisparar o deploy usando o commit que já
estava associado ao serviço, **sem checar o GitHub por commits novos**. Isso já causou um bug
real neste projeto: uma edição de estilo era publicada certinho no GitHub, o redeploy
reportava sucesso, mas o site no ar continuava com o código antigo — porque o redeploy tinha
rebuildado o commit de antes, não o novo. A correção: toda chamada de redeploy (auto-correção
e edição) busca o SHA do commit recém-criado com `github_deployer.obter_commit_sha_atual()`
logo depois de publicar, e manda esse SHA explicitamente. `railway_deployer.redisparar_deploy()`
exige `commit_sha` como parâmetro obrigatório (sem valor padrão) exatamente para que essa classe
de bug não volte a acontecer silenciosamente se um novo caminho de código esquecer de passá-lo.

**Outras coisas que podem dar errado nessa etapa** (o app trata todas sem perder o trabalho já
feito no GitHub):
- **GitHub App do Railway sem acesso ao repo novo.** Se sua conta do Railway está com o
  GitHub App configurado para "Only select repositories", um repositório recém-criado por
  token/API não aparece automaticamente — é preciso ir em GitHub → Settings → Applications →
  Railway → Repository access e adicionar o repo manualmente (ou trocar para "All
  repositories"). O erro nesse caso vem com essa dica embutida.
- **Erros genéricos do tipo "Problem processing request".** É uma mensagem conhecida e
  pouco específica da própria API do Railway; normalmente indica um problema de permissão
  do token ou do GitHub App, não um bug no código deste projeto.
- **Build demora mais que o tempo de espera configurado.** Isso não é tratado como falha —
  a URL já é válida e mostrada mesmo assim, só o status fica "ainda em andamento" em vez de
  "no ar".

## Auto-correção quando o deploy falha (opcional)

Se, depois do deploy automático, o status do Railway vier `FAILED` ou `CRASHED`, o app tenta
se corrigir sozinho (configurável na sidebar, padrão 2 tentativas — 0 desliga):
1. Busca o log real do erro (`deploymentLogs`).
2. Manda o log + o código atual pra esteira de IA, pedindo uma correção no mesmo formato de sempre.
3. Publica a correção no **mesmo** repositório (atualiza os arquivos existentes via
   `repo.update_file`, cria os que forem novos).
4. Dispara um novo deploy e espera o resultado.
5. Repete até corrigir ou esgotar as tentativas.

**Limitação conhecida, documentada de propósito:** a query `deploymentLogs` da API do Railway
tem relatos consistentes na comunidade de devolver `"Problem processing request"` mesmo com um
token válido — não é exclusivo deste projeto. Quando isso acontece, a auto-correção **para
imediatamente** em vez de tentar corrigir "no escuro" sem evidência do que quebrou, e o chat
te avisa claramente — nesse caso, exporte o log manualmente do painel do Railway e mande pra
mim, do mesmo jeito que já funciona hoje. Cada etapa (busca de log, IA, GitHub, redeploy) tem
seu próprio tratamento de erro isolado: se qualquer uma falhar, o app para ali, explica o
motivo exato e preserva tudo que já funcionou até aquele ponto (o repositório no GitHub nunca
é desfeito).

## Nome automático do repositório

Se o campo "Nome do repositório" ficar em branco, a própria IA sugere um (a primeira linha da
resposta dela é sempre `### PROJECT_NAME: nome-sugerido`, à parte dos arquivos). Se por algum
motivo esse marcador não vier, cai um slug gerado a partir do próprio pedido do usuário (ex.:
"Um app de lista de tarefas" → `um-app-de-lista-de-tarefas`). Colisão de nome já existente
continua tratada como antes (sufixo automático).

## Conversas salvas na barra lateral

Cada conversa é salva localmente em SQLite (`vibe_coding_chats.db`, biblioteca padrão — nenhuma
dependência nova) assim que a primeira mensagem é enviada, com um título derivado dela (estilo
ChatGPT/Claude). A barra lateral lista as conversas mais recentes primeiro, com um botão pra
abrir cada uma e outro pra excluir. "➕ Nova conversa" começa do zero sem apagar as anteriores.

**Limitação por design:** por ser um arquivo local, o histórico só sobrevive enquanto o
filesystem do ambiente onde o Streamlit roda persistir. Em plataformas com filesystem efêmero
(que zeram tudo a cada redeploy), as conversas não atravessam um redeploy — troque
`chat_storage.CAMINHO_BANCO` por um banco externo se isso for um problema no seu ambiente.

## Edição contínua na mesma conversa (com imagem opcional)

Depois que um app é criado numa conversa, qualquer mensagem seguinte NESSA MESMA conversa vira
um pedido de **edição** no app já publicado, em vez de criar um app novo — é assim que "o chat
de edição" pedido mora na mesma aba onde o app nasceu, sem precisar de uma tela separada. Uma
imagem pode ser anexada direto no campo de mensagem (ícone de anexo do `st.chat_input`) para
pedir mudanças de estilo visual (cores, tipografia, layout) usando a imagem como referência —
o Gemini (nativamente multimodal) recebe a imagem de verdade via `Part.from_bytes`; o OpenRouter
também, no formato `image_url`/base64 padrão da OpenAI. Uma edição:
1. Manda o pedido (+ imagem, se houver) e os arquivos atuais pra esteira de IA.
2. Publica a mudança no **mesmo** repositório (atualiza os arquivos, não recria nada).
3. Redisparar o deploy no Railway automaticamente, se essa conversa já tiver um.

**Nota sobre a DeepSeek e imagens:** o Gemini (oficial e via OpenRouter) é multimodal nativo;
os modelos de texto da DeepSeek, historicamente, não processam imagem. Se um pedido com imagem
cascatear até a Tentativa 3 (raro, já que o Gemini é tentado primeiro), a chamada tende a falhar
— a esteira trata isso como qualquer outra falha de camada, só que sem mais nenhum fallback
depois da DeepSeek.

## Arquitetura

```
app.py               → interface Streamlit (chat, sidebar, orquestração, roteamento criação/edição)
ai_cascade.py         → esteira de fallback: Gemini oficial → OpenRouter → DeepSeek direto (+imagem opcional)
file_parser.py         → regex que extrai arquivos e o nome de projeto sugerido do Markdown da IA
github_deployer.py     → cria/atualiza o repositório e sobe os arquivos via PyGithub
railway_deployer.py     → (opcional) cria projeto/serviço/domínio e busca logs via GraphQL
auto_correcao.py         → (opcional) loop de correção automática pós-falha de deploy
edicao.py                 → aplica um pedido de ajuste num app já publicado nesta conversa
chat_storage.py            → persistência de conversas em SQLite, para a lista na sidebar
prompts.py                  → system prompt + prompts de correção/edição, compartilhados pela esteira
```

### Decisões de design que valem explicar

- **Backend serve o frontend.** O `SYSTEM_PROMPT` pede um único serviço Python
  (Flask/FastAPI) servindo o frontend estático a partir da mesma porta. Isso é
  proposital: o Nixpacks builda um serviço por repositório, então separar
  frontend e backend em processos distintos exigiria dois serviços no Railway
  e complicaria o deploy 100% automatizado que você pediu.
- **Tokens de saída sempre no teto máximo de cada modelo** — não há mais um
  número fixo/configurável. Para o Gemini oficial, o teto real é buscado ao
  vivo via `client.models.get(modelo).output_token_limit` (com um valor fixo
  de segurança caso essa busca falhe); para OpenRouter e DeepSeek direto, usa
  os tetos documentados de cada modelo (65.536 para Gemini 3.5 Flash, 384.000
  para DeepSeek V4). Isso não deixa a geração mais lenta por padrão — o
  `max_tokens` é só um teto, não uma meta — mas evita que um app grande, com
  muitos arquivos, seja cortado no meio.
- **Timeout reforçado manualmente.** Cada camada roda em uma thread com um
  teto de tempo (`ConfiguracaoModelos.timeout_segundos`, padrão 180s). Isso
  existe porque o SDK oficial do Gemini tem bugs conhecidos onde o parâmetro
  de timeout interno nem sempre é respeitado — sem esse reforço, uma API
  "pendurada" travaria a esteira inteira em vez de cair pro fallback.
- **`railway.toml` é garantido no código**, não só pedido via prompt: se a IA
  esquecer de gerá-lo, `github_deployer.garantir_railway_toml()` injeta a
  versão padrão antes do upload.
- **Nomes de modelo ficam na barra lateral** ("⚙️ Configuração avançada"), não
  fixos no código. Esse mercado muda toda semana; trocar um modelo não deve
  exigir editar `ai_cascade.py`.
- **Zip de segurança.** Se a publicação no GitHub falhar por qualquer motivo
  (token inválido, rate limit), o código já gerado nunca é descartado — fica
  disponível para download em `.zip` direto no chat.
- **Railway via GraphQL puro (POST com `requests`), não a CLI nem uma lib de
  GraphQL dedicada.** A CLI exigiria instalar um binário externo no ambiente
  onde o Streamlit roda (inviável em plataformas como o Community Cloud); uma
  lib de GraphQL seria uma dependência a mais para só 5 operações. Um POST
  simples com `query` + `variables` é o que a própria documentação do Railway
  recomenda para isso.
