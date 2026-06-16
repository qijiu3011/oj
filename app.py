import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
import zipfile
from datetime import datetime
from functools import wraps

import bcrypt
from flask import (Flask, jsonify, render_template, request, session,
                   redirect, url_for)

from docker_runner import run_sandboxed

app = Flask(__name__)
BASE = os.path.dirname(os.path.abspath(__file__))
PROBLEMS_DIR = os.path.join(BASE, "problems")
DB_PATH = os.path.join(BASE, "oj.db")
UPLOAD_TEMP = os.path.join(BASE, ".upload_tmp")
app.secret_key = os.environ.get("OJ_SECRET_KEY",
                                "change-me-in-production-2024")
os.makedirs(UPLOAD_TEMP, exist_ok=True)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id    TEXT UNIQUE NOT NULL,
                name          TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin      INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER REFERENCES users(id),
                user_name       TEXT NOT NULL,
                problem_id      TEXT NOT NULL,
                code            TEXT NOT NULL,
                lang            TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'Pending',
                result_details  TEXT,
                created_at      TEXT NOT NULL
            )
        """)
        # Ensure user_id column exists (for DB created before this feature)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(submissions)")]
        if "user_id" not in cols:
            conn.execute("ALTER TABLE submissions ADD COLUMN user_id INTEGER REFERENCES users(id)")
        conn.commit()
        # Create default admin if no users exist
        cur = conn.execute("SELECT COUNT(*) FROM users")
        if cur.fetchone()[0] == 0:
            _create_user(conn, "admin", "管理员", "admin123", is_admin=True)


def _create_user(conn, student_id, name, password, is_admin=False):
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO users (student_id, name, password_hash, is_admin, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (student_id, name, pw_hash, 1 if is_admin else 0, now),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "请先登录"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "请先登录"}), 401
            return redirect(url_for("login_page"))
        if not session.get("is_admin"):
            return jsonify({"error": "需要管理员权限"}), 403
        return f(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Problem helpers
# ---------------------------------------------------------------------------

ACCEPTED_EXT = (".txt", ".in", ".out", ".ans")


def get_problems():
    problems = []
    if not os.path.isdir(PROBLEMS_DIR):
        return problems
    for pid in sorted(os.listdir(PROBLEMS_DIR),
                      key=lambda x: int(x) if x.isdigit() else x):
        desc_path = os.path.join(PROBLEMS_DIR, pid, "desc.txt")
        if os.path.isfile(desc_path):
            with open(desc_path, encoding="utf-8") as f:
                first_line = f.readline().strip()
            problems.append({"id": pid, "title": first_line})
    return problems


def get_problem(pid):
    desc_path = os.path.join(PROBLEMS_DIR, pid, "desc.txt")
    if not os.path.isfile(desc_path):
        return None
    with open(desc_path, encoding="utf-8") as f:
        desc = f.read()
    inputs, outputs = {}, {}
    for fname in os.listdir(os.path.join(PROBLEMS_DIR, pid)):
        m = re.match(r"input(\d+)\.txt", fname)
        if m:
            with open(os.path.join(PROBLEMS_DIR, pid, fname), encoding="utf-8") as f:
                inputs[int(m.group(1))] = f.read()
        m = re.match(r"output(\d+)\.txt", fname)
        if m:
            with open(os.path.join(PROBLEMS_DIR, pid, fname), encoding="utf-8") as f:
                outputs[int(m.group(1))] = f.read()
    test_cases = []
    for idx in sorted(inputs):
        if idx in outputs:
            test_cases.append({"input": inputs[idx], "output": outputs[idx]})
    return {"id": pid, "desc": desc, "test_cases": test_cases}


# ---------------------------------------------------------------------------
# Language detection & judge
# ---------------------------------------------------------------------------

def detect_lang(code, lang_hint=None):
    if lang_hint:
        lang_hint = lang_hint.lower()
        if lang_hint in ("py", "python", "python3"):
            return "py"
        if lang_hint in ("cpp", "c++", "cxx", "cc"):
            return "cpp"
        if lang_hint in ("java",):
            return "java"
        if lang_hint in ("js", "javascript", "node"):
            return "js"
    first = code.strip().split("\n")[0] if code.strip() else ""
    if first.startswith("#!/"):
        if "python" in first:
            return "py"
        elif "g++" in first or "c++" in first:
            return "cpp"
        elif "node" in first:
            return "js"
        elif "java" in first:
            return "java"
    return "py"


def judge_code(code, test_cases, timeout=2, lang_hint=None):
    lang = detect_lang(code, lang_hint)
    results = []
    all_passed = True
    for idx, tc in enumerate(test_cases):
        inp = tc["input"]
        expected = tc["output"].strip()
        try:
            stdout, stderr, retcode, _mode = run_sandboxed(
                code=code, lang=lang, test_input=inp,
                timeout=timeout, prefer_docker=True,
            )
            actual = (stdout or "").strip()
            if retcode != 0:
                passed = False
                err_msg = (stderr or "").strip()
                actual = "Runtime Error: %s" % err_msg[:200] if err_msg else "Runtime Error (exit %d)" % retcode
            else:
                passed = actual == expected
            if not passed:
                all_passed = False
        except subprocess.TimeoutExpired:
            passed = False
            actual = "Time Limit Exceeded"
            all_passed = False
        except Exception as e:
            passed = False
            actual = "Judge Error: %s" % str(e)[:100]
            all_passed = False
        results.append({
            "idx": idx + 1, "input": inp.strip() or "(empty)",
            "expected": expected, "actual": actual, "passed": passed,
        })
    return ("Accepted" if all_passed else "Wrong Answer"), results


# ---------------------------------------------------------------------------
# Judge worker
# ---------------------------------------------------------------------------

def judge_worker():
    while True:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                cur = conn.execute(
                    "SELECT id, problem_id, code, lang FROM submissions "
                    "WHERE status = 'Pending' ORDER BY id ASC LIMIT 1"
                )
                row = cur.fetchone()
                if row is None:
                    time.sleep(0.5)
                    continue
                sub_id, pid, code, lang = row
                conn.execute("UPDATE submissions SET status = 'Running' WHERE id = ?", (sub_id,))
                conn.commit()
            problem = get_problem(pid)
            if problem is None:
                verdict = "System Error"
                details = json.dumps(
                    [{"input": "", "expected": "", "actual": "Problem not found", "passed": False}],
                    ensure_ascii=False,
                )
            else:
                verdict, dl = judge_code(code, problem["test_cases"], lang_hint=lang)
                details = json.dumps(dl, ensure_ascii=False)
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE submissions SET status = ?, result_details = ? WHERE id = ?",
                    (verdict, details, sub_id),
                )
                conn.commit()
        except Exception:
            import traceback
            traceback.print_exc()
            time.sleep(1)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/login")
def login_page():
    return render_template("login.html")


@app.route("/register")
def register_page():
    return render_template("register.html")


@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json()
    sid = data.get("student_id", "").strip()
    name = data.get("name", "").strip()
    password = data.get("password", "")
    if not sid or not name or not password:
        return jsonify({"error": "请填写完整信息"}), 400
    if len(password) < 4:
        return jsonify({"error": "密码至少 4 位"}), 400
    if not re.match(r"^[a-zA-Z0-9_\-]+$", sid):
        return jsonify({"error": "学号只能包含字母、数字、下划线和连字符"}), 400

    with sqlite3.connect(DB_PATH) as conn:
        try:
            _create_user(conn, sid, name, password)
            return jsonify({"ok": True, "message": "注册成功"})
        except sqlite3.IntegrityError:
            return jsonify({"error": "该学号已注册"}), 409


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json()
    sid = data.get("student_id", "").strip()
    password = data.get("password", "")
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT id, student_id, name, password_hash, is_admin FROM users WHERE student_id = ?",
            (sid,),
        )
        row = cur.fetchone()
    if not row:
        return jsonify({"error": "学号或密码错误"}), 401
    uid, student_id, name, pw_hash, is_admin = row
    if not bcrypt.checkpw(password.encode(), pw_hash.encode()):
        return jsonify({"error": "学号或密码错误"}), 401

    session["user_id"] = uid
    session["student_id"] = student_id
    session["name"] = name
    session["is_admin"] = bool(is_admin)
    return jsonify({
        "ok": True,
        "user": {
            "student_id": student_id,
            "name": name,
            "is_admin": bool(is_admin),
        },
    })


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me")
def api_me():
    if "user_id" not in session:
        return jsonify({"user": None}), 200
    return jsonify({
        "user": {
            "student_id": session["student_id"],
            "name": session["name"],
            "is_admin": session.get("is_admin", False),
        }
    })


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    problems = get_problems()
    return render_template("index.html", problems=problems)


@app.route("/problem/<pid>")
@login_required
def problem_page(pid):
    problem = get_problem(pid)
    if not problem:
        return "题目不存在", 404
    problems = get_problems()
    return render_template("problem.html", problem=problem, problems=problems)


@app.route("/rank")
@login_required
def rank_page():
    return render_template("rank.html")


@app.route("/admin")
@admin_required
def admin_page():
    problems = get_problems()
    return render_template("admin.html", problems=problems)


# ---------------------------------------------------------------------------
# API: problem
# ---------------------------------------------------------------------------

@app.route("/api/problem/<pid>")
def api_problem(pid):
    problem = get_problem(pid)
    if not problem:
        return jsonify({"error": "Problem not found"}), 404
    return jsonify({"id": problem["id"], "desc": problem["desc"]})


@app.route("/api/submit", methods=["POST"])
@login_required
def api_submit():
    data = request.get_json()
    pid = data.get("pid", "")
    code = data.get("code", "")
    lang = data.get("lang", "")

    if not code.strip():
        return jsonify({"error": "Code is empty"}), 400
    if len(code) > 65536:
        return jsonify({"error": "Code too long (max 64KB)"}), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO submissions (user_id, user_name, problem_id, code, lang, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'Pending', ?)",
            (session["user_id"], session["student_id"], pid, code, lang, now),
        )
        conn.commit()
        submission_id = cur.lastrowid
    return jsonify({"submission_id": submission_id, "status": "Pending"}), 202


@app.route("/api/result/<int:submission_id>")
def api_result(submission_id):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT id, status, result_details FROM submissions WHERE id = ?",
            (submission_id,),
        )
        row = cur.fetchone()
    if row is None:
        return jsonify({"error": "Submission not found"}), 404
    sid, status, details_raw = row
    resp = {"id": sid, "status": status}
    if details_raw:
        resp["results"] = json.loads(details_raw)
    return jsonify(resp)


# ---------------------------------------------------------------------------
# API: rank
# ---------------------------------------------------------------------------

@app.route("/api/rank")
def api_rank():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT
                u.student_id,
                u.name,
                COUNT(DISTINCT CASE WHEN s.status = 'Accepted' THEN s.problem_id END) AS ac_count,
                COUNT(s.id) AS total_sub
            FROM users u
            LEFT JOIN submissions s ON s.user_id = u.id
            GROUP BY u.id
            ORDER BY ac_count DESC, total_sub ASC, u.student_id ASC
        """).fetchall()
    rank = [
        {
            "rank": i + 1,
            "student_id": r[0],
            "name": r[1],
            "ac_count": r[2],
            "total_sub": r[3],
        }
        for i, r in enumerate(rows)
    ]
    return jsonify(rank)


