# Deploy no Easypanel (VPS)

Guia do zero até o app no ar. Escrito para uma VPS Linux com Easypanel já
instalado e **8 GB de RAM**.

---

## 1. Criar a app no Easypanel

1. No projeto do Easypanel, **+ Service → App**.
2. Nome: `transcritor`.
3. Aba **Source**:
   - Tipo **GitHub**.
   - Owner/Repo: `grupodigitalizei/whisper-transcritor-vps` (privado — autorize o
     Easypanel a acessar a org na primeira vez).
   - Branch: `main`.
4. Aba **Build**: método **Dockerfile**, caminho `Dockerfile`.

> Sem repositório? A aba **Source** também aceita **Upload** — um `.zip`/`.tar`
> com o projeto. O `Dockerfile` faz `COPY . .`, então o arquivo precisa conter o
> código todo (`whisper-app.py`, `index.html`, `static/`, `social/`…), não só o
> Dockerfile.

## 2. Domínio

Aba **Domains** → **Add Domain**:

- Host: o seu domínio (ex.: `transcritor.seudominio.com`).
- **Port: `80`** — o container escuta na 80 justamente para casar com o padrão do
  Easypanel. Se o painel já preencheu 80, não mexa.
- HTTPS ligado (Let's Encrypt).

No DNS, um registro `A` do host apontando para o IP da VPS.

## 3. Variáveis de ambiente

Aba **Environment**:

```
WHISPER_ADMIN_PASSWORD=troque-esta-senha
WHISPER_PUBLIC_PASSWORD=troque-esta-tambem
WHISPER_MAX_CACHED_MODELS=1
WHISPER_MAX_UPLOAD_MB=4096
WHISPER_YTDLP_COOKIES=none
```

- `WHISPER_HOST` e `WHISPER_PORT` não aparecem aqui porque o Dockerfile já os
  define (`0.0.0.0:80`). Só declare se quiser outra porta — e então o domínio
  precisa apontar para ela.
- As duas senhas são lidas **só na primeira execução** (viram hash em
  `.whisper_data/auth.json`). Depois disso, trocar aqui não muda nada — a troca
  se faz dentro do app, em Configurações.
- `WHISPER_MAX_CACHED_MODELS=1` em 8 GB: com 2 modelos residentes o kernel mata
  o processo no meio de uma transcrição.

## 4. Volumes (não pule)

Aba **Mounts** → dois volumes. Sem eles, cada redeploy apaga todas as
transcrições e baixa os modelos de novo (~3 GB).

| Tipo   | Nome     | Caminho no container    |
| ------ | -------- | ----------------------- |
| Volume | `dados`  | `/app/.whisper_data`    |
| Volume | `modelos`| `/root/.cache/whisper`  |

## 5. Recursos

Aba **Advanced/Deploy**: **1 réplica**. O app guarda estado em disco e mantém
modelo na memória — duas réplicas brigam pelos mesmos arquivos.

## 6. Deploy

Botão **Deploy**. O primeiro build demora (uns 10–20 min: torch e as
dependências). Acompanhe em **Logs**. Quando subir, aparece:

```
✅  Whisper Transcritor → http://0.0.0.0:80
```

Abra o domínio, faça login com a senha de admin.

---

## O que muda em relação ao Mac

**Transcrição roda em CPU, sem GPU.** Ordem de grandeza: ~1× a 3× a duração do
áudio no `small`, e bem mais nos modelos grandes. Com 8 GB:

| Modelo       | RAM aprox. | Em 8 GB          |
| ------------ | ---------- | ---------------- |
| `tiny`/`base`| < 1 GB     | tranquilo        |
| `small`      | ~2 GB      | recomendado      |
| `medium`     | ~5 GB      | funciona, aperta |
| `turbo`      | ~6 GB      | risco de OOM     |
| `large-v3`   | ~10 GB     | **não carrega**  |

Vale criar 2–4 GB de swap na VPS como rede de segurança:

```bash
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
```

**A aba Redes Sociais não funciona.** A coleta do Instagram roda por
`ego-browser` (ego-lite), que precisa de um navegador com a sessão logada do
dono — não existe no container. Os endpoints respondem com erro claro
("comando 'ego-browser' não encontrado"), sem derrubar o resto do app.

**Compressão de vídeo fica mais lenta.** No Mac usa o encoder do chip
(`videotoolbox`); no Linux cai para `libx264` em software. Funciona, só demora.

**Download do YouTube sem cookies.** Sem Chrome no container, `cookiesfrombrowser`
está desligado (`WHISPER_YTDLP_COOKIES=none`). Vídeo público baixa normalmente;
o que exige login, não.

---

## HTTP 403 ao baixar do YouTube

Sintoma: a extração funciona, mas o download dos dados falha com
`unable to download video data: HTTP Error 403: Forbidden`.

São **dois problemas diferentes** com o mesmo sintoma, e a distinção está nos
avisos que aparecem antes do erro.

### 1. PO Token faltando (já resolvido na imagem)

```
tv_simply client https formats require a GVS PO Token which was not provided
mweb client https formats require a GVS PO Token which was not provided
```

Aqui o YouTube não bloqueou o IP: ele exigiu um Proof-of-Origin Token que o
yt-dlp sozinho não gera. Sem o token, os formatos desses clientes são
descartados e sobra o 403. Proxy não resolve isto.

A imagem já embute o [bgutil POT provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider):
o `docker-entrypoint.sh` sobe o provedor na porta 4416 e o app aponta o plugin
para ele automaticamente. Não precisa configurar nada.

Para desligar (ou usar um provedor externo):

```
WHISPER_POT_DISABLE=1
WHISPER_POT_BASE_URL=http://outro-host:4416
```

### 2. Bloqueio de IP de datacenter

```
Sign in to confirm you're not a bot
```

Aí sim o IP foi barrado, e nenhum `player_client` nem PO Token resolve — a
mesma imagem baixa o mesmo vídeo sem erro a partir de um IP residencial. As
saídas são via variável de ambiente:

### Proxy

```
WHISPER_YTDLP_PROXY=http://usuario:senha@host:porta
```

Faz o yt-dlp sair por outro IP. Não põe conta nenhuma em risco.

Aceita **vários**, separados por vírgula ou espaço:

```
WHISPER_YTDLP_PROXY=http://ip1:8080, http://ip2:3128, http://ip3:80
```

O primeiro é usado direto; os demais viram motores extras da cascata do
`download_engine` e entram sozinhos quando o anterior falha. Isso existe porque
proxy público morre o tempo todo — numa lista recém-publicada, 8 de 10 já
estavam fora do ar no momento do teste, e um que funcionou às 22h estava morto
20 minutos depois. Com a lista, o app se vira sem ninguém editar variável e
reiniciar container.

Proxy residencial pago é mais estável (~$1–3/GB), mas com a rotação a rota
gratuita fica utilizável. Espere lentidão: medimos 84 KB/s num proxy público
que funcionava, contra download direto em segundos.

> **Nunca combine proxy público com `WHISPER_YTDLP_COOKIEFILE`.** Um proxy HTTP
> vê todo o tráfego que passa por ele; mandar cookie de sessão através de um
> servidor desconhecido entrega a conta ao operador.

### Arquivo de cookies

```
WHISPER_YTDLP_COOKIEFILE=/app/.whisper_data/cookies.txt
```

Cookies de uma conta logada, formato Netscape. Exporte do navegador (extensão
"Get cookies.txt") e ponha o arquivo dentro do volume de dados, para sobreviver
ao redeploy.

> **Use uma conta descartável.** Cookies da sua conta pessoal usados a partir de
> um IP de datacenter são um bom jeito de o Google bloquear a conta.

O cookie só é enviado para hosts da allowlist (YouTube/Google) — colar uma URL
de outro domínio nunca vaza sua sessão. Se o arquivo não existir, o app avisa no
log e segue sem ele.

### Sem nenhuma das duas

Baixe no Mac (onde o IP é residencial) e envie o arquivo por upload. As duas
variáveis são opcionais — o resto do app funciona normalmente sem elas.

## Redeploy

Este repositório (`whisper-transcritor-vps`) é **só o espelho de deploy**. O
repositório principal do sistema continua sendo `whisper-transcritor` e não é
tocado por nada daqui.

No clone local, o fluxo é:

```bash
git checkout deploy-vps
git merge main          # traz o que você desenvolveu no dia a dia
git push vps deploy-vps:main
```

Depois, **Deploy** no Easypanel (ou ligue o webhook em **Source → Auto Deploy**).

Os volumes sobrevivem ao redeploy — transcrições, histórico e senhas continuam lá.

## Se der errado

| Sintoma                          | Causa provável                                        |
| -------------------------------- | ----------------------------------------------------- |
| 502 / "Service is not reachable" | Porta do domínio ≠ porta do container. Confira nos Logs em que porta o app subiu |
| Container reinicia sozinho       | OOM. Baixe o modelo, ponha `WHISPER_MAX_CACHED_MODELS=1`, adicione swap |
| Perdeu tudo depois do deploy     | Volume de `/app/.whisper_data` não foi criado          |
| Baixa 3 GB de modelo toda vez    | Volume de `/root/.cache/whisper` não foi criado        |
| Não aceita a senha do env        | Já existe `auth.json` no volume — troque dentro do app |
| Build falha em `torch`           | Arquitetura sem wheel CPU. Confira `uname -m` na VPS (esperado `x86_64` ou `aarch64`) |
