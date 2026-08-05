# CLAUDE.md

Guia para o Claude Code trabalhar neste repositório. Leia inteiro antes de mexer
na aba **Sonda** — a maior parte das dores de cabeça já aconteceu e está
documentada aqui.

## O que é

Telemetry Portal — portal em **Flask (`app.py`)** para monitoramento de MikroTik:

1. **OSPF/BFD** dos PEs (core), lido pela **API do RouterOS** por um poller que
   roda em background a cada ~30s.
2. **Alertas no Telegram** (queda/retorno de vizinho, sonda, PPPoE, e cada
   métrica ao passar/normalizar o limite).
3. Aba **Sonda** (Active Probing): uma RB750Gr3 na ponta envia `POST /api/probe`
   com perda, latência, PPPoE, CPU, memória, DNS e HTTP. A aba mostra ao vivo
   (cards + gráficos Chart.js, auto-refresh 10s).

Front em Jinja (`templates/`) + Chart.js. Dashboards Grafana em `grafana/`.
Banco SQLite (`telemetry.db`, gerado no 1º run).

## Estrutura dos arquivos

- `app.py` — servidor Flask: rotas web, API JSON, ingestão da sonda, alertas
  Telegram e o poller OSPF/BFD (tudo num arquivo; roda em `__main__`).
- `templates/` — páginas Jinja, uma por aba:
  - `base.html` — layout/nav. `login.html` — login.
  - `page_graficos.html` — painel OSPF/BFD. `page_roteadores.html` — cadastro de PEs.
  - `page_sonda.html` — **aba Sonda** (cards + gráficos + guia do script da RB).
  - `page_telegram.html`, `page_usuarios.html`, `page_logs.html`.
- `grafana/core-ospf-bfd.json`, `grafana/sonda.json` — dashboards (datasource Infinity → endpoints `/api/.../grafana*`).
- `install.sh` / `telemetry.service` — instalação e unit systemd.
- `DEPLOY.local.md` — infra interna de deploy (**gitignored**, ver abaixo).
- Gerados no 1º run (não versionados): `telemetry.db`, `secret.key`, `venv/`.

## Rodar localmente

```bash
python -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python app.py   # http://localhost:8080  (login inicial admin/admin)
```

## Deploy na VM de produção

Deploy por **SSH/SFTP** com **Paramiko**, reutilizando a conexão salva na
extensão **SSH FS do VSCode** (a senha **nunca** fica no repo — é lida do
`settings.json` local em runtime). O script faz backup, sobe os arquivos
alterados e valida por `md5`.

- Fluxo típico: só `templates/*.html` muda entre ajustes. `app.py` e
  `grafana/*.json` mudam com menos frequência.
- **Templates Jinja ficam em cache em produção** → depois de subir HTML,
  **reiniciar o serviço** (`systemctl restart telemetry.service`) e dar
  **hard refresh** (Ctrl+Shift+R) no navegador.
- **Restart NÃO muda o que a sonda envia** — quem decide os dados da sonda é o
  script da RB (ver abaixo). Restart só recarrega o front.

> Host/IP, usuário SSH, caminho no servidor, nome da conexão SSH FS e o script
> de deploy pronto ficam em **`DEPLOY.local.md`** (gitignored). **Não** coloque
> IPs de servidor, credenciais ou caminhos neste CLAUDE.md nem no README (são
> públicos).

## Como a ingestão da sonda funciona (backend)

- `POST/GET /api/probe` — tolerante a **JSON**, **form** ou **query string**
  (`_parse_probe_request`). RouterOS v6 não manda corpo em POST → a RB envia
  **tudo na query string**.
- Campos reservados: `probe` (nome da sonda), `token`, `pppoe`. Qualquer outra
  chave numérica vira **métrica**.
- Cada POST grava **uma linha** em `probe_samples(ts, probe, payload)`.
  ⚠️ **A página mostra o `last` = a ÚLTIMA linha.** Portanto **mande todas as
  métricas num único POST**. Se você quebrar em vários POSTs, o "last" fica só
  com o que veio no último → some o resto da tela. (Já erramos isso.)
- Famílias de métrica são inferidas pelo **prefixo/sufixo** da chave — e existem
  em DOIS lugares que precisam ficar em sincronia:
  - Backend: `_metric_family` / `_metric_is_bad` em `app.py` (define alertas).
  - Front: `familyOf` em `page_sonda.html` (define cards/gráficos).
  - Prefixos: `loss*`→perda, `rtt*`/`latency*`→latência, `jitter*`→jitter,
    `cpu*`→cpu, `mem*`→memória livre, `dns_ok`/`http_ok`→bool, `*mbps`→vazão,
    `mos`→qualidade. `uptime*` é só informativo (não alerta).

## ⚠️ A SONDA RB (RouterOS) — LIÇÕES CRÍTICAS

A RB de produção (`RB-PROBE-UBERABA`) roda **RouterOS 6.49.20**. Quase todas as
dores vieram daqui. Regras que NÃO podem ser esquecidas:

