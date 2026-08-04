import os
import sqlite3
import time
import json
import secrets
import threading
import urllib.request
import urllib.parse
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from librouteros import connect
import socket
import concurrent.futures

app = Flask(__name__)
# Recarrega os templates a cada requisicao (ajustes de tela valem sem restart)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

# Diretorio base do app (para funcionar independente de onde for iniciado)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Chave de sessao: gerada automaticamente e persistida em secret.key
# (o arquivo NAO deve ser versionado - veja o .gitignore)
_SECRET_FILE = os.path.join(BASE_DIR, 'secret.key')
if os.path.exists(_SECRET_FILE):
    with open(_SECRET_FILE, 'rb') as _f:
        app.secret_key = _f.read()
else:
    app.secret_key = os.urandom(32)
    with open(_SECRET_FILE, 'wb') as _f:
        _f.write(app.secret_key)

# Setup Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Caminho do banco (permite apontar para outro arquivo em testes via TELEMETRY_DB)
DB_FILE = os.environ.get('TELEMETRY_DB') or os.path.join(BASE_DIR, 'telemetry.db')


def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT UNIQUE, password TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS routers (id INTEGER PRIMARY KEY, name TEXT, ip TEXT, port INTEGER, username TEXT, password TEXT)''')
    # Registro de vizinhos: nome amigavel por (roteador, ip)
    c.execute('''CREATE TABLE IF NOT EXISTS neighbor_names (
        router_id INTEGER, ip TEXT, name TEXT,
        PRIMARY KEY (router_id, ip))''')
    # Registro de "ja visto" para detectar sumico (offline)
    c.execute('''CREATE TABLE IF NOT EXISTS neighbor_seen (
        router_id INTEGER, type TEXT, ip TEXT,
        last_seen REAL, last_state TEXT, up INTEGER,
        PRIMARY KEY (router_id, type, ip))''')
    # Vizinhos ignorados: seguem no portal, mas ocultos no Grafana
    c.execute('''CREATE TABLE IF NOT EXISTS neighbor_ignored (
        router_id INTEGER, ip TEXT,
        PRIMARY KEY (router_id, ip))''')
    # Log de auditoria (append-only: nao ha rota para apagar)
    c.execute('''CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY, ts TEXT, user TEXT, action TEXT, details TEXT)''')
    # Configuracoes simples (chave/valor) - usado pelo Telegram
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT)''')
    # Historico de eventos de estado (queda / restabelecimento)
    c.execute('''CREATE TABLE IF NOT EXISTS state_events (
        id INTEGER PRIMARY KEY, ts REAL, router TEXT, type TEXT, ip TEXT,
        name TEXT, new_state TEXT, up INTEGER, duration REAL)''')
    # Leituras recebidas das sondas (Active Probing - RB750Gr3 etc.)
    c.execute('''CREATE TABLE IF NOT EXISTS probe_samples (
        id INTEGER PRIMARY KEY, ts REAL, probe TEXT, payload TEXT)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_probe_samples_probe_ts
        ON probe_samples (probe, ts)''')
    # Estado atual de cada sonda (para detectar borda: offline/online, PPPoE)
    c.execute('''CREATE TABLE IF NOT EXISTS probe_status (
        probe TEXT PRIMARY KEY, last_ts REAL, online INTEGER,
        pppoe TEXT, since REAL)''')
    # Estado de alarme por metrica (para detectar borda de PERDA por destino)
    c.execute('''CREATE TABLE IF NOT EXISTS probe_metric_state (
        probe TEXT, metric TEXT, alarmed INTEGER, since REAL, last_value REAL,
        PRIMARY KEY (probe, metric))''')

    # Migracao: coluna 'since' (desde quando o vizinho esta no estado atual)
    cols = [row[1] for row in c.execute("PRAGMA table_info(neighbor_seen)").fetchall()]
    if 'since' not in cols:
        c.execute("ALTER TABLE neighbor_seen ADD COLUMN since REAL")

    # Create default admin if not exists
    c.execute("SELECT * FROM users WHERE username='admin'")
    if not c.fetchone():
        c.execute("INSERT INTO users (username, password) VALUES (?, ?)", ('admin', generate_password_hash('admin')))

    conn.commit()
    conn.close()


init_db()


class User(UserMixin):
    def __init__(self, id, username):
        self.id = id
        self.username = username


@login_manager.user_loader
def load_user(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, username FROM users WHERE id=?", (user_id,))
    user = c.fetchone()
    conn.close()
    if user:
        return User(user[0], user[1])
    return None


def log_action(action, details=''):
    """Registra uma acao no log de auditoria (append-only)."""
    try:
        user = current_user.username if getattr(current_user, 'is_authenticated', False) else 'sistema'
    except Exception:
        user = 'sistema'
    ts = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO audit_log (ts, user, action, details) VALUES (?, ?, ?, ?)",
              (ts, user, action, details))
    conn.commit()
    conn.close()


# --- CONFIGURACOES (settings) ---

def get_setting(key, default=''):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def set_setting(key, value):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    conn.commit()
    conn.close()


# --- TELEGRAM ---

def _fmt_duration(seconds):
    """Formata segundos em algo legivel: 45s, 3m 20s, 2h 5m, 1d 3h."""
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


