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

---

## 🖥️ Requisitos

- macOS (testado) ou Linux
- Python 3.10+ (o `yt-dlp-ejs`, usado para baixar do YouTube, exige 3.10+)
- ffmpeg

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

## 📂 Estrutura do projeto

```
whisper-transcritor/
├── whisper-app.py   # Backend FastAPI (API + servidor)
├── index.html       # Frontend (HTML/CSS/JS puro)
├── .gitignore
└── .whisper_data/   # Criado automaticamente — NÃO vai pro GitHub
    ├── history.json      # Histórico de transcrições
    ├── results/          # Arquivos de resultado por transcrição
    └── uploads/          # Uploads temporários (limpos após transcrição)
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

> **Segurança:** todos os endpoints com `{filename}` validam o nome contra path traversal (`..`, `/`, `\`, etc.). Como o servidor escuta em `127.0.0.1`, não há autenticação — não exponha em rede sem proxy reverso.

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