# ---------------------------------------------------------------------------
# API: admin
# ---------------------------------------------------------------------------

@app.route("/api/admin/problem/<pid>", methods=["GET"])
@admin_required
def api_admin_get_problem(pid):
    problem = get_problem(pid)
    if not problem:
        return jsonify({"error": "Problem not found"}), 404
    # List test files
    test_files = []
    pdir = os.path.join(PROBLEMS_DIR, pid)
    if os.path.isdir(pdir):
        for fn in sorted(os.listdir(pdir)):
            if re.match(r"(input|output)\d+\.txt$", fn):
                test_files.append(fn)
    return jsonify({
        "id": problem["id"],
        "desc": problem["desc"],
        "test_files": test_files,
    })


@app.route("/api/admin/problem/<pid>/desc", methods=["PUT"])
@admin_required
def api_admin_update_desc(pid):
    pdir = os.path.join(PROBLEMS_DIR, pid)
    if not os.path.isdir(pdir):
        return jsonify({"error": "题目目录不存在"}), 404
    data = request.get_json()
    new_desc = data.get("desc", "")
    if not new_desc.strip():
        return jsonify({"error": "描述不能为空"}), 400
    with open(os.path.join(pdir, "desc.txt"), "w", encoding="utf-8") as f:
        f.write(new_desc)
    return jsonify({"ok": True})


