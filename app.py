import os, sqlite3, zipfile, subprocess, signal, shutil, psutil, time, datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file
from werkzeug.utils import secure_filename
from flask_socketio import SocketIO, emit

# Global process tracker
running_procs = {}
start_times = {}

# Initialize SocketIO
socketio = SocketIO()

# Default single user ID for open system
GUEST_USER_ID = 1

def get_db():
    db_path = os.path.join(os.getcwd(), 'storage/nehost.db')
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    if not os.path.exists('storage'): 
        os.makedirs('storage')
    db = get_db()
    
    # Server Table Only (No user auth needed)
    db.execute('''CREATE TABLE IF NOT EXISTS servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        user_id INTEGER DEFAULT 1, name TEXT, folder TEXT, 
        status TEXT, startup TEXT, pid INTEGER,
        server_status TEXT DEFAULT 'active'
    )''')
    
    db.commit()
    db.close()

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'nehost_open_system_key'
    app.config['BASE_STORAGE'] = os.path.join(os.getcwd(), 'storage/instances')
    app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'static/uploads')
    
    if not os.path.exists(app.config['BASE_STORAGE']):
        os.makedirs(app.config['BASE_STORAGE'])
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        os.makedirs(app.config['UPLOAD_FOLDER'])
        
    init_db()
    socketio.init_app(app)

    def get_precise_uptime(start_timestamp):
        if not start_timestamp: return "Offline"
        diff = int(time.time() - start_timestamp)
        months, rem = divmod(diff, 2592000)
        days, rem = divmod(rem, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        
        parts = []
        if months > 0: parts.append(f"{months}mo")
        if days > 0: parts.append(f"{days}d")
        if hours > 0: parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)
    
    # --- DIRECT REDIRECT TO DASHBOARD ---
    @app.route('/')
    def home():
        return render_template('web/dashboard.html', user={'fname': 'Guest User', 'role': 'free'})

    # Redirect auth pages directly to dashboard (accept GET + POST so login/signup form submits don't 405)
    @app.route('/login', methods=['GET', 'POST'])
    @app.route('/signup', methods=['GET', 'POST'])
    @app.route('/admin-login', methods=['GET', 'POST'])
    @app.route('/admin/panel', methods=['GET', 'POST'])
    def disable_auth():
        return redirect(url_for('dashboard'))

    # Logout just sends the user back to the dashboard (no real auth/session in this open system)
    @app.route('/logout')
    def logout():
        return redirect(url_for('dashboard'))

    @app.route('/dashboard')
    def dashboard():
        return render_template('web/dashboard.html', user={'fname': 'Guest User', 'role': 'free'})

    @app.route('/api/announcement')
    def get_announcement():
        return jsonify({'show_popup': 0})

    # Fixed Safe Path Helper Function
    def safe_join(base, *paths):
        # Filter out empty paths or non-string values
        clean_paths = [p for p in paths if p and isinstance(p, str)]
        final_path = os.path.abspath(os.path.join(base, *clean_paths))
        base_path = os.path.abspath(base)
        
        # Ensure security against Directory Traversal attacks
        if not final_path.startswith(base_path):
            return None
        return final_path

    # File Manager Routes
    @app.route('/files/list/<folder>')
    def flist(folder):
        sub_path = request.args.get('path', '').strip('/')
        full_path = safe_join(app.config['BASE_STORAGE'], folder, sub_path)
        
        if not full_path or not os.path.exists(full_path): 
            return jsonify([])
            
        items = []
        try:
            for f in sorted(os.listdir(full_path)):
                if f == 'console.log': continue
                p = os.path.join(full_path, f)
                rel = os.path.join(sub_path, f) if sub_path else f
                items.append({
                    'name': f, 
                    'is_dir': os.path.isdir(p), 
                    'is_zip': f.lower().endswith('.zip'), 
                    'rel_path': rel
                })
        except Exception:
            pass
        return jsonify(items)

    @app.route('/files/content/<folder>/<path:name>', methods=['GET', 'POST'])
    @app.route('/files/content/<folder>/', methods=['GET', 'POST'])
    @app.route('/files/read/<folder>', methods=['GET', 'POST'])  # alias used by the file-manager UI
    def fcontent(folder, name=""):
        # Handle sub_path from request query or body
        sub_path = request.args.get('path', '') or (request.json.get('path', '') if request.is_json else '')
        sub_path = sub_path.strip('/')
        
        # Name can arrive as a URL segment (/files/content/...), a query string (/files/read/...?name=...),
        # or JSON body
        if not name:
            name = request.args.get('name', '')
        if not name and request.is_json:
            name = request.json.get('name', '')

        full_path = safe_join(app.config['BASE_STORAGE'], folder, sub_path, name)
        
        if not full_path or not os.path.exists(full_path):
            return jsonify({'content': 'Error: File not found'}), 404
            
        if os.path.isdir(full_path):
            return jsonify({'content': 'Error: Path is a directory'}), 400

        try:
            with open(full_path, 'r', encoding='utf-8', errors='ignore') as f: 
                return jsonify({'content': f.read()})
        except Exception as e: 
            return jsonify({'content': f'Error reading file: {str(e)}'}), 500

    @app.route('/files/save/<folder>/<path:name>', methods=['POST'])
    @app.route('/files/save/<folder>/', methods=['POST'])
    @app.route('/files/save/<folder>', methods=['POST'])  # exact match used by the UI — avoids a 308 redirect that breaks over HTTPS proxies (Railway/Render)
    def fsave(folder, name=""):
        d = request.json or {}
        sub_path = request.args.get('path', '') or d.get('path', '')
        sub_path = sub_path.strip('/')
        
        if not name:
            name = d.get('name', '')

        full_path = safe_join(app.config['BASE_STORAGE'], folder, sub_path, name)
        
        if not full_path:
            return jsonify({'status': 'error', 'msg': 'Access denied'}), 403
            
        try:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            content = d.get('content', '')
            with open(full_path, 'w', encoding='utf-8') as f: 
                f.write(content)
            return jsonify({'status': 'saved'})
        except Exception as e: 
            return jsonify({'status': 'error', 'msg': str(e)}), 500

    @app.route('/files/delete-bulk/<folder>', methods=['POST'])
    def delete_bulk(folder):
        d = request.json or {}
        sub_path = d.get('path', '').strip('/')
        names = d.get('names', [])
        base = safe_join(app.config['BASE_STORAGE'], folder, sub_path)
        
        if not base or not os.path.exists(base):
            return jsonify({'status': 'error'})

        if not names: 
            names = [f for f in os.listdir(base) if f != 'console.log']
            
        for name in names:
            p = os.path.join(base, name)
            if name == 'console.log': continue
            try:
                if os.path.isdir(p): shutil.rmtree(p)
                elif os.path.exists(p): os.remove(p)
            except: pass
        return jsonify({"status": "ok"})

    @app.route('/files/create-file/<folder>', methods=['POST'])
    def create_file(folder):
        d = request.json or {}
        sub_path = d.get('path', '').strip('/')
        file_name = d.get('name', '').strip()
        
        if not file_name:
            return jsonify({'status': 'error', 'msg': 'Filename required'})
            
        full_path = safe_join(app.config['BASE_STORAGE'], folder, sub_path, file_name)
        if not full_path:
            return jsonify({'status': 'error', 'msg': 'Invalid path'}), 400

        try:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            if not os.path.exists(full_path):
                with open(full_path, 'w', encoding='utf-8') as f: 
                    f.write("")
            return jsonify({'status': 'success'})
        except Exception as e:
            return jsonify({'status': 'error', 'msg': str(e)})

    @app.route('/files/create-folder/<folder>', methods=['POST'])
    def create_folder(folder):
        d = request.json or {}
        sub_path = d.get('path', '').strip('/')
        folder_name = d.get('name', '').strip()
        
        if not folder_name:
            return jsonify({'status': 'error', 'msg': 'Folder name required'})
            
        full_path = safe_join(app.config['BASE_STORAGE'], folder, sub_path, folder_name)
        if not full_path:
            return jsonify({'status': 'error', 'msg': 'Invalid path'}), 400

        try:
            os.makedirs(full_path, exist_ok=True)
            return jsonify({'status': 'success'})
        except Exception as e:
            return jsonify({'status': 'error', 'msg': str(e)})

    @app.route('/files/upload/<folder>', methods=['POST'])
    def upload_file(folder):
        sub_path = request.form.get('path', '').strip('/')
        file = request.files.get('file')
        if not file:
            return jsonify({'status': 'error', 'msg': 'No file uploaded'})
            
        dest = safe_join(app.config['BASE_STORAGE'], folder, sub_path)
        if not dest:
            return jsonify({'status': 'error', 'msg': 'Invalid folder path'}), 400
            
        os.makedirs(dest, exist_ok=True)
        file.save(os.path.join(dest, secure_filename(file.filename)))
        return jsonify({'status': 'success'})

    @app.route('/files/rename/<folder>', methods=['POST'])
    def rename_file(folder):
        d = request.json or {}
        sub_path = d.get('path', '').strip('/')
        base = safe_join(app.config['BASE_STORAGE'], folder, sub_path)
        
        if not base or not os.path.exists(base):
            return jsonify({'status': 'error', 'msg': 'Path not found'}), 404
            
        old_p = os.path.join(base, d.get('old', ''))
        new_p = os.path.join(base, d.get('new', ''))
        
        if os.path.exists(old_p):
            os.rename(old_p, new_p)
            return jsonify({'status': 'success'})
        return jsonify({'status': 'error', 'msg': 'File not found'}), 404

    @app.route('/files/download/<folder>/<path:name>')
    def download_file(folder, name):
        sub_path = request.args.get('path', '').strip('/')
        p = safe_join(app.config['BASE_STORAGE'], folder, sub_path, name)
        
        if not p or not os.path.isfile(p): 
            return "Access Denied or File Not Found", 403
            
        return send_file(p, as_attachment=True)

    @app.route('/files/zip-bulk/<folder>', methods=['POST'])
    def zip_bulk(folder):
        d = request.json or {}
        names = d.get('names', [])
        sub_path = d.get('path', '').strip('/')
        base = safe_join(app.config['BASE_STORAGE'], folder, sub_path)
        
        if not base or not os.path.exists(base):
            return jsonify({'status': 'error', 'msg': 'Invalid base path'})

        if not names: 
            names = [f for f in os.listdir(base) if f != 'console.log']
            
        zip_name = f"archive_{int(time.time())}.zip"
        zip_path = os.path.join(base, zip_name)
        
        with zipfile.ZipFile(zip_path, 'w') as z:
            for n in names:
                p = os.path.join(base, n)
                if n == zip_name: continue
                if os.path.isdir(p):
                    for root, dirs, files in os.walk(p):
                        for file in files:
                            full_p = os.path.join(root, file)
                            z.write(full_p, os.path.relpath(full_p, base))
                elif os.path.exists(p): 
                    z.write(p, n)
        return jsonify({'status': 'success', 'zip': zip_name})

    @app.route('/files/unzip/<folder>', methods=['POST'])
    def unzip_file(folder):
        d = request.json or {}
        zip_name = d.get('name')
        sub_path = d.get('path', '').strip('/')
        base = safe_join(app.config['BASE_STORAGE'], folder, sub_path)
        
        if not base:
            return jsonify({'status': 'error', 'msg': 'Invalid path'})

        zip_path = os.path.join(base, zip_name)
        
        if os.path.exists(zip_path) and zipfile.is_zipfile(zip_path):
            try:
                with zipfile.ZipFile(zip_path, 'r') as z:
                    z.extractall(base)
                return jsonify({'status': 'success'})
            except Exception as e:
                return jsonify({'status': 'error', 'msg': str(e)})
        return jsonify({'status': 'error', 'msg': 'Invalid zip file'})

    # Server Control Routes
    @app.route('/server/action/<folder>/<act>', methods=['POST'])
    def server_action(folder, act):
        db = get_db()
        path = safe_join(app.config['BASE_STORAGE'], folder)
        if not path or not os.path.exists(path):
            db.close()
            return jsonify({'status': 'error', 'msg': 'Instance path does not exist'})

        log_file_path = os.path.join(path, 'console.log')
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        if act == 'install':
            req_path = os.path.join(path, 'requirements.txt')
            if os.path.exists(req_path):
                f_log = open(log_file_path, 'a')
                f_log.write(f"\n[{now}] 📦 Package Installation Started...\n")
                f_log.flush()
                subprocess.Popen(['pip', 'install', '-r', 'requirements.txt'], cwd=path, stdout=f_log, stderr=f_log)
                db.close()
                return jsonify({'status': 'installing'})
            db.close()
            return jsonify({'status': 'error', 'msg': 'requirements.txt missing'})

        if act in ['start', 'restart']:
            row = db.execute('SELECT pid FROM servers WHERE folder=?', (folder,)).fetchone()
            old_pid = row['pid'] if row else None
            if folder in running_procs or (old_pid and psutil.pid_exists(old_pid)):
                try: 
                    t_pid = running_procs[folder].pid if folder in running_procs else old_pid
                    os.killpg(os.getpgid(t_pid), signal.SIGKILL)
                except: pass
            srv = db.execute('SELECT startup FROM servers WHERE folder=?', (folder,)).fetchone()
            startup_file = srv['startup'] if srv and srv['startup'] else 'main.py'
            
            # Create startup file if missing
            st_path = os.path.join(path, startup_file)
            if not os.path.exists(st_path):
                with open(st_path, 'w') as f:
                    f.write('# Startup File — replace with your own bot/script\nimport time\nprint("Started...")\nwhile True:\n    time.sleep(3600)\n')

            f_log = open(log_file_path, 'a')
            proc = subprocess.Popen(['python3', startup_file], cwd=path, stdin=subprocess.PIPE, stdout=f_log, stderr=f_log, preexec_fn=os.setsid, text=True)
            running_procs[folder], start_times[folder] = proc, time.time()
            db.execute('UPDATE servers SET pid=? WHERE folder=?', (proc.pid, folder))
            db.commit()
            db.close()

            # Wait a moment and actually verify the process is still alive before the
            # log claims success — this is what was producing fake "Successfully" lines
            # while the instance was really crashing/exiting immediately.
            time.sleep(1.2)
            f_log.flush()
            if proc.poll() is not None:
                exit_code = proc.returncode
                f_log.write(f"\n[{now}] ❌ Instance {act} FAILED — process exited immediately (exit code {exit_code}). See output above for the actual error.\n")
                f_log.flush()
                if folder in running_procs: del running_procs[folder]
                if folder in start_times: del start_times[folder]
                db2 = get_db()
                db2.execute('UPDATE servers SET pid=NULL WHERE folder=?', (folder,))
                db2.commit()
                db2.close()
                return jsonify({'status': 'error', 'msg': f'Process exited immediately (exit code {exit_code}). Check the console log.'})

            f_log.write(f"\n[{now}] 🚀 Instance {act.upper()}ED Successfully\n")
            f_log.flush()
            return jsonify({'status': 'started'})

        elif act == 'stop':
            row = db.execute('SELECT pid FROM servers WHERE folder=?', (folder,)).fetchone()
            t_pid = running_procs[folder].pid if folder in running_procs else (row['pid'] if row else None)
            if t_pid:
                try: os.killpg(os.getpgid(t_pid), signal.SIGKILL)
                except: pass
            if folder in running_procs: del running_procs[folder]
            db.execute('UPDATE servers SET pid=NULL WHERE folder=?', (folder,))
            db.commit()
            db.close()
            with open(log_file_path, 'a') as f: f.write(f"\n[{now}] 🛑 Instance STOPPED\n")
            return jsonify({'status': 'stopped'})
            
        db.close()
        return jsonify({'status': 'ok'})

    @app.route('/server/command/<folder>', methods=['POST'])
    def server_command(folder):
        d = request.json or {}
        cmd = d.get('command', '')
        proc = running_procs.get(folder)
        if not proc or proc.poll() is not None:
            return jsonify({'status': 'error', 'msg': 'Instance is not running'}), 400
        if not proc.stdin:
            return jsonify({'status': 'error', 'msg': 'No stdin available for this instance'}), 400
        try:
            proc.stdin.write(cmd + '\n')
            proc.stdin.flush()
            path = safe_join(app.config['BASE_STORAGE'], folder)
            if path:
                now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                with open(os.path.join(path, 'console.log'), 'a') as f:
                    f.write(f"\n[{now}] >>> {cmd}\n")
            return jsonify({'status': 'sent'})
        except Exception as e:
            return jsonify({'status': 'error', 'msg': str(e)}), 500

    @app.route('/server/log/<folder>')
    def server_log(folder):
        path = safe_join(app.config['BASE_STORAGE'], folder, 'console.log')
        log_text = 'Waiting for logs...'
        if path and os.path.exists(path):
            with open(path, 'r') as f:
                log_text = f.read()[-5000:]

        # Also report live online/uptime status so the UI's UPTIME indicator works
        db = get_db()
        row = db.execute('SELECT pid FROM servers WHERE folder=?', (folder,)).fetchone()
        db.close()
        saved_pid = row['pid'] if row else None

        online = False
        if saved_pid and psutil.pid_exists(saved_pid):
            try:
                p = psutil.Process(saved_pid)
                if p.is_running() and p.status() != psutil.STATUS_ZOMBIE: online = True
            except Exception: pass
        elif folder in running_procs and running_procs[folder].poll() is None:
            online = True

        uptime = get_precise_uptime(start_times.get(folder)) if online and folder in start_times else ("Online" if online else "Offline")

        return jsonify({'log': log_text, 'online': online, 'uptime': uptime})

    @app.route('/server/set-startup/<folder>', methods=['POST'])
    def set_startup(folder):
        cmd = request.json.get('file')
        db = get_db()
        db.execute('UPDATE servers SET startup=? WHERE folder=?', (cmd, folder))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/server/delete/<folder>', methods=['POST'])
    def delete_server(folder):
        db = get_db()
        srv = db.execute('SELECT pid FROM servers WHERE folder=?', (folder,)).fetchone()
        
        t_pid = running_procs[folder].pid if folder in running_procs else (srv['pid'] if srv else None)
        if t_pid:
            try: os.killpg(os.getpgid(t_pid), signal.SIGKILL)
            except: pass
        if folder in running_procs: del running_procs[folder]
        db.execute('DELETE FROM servers WHERE folder=?', (folder,))
        db.commit()
        db.close()
        path = safe_join(app.config['BASE_STORAGE'], folder)
        if path and os.path.exists(path): shutil.rmtree(path)
        return jsonify({'status': 'deleted'})

    @app.route('/servers')
    def list_servers():
        db = get_db()
        rows = db.execute('SELECT * FROM servers').fetchall()
        db.close()
        srvs = []
        for r in rows:
            f, saved_pid = r['folder'], r['pid']
            online = False
            if saved_pid and psutil.pid_exists(saved_pid):
                try:
                    p = psutil.Process(saved_pid)
                    if p.is_running() and p.status() != psutil.STATUS_ZOMBIE: online = True
                except: pass
            elif f in running_procs and running_procs[f].poll() is None: online = True
            uptime = get_precise_uptime(start_times.get(f)) if online and f in start_times else ("Online" if online else "Offline")
            cpu, ram = "0%", "0MB"
            if online:
                try:
                    p_pid = running_procs[f].pid if f in running_procs else saved_pid
                    process = psutil.Process(p_pid)
                    cpu, ram = f"{process.cpu_percent(interval=None)}%", f"{process.memory_info().rss / (1024 * 1024):.1f}MB"
                except: pass
            srvs.append({'name': r['name'], 'folder': f, 'online': online, 'startup': r['startup'], 'uptime': uptime, 'cpu': cpu, 'ram': ram, 'status': r['server_status']})
        return jsonify({'servers': srvs})

    @app.route('/add', methods=['POST'])
    def add_srv():
        name = request.json.get('name')
        folder = secure_filename(name).lower() + "_" + str(int(time.time()))
        db = get_db()
        db.execute('INSERT INTO servers (user_id, name, folder, status, startup) VALUES (?,?,?,?,?)', (GUEST_USER_ID, name, folder, 'Offline', 'main.py'))
        db.commit()
        db.close()
        
        inst_path = safe_join(app.config['BASE_STORAGE'], folder)
        os.makedirs(inst_path, exist_ok=True)
        
        # Create default startup main.py file
        main_py = os.path.join(inst_path, 'main.py')
        if not os.path.exists(main_py):
            with open(main_py, 'w') as f:
                f.write('# Auto-generated main.py — replace with your own bot/script\nimport time\nprint("Server started successfully!")\nwhile True:\n    time.sleep(3600)\n')
                
        return jsonify({'status': 'success'})

    return app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)