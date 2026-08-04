# 🛰️ Telemetry Portal — Monitoramento OSPF & BFD (MikroTik)

Portal web leve, auto-hospedado, para monitorar **vizinhanças OSPF** e **sessões BFD** de roteadores MikroTik (PEs). Ele coleta os dados direto da API do RouterOS e mostra tudo num painel bonito — **sem precisar de Zabbix**. Também expõe os dados para o **Grafana** (opcional).

Feito para rodar numa VM Debian/Ubuntu e ser usado por qualquer empresa/provedor.

---

## ✨ Recursos

- 📊 **Painel de status próprio** (aba Gráficos) — cards, disponibilidade (%), alarmes e tabelas **separadas por PE**, com auto-refresh. Não precisa de Grafana.
- 🔎 **Detecção automática de OFFLINE** — no MikroTik, quando um vizinho cai ele simplesmente *some*. O portal **lembra** cada vizinho já visto e marca como `OFFLINE` quando ele desaparece (com proteção contra falso-positivo se o PE inteiro ficar inacessível).
- 🏷️ **Nomes amigáveis** — troque o IP do vizinho por um nome (ex.: `PE-Core-SP`).
- 🙈 **Ignorar / Esquecer** — *ignorar* tira do painel mas mantém no registro; *esquecer* remove de vez.
- 📨 **Alertas no Telegram** — avisa no seu grupo **assim que** um vizinho cai e **quando volta** (com horário e há quanto tempo ficou fora). Inteligente: notifica **uma única vez** por transição (não a cada consulta). A coleta roda em segundo plano a cada 30s, então alerta mesmo com ninguém olhando. Guia de configuração do zero embutido na própria aba.
- ⏱️ **Tempo em cada estado** — o portal guarda desde quando cada vizinho está `online`/`offline` e mostra na tela, além do histórico de quedas e retornos.
- 📡 **Sondas de experiência (Active Probing)** — uma RB750Gr3 (ou qualquer equipamento) envia métricas de experiência do cliente (PPPoE, latência, perda de pacotes, DNS, jitter…) para o portal via HTTP. A aba **Sonda** mostra o status ao vivo com **gráficos de linha** e avisa no Telegram quando a sonda cai ou o PPPoE quebra. Ingestão protegida por **token** configurável.
- 🔀 **Auto-detecção de RouterOS v6 e v7** — o BFD é consultado por caminhos diferentes em cada versão; o portal descobre sozinho (sem checkbox).
- 👥 **Multiusuário** — login, criar/remover usuários, trocar a própria senha.
- 📜 **Log de auditoria** — registra quem adicionou/removeu/renomeou/ignorou cada coisa. Somente leitura (não pode ser apagado).
- 📈 **Integração com Grafana** (opcional) via plugin Infinity — dashboard pronto incluído.
- 🔌 **Endpoint Prometheus** (`/metrics`) também disponível.

---

## 🚀 Instalação rápida (Debian / Ubuntu)

Numa VM nova, com acesso à internet e à rede de gerência dos MikroTiks:

```bash
# 1) Clone o repositório
git clone https://github.com/Davicjc/telemetry-portal.git
cd telemetry-portal

# 2) Rode o instalador (instala tudo e sobe como serviço)
sudo bash install.sh
```

Pronto! Ao final o instalador mostra o endereço de acesso. Abra no navegador:

```
http://<IP-DA-VM>:8080
```

**Login inicial:** `admin` / `admin`  → troque a senha na aba **Usuários**.

> O serviço sobe sozinho no boot (systemd) e reinicia se cair.

---

## 🔧 Configuração no MikroTik (uma vez por roteador)

O portal acessa cada PE pela **API do RouterOS**. Em cada MikroTik:

1. Habilite o serviço de API:
   ```
   /ip service enable api
   ```
   (porta padrão **8728**)

2. Crie um usuário só-leitura para o monitoramento (recomendado):
   ```
   /user add name=telemetria password=UMA_SENHA_FORTE group=read
   ```

Depois, no portal (aba **Roteadores**), clique em adicionar e informe: **Nome**, **IP**, **Porta** (8728), **Usuário** e **Senha**. A versão (v6/v7) é detectada automaticamente.

---

## 📨 Alertas no Telegram

Na aba **Telegram** você recebe um aviso no seu grupo sempre que algo cai e quando volta. O passo a passo completo está dentro da própria aba, mas em resumo:

1. Crie um bot com o **@BotFather** (`/newbot`) e copie o **token**.
2. Crie um grupo, adicione o bot e envie qualquer mensagem nele.
3. Descubra o **Chat ID** abrindo `https://api.telegram.org/botSEU_TOKEN/getUpdates` (procure `"chat":{"id":-100...}`).
4. Cole **token** e **chat_id** na aba, marque **Ativar**, salve e clique em **Enviar mensagem de teste**.

O alerta é enviado **uma vez** na queda e **uma vez** no retorno (com horário e duração). A verificação roda sozinha a cada 30s.

---

## 📡 Sondas de experiência (Active Probing)

Além de monitorar o **Core** (OSPF/BFD via API), o portal recebe telemetria de **sondas** instaladas no cliente/ponta — tipicamente uma **RB750Gr3** que autentica via PPPoE e mede a experiência real (latência até o gateway/sede/DNS, perda de pacotes, jitter, throughput). Assim você separa "problema no circuito" de "problema na ponta".

Na aba **Sonda**:

1. Copie a **URL de ingestão** (`http://<IP-DA-VM>:8080/api/probe`) e o **token**.
2. Na RB, crie um script que coleta as métricas e envia via `/tool fetch` (modelo pronto na própria aba) e agende no `/system scheduler` (ex.: a cada 30s).
3. As leituras aparecem em segundos: **cards de status** (PPPoE, latências, perda…) e **gráficos de linha** ao longo do tempo.

