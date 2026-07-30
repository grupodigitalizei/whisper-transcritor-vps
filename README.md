# 🎙️ Whisper Transcritor

Transcritor de áudio e vídeo **100% local** usando [OpenAI Whisper](https://github.com/openai/whisper). Nenhum dado sai do seu computador.

---

## ✨ Funcionalidades

- 📁 Upload de múltiplos arquivos em fila
- 🎤 Gravação direta do microfone
- 📊 Histórico persistente de todas as transcrições
- 🔄 Página atualiza automaticamente — sem precisar dar refresh
- ⬇️ Download em **TXT, SRT, Timestamps e JSON**
- 🌍 Suporte a múltiplos idiomas (PT, EN, ES, FR, DE, IT, JP, ZH)
- 🤖 Modelos: `tiny`, `base`, `small`, `medium`, `large-v3`, `turbo`
- 🔇 Filtro de vícios de linguagem ("né", "hmm", "ã...", etc.)
- 📱 **Aba Redes Sociais** — coleta reels/posts de perfis do Instagram via [ego lite](https://lite.ego.app) (sua sessão logada), mostra um mosaico 9:16 com métricas ricas (views, likes, comentários, **ER**), e deixa você selecionar o que **baixar** e **transcrever** em lote

---

## 🖥️ Requisitos

- macOS (testado) ou Linux
- Python 3.10+ (o `yt-dlp-ejs`, usado para baixar do YouTube, exige 3.10+)
- ffmpeg
- **[ego lite](https://lite.ego.app)** — opcional, necessário **apenas** para a aba **Redes Sociais** (Instagram). Instale e faça login no instagram.com uma vez.

---

## 🚀 Instalação

### 1. Clonar o repositório

```bash
git clone https://github.com/grupodigitalizei/whisper-transcritor.git
cd whisper-transcritor
```

### 2. Instalar o ffmpeg

**macOS:**
```bash
brew install ffmpeg
```

**Linux (Ubuntu/Debian):**
```bash
sudo apt update && sudo apt install ffmpeg
```

### 3. Criar o ambiente virtual e instalar as dependências

```bash
python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
```

> **Nota:** Na primeira execução, o Whisper vai baixar o modelo escolhido automaticamente. O modelo `turbo` (~800 MB) é o recomendado.
>
> **Importante:** use sempre `./venv/bin/python`, nunca o `python3` do sistema. O
> `yt-dlp` (usado para baixar do YouTube) precisa de atualizações frequentes —
> rodando fora do venv, o botão "Atualizar agora" da tela inicial tenta
> atualizar o Python errado (o do sistema) e falha silenciosamente.
> Como rede de segurança, o app **se re-executa automaticamente** no Python do
> venv se for iniciado com o interpretador errado.

### 4. (Opcional) Miniaturas no Excel da aba Redes Sociais

A exportação para Excel funciona sem nada extra (`openpyxl` já está no
`requirements.txt`). Para incluir as **miniaturas dos posts** dentro do `.xlsx`,
instale o Pillow:

```bash
./venv/bin/python -m pip install Pillow
```

---

## ▶️ Executar

```bash
./venv/bin/python whisper-app.py
```

Abra o navegador em: **http://127.0.0.1:7860**

### Manter o yt-dlp atualizado

O YouTube muda seu player com frequência, e uma versão antiga do `yt-dlp`
costuma quebrar **todos** os downloads de uma vez. Para atualizar manualmente:

```bash
./venv/bin/python -m pip install -U yt-dlp yt-dlp-ejs
```

A tela inicial do app também avisa automaticamente quando há uma versão mais
nova disponível, com um botão de atualização — mas ele só funciona
corretamente se o app estiver rodando via `./venv/bin/python` (veja a nota
acima).

---

## 📱 Aba Redes Sociais (Instagram)

Coleta de conteúdo do Instagram usando a **sua sessão logada** via
[ego lite](https://lite.ego.app) — bem mais confiável que o download comum
(yt-dlp) para o Instagram, e com metadados que o yt-dlp não traz.

**Pré-requisito:** ter o **ego lite** instalado e logado no instagram.com. O
app detecta automaticamente se o `ego-browser` está disponível e mostra o
status ("ego lite conectado") no topo da aba.

**Como usar:**

1. Aba **Redes Sociais** → **Por perfil** (`@perfil` + período + máx. posts) ou
   **Por URLs** (cole links de reels/posts).
2. Clique em **Coletar**. Vira um **mosaico 9:16** com capa, preview no hover,
   e métricas (views, likes, comentários, reposts, **ER%**, duração).
3. **Selecione** os itens: clique no card, **Shift+clique** para intervalo,
   **⌘/Ctrl+clique** para vários. Ou use **Selecionar todos**.
4. Na barra flutuante: **Baixar mídia** (HD → Biblioteca de Mídia),
   **Baixar métricas** (CSV) ou **Transcrever** (baixa e emenda no Whisper).
   Cada card também tem ações individuais (baixar mídia, baixar métricas, abrir).
5. **Insights** (tendências) e **Exportar Excel** da coleta inteira.

> **Facebook** ainda não é suportado por este caminho — o coletor é específico
> do Instagram. Vídeos avulsos de Facebook podem ser baixados pela aba
> **Download Avançado** (yt-dlp).
>
> As coletas ficam em `.whisper_data/social/` e **não** vão para o repositório.
> URLs de mídia do CDN do Instagram expiram em poucos dias — baixe logo após coletar.

---

## 📂 Estrutura do projeto

```
whisper-transcritor/
├── whisper-app.py   # Backend FastAPI (API + servidor)
├── index.html       # Frontend (HTML)
├── static/          # app.js, style.css, fontes, favicon
├── social/          # Aba Redes Sociais: coletor ego-lite + normalização + download + export
│   ├── collector.py     # Coleta Instagram (perfil/URLs) via ego-browser
│   ├── core.py          # Normalização, cálculo de ER e tendências
│   ├── downloader.py     # Download de mídia HD do CDN
│   ├── excel.py         # Exportação Excel/CSV
│   └── jobs.py          # Registro de jobs em background
├── requirements.txt
├── .gitignore
└── .whisper_data/   # Criado automaticamente — NÃO vai pro GitHub
    ├── history.json      # Histórico de transcrições
    ├── results/          # Arquivos de resultado por transcrição
    ├── uploads/          # Uploads temporários (limpos após transcrição)
    └── social/           # Coletas do Instagram (datasets + cache + exports)
```

---

## 🔌 API REST

### Histórico e estatísticas

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `GET` | `/api/history` | Lista todo o histórico de transcrições |
| `GET` | `/api/stats` | Total de arquivos e horas transcritas |

### Transcrição

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `POST` | `/api/transcribe` | Envia arquivo para transcrição |
| `POST` | `/api/transcribe-url` | Baixa de URL (yt-dlp) e transcreve |
| `GET` | `/api/progress/{task_id}` | Progresso de uma tarefa |
| `GET` | `/api/active-tasks` | Tarefas ativas em memória |
| `POST` | `/api/reset-stale` | Marca tarefas órfãs após restart como erro |

### Resultados e downloads

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `GET` | `/api/result/{filename}` | Texto da transcrição (todos os formatos) |
| `GET` | `/api/download/{filename}/{fmt}` | Download de um formato (`txt`, `srt`, `timestamps`, `json`) |
| `GET` | `/api/download-all` | Download de tudo em `.zip` |
| `GET` | `/api/gaps/{filename}?min_gap=1.0` | Detecta silêncios/respiros entre segmentos |
| `DELETE` | `/api/delete/{filename}` | Remove transcrição (histórico + arquivos) |

### Biblioteca de mídia

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `GET` | `/api/media-history` | Lista mídias baixadas/enviadas |
| `POST` | `/api/yt-download-only` | Baixa apenas (sem transcrever) — `media_type=video|audio`, `quality=best|1080p|...` |
| `GET` | `/api/download-media/{filename}` | Baixa o arquivo original armazenado |
| `DELETE` | `/api/delete-media/{filename}` | Remove o arquivo físico do upload |

### Redes Sociais (Instagram via ego lite)

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `GET` | `/api/social/status` | Diz se o motor de coleta (ego lite) está disponível |
| `POST` | `/api/social/collect` | Coleta o feed de um perfil (`username`, `max_posts`, `since_days`) |
| `POST` | `/api/social/collect-urls` | Resolve URLs de posts/reels individuais |
| `GET` | `/api/social/job/{job_id}` | Progresso de uma coleta/download em background |
| `GET` | `/api/social/datasets` | Lista as coletas salvas |
| `GET` | `/api/social/dataset/{ds_id}` | Posts normalizados + perfil + tendências |
| `GET` | `/api/social/thumb?url=` | Proxy+cache de capa (só CDN do IG/FB) |
| `GET` | `/api/social/media?url=` | Proxy com Range p/ preview de vídeo (só CDN do IG/FB) |
| `POST` | `/api/social/fetch` | Baixa mídia dos selecionados e (opcional) transcreve |
| `POST` | `/api/social/export` | Gera Excel/CSV da coleta (+ aba de Tendências) |
| `GET` | `/api/social/export-file/{name}` | Baixa a planilha gerada |

> **Segurança:** todos os endpoints com `{filename}` validam o nome contra path traversal (`..`, `/`, `\`, etc.). Os proxies `/api/social/thumb` e `/api/social/media` só falam com o CDN do Instagram/Facebook (trava SSRF, sem seguir redirecionamentos). Como o servidor escuta em `127.0.0.1`, não há autenticação — não exponha em rede sem proxy reverso.

---

## 🤖 Modelos disponíveis

| Modelo | Tamanho | Velocidade | Precisão |
|--------|---------|-----------|----------|
| `tiny` | ~39 MB | ⚡⚡⚡⚡ | ⭐ |
| `base` | ~74 MB | ⚡⚡⚡ | ⭐⭐ |
| `small` | ~244 MB | ⚡⚡ | ⭐⭐⭐ |
| `medium` | ~769 MB | ⚡ | ⭐⭐⭐⭐ |
| `large-v3` | ~1.5 GB | 🐢 | ⭐⭐⭐⭐⭐ |
| `turbo` | ~809 MB | ⚡⚡⚡ | ⭐⭐⭐⭐⭐ ✅ recomendado |

---

## 📝 Notas

- As transcrições ficam salvas em `.whisper_data/` na pasta do projeto
- O histórico sobrevive a reinicializações do servidor
- Tamanho máximo de arquivo: limitado apenas pelo disco e RAM disponível
- Processamento 100% local — nenhum dado é enviado para servidores externos