def _tg_post(token, chat_id, text):
    """Envia uma mensagem via API do Telegram. Lanca excecao em falha."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(url, data=payload)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8", "ignore")


def send_telegram(text):
    """Envia usando a config salva. Silencioso: nunca derruba o coletor."""
    if get_setting('telegram_enabled', '0') != '1':
        return
    token = get_setting('telegram_token', '').strip()
    chat_id = get_setting('telegram_chat_id', '').strip()
    if not token or not chat_id:
        return
    try:
        _tg_post(token, chat_id, text)
    except Exception:
        pass


def _tg_send_async(text):
    """Dispara o envio em thread separada para nao travar a coleta/UI."""
    threading.Thread(target=send_telegram, args=(text,), daemon=True).start()


def _format_event(ev):
    """Monta a mensagem de queda/restabelecimento para o Telegram."""
    tipo = ev["type"].upper()
    quando = datetime.fromtimestamp(ev["ts"]).strftime('%d/%m/%Y %H:%M:%S')
    alvo = ev["label"]
    ip = ev["ip"]
    router = ev["router"]
    if ev["up"]:
        linhas = [
            f"🟢 <b>RESTABELECIDO</b> — {tipo}",
            f"PE: <b>{router}</b>",
            f"Alvo: {alvo} ({ip})",
            f"Quando: {quando}",
        ]
        if ev.get("duration"):
            linhas.append(f"Ficou fora por: {_fmt_duration(ev['duration'])}")
    else:
        estado = ev.get("state") or "down"
        linhas = [
            f"🔴 <b>QUEDA</b> — {tipo}",
            f"PE: <b>{router}</b>",
            f"Alvo: {alvo} ({ip})",
            f"Estado: {estado}",
            f"Quando: {quando}",
        ]
        if ev.get("duration"):
            linhas.append(f"Estava estável há: {_fmt_duration(ev['duration'])}")
    return "\n".join(linhas)


# --- SONDAS (ACTIVE PROBING) ---

# Chaves que nao sao metricas dentro do corpo recebido
_PROBE_RESERVED = {"probe", "token", "pppoe"}


def get_probe_token():
    """Token da sonda: gera e persiste automaticamente na primeira vez."""
    tok = get_setting('probe_token', '').strip()
    if not tok:
        tok = secrets.token_hex(16)
        set_setting('probe_token', tok)
    return tok


def _coerce_num(v):
    """Converte para float se parecer numero; senao retorna None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace('%', '').replace(',', '.')
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _parse_probe_request(req):
    """Aceita JSON no corpo, form ou query string. Retorna dict normalizado:
    {probe, token, pppoe, metrics{...}}. Tolerante ao /tool fetch do RouterOS."""
    data = {}
    if req.is_json:
        data = req.get_json(silent=True) or {}
    if not data and req.data:
        try:
            data = json.loads(req.data.decode('utf-8', 'ignore'))
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}

    # campos soltos (query + form) servem de fallback e complemento
    flat = {}
    flat.update(req.args.to_dict())
    flat.update(req.form.to_dict())

    token = (req.headers.get('X-Probe-Token')
             or data.get('token') or flat.get('token') or '').strip()
    probe = str(data.get('probe') or flat.get('probe') or 'sonda').strip() or 'sonda'
    pppoe = str(data.get('pppoe') or flat.get('pppoe') or '').strip().lower()

    metrics = {}
    raw_metrics = data.get('metrics')
    if isinstance(raw_metrics, dict):
        for k, v in raw_metrics.items():
            n = _coerce_num(v)
            if n is not None:
                metrics[str(k)] = n
    # quaisquer campos numericos soltos (nao reservados) tambem viram metricas
    for src in (data, flat):
        for k, v in src.items():
            if k in _PROBE_RESERVED or k == 'metrics':
                continue
            if k in metrics:
                continue
            n = _coerce_num(v)
            if n is not None:
                metrics[str(k)] = n

    return {"probe": probe, "token": token, "pppoe": pppoe, "metrics": metrics}


def _probe_offline_after():
    try:
        return max(15, int(float(get_setting('probe_offline_after', '120'))))
    except (ValueError, TypeError):
        return 120


def _probe_loss_threshold():
    """% de perda a partir da qual um destino e considerado em ALARME."""
    try:
        return max(1.0, float(get_setting('probe_loss_threshold', '20')))
    except (ValueError, TypeError):
        return 20.0


def _probe_thresholds():
    """Limites de alarme por familia de metrica (configuraveis no painel)."""
    def g(key, default):
        try:
            return float(get_setting(key, str(default)))
        except (ValueError, TypeError):
            return float(default)
    return {
        "loss": g('probe_loss_threshold', 20),
        "latency": g('probe_latency_threshold', 150),
        "jitter": g('probe_jitter_threshold', 30),
        "cpu": g('probe_cpu_threshold', 90),
        "memfree": g('probe_memfree_min', 10),
    }


# Familias de metrica -> rotulo e unidade (exibicao + alertas)
_FAMILY_LABEL = {"loss": "Perda", "latency": "Latencia", "jitter": "Jitter",
                 "cpu": "CPU", "memfree": "Memoria livre", "bool": "Teste"}
_FAMILY_UNIT = {"loss": "%", "latency": "ms", "jitter": "ms",
                "cpu": "%", "memfree": "%", "bool": ""}


def _metric_family(key):
    """Classifica a metrica numa familia. None = so informativa (sem alarme)."""
    if key.startswith('loss'):
        return 'loss'
    if key.startswith('rtt') or key.startswith('latency'):
        return 'latency'
    if key.startswith('jitter'):
        return 'jitter'
    if key.startswith('cpu'):
        return 'cpu'
    if key.startswith('mem_free') or key == 'memfree':
        return 'memfree'
    if key in ('dns_ok', 'http_ok') or key.startswith('up_') or key.startswith('reach'):
        return 'bool'
    return None


def _metric_is_bad(key, value, thr):
    """True/False se a metrica esta ruim; None se nao e alertavel."""
    fam = _metric_family(key)
    if fam is None or value is None:
        return None
    if fam == 'loss':
        return value >= thr['loss']
    if fam == 'latency':
        return value >= thr['latency']
    if fam == 'jitter':
        return value >= thr['jitter']
    if fam == 'cpu':
        return value >= thr['cpu']
    if fam == 'memfree':
        return value <= thr['memfree']
    if fam == 'bool':
        return value == 0
    return None


# Rotulos amigaveis para itens conhecidos (destinos e testes)
_PROBE_DEST_LABELS = {
    "loss_bras": "BRAS", "loss_core": "Core (Sede)", "loss_google": "Google",
    "loss_cloudflare": "Cloudflare", "loss_facebook": "Facebook", "loss_dns2": "DNS 2",
    "rtt_bras": "BRAS", "rtt_core": "Core (Sede)", "rtt_google": "Google",
    "rtt_cloudflare": "Cloudflare", "rtt_dns2": "DNS 2",
    "jitter_bras": "BRAS", "jitter_core": "Core (Sede)", "jitter_google": "Google",
    "jitter_cloudflare": "Cloudflare", "jitter_dns2": "DNS 2",
    "cpu": "CPU", "mem_free": "Memoria livre", "uptime_s": "Uptime",
    "dns_ok": "DNS", "http_ok": "HTTP",
}


def _probe_metric_label(metric):
    if metric in _PROBE_DEST_LABELS:
        return _PROBE_DEST_LABELS[metric]
    base = metric
    for pre in ("loss_", "rtt_", "latency_", "jitter_", "up_", "reach_"):
        if base.startswith(pre):
            base = base[len(pre):]
            break
    return base.replace("_", " ").upper()


