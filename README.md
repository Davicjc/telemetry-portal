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

## 🖥️ As abas do portal

| Aba | O que faz |
|-----|-----------|
| **📊 Gráficos** | Painel de status ao vivo, separado por PE. Auto-refresh a cada 10s. |
| **🖥️ Roteadores** | Cadastrar PEs e gerenciar vizinhos (nomear / ignorar / esquecer) e ver desde quando cada um está no estado atual. |
| **📨 Telegram** | Configurar o bot e o grupo de alertas, testar o envio, e ver o histórico de quedas/retornos. Traz um passo a passo completo de como criar o bot. |
| **👥 Usuários** | Trocar a própria senha, criar e remover usuários. |
| **📜 Logs** | Histórico de auditoria (somente leitura). |

---

## 📈 Integração com Grafana (opcional)

O portal já tem seu próprio painel, mas se quiser usar o Grafana:

1. Instale o plugin **Infinity** (`yesoreyeram-infinity-datasource`) no Grafana.
2. Crie um **Data Source Infinity** e anote o **UID** dele.
3. Abra `grafana/dashboard.json` deste repositório e substitua:
   - `REPLACE_VM_IP` → o IP da sua VM (ex.: `10.0.0.5`)
   - `REPLACE_INFINITY_DATASOURCE_UID` → o UID do seu Data Source Infinity
4. No Grafana: **Dashboards → Import** → suba o `dashboard.json`.

Os endpoints usados pelo Grafana (e pelo painel próprio):

| Endpoint | Conteúdo |
|----------|----------|
| `/api/summary` | Totais e disponibilidade (%) |
| `/api/ospf` | Vizinhos OSPF (achatado) |
| `/api/bfd` | Sessões BFD (achatado, com interface) |
| `/api/alarms` | Somente o que está fora do ar |
| `/api/routers` | Lista de PEs |
| `/api/grafana` | JSON completo (aninhado) |
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
    └── dashboard.json     # Dashboard do Grafana (opcional)
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
