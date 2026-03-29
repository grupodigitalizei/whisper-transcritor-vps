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
- Python 3.9+
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

### 3. Instalar as dependências Python

```bash
pip3 install openai-whisper fastapi uvicorn python-multipart
```

> **Nota:** Na primeira execução, o Whisper vai baixar o modelo escolhido automaticamente. O modelo `turbo` (~800 MB) é o recomendado.

---

## ▶️ Executar

```bash
python3 whisper-app.py
```

Abra o navegador em: **http://127.0.0.1:7860**

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

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `GET` | `/api/history` | Lista todo o histórico |
| `GET` | `/api/stats` | Total de arquivos e horas transcritas |
| `POST` | `/api/transcribe` | Envia arquivo para transcrição |
| `GET` | `/api/progress/{task_id}` | Progresso de uma tarefa |
| `GET` | `/api/result/{filename}` | Texto da transcrição |
| `GET` | `/api/download/{filename}/{fmt}` | Download (`txt`, `srt`, `timestamps`, `json`) |
| `GET` | `/api/download-all` | Download de tudo em `.zip` |
| `DELETE` | `/api/delete/{filename}` | Remove do histórico |
| `GET` | `/api/active-tasks` | Tarefas ativas em memória |
| `POST` | `/api/reset-stale` | Marca tarefas interrompidas como erro |

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