### 1. `as-value` NÃO existe no 6.49 → latência via arquivo
`/ping ... as-value` dá **`expected end of command`** (erro de PARSE, que aborta
o script inteiro — nem `:do on-error` captura, pois é antes de rodar). Também
não use `:while` nem arrays `{...}` complexos. **Nunca proponha as-value nesta RB.**

Como capturamos latência mesmo assim (funciona, testado): `:execute` grava a
saída do `/ping` num arquivo e extraímos o `avg-rtt` por texto:

```
:execute script="/ping 8.8.8.8 count=5" file="pr3"   # dispara em background
# ... (mede a perda enquanto isso) ...
:do {
  :local c [/file get [/file find where name~"pr3"] contents]
  :local a [:find $c "avg-rtt="]
  :if ([:typeof $a]!="nil") do={
    :local s [:pick $c ($a+8) [:len $c]]     # "avg-rtt=" tem 8 chars
    :local m [:find $s "ms"]
    :if ([:typeof $m]!="nil") do={ :set rtt3 [:pick $s 0 $m] }   # número antes de "ms"
  }
} on-error={}
```

Dispare os 5 `:execute` no início (rodam em paralelo/background enquanto a perda
é medida → quase não somam tempo). Tudo em `:do on-error` pra não derrubar o
envio. Perda continua pelo método confiável `[/ping ADDR count=N]` (retorna os
**recebidos**; loss = `(N-recv)*100/N`). **O script final completo é o guia
dentro de `page_sonda.html`** (fonte da verdade — copie de lá).

### 2. Edite o script que o SCHEDULER roda (não crie um novo!)
Sintoma clássico: "editei o script e não muda nada". Causa: existem vários
scripts na RB (`script1`, `sonda-telemetry`, ...). O **scheduler** dispara um
nome específico via `on-event=/system script run sonda-telemetry`. Se você editar
`script1` mas o scheduler roda `sonda-telemetry`, nada muda. **Sempre edite o
script que o scheduler executa.** Confirme com:

```
/system scheduler print detail    # veja o on-event (qual script roda)
/system script print detail        # veja o source de cada script
```

### 3. O `?` da URL quebra no TERMINAL
A URL tem `api/probe?...` e no **New Terminal** do RouterOS o `?` abre a ajuda e
quebra a linha. **Configure/edite o script pelo Winbox → System → Scripts (campo
Source), nunca colando no terminal.** Comandos de leitura (`/log print`,
`/system script print`) podem colar no terminal (não têm `?`).

### 4. Destinos monitorados (nomes = sufixo das chaves)
`bras` 198.18.255.45 · `core` 198.18.255.0 · `google` 8.8.8.8 ·
`cloudflare` 1.1.1.1 · `facebook` 57.144.232.1. Geram `loss_<nome>` e
`rtt_<nome>`. Saúde: `cpu`, `mem_free`, `dns_ok`, `http_ok`.

## Alertas no Telegram (por métrica)

`app.py` roda um alerta por **cada métrica recebida**, com **detecção de borda**
(avisa 1× ao passar o limite, 1× ao normalizar — sem spam; a 1ª leitura só
registra, não notifica). Limites configuráveis no painel da aba Sonda:

| Família | Alerta quando | Padrão |
|---|---|---|
| Perda (`loss_*`) | ≥ limite | 20% |
| Latência (`rtt_*`) | ≥ limite | 150 ms |
| Jitter (`jitter_*`) | ≥ limite | 30 ms |
| CPU (`cpu`) | ≥ limite | 90% |
| Memória livre (`mem_free`) | ≤ limite | 10% |
| DNS/HTTP (`dns_ok`/`http_ok`) | = 0 (falha) | — |

Também alerta sonda offline (parou de enviar) e PPPoE up/down. Depende do
Telegram estar configurado na aba Telegram.

## Debug: inspecionar o que a VM está recebendo

O `telemetry.db` da VM é do **root** e a VM **não tem** `sqlite3` nem `sudo`.
Para ler ao vivo, use o Python do venv via SSH (Paramiko), abrindo o banco em
modo somente-leitura:

```python
# via Paramiko, remoto:
import sqlite3, json, collections
con = sqlite3.connect("file:/opt/grafana-telemetry/telemetry.db?mode=ro", uri=True)
c = con.cursor()
# última leitura + todas as chaves já recebidas:
print(c.execute("SELECT payload FROM probe_samples ORDER BY ts DESC LIMIT 1").fetchone())
```

Se "só aparece perda" (ou só parte), **quase nunca é a página** — é o script da
RB (ou o script errado sendo editado). Confirme primeiro o payload real no banco.

## Convenções

- Código e mensagens de UI em **português** (com acentuação correta).
- Não versionar segredos/infra: `telemetry.db`, `secret.key`, `.vscode/`,
  `*.local.md`, `.claude/` e exports de chat (`20YY-MM-DD-*.txt`) já estão no
  `.gitignore`.
- Ao mexer nas famílias de métrica, **atualize os dois lados** (`app.py` e
  `page_sonda.html`) pra não dessincronizar alerta × exibição.
- Ao mudar o script da RB no guia, lembre que ele é **copiado à mão** pelo
  usuário para o Winbox — mantenha simples e sem `as-value`.
