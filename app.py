import os
import sqlite3
import time
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from librouteros import connect
import socket
import concurrent.futures

app = Flask(__name__)

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

DB_FILE = os.path.join(BASE_DIR, 'telemetry.db')


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
                        CASE WHEN g.ip IS NOT NULL THEN 1 ELSE 0 END
                 FROM neighbor_seen s
                 JOIN routers r ON r.id = s.router_id
                 LEFT JOIN neighbor_names n ON n.router_id = s.router_id AND n.ip = s.ip
                 LEFT JOIN neighbor_ignored g ON g.router_id = s.router_id AND g.ip = s.ip
                 ORDER BY r.name, s.type, s.ip''')
    rows = [{"router_id": a, "router": b, "type": t, "ip": ip, "name": nm, "state": st, "up": up, "ignored": ig}
            for (a, b, t, ip, nm, st, up, ig) in c.fetchall()]
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


def _merge_registry(results):
    """Aplica nomes amigaveis, separa IP/interface do BFD e injeta vizinhos
    que sumiram (offline). Persiste tudo que foi visto para deteccao futura."""
    now = time.time()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    for r in results:
        rid = r.get("_id")
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

            # registra os presentes
            for e in entries:
                c.execute(
                    "INSERT INTO neighbor_seen (router_id, type, ip, last_seen, last_state, up) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(router_id, type, ip) DO UPDATE SET "
                    "last_seen=excluded.last_seen, last_state=excluded.last_state, up=excluded.up",
                    (rid, typ, e["ip"], now, e.get("state", ""), e.get("up", 0)),
                )

            # descobre os que sumiram -> offline
            c.execute("SELECT ip FROM neighbor_seen WHERE router_id=? AND type=?", (rid, typ))
            known = set(row[0] for row in c.fetchall())
            for ip in known - current_ips:
                c.execute(
                    "UPDATE neighbor_seen SET last_state='offline', up=0 WHERE router_id=? AND type=? AND ip=?",
                    (rid, typ, ip),
                )
                nm = names.get(ip, "")
                off = {"remote_ip": ip, "ip": ip, "state": "offline", "up": 0,
                       "name": nm, "label": nm if nm else ip, "offline": True,
                       "ignored": ip in ignored}
                if typ == "bfd":
                    off.update({"local_ip": "", "vizinho": ip, "interface": ""})
                entries.append(off)

    conn.commit()
    conn.close()


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

    _merge_registry(results)
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


if __name__ == '__main__':
    ip = get_local_ip()
    print(f"\n\n=== VM Portal rodando em: http://{ip}:8080 ===\n\n")
    app.run(host='0.0.0.0', port=8080)