def _fmt_probe_event(probe, kind, pppoe=None, downtime=None, dest=None, value=None,
                     family=None, unit=None):
    """Monta a mensagem de sonda para o Telegram (borda unica)."""
    quando = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
    fam = _FAMILY_LABEL.get(family, family or 'Metrica')
    unit = unit if unit is not None else _FAMILY_UNIT.get(family, '')
    if kind == 'metric':
        if family == 'bool':
            linhas = [
                f"🔴 <b>TESTE FALHOU</b> — Sonda",
                f"Sonda: <b>{probe}</b>",
                f"Teste: <b>{dest}</b>",
                f"Resultado: <b>FALHOU</b>",
                f"Quando: {quando}",
            ]
        else:
            linhas = [
                f"🔴 <b>{fam.upper()} FORA DO LIMITE</b> — Sonda",
                f"Sonda: <b>{probe}</b>",
                f"Item: <b>{dest}</b>",
                f"{fam}: <b>{value:.0f}{unit}</b>",
                f"Quando: {quando}",
            ]
    elif kind == 'metric_ok':
        if family == 'bool':
            linhas = [
                f"🟢 <b>TESTE OK</b> — Sonda",
                f"Sonda: <b>{probe}</b>",
                f"Teste: <b>{dest}</b>",
                f"Resultado: OK",
                f"Quando: {quando}",
            ]
        else:
            linhas = [
                f"🟢 <b>{fam.upper()} NORMALIZADO</b> — Sonda",
                f"Sonda: <b>{probe}</b>",
                f"Item: <b>{dest}</b>",
                f"{fam} agora: {value:.0f}{unit}",
                f"Quando: {quando}",
            ]
        if downtime:
            linhas.append(f"Ficou fora do limite por: {_fmt_duration(downtime)}")
    elif kind == 'offline':
        linhas = [
            f"🔴 <b>SONDA SEM DADOS</b>",
            f"Sonda: <b>{probe}</b>",
            f"Parou de enviar telemetria.",
            f"Quando: {quando}",
        ]
    elif kind == 'online':
        linhas = [
            f"🟢 <b>SONDA RESTABELECIDA</b>",
            f"Sonda: <b>{probe}</b>",
            f"Voltou a enviar telemetria.",
            f"Quando: {quando}",
        ]
        if downtime:
            linhas.append(f"Ficou fora por: {_fmt_duration(downtime)}")
    elif kind == 'pppoe_down':
        linhas = [
            f"🔴 <b>PPPoE CAIU</b> — Sonda",
            f"Sonda: <b>{probe}</b>",
            f"Sessao PPPoE: down",
            f"Quando: {quando}",
        ]
    elif kind == 'pppoe_up':
        linhas = [
            f"🟢 <b>PPPoE RESTABELECIDO</b> — Sonda",
            f"Sonda: <b>{probe}</b>",
            f"Sessao PPPoE: up",
            f"Quando: {quando}",
        ]
        if downtime:
            linhas.append(f"Ficou fora por: {_fmt_duration(downtime)}")
    else:
        linhas = [f"Sonda <b>{probe}</b>: {kind}"]
    return "\n".join(linhas)


_probe_lock = threading.Lock()


def _probe_ingest_update(probe, pppoe, metrics=None):
    """Atualiza o estado da sonda ao receber dados e detecta transicoes de
    borda para notificar UMA vez: offline<->online, PPPoE up<->down e
    PERDA por destino (loss_* cruzando o limite configurado)."""
    now = time.time()
    metrics = metrics or {}
    thr = _probe_thresholds()
    msgs = []
    with _probe_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        row = c.execute("SELECT online, pppoe, since FROM probe_status WHERE probe=?",
                        (probe,)).fetchone()
        if row is None:
            c.execute("INSERT INTO probe_status (probe, last_ts, online, pppoe, since) "
                      "VALUES (?, ?, 1, ?, ?)", (probe, now, pppoe, now))
        else:
            old_online, old_pppoe, old_since = row
            old_since = old_since or now
            # offline -> online
            if not old_online:
                msgs.append(_fmt_probe_event(probe, 'online', downtime=now - old_since))
                new_since = now
            else:
                new_since = old_since
            # transicao de PPPoE (so quando ja conheciamos o estado anterior)
            if pppoe and old_pppoe and pppoe != old_pppoe:
                if pppoe == 'down':
                    msgs.append(_fmt_probe_event(probe, 'pppoe_down'))
                elif pppoe == 'up' and old_pppoe == 'down':
                    msgs.append(_fmt_probe_event(probe, 'pppoe_up'))
            c.execute("UPDATE probe_status SET last_ts=?, online=1, pppoe=?, since=? WHERE probe=?",
                      (now, pppoe or old_pppoe, new_since, probe))

        # --- alertas por metrica, qualquer familia (deteccao de borda) ---
        for metric, value in metrics.items():
            bad = _metric_is_bad(metric, value, thr)
            if bad is None:
                continue  # metrica so informativa (ex.: uptime) -> nao alerta
            fam = _metric_family(metric)
            prev = c.execute("SELECT alarmed, since FROM probe_metric_state WHERE probe=? AND metric=?",
                             (probe, metric)).fetchone()
            if prev is None:
                # primeira vez: so registra, sem notificar (evita spam no boot)
                c.execute("INSERT INTO probe_metric_state (probe, metric, alarmed, since, last_value) "
                          "VALUES (?, ?, ?, ?, ?)", (probe, metric, 1 if bad else 0, now, value))
            else:
                was, psince = prev
                psince = psince or now
                if bad and not was:
                    msgs.append(_fmt_probe_event(probe, 'metric',
                                dest=_probe_metric_label(metric), value=value, family=fam))
                    c.execute("UPDATE probe_metric_state SET alarmed=1, since=?, last_value=? "
                              "WHERE probe=? AND metric=?", (now, value, probe, metric))
                elif (not bad) and was:
                    msgs.append(_fmt_probe_event(probe, 'metric_ok',
                                dest=_probe_metric_label(metric), value=value, family=fam,
                                downtime=now - psince))
                    c.execute("UPDATE probe_metric_state SET alarmed=0, since=?, last_value=? "
                              "WHERE probe=? AND metric=?", (now, value, probe, metric))
                else:
                    c.execute("UPDATE probe_metric_state SET last_value=? "
                              "WHERE probe=? AND metric=?", (value, probe, metric))
        conn.commit()
        conn.close()
    for m in msgs:
        _tg_send_async(m)


def _check_probe_offline():
    """Chamado pelo poller: marca como offline (borda) as sondas que pararam
    de enviar dados alem do limite e avisa UMA vez no Telegram."""
    limite = _probe_offline_after()
    now = time.time()
    msgs = []
    with _probe_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        rows = c.execute("SELECT probe, last_ts FROM probe_status WHERE online=1").fetchall()
        for probe, last_ts in rows:
            if last_ts and (now - last_ts) > limite:
                c.execute("UPDATE probe_status SET online=0, since=? WHERE probe=?", (now, probe))
                msgs.append(_fmt_probe_event(probe, 'offline'))
        conn.commit()
        conn.close()
    for m in msgs:
        _tg_send_async(m)


# --- WEB UI ROUTES ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT id, username, password FROM users WHERE username=?", (username,))
        user = c.fetchone()
        conn.close()

        if user and check_password_hash(user[2], password):
            user_obj = User(user[0], user[1])
            login_user(user_obj)
            log_action('Entrou no sistema')
            return redirect(url_for('index'))
        else:
            flash('Credenciais invalidas.')

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


