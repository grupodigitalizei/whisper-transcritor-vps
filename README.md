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
- 🔗 Download por URL (YouTube, Vimeo, TikTok…) e **Google Drive** — cole o link de um arquivo público (`Qualquer pessoa com o link`) para baixar ou transcrever
- 📱 **Aba Redes Sociais** — coleta reels/posts de perfis do Instagram via [ego lite](https://lite.ego.app) (sua sessão logada), mostra um mosaico 9:16 com métricas ricas (views, likes, comentários, **ER**), e deixa você selecionar o que **baixar** e **transcrever** em lote

---

## 🖥️ Requisitos

- macOS (testado) ou Linux
- Python 3.10+ (o `yt-dlp-ejs`, usado para baixar do YouTube, exige 3.10+) — `./install.sh` detecta e instala isso automaticamente
- ffmpeg — `./install.sh` também cuida disso
- **[ego lite](https://lite.ego.app)** — opcional, necessário **apenas** para a aba **Redes Sociais** (Instagram). Instale e faça login no instagram.com uma vez.

---

## 🚀 Instalação

### 1. Clonar o repositório

```bash
git clone https://github.com/grupodigitalizei/whisper-transcritor.git
cd whisper-transcritor
```

### 2. Rodar o script de instalação

```bash
./install.sh
```

Esse script resolve sozinho as fricções mais comuns de instalar isso num
ambiente novo (servidor Linux limpo, outro Mac, etc.):

- detecta o Python disponível e **prefere a faixa 3.10–3.12** quando existir
  mais de uma versão instalada — Python muito recente (3.13+) costuma ficar
  meses sem build pronto (wheel) de torch/openai-whisper no PyPI, e o
  `pip install` trava tentando compilar do zero ou falha;
- instala o **ffmpeg** (via `apt`/`brew`, conforme o sistema) se não estiver presente;
- em Debian/Ubuntu, instala o pacote **`python3.X-venv`** — o módulo `venv` é
  separado do Python principal nessas distros, e sem ele `python3 -m venv`
  cria um ambiente quebrado, sem `pip` (erro "ensurepip is not available");
- cria o `venv/` (recriando se um anterior estiver quebrado) e instala tudo
  de `requirements.txt` lá dentro.

É seguro rodar mais de uma vez — pula o que já está instalado.

Se seu sistema tiver mais de um Python instalado e quiser forçar um específico:
```bash
PYTHON_BIN=python3.11 ./install.sh
```

<details>
<summary><strong>Instalação manual</strong> (se preferir não usar o script, ou estiver num sistema que ele não cobre)</summary>

**Ffmpeg:**
```bash
# macOS
brew install ffmpeg

# Linux (Ubuntu/Debian)
sudo apt update && sudo apt install ffmpeg
```

**Ambiente virtual e dependências:**
```bash
python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
```

> **Nota:** Na primeira execução, o Whisper vai baixar o modelo escolhido automaticamente. O modelo `turbo` (~800 MB) é o recomendado.

</details>

> **Importante:** use sempre `./venv/bin/python`, nunca o `python3` do sistema. O
> `yt-dlp` (usado para baixar do YouTube) precisa de atualizações frequentes —
> rodando fora do venv, o botão "Atualizar agora" da tela inicial tenta
> atualizar o Python errado (o do sistema) e falha silenciosamente.
> Como rede de segurança, o app **se re-executa automaticamente** no Python do
> venv se for iniciado com o interpretador errado.

### 3. (Opcional) Miniaturas no Excel da aba Redes Sociais

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

## 🔐 Acesso e Área Pública

O app pede senha. Existem **dois acessos**, cada um com sua senha:

| Acesso | Quem usa | O que vê |
|--------|----------|----------|
| **Admin** | você | **Tudo** — todo o acervo, privado e público, mais Configurações e Redes Sociais |
| **Equipe** | funcionários | **Só o que você marcou como Público** — sem Redes Sociais |

A primeira execução gera as duas senhas e as imprime no terminal — **anote, elas
não podem ser recuperadas depois**, só trocadas em *Configurações → Área Pública*.
Para redefinir uma senha esquecida sem mexer em JSON:

```bash
WHISPER_ADMIN_PASSWORD='sua-nova-senha' ./venv/bin/python whisper-app.py
```

### Como um item vai para a Área Pública

Nada é compartilhado por acidente: **todo o acervo que já existia é privado**, e
qualquer item novo enviado por você também nasce privado.

- **Você publica manualmente**: selecione itens na aba *Transcrições* (ou na
  *Biblioteca de Mídia*) e clique em **Publicar** — ou use *Publicar na Área
  Pública* no menu de três pontinhos da linha.
- **O que o funcionário envia já nasce público**: é o acervo compartilhado dele.
- A aba **Transcrições Públicas** (só o admin vê) mostra exatamente o que está
  compartilhado, com as mesmas funções da aba normal.

Publicar uma transcrição publica junto o vídeo/áudio original — senão o
funcionário leria o texto sem conseguir abrir a mídia.

### Como isolamento funciona (e o que ele garante)

O filtro é no **servidor**, não na tela. Um funcionário que adivinhe a URL de um
item privado recebe `404`, não o arquivo. Concretamente, para o acesso da equipe:

- `/api/history`, `/api/media-history`, `/api/stats`, `/api/folders` só devolvem
  itens públicos — **inclusive os nomes das pastas** ficam escondidos;
- baixar, ler, renomear, mover, apagar ou retentar um item privado → `404`;
- Configurações, faxina de disco, atualização do yt-dlp, renomear/apagar pastas
  e publicar/despublicar → `403` (só admin);
- **a aba Redes Sociais não existe para a equipe**: todo o prefixo
  `/api/social/*` responde `403`. O bloqueio é por prefixo no middleware, então
  um endpoint social novo já nasce fechado. Ela usa a sua sessão logada do
  Instagram (via ego lite) — é ferramenta de administração, não de acervo.

As abas que a equipe **tem**: Transcrições, Biblioteca de Mídia e Download
Avançado, com todas as funções (enviar, transcrever, baixar em todos os
formatos, renomear, mover, excluir, tentar novamente).

### Revogar acesso

Trocar a senha da equipe **derruba todas as sessões abertas** daquele acesso — é
assim que se remove quem saiu da empresa. Há também *Desconectar todos os
funcionários agora*, que encerra as sessões mantendo a mesma senha.

O login tem freio de força-bruta: 10 tentativas erradas em 10 min bloqueiam
novas tentativas por 5 min.

---

## 🌐 Publicar na internet (Tailscale Funnel)

O app **precisa continuar rodando neste Mac** — o Whisper usa a CPU/GPU daqui, o
que hospedagem compartilhada (Hostgator e afins) não oferece. Para os
funcionários acessarem de fora, um túnel HTTPS aponta para o servidor local.

O Tailscale é usado em modo **userspace-networking**: o daemon roda como o seu
usuário, sem senha de administrador do Mac e sem extensão de kernel. Este Mac não
entra na sua rede Tailscale como um nó comum — ele só serve este túnel.

**Instalação (uma vez só):**

```bash
brew install tailscale
```

Depois é só rodar o script — ele sobe o daemon sozinho e, na primeira vez, mostra
o link para você entrar na sua conta Tailscale (crie uma grátis, se não tiver):

```bash
./publicar.sh
```

Se o Funnel ainda não estiver liberado na conta, o script avisa e mostra o link
do admin console para habilitar. Rode de novo depois de cada um desses passos.

O script confere que o app está no ar, sobe o túnel e imprime o endereço
`https://<nome>.<tailnet>.ts.net`. Mande esse link + a senha da equipe para os
funcionários.

Comandos auxiliares:

```bash
./publicar.sh login    # mostra o link de login na conta Tailscale
```

```bash
./publicar.sh status   # ver se está no ar e qual o endereço
./publicar.sh parar    # derrubar o túnel (o app continua local)
```

### E a Hostgator?

**Nada precisa ser configurado nela para o app funcionar.** Hospedagem
compartilhada não roda este projeto: o Whisper exige PyTorch (~2,5 GB), ffmpeg e
vários GB de RAM com CPU saturada por minutos a cada arquivo — plano
compartilhado derruba processo longo e limita RAM/CPU por conta. E mesmo num VPS
barato o resultado seria **mais lento** que o Mac (Apple Silicon), com
mensalidade a mais.

O que a Hostgator faz bem aqui é ser o **endereço de entrada**, para a equipe não
precisar decorar uma URL `.ts.net`. No cPanel:

1. *Domínios → Subdomínios*: crie `transcricoes` no seu domínio.
2. *Domínios → Redirecionamentos*: origem = o subdomínio, destino = a URL
   `https://<nome>.<tailnet>.ts.net`, tipo **302 (temporário)**.

Ou, se preferir editar arquivo, um `.htaccess` na pasta do subdomínio:

```apache
RewriteEngine On
RewriteRule ^(.*)$ https://SEU-ENDERECO.ts.net/$1 [R=302,L]
```

Use **302 e não 301**: se um dia a URL do túnel mudar, o 301 fica gravado no
navegador de quem já acessou. E note que é redirect, não proxy — a barra de
endereços do funcionário vai mostrar o `.ts.net`, porque é de lá que vem o
certificado HTTPS. O domínio bonito é o atalho de entrada, não uma máscara.

**O que lembrar:** o link só funciona com este Mac ligado e o app rodando. O
túnel não sobrevive a um reboot — rode `./publicar.sh` de novo depois. Se você
tem domínio na Hostgator, aponte `transcricoes.seudominio.com` para esse
endereço com um redirect.

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
├── auth.py          # Senhas (PBKDF2) + sessões dos dois acessos
├── index.html       # Frontend (HTML)
├── login.html       # Tela de login
├── publicar.sh      # Sobe/derruba o túnel público (Tailscale Funnel)
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
    ├── auth.json         # Hashes das senhas (0600) — NUNCA versionar
    ├── sessions.json     # Sessões ativas (0600) — NUNCA versionar
    ├── history.json      # Histórico de transcrições (campo `visibility` por item)
    ├── results/          # Arquivos de resultado por transcrição
    ├── uploads/          # Uploads temporários (limpos após transcrição)
    └── social/           # Coletas do Instagram (datasets + cache + exports)
```

---

## 🔌 API REST

> Todas as rotas exigem sessão. O que cada uma devolve depende do acesso
> (admin vê tudo; equipe vê só o público) — ver *Acesso e Área Pública*.

### Autenticação e Área Pública

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `POST` | `/api/auth/login` | Entra com a senha; descobre o papel pela senha |
| `POST` | `/api/auth/logout` | Encerra a sessão |
| `GET` | `/api/me` | Papel atual + nº de sessões ativas (admin) |
| `POST` | `/api/auth/password` | Troca a senha de `admin` ou `public` (admin) |
| `POST` | `/api/auth/revoke-public` | Desconecta todos os funcionários (admin) |
| `POST` | `/api/visibility` | Publica/despublica itens (admin) |

### Histórico e estatísticas

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `GET` | `/api/history` | Lista o histórico de transcrições (filtrado pelo papel) |
| `GET` | `/api/stats` | Total de arquivos e horas transcritas (filtrado pelo papel) |

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
