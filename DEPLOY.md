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
- **Port: `7860`** ← o padrão do Easypanel é 3000; trocar isto é obrigatório.
- HTTPS ligado (Let's Encrypt).

No DNS, um registro `A` do host apontando para o IP da VPS.

## 3. Variáveis de ambiente

Aba **Environment**:

```
WHISPER_HOST=0.0.0.0
WHISPER_PORT=7860
WHISPER_ADMIN_PASSWORD=troque-esta-senha
WHISPER_PUBLIC_PASSWORD=troque-esta-tambem
WHISPER_MAX_CACHED_MODELS=1
WHISPER_MAX_UPLOAD_MB=4096
WHISPER_YTDLP_COOKIES=none
```

- `WHISPER_HOST=0.0.0.0` é obrigatório. Em `127.0.0.1` o app só responde a si
  mesmo e o proxy do Easypanel devolve 502.
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
✅  Whisper Transcritor → http://0.0.0.0:7860
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
| 502 no domínio                   | `WHISPER_HOST` não é `0.0.0.0`, ou porta do domínio ≠ 7860 |
| Container reinicia sozinho       | OOM. Baixe o modelo, ponha `WHISPER_MAX_CACHED_MODELS=1`, adicione swap |
| Perdeu tudo depois do deploy     | Volume de `/app/.whisper_data` não foi criado          |
| Baixa 3 GB de modelo toda vez    | Volume de `/root/.cache/whisper` não foi criado        |
| Não aceita a senha do env        | Já existe `auth.json` no volume — troque dentro do app |
| Build falha em `torch`           | Arquitetura sem wheel CPU. Confira `uname -m` na VPS (esperado `x86_64` ou `aarch64`) |