def get_neighbors_for_ui():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''SELECT s.router_id, r.name, s.type, s.ip, COALESCE(n.name, ''), s.last_state, s.up,
                        CASE WHEN g.ip IS NOT NULL THEN 1 ELSE 0 END, s.since
                 FROM neighbor_seen s
                 JOIN routers r ON r.id = s.router_id
                 LEFT JOIN neighbor_names n ON n.router_id = s.router_id AND n.ip = s.ip
                 LEFT JOIN neighbor_ignored g ON g.router_id = s.router_id AND g.ip = s.ip
                 ORDER BY r.name, s.type, s.ip''')
    now = time.time()
    rows = [{"router_id": a, "router": b, "type": t, "ip": ip, "name": nm, "state": st, "up": up, "ignored": ig,
             "since_txt": _fmt_duration(now - since) if since else "—"}
            for (a, b, t, ip, nm, st, up, ig, since) in c.fetchall()]
    conn.close()
    return rows


@app.route('/')
@login_required
def index():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, name, ip, port, username FROM routers")
    routers = c.fetchall()
    conn.close()
    return render_template('page_roteadores.html', active='roteadores',
                           routers=routers, neighbors=get_neighbors_for_ui())


@app.route('/graficos')
@login_required
def graficos():
    return render_template('page_graficos.html', active='graficos')


@app.route('/usuarios')
@login_required
def usuarios():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, username FROM users ORDER BY username")
    users = c.fetchall()
    conn.close()
    return render_template('page_usuarios.html', active='usuarios', users=users)


@app.route('/logs')
@login_required
def logs():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT ts, user, action, details FROM audit_log ORDER BY id DESC LIMIT 300")
    rows = c.fetchall()
    conn.close()
    return render_template('page_logs.html', active='logs', logs=rows)


@app.route('/telegram')
@login_required
def telegram():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT ts, router, type, ip, name, new_state, up, duration "
              "FROM state_events ORDER BY id DESC LIMIT 100")
    rows = c.fetchall()
    conn.close()
    eventos = []
    for (ts, router, typ, ip, name, new_state, up, dur) in rows:
        eventos.append({
            "quando": datetime.fromtimestamp(ts).strftime('%d/%m/%Y %H:%M:%S'),
            "router": router, "type": typ, "ip": ip,
            "alvo": name if name else ip,
            "state": new_state, "up": up,
            "duracao": _fmt_duration(dur) if dur else "—",
        })
    cfg = {
        "enabled": get_setting('telegram_enabled', '0') == '1',
        "token": get_setting('telegram_token', ''),
        "chat_id": get_setting('telegram_chat_id', ''),
    }
    return render_template('page_telegram.html', active='telegram', cfg=cfg, eventos=eventos)


@app.route('/telegram/save', methods=['POST'])
@login_required
def telegram_save():
    token = request.form.get('token', '').strip()
    chat_id = request.form.get('chat_id', '').strip()
    enabled = '1' if request.form.get('enabled') else '0'
    set_setting('telegram_token', token)
    set_setting('telegram_chat_id', chat_id)
    set_setting('telegram_enabled', enabled)
    log_action('Salvou config do Telegram',
               f'ativo={enabled}, chat_id={chat_id or "(vazio)"}')
    flash('Configuração do Telegram salva.')
    return redirect(url_for('telegram'))


@app.route('/telegram/test', methods=['POST'])
@login_required
def telegram_test():
    token = get_setting('telegram_token', '').strip()
    chat_id = get_setting('telegram_chat_id', '').strip()
    if not token or not chat_id:
        flash('Preencha e salve o token e o chat_id antes de testar.')
        return redirect(url_for('telegram'))
    quando = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
    msg = ("✅ <b>Telemetry Portal</b>\n"
           "Mensagem de teste — o bot está conectado a este grupo.\n"
           f"Enviado em: {quando}")
    try:
        _tg_post(token, chat_id, msg)
        log_action('Testou o Telegram', 'sucesso')
        flash('Mensagem de teste enviada! Confira o grupo no Telegram.')
    except Exception as e:
        log_action('Testou o Telegram', f'falha: {e}')
        flash(f'Falha ao enviar: {e}')
    return redirect(url_for('telegram'))


# --- SONDAS: PAGINA E INGESTAO ---

@app.route('/sonda')
@login_required
def sonda():
    thr = _probe_thresholds()
    cfg = {
        "token": get_probe_token(),
        "auth_enabled": get_setting('probe_auth_enabled', '1') == '1',
        "offline_after": _probe_offline_after(),
        "loss_threshold": int(thr["loss"]),
        "latency_threshold": int(thr["latency"]),
        "jitter_threshold": int(thr["jitter"]),
        "cpu_threshold": int(thr["cpu"]),
        "memfree_min": int(thr["memfree"]),
        "ingest_url": f"http://{get_local_ip()}:8080/api/probe",
    }
    return render_template('page_sonda.html', active='sonda', cfg=cfg)


@app.route('/sonda/save', methods=['POST'])
@login_required
def sonda_save():
    auth_enabled = '1' if request.form.get('auth_enabled') else '0'
    set_setting('probe_auth_enabled', auth_enabled)
    token = request.form.get('token', '').strip()
    if token:
        set_setting('probe_token', token)
    try:
        after = max(15, int(float(request.form.get('offline_after', '120'))))
    except (ValueError, TypeError):
        after = 120
    set_setting('probe_offline_after', str(after))
    def _save_num(field, key, default, lo=1):
        try:
            v = max(lo, int(float(request.form.get(field, str(default)))))
        except (ValueError, TypeError):
            v = default
        set_setting(key, str(v))
        return v
    loss_thr = _save_num('loss_threshold', 'probe_loss_threshold', 20)
    lat_thr = _save_num('latency_threshold', 'probe_latency_threshold', 150)
    jit_thr = _save_num('jitter_threshold', 'probe_jitter_threshold', 30)
    cpu_thr = _save_num('cpu_threshold', 'probe_cpu_threshold', 90)
    mem_min = _save_num('memfree_min', 'probe_memfree_min', 10)
    log_action('Salvou config da Sonda',
               f'auth={auth_enabled}, offline={after}s, loss>={loss_thr}%, '
               f'lat>={lat_thr}ms, jitter>={jit_thr}ms, cpu>={cpu_thr}%, memfree<={mem_min}%')
    flash('Configuração da Sonda salva.')
    return redirect(url_for('sonda'))


@app.route('/sonda/forget', methods=['POST'])
@login_required
def sonda_forget():
    """Apaga uma sonda ao vivo: leituras, estado e alarmes."""
    probe = request.form.get('probe', '').strip()
    if not probe:
        return jsonify({"ok": False, "error": "probe vazio"}), 400
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM probe_samples WHERE probe=?", (probe,))
    c.execute("DELETE FROM probe_status WHERE probe=?", (probe,))
    c.execute("DELETE FROM probe_metric_state WHERE probe=?", (probe,))
    conn.commit()
    conn.close()
    log_action('Esqueceu sonda', probe)
    return jsonify({"ok": True, "probe": probe})


@app.route('/sonda/regenerate-token', methods=['POST'])
@login_required
def sonda_regenerate_token():
    novo = secrets.token_hex(16)
    set_setting('probe_token', novo)
    log_action('Regenerou o token da Sonda')
    flash('Novo token gerado. Atualize o token na configuração da RB750Gr3.')
    return redirect(url_for('sonda'))


@app.route('/api/probe', methods=['POST', 'GET'])
def api_probe_ingest():
    """Recebe a telemetria das sondas (Active Probing). Sem login: autentica
    por token configuravel no painel. Tolerante a JSON, form ou query."""
    p = _parse_probe_request(request)

    if get_setting('probe_auth_enabled', '1') == '1':
        if p["token"] != get_probe_token():
            return jsonify({"ok": False, "error": "token invalido"}), 401

    now = time.time()
    payload = {"probe": p["probe"], "pppoe": p["pppoe"], "metrics": p["metrics"]}
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO probe_samples (ts, probe, payload) VALUES (?, ?, ?)",
              (now, p["probe"], json.dumps(payload)))
    # retencao: mantem 2 dias
    c.execute("DELETE FROM probe_samples WHERE ts < ?", (now - 2 * 86400,))
    conn.commit()
    conn.close()

    # detecta borda (offline / PPPoE / perda por destino) e notifica uma vez
    _probe_ingest_update(p["probe"], p["pppoe"], p["metrics"])

    return jsonify({"ok": True, "probe": p["probe"], "metrics": len(p["metrics"])})


@app.route('/api/probe/samples')
@login_required
def api_probe_samples():
    """Alimenta os cards e graficos da aba Sonda (com auto-refresh no front)."""
    limite = _probe_offline_after()
    thr = _probe_thresholds()
    now = time.time()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    probes = [r[0] for r in c.execute(
        "SELECT DISTINCT probe FROM probe_samples ORDER BY probe").fetchall()]
    status = {r[0]: r for r in c.execute(
        "SELECT probe, last_ts, online, pppoe FROM probe_status").fetchall()}
    counts = {r[0]: r[1] for r in c.execute(
        "SELECT probe, COUNT(*) FROM probe_samples GROUP BY probe").fetchall()}

    out = []
    for probe in probes:
        rows = c.execute(
            "SELECT ts, payload FROM probe_samples WHERE probe=? ORDER BY ts DESC LIMIT 300",
            (probe,)).fetchall()
        rows = list(reversed(rows))  # ordem cronologica
        samples = []
        allkeys = set()
        for ts, payload in rows:
            try:
                d = json.loads(payload)
            except Exception:
                continue
            m = d.get("metrics") or {}
            samples.append((ts, d.get("pppoe", "") or "", m))
            allkeys.update(m.keys())
        # series alinhadas (None quando a metrica nao veio naquela leitura)
        series = {"ts": [s[0] for s in samples]}
        for k in allkeys:
            series[k] = [s[2].get(k) for s in samples]
        last_metrics = samples[-1][2] if samples else {}
        last_pppoe = samples[-1][1] if samples else ''

        st = status.get(probe)
        last_ts = st[1] if st else (rows[-1][0] if rows else 0)
        age = now - last_ts if last_ts else None
        online = bool(age is not None and age <= limite)
        # itens em alarme agora (qualquer familia fora do limite na ultima leitura)
        alarms = sorted(k for k, v in last_metrics.items()
                        if _metric_is_bad(k, v, thr) is True)
        out.append({
            "probe": probe,
            "online": online,
            "pppoe": st[3] if st else last_pppoe,
            "last_ts": last_ts,
            "age_s": round(age) if age is not None else None,
            "samples": counts.get(probe, len(rows)),
            "alarms": alarms,
            "last": last_metrics,
            "series": series,
        })
    conn.close()
    return jsonify({
        "offline_after": limite,
        "loss_threshold": int(thr["loss"]),
        "thresholds": {k: int(v) for k, v in thr.items()},
        "probes": out,
    })


def _probe_latest(c):
    """Ultima leitura de cada sonda: {probe: (ts, pppoe, metrics)}."""
    out = {}
    for (probe,) in c.execute("SELECT DISTINCT probe FROM probe_samples").fetchall():
        row = c.execute("SELECT ts, payload FROM probe_samples WHERE probe=? ORDER BY ts DESC LIMIT 1",
                        (probe,)).fetchone()
        if not row:
            continue
        try:
            d = json.loads(row[1])
        except Exception:
            d = {}
        out[probe] = (row[0], d.get("pppoe", "") or "", d.get("metrics") or {})
    return out


@app.route('/api/probe/grafana')
def api_probe_grafana():
    """PUBLICO (Grafana): ultima leitura achatada por sonda."""
    limite = _probe_offline_after()
    thr = _probe_thresholds()
    now = time.time()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    status = {r[0]: r for r in c.execute(
        "SELECT probe, last_ts, online, pppoe FROM probe_status").fetchall()}
    latest = _probe_latest(c)
    conn.close()
    rows = []
    for probe, (ts, pppoe, metrics) in latest.items():
        st = status.get(probe)
        last_ts = (st[1] if st else ts) or ts
        age = now - last_ts if last_ts else 0
        online = 1 if age <= limite else 0
        losses = [v for k, v in metrics.items() if k.startswith('loss') and v is not None]
        worst = max(losses) if losses else 0
        bad_items = [k for k, v in metrics.items() if _metric_is_bad(k, v, thr) is True]
        row = {
            "probe": probe,
            "pppoe": (st[3] if st and st[3] else pppoe) or "n/d",
            "online": online,
            "status": "Online" if online else "Offline",
            "age_s": round(age),
            "worst_loss": round(worst, 1),
            "alarm": 1 if bad_items else 0,
            "alarm_count": len(bad_items),
        }
        for k, v in metrics.items():
            row[k] = v
        rows.append(row)
    rows.sort(key=lambda r: r["probe"])
    return jsonify(rows)


@app.route('/api/probe/grafana/series')
def api_probe_grafana_series():
    """PUBLICO (Grafana): serie temporal (formato largo) de uma sonda.
    Colunas = time (epoch ms) + rotulo de cada item. ?probe=NOME e ?type=loss|latency|jitter."""
    probe = (request.args.get('probe') or '').strip()
    fam = (request.args.get('type') or 'loss').strip().lower()
    if fam not in ('loss', 'latency', 'jitter', 'health'):
        fam = 'loss'
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if not probe or probe in ('All', '$__all', '${SONDA}'):
        r = c.execute("SELECT probe FROM probe_samples ORDER BY ts DESC LIMIT 1").fetchone()
        probe = r[0] if r else ''
    rows = c.execute("SELECT ts, payload FROM probe_samples WHERE probe=? ORDER BY ts DESC LIMIT 300",
                     (probe,)).fetchall()
    conn.close()
    out = []
    for ts, payload in reversed(rows):
        try:
            d = json.loads(payload)
        except Exception:
            continue
        point = {"time": int(ts * 1000)}
        metrics = d.get("metrics") or {}
        if fam == 'health':
            if metrics.get('cpu') is not None:
                point["CPU"] = metrics['cpu']
            if metrics.get('mem_free') is not None:
                point["Mem livre"] = metrics['mem_free']
        else:
            for k, v in metrics.items():
                if v is not None and _metric_family(k) == fam:
                    point[_probe_metric_label(k)] = v
        out.append(point)
    return jsonify(out)


@app.route('/router/add', methods=['POST'])
@login_required
def add_router():
    name = request.form['name']
    ip = request.form['ip']
    port = request.form['port']
    username = request.form['username']
    password = request.form['password']

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO routers (name, ip, port, username, password) VALUES (?, ?, ?, ?, ?)",
              (name, ip, port, username, password))
    conn.commit()
    conn.close()
    log_action('Adicionou PE', f'{name} ({ip}:{port})')
    return redirect(url_for('index'))


@app.route('/router/delete/<int:id>')
@login_required
def delete_router(id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    row = c.execute("SELECT name, ip FROM routers WHERE id=?", (id,)).fetchone()
    c.execute("DELETE FROM routers WHERE id=?", (id,))
    # limpa o registro de vizinhos daquele roteador
    c.execute("DELETE FROM neighbor_seen WHERE router_id=?", (id,))
    c.execute("DELETE FROM neighbor_names WHERE router_id=?", (id,))
    c.execute("DELETE FROM neighbor_ignored WHERE router_id=?", (id,))
    conn.commit()
    conn.close()
    log_action('Removeu PE', f'{row[0]} ({row[1]})' if row else f'id {id}')
    return redirect(url_for('index'))


@app.route('/neighbor/rename', methods=['POST'])
@login_required
def neighbor_rename():
    rid = request.form['router_id']
    ip = request.form['ip']
    name = request.form.get('name', '').strip()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if name:
        c.execute("INSERT INTO neighbor_names (router_id, ip, name) VALUES (?, ?, ?) "
                  "ON CONFLICT(router_id, ip) DO UPDATE SET name=excluded.name", (rid, ip, name))
    else:
        c.execute("DELETE FROM neighbor_names WHERE router_id=? AND ip=?", (rid, ip))
    conn.commit()
    conn.close()
    log_action('Renomeou vizinho', f'{ip} -> "{name}"' if name else f'{ip} (nome removido)')
    return redirect(url_for('index'))


@app.route('/neighbor/forget/<int:router_id>/<path:ip>')
@login_required
def neighbor_forget(router_id, ip):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM neighbor_seen WHERE router_id=? AND ip=?", (router_id, ip))
    c.execute("DELETE FROM neighbor_names WHERE router_id=? AND ip=?", (router_id, ip))
    c.execute("DELETE FROM neighbor_ignored WHERE router_id=? AND ip=?", (router_id, ip))
    conn.commit()
    conn.close()
    log_action('Esqueceu vizinho', ip)
    return redirect(url_for('index'))


@app.route('/neighbor/ignore/<int:router_id>/<path:ip>')
@login_required
def neighbor_ignore(router_id, ip):
    # Alterna: ignorado <-> visivel. Ignorado some do Grafana, mas segue no portal.
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM neighbor_ignored WHERE router_id=? AND ip=?", (router_id, ip))
    if c.fetchone():
        c.execute("DELETE FROM neighbor_ignored WHERE router_id=? AND ip=?", (router_id, ip))
        acao = 'Reexibiu vizinho no Grafana'
    else:
        c.execute("INSERT OR IGNORE INTO neighbor_ignored (router_id, ip) VALUES (?, ?)", (router_id, ip))
        acao = 'Ignorou vizinho (oculto no Grafana)'
    conn.commit()
    conn.close()
    log_action(acao, ip)
    return redirect(url_for('index'))


# --- USUARIOS E CONTA ---

@app.route('/user/add', methods=['POST'])
@login_required
def user_add():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    if not username or not password:
        flash('Usuario e senha sao obrigatorios.')
        return redirect(url_for('usuarios'))
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    ok = False
    try:
        c.execute("INSERT INTO users (username, password) VALUES (?, ?)",
                  (username, generate_password_hash(password)))
        conn.commit()
        ok = True
    except sqlite3.IntegrityError:
        ok = False
    conn.close()
    if ok:
        log_action('Criou usuario', username)
        flash(f'Usuario "{username}" criado.')
    else:
        flash(f'Usuario "{username}" ja existe.')
    return redirect(url_for('usuarios'))


@app.route('/account/password', methods=['POST'])
@login_required
def account_password():
    cur = request.form.get('current_password', '')
    new = request.form.get('new_password', '')
    if not new:
        flash('Informe a nova senha.')
        return redirect(url_for('usuarios'))
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    row = c.execute("SELECT password FROM users WHERE id=?", (current_user.id,)).fetchone()
    if row and check_password_hash(row[0], cur):
        c.execute("UPDATE users SET password=? WHERE id=?",
                  (generate_password_hash(new), current_user.id))
        conn.commit()
        conn.close()
        log_action('Alterou a propria senha')
        flash('Senha alterada com sucesso.')
    else:
        conn.close()
        flash('Senha atual incorreta.')
    return redirect(url_for('usuarios'))


@app.route('/user/delete/<int:id>')
@login_required
def user_delete(id):
    if id == int(current_user.id):
        flash('Você não pode apagar o próprio usuário.')
        return redirect(url_for('usuarios'))
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    row = c.execute("SELECT username FROM users WHERE id=?", (id,)).fetchone()
    total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if not row:
        conn.close()
        flash('Usuario nao encontrado.')
        return redirect(url_for('usuarios'))
    if total <= 1:
        conn.close()
        flash('Nao e possivel apagar o ultimo usuario.')
        return redirect(url_for('usuarios'))
    c.execute("DELETE FROM users WHERE id=?", (id,))
    conn.commit()
    conn.close()
    log_action('Removeu usuario', row[0])
    flash(f'Usuario "{row[0]}" removido.')
    return redirect(url_for('usuarios'))


# --- METRICS LOGIC ---

def _clean_ip(s):
    return (s or '').split('%', 1)[0]


def _ros_major(api):
    """Detecta a versao maior do RouterOS (6 ou 7) via /system/resource."""
    try:
        res = list(api('/system/resource/print'))
        ver = res[0].get('version', '') if res else ''
        major = int(ver.split('.')[0]) if ver and ver[0].isdigit() else 7
        return major, ver
    except Exception:
        return 7, ''


def _bfd_from_v7(api):
    out = []
    for s in api('/routing/bfd/session/print'):
        state = s.get('state', 'down')
        out.append({
            "remote_ip": s.get('remote-address', ''),
            "local_ip": s.get('local-address', ''),
            "state": state,
            "up": 1 if state == 'up' else 0,
        })
    return out


def _bfd_from_v6(api):
    out = []
    for s in api('/routing/bfd/neighbor/print'):
        state = s.get('state', 'down')
        addr = s.get('address', '')
        iface = s.get('interface', '')
        # imita o formato v7 (ip%interface) para o resto do codigo tratar igual
        remote = addr + ('%' + iface if iface else '')
        up = 1 if (state == 'up' or s.get('up') is True) else 0
        out.append({
            "remote_ip": remote,
            "local_ip": "",
            "state": state,
            "up": up,
        })
    return out


def _fetch_bfd(api, major):
    # escolhe pela versao detectada, mas com fallback automatico (self-healing)
    order = [_bfd_from_v6, _bfd_from_v7] if major <= 6 else [_bfd_from_v7, _bfd_from_v6]
    for fn in order:
        try:
            return fn(api)
        except Exception:
            continue
    return []


def fetch_router_data(router):
    r_id, r_name, r_ip, r_port, r_user, r_pwd = router
    data = {"_id": r_id, "router": r_name, "ip": r_ip, "ospf": [], "bfd": [], "error": None, "version": ""}
    try:
        api = connect(username=r_user, password=r_pwd, host=r_ip, port=int(r_port))

        major, ver = _ros_major(api)
        data["version"] = ver

        # OSPF (identico em v6 e v7)
        try:
            for n in api('/routing/ospf/neighbor/print'):
                state = n.get('state', 'down')
                data["ospf"].append({
                    "remote_ip": n.get('address', ''),
                    "state": state,
                    "up": 1 if state == 'Full' else 0
                })
        except Exception:
            pass

        # BFD (v6 usa /routing/bfd/neighbor, v7 usa /routing/bfd/session)
        data["bfd"] = _fetch_bfd(api, major)

    except Exception as e:
        data["error"] = str(e)

    return data


_merge_lock = threading.Lock()


def _merge_registry(results):
    """Aplica nomes amigaveis, separa IP/interface do BFD e injeta vizinhos
    que sumiram (offline). Persiste tudo que foi visto para deteccao futura.

    Detecta TRANSICOES de estado (borda): um vizinho so gera evento quando
    realmente muda up<->down. Retorna a lista de eventos para notificacao.
    """
    now = time.time()
    events = []
    with _merge_lock:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()

        for r in results:
            rid = r.get("_id")
            rname = r.get("router", "")
            c.execute("SELECT ip, name FROM neighbor_names WHERE router_id=?", (rid,))
            names = {row[0]: row[1] for row in c.fetchall()}
            c.execute("SELECT ip FROM neighbor_ignored WHERE router_id=?", (rid,))
            ignored = set(row[0] for row in c.fetchall())

            for typ in ("ospf", "bfd"):
                entries = r.get(typ, [])

                # normaliza os presentes
                for e in entries:
                    ip = _clean_ip(e.get("remote_ip", ""))
                    nm = names.get(ip, "")
                    e["ip"] = ip
                    e["name"] = nm
                    e["label"] = nm if nm else ip
                    e["offline"] = False
                    e["ignored"] = ip in ignored
                    if typ == "bfd":
                        rem = e.get("remote_ip", "") or ""
                        e["vizinho"] = ip
                        e["interface"] = rem.split("%", 1)[1] if "%" in rem else ""

                # se o PE falhou na coleta, NAO mexe no registro (evita falso offline)
                if r.get("error"):
                    continue

                current_ips = set(e["ip"] for e in entries)

                # estado anterior de todos os conhecidos deste tipo
                c.execute("SELECT ip, up, since FROM neighbor_seen WHERE router_id=? AND type=?", (rid, typ))
                prev = {row[0]: (row[1], row[2]) for row in c.fetchall()}

                # registra os presentes + detecta transicao
                for e in entries:
                    ip = e["ip"]
                    new_up = e.get("up", 0)
                    new_state = e.get("state", "")
                    old = prev.get(ip)
                    if old is None:
                        # primeira vez que vemos: registra sem notificar
                        since = now
                    else:
                        old_up, old_since = old
                        old_since = old_since or now
                        if (old_up or 0) != new_up:
                            # TRANSICAO (borda) -> gera evento
                            events.append(_mk_event(now, rname, typ, ip, names.get(ip, ""),
                                                    new_up, new_state, old_since))
                            since = now
                        else:
                            since = old_since  # mantem desde quando esta assim

                    c.execute(
                        "INSERT INTO neighbor_seen (router_id, type, ip, last_seen, last_state, up, since) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(router_id, type, ip) DO UPDATE SET "
                        "last_seen=excluded.last_seen, last_state=excluded.last_state, "
                        "up=excluded.up, since=excluded.since",
                        (rid, typ, ip, now, new_state, new_up, since),
                    )

                # descobre os que sumiram -> offline
                for ip, (old_up, old_since) in prev.items():
                    if ip in current_ips:
                        continue
                    old_since = old_since or now
                    if (old_up or 0) == 1:
                        # transicao up -> offline
                        events.append(_mk_event(now, rname, typ, ip, names.get(ip, ""),
                                                0, "offline", old_since))
                        c.execute(
                            "UPDATE neighbor_seen SET last_state='offline', up=0, since=? "
                            "WHERE router_id=? AND type=? AND ip=?",
                            (now, rid, typ, ip),
                        )
                    else:
                        # ja estava down/offline: mantem o since
                        c.execute(
                            "UPDATE neighbor_seen SET last_state='offline', up=0 "
                            "WHERE router_id=? AND type=? AND ip=?",
                            (rid, typ, ip),
                        )
                    nm = names.get(ip, "")
                    off = {"remote_ip": ip, "ip": ip, "state": "offline", "up": 0,
                           "name": nm, "label": nm if nm else ip, "offline": True,
                           "ignored": ip in ignored}
                    if typ == "bfd":
                        off.update({"local_ip": "", "vizinho": ip, "interface": ""})
                    entries.append(off)

        # grava o historico de eventos
        for ev in events:
            c.execute(
                "INSERT INTO state_events (ts, router, type, ip, name, new_state, up, duration) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ev["ts"], ev["router"], ev["type"], ev["ip"], ev["name"],
                 ev["state"], ev["up"], ev["duration"]),
            )

        conn.commit()
        conn.close()
    return events


def _mk_event(now, rname, typ, ip, nm, new_up, new_state, old_since):
    return {
        "ts": now, "router": rname, "type": typ, "ip": ip,
        "name": nm, "label": nm if nm else ip,
        "up": new_up, "state": new_state,
        "duration": max(0, now - old_since) if old_since else 0,
    }


def get_all_metrics():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, name, ip, port, username, password FROM routers")
    routers = c.fetchall()
    conn.close()

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch_router_data, r) for r in routers]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    events = _merge_registry(results)
    # Notifica no Telegram apenas as transicoes (uma vez por queda/subida)
    for ev in events:
        _tg_send_async(_format_event(ev))
    # Remove os ignorados APENAS da visao do Grafana (o portal usa o banco direto)
    for r in results:
        r["ospf"] = [e for e in r["ospf"] if not e.get("ignored")]
        r["bfd"] = [e for e in r["bfd"] if not e.get("ignored")]
    return results


# --- CACHE ---

_cache = {"ts": 0.0, "data": None}
_CACHE_TTL = 8  # segundos: evita marretar os Mikrotiks a cada painel


def get_all_metrics_cached():
    now = time.time()
    if _cache["data"] is not None and (now - _cache["ts"]) < _CACHE_TTL:
        return _cache["data"]
    data = get_all_metrics()
    _cache["ts"] = now
    _cache["data"] = data
    return data


# --- GRAFANA ENDPOINTS ---

@app.route('/api/grafana')
def api_grafana():
    return jsonify(get_all_metrics_cached())


@app.route('/api/routers')
def api_routers():
    return jsonify([{"router": r["router"]} for r in get_all_metrics_cached()])


@app.route('/api/ospf')
def api_ospf():
    rows = []
    for r in get_all_metrics_cached():
        for o in r["ospf"]:
            rows.append({
                "router": r["router"],
                "ip": o.get("ip", ""),
                "vizinho": o.get("label", ""),
                "name": o.get("name", ""),
                "state": o["state"],
                "up": o["up"],
            })
    return jsonify(rows)


@app.route('/api/bfd')
def api_bfd():
    rows = []
    for r in get_all_metrics_cached():
        for b in r["bfd"]:
            rows.append({
                "router": r["router"],
                "ip": b.get("ip", ""),
                "vizinho": b.get("label", ""),
                "name": b.get("name", ""),
                "interface": b.get("interface", ""),
                "state": b["state"],
                "up": b["up"],
            })
    return jsonify(rows)


@app.route('/api/summary')
def api_summary():
    data = get_all_metrics_cached()
    ospf_all = [o for r in data for o in r["ospf"]]
    bfd_all = [b for r in data for b in r["bfd"]]
    ospf_up = sum(o["up"] for o in ospf_all)
    bfd_up = sum(b["up"] for b in bfd_all)
    return jsonify([{
        "pes": len(data),
        "ospf_up": ospf_up,
        "ospf_down": len(ospf_all) - ospf_up,
        "bfd_up": bfd_up,
        "bfd_down": len(bfd_all) - bfd_up,
        "ospf_avail": round(ospf_up / len(ospf_all) * 100, 1) if ospf_all else 100,
        "bfd_avail": round(bfd_up / len(bfd_all) * 100, 1) if bfd_all else 100,
    }])


@app.route('/api/alarms')
def api_alarms():
    rows = []
    for r in get_all_metrics_cached():
        for o in r["ospf"]:
            if not o["up"]:
                rows.append({"router": r["router"], "tipo": "OSPF", "alvo": o.get("label", ""),
                             "ip": o.get("ip", ""), "nome": o.get("name", ""), "state": o["state"]})
        for b in r["bfd"]:
            if not b["up"]:
                rows.append({"router": r["router"], "tipo": "BFD", "alvo": b.get("label", ""),
                             "ip": b.get("ip", ""), "nome": b.get("name", ""), "state": b["state"]})
    return jsonify(rows)


@app.route('/metrics')
def prometheus_metrics():
    metrics_data = get_all_metrics_cached()

    lines = []
    lines.append("# HELP router_ospf_status OSPF neighbor status (1=Full, 0=Down/Offline)")
    lines.append("# TYPE router_ospf_status gauge")

    for r in metrics_data:
        if r["error"]:
            lines.append(f'router_up{{name="{r["router"]}",ip="{r["ip"]}"}} 0')
            continue

        lines.append(f'router_up{{name="{r["router"]}",ip="{r["ip"]}"}} 1')

        for o in r["ospf"]:
            lines.append(f'router_ospf_status{{router="{r["router"]}",remote_ip="{o.get("ip","")}",name="{o.get("name","")}"}} {o["up"]}')

    lines.append("# HELP router_bfd_status BFD session status (1=Up, 0=Down/Offline)")
    lines.append("# TYPE router_bfd_status gauge")

    for r in metrics_data:
        if r["error"]:
            continue
        for b in r["bfd"]:
            lines.append(f'router_bfd_status{{router="{r["router"]}",remote_ip="{b.get("ip","")}",name="{b.get("name","")}"}} {b["up"]}')

    return "\n".join(lines) + "\n", 200, {'Content-Type': 'text/plain; charset=utf-8'}


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP


# --- POLLER DE FUNDO ---
# Coleta os PEs periodicamente mesmo sem ninguem olhando o portal, para que as
# quedas/subidas sejam detectadas e notificadas no Telegram em tempo real.

_POLL_INTERVAL = 30  # segundos
_poller_started = False


def _start_poller():
    global _poller_started
    if _poller_started:
        return
    _poller_started = True

    def _loop():
        while True:
            try:
                get_all_metrics()      # detecta quedas/retornos de OSPF/BFD
                _check_probe_offline()  # detecta sondas que pararam de enviar
            except Exception:
                pass
            time.sleep(_POLL_INTERVAL)

    threading.Thread(target=_loop, daemon=True).start()


# Inicia o coletor de fundo JA no import, para que as quedas/retornos sejam
# detectados e notificados no Telegram independentemente de como o processo
# suba (python app.py, gunicorn/wsgi, etc.). Em testes, defina DISABLE_POLLER=1.
if not os.environ.get('DISABLE_POLLER'):
    _start_poller()


if __name__ == '__main__':
    ip = get_local_ip()
    print(f"\n\n=== VM Portal rodando em: http://{ip}:8080 ===\n\n")
    app.run(host='0.0.0.0', port=8080)