A ingestão é um `POST` para `/api/probe`, tolerante a **JSON**, **form** ou **query string**. Campos: `probe` (nome da sonda), `token`, `pppoe` (`up`/`down`) e quaisquer **métricas numéricas** (ex.: `loss_bras`, `loss_core`, `latency_gateway_ms`). Exemplo simples:

```
/tool fetch keep-result=no http-method=post http-data="" \
  url="http://<IP-DA-VM>:8080/api/probe?token=SEU_TOKEN&probe=uberaba&pppoe=up&loss_bras=0&loss_core=0"
```

> **RouterOS v6:** o `/tool fetch` **não envia corpo** em POST — por isso mande tudo na **query string** (como acima). E **configure o script pelo Winbox → System → Scripts**, não pelo terminal: no terminal o caractere `?` abre a ajuda e quebra a colagem da URL.

A aba **Sonda** traz **KPIs** (sondas online, com perda, PPPoE down), **tiles de perda por destino** (coloridos), **gráficos de linha multidestino** e um botão para **apagar uma sonda ao vivo** que não está mais em uso.

**Alertas no Telegram** (uma vez por transição): a sonda **parou de enviar** / **voltou** (com tempo fora), o **PPPoE caiu** / **voltou**, e **perda de pacotes por destino** ao **passar** e ao **normalizar** o limite (%) configurável na aba. Se o token estiver ativo, POSTs sem o token correto são recusados (`401`).

---

## 🖥️ As abas do portal

| Aba | O que faz |
|-----|-----------|
| **📊 Gráficos** | Painel de status ao vivo, separado por PE. Auto-refresh a cada 10s. |
| **🖥️ Roteadores** | Cadastrar PEs e gerenciar vizinhos (nomear / ignorar / esquecer) e ver desde quando cada um está no estado atual. |
| **📡 Sonda** | Receber telemetria de sondas (RB750Gr3), com status ao vivo e gráficos de linha. Configura o token de ingestão e traz o passo a passo da RB. |
| **📨 Telegram** | Configurar o bot e o grupo de alertas, testar o envio, e ver o histórico de quedas/retornos. Traz um passo a passo completo de como criar o bot. |
| **👥 Usuários** | Trocar a própria senha, criar e remover usuários. |
| **📜 Logs** | Histórico de auditoria (somente leitura). |

---

## 📈 Integração com Grafana (opcional)

O portal já tem seus próprios painéis, mas se quiser usar o Grafana há **dois dashboards** prontos na pasta `grafana/`:

| Arquivo | Dashboard |
|---------|-----------|
| `grafana/core-ospf-bfd.json` | 🛰️ **Core OSPF & BFD** — status das vizinhanças/sessões dos PEs |
| `grafana/sonda.json` | 📡 **Sonda (Active Probing)** — PPPoE e perda por destino, com série temporal |

Para cada um:

1. Instale o plugin **Infinity** (`yesoreyeram-infinity-datasource`) no Grafana.
2. Crie um **Data Source Infinity** e anote o **UID** dele.
3. Abra o `.json` e substitua:
   - `REPLACE_VM_IP` → o IP da sua VM (ex.: `10.0.0.5`)
   - `REPLACE_INFINITY_DATASOURCE_UID` → o UID do seu Data Source Infinity
4. No Grafana: **Dashboards → Import** → suba o arquivo.

Os endpoints usados pelo Grafana (e pelos painéis próprios):

| Endpoint | Conteúdo |
|----------|----------|
| `/api/summary` | Totais e disponibilidade (%) — core |
| `/api/ospf` | Vizinhos OSPF (achatado) |
| `/api/bfd` | Sessões BFD (achatado, com interface) |
| `/api/alarms` | Somente o que está fora do ar |
| `/api/routers` | Lista de PEs |
| `/api/grafana` | JSON completo (aninhado) |
| `/api/probe/grafana` | Sondas: última leitura achatada por sonda |
| `/api/probe/grafana/series?probe=X` | Sondas: série temporal (perda por destino) |
| `/metrics` | Formato Prometheus |

---

## 🛠️ Comandos úteis (serviço)

```bash
systemctl status telemetry-portal      # ver status
systemctl restart telemetry-portal     # reiniciar
journalctl -u telemetry-portal -f      # ver logs em tempo real
```

---

## 🔐 Segurança

- **Troque a senha do `admin`** logo no primeiro acesso (aba Usuários).
- A **chave de sessão** é gerada automaticamente e salva em `secret.key` (não versionado).
- O banco `telemetry.db` guarda usuários e senhas de API dos roteadores — ele **não** é versionado (está no `.gitignore`). Faça backup dele se quiser.
- Recomenda-se manter o portal numa **rede de gerência** e/ou atrás de um proxy/HTTPS.

---

## 📂 Estrutura

```
telemetry-portal/
├── app.py                 # Aplicação Flask (backend + API)
├── requirements.txt       # Dependências Python
├── install.sh             # Instalador automático
├── telemetry.service      # Unit do systemd (template)
├── templates/             # Páginas (login + abas)
└── grafana/
    ├── core-ospf-bfd.json # Dashboard Grafana: Core OSPF & BFD (opcional)
    └── sonda.json         # Dashboard Grafana: Sonda / Active Probing (opcional)
```

Arquivos gerados no primeiro run (não versionados): `telemetry.db`, `secret.key`, `venv/`.

---

## ⚙️ Rodando manualmente (desenvolvimento)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py         # sobe em http://0.0.0.0:8080
```

---

## 📜 Licença

MIT — veja [LICENSE](LICENSE). Use à vontade, inclusive comercialmente.