@app.route("/api/admin/problem/<pid>/upload", methods=["POST"])
@admin_required
def api_admin_upload(pid):
    """Upload a zip containing input/output test files."""
    pdir = os.path.join(PROBLEMS_DIR, pid)
    if not os.path.isdir(pdir):
        os.makedirs(pdir, exist_ok=True)

    if "file" not in request.files:
        return jsonify({"error": "请选择 zip 文件"}), 400
    zip_file = request.files["file"]
    if zip_file.filename == "" or not zip_file.filename.endswith(".zip"):
        return jsonify({"error": "请上传 .zip 文件"}), 400

    tmp_dir = os.path.join(UPLOAD_TEMP, uuid.uuid4().hex)
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        zip_path = os.path.join(tmp_dir, "upload.zip")
        zip_file.save(zip_path)

        extracted = set()
        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = os.path.basename(info.filename)
                if not re.match(r"(input|output)\d+\.txt$", name):
                    continue
                zf.extract(info, tmp_dir)
                extracted.add(name)

        if not extracted:
            return jsonify({"error": "zip 中没有找到 input*.txt / output*.txt 文件"}), 400

        # Remove old test files
        for fn in os.listdir(pdir):
            if re.match(r"(input|output)\d+\.txt$", fn):
                os.remove(os.path.join(pdir, fn))

        # Copy new files
        for name in sorted(extracted):
            shutil.copy2(os.path.join(tmp_dir, name), os.path.join(pdir, name))

        return jsonify({"ok": True, "files": sorted(extracted)})

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


import shutil  # noqa: last import for admin upload


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=judge_worker, daemon=True)
    t.start()
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=5000)
    except ImportError:
        app.run(host="0.0.0.0", port=5000, debug=True)
