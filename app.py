import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta
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

TEMP_PROBLEM_START = 1000  # temp contest problems use id >= this

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
            CREATE TABLE IF NOT EXISTS problems (
                id          TEXT PRIMARY KEY,
                is_visible  INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL
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
                contest_id      INTEGER DEFAULT NULL,
                created_at      TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS contests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                description TEXT DEFAULT '',
                start_time  TEXT NOT NULL,
                end_time    TEXT NOT NULL,
                created_by  INTEGER REFERENCES users(id),
                created_at  TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS contest_problems (
                contest_id    INTEGER NOT NULL,
                problem_id    TEXT NOT NULL,
                display_order INTEGER DEFAULT 0,
                PRIMARY KEY (contest_id, problem_id)
            )
        """)
        # Migrations for existing tables
        for table_cols, col_def in [
            ("submissions", "contest_id"),
            ("submissions", "user_id"),
        ]:
            tbl = table_cols.split(",")[0]
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")]
            if col_def not in cols:
                # Only submissions.contest_id is new here
                conn.execute(
                    f"ALTER TABLE {tbl} ADD COLUMN {col_def} "
                    f"({'INTEGER DEFAULT NULL' if col_def != 'user_id' else 'INTEGER REFERENCES users(id)'})"
                )
        # Ensure problems table has entries for existing directories
        if os.path.isdir(PROBLEMS_DIR):
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for pid in sorted(os.listdir(PROBLEMS_DIR),
                              key=lambda x: int(x) if x.isdigit() else x):
                p = os.path.join(PROBLEMS_DIR, pid, "desc.md")
                if not os.path.isfile(p):
                    p = os.path.join(PROBLEMS_DIR, pid, "desc.txt")
                if os.path.isfile(p):
                    conn.execute(
                        "INSERT OR IGNORE INTO problems (id, is_visible, created_at) VALUES (?, 1, ?)",
                        (pid, now),
                    )
        conn.commit()
        # Create default admin
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

def _desc_path(pid, ext=".md"):
    p = os.path.join(PROBLEMS_DIR, pid, "desc" + ext)
    if ext == ".md" and not os.path.isfile(p):
        p = os.path.join(PROBLEMS_DIR, pid, "desc.txt")
        if not os.path.isfile(p):
            return None
    elif not os.path.isfile(p):
        return None
    return p

def _read_title(filepath):
    with open(filepath, encoding="utf-8") as f:
        line = f.readline().strip()
    return line.lstrip("# ").strip()

def get_problems(visible_only=True):
    problems = []
    if not os.path.isdir(PROBLEMS_DIR):
        return problems
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT id, is_visible FROM problems ORDER BY CAST(id AS INTEGER) ASC"
        )
        for pid, is_visible in cur.fetchall():
            if visible_only and not is_visible:
                continue
            p = _desc_path(pid)
            if p:
                problems.append({"id": pid, "title": _read_title(p)})
    return problems

def get_problem(pid):
    p = _desc_path(pid)
    if not p:
        return None
    with open(p, encoding="utf-8") as f:
        desc = f.read()
    inputs, outputs = {}, {}
    pdir = os.path.join(PROBLEMS_DIR, pid)
    if os.path.isdir(pdir):
        for fname in os.listdir(pdir):
            m = re.match(r"input(\d+)\.txt", fname)
            if m:
                with open(os.path.join(pdir, fname), encoding="utf-8") as f:
                    inputs[int(m.group(1))] = f.read()
            m = re.match(r"output(\d+)\.txt", fname)
            if m:
                with open(os.path.join(pdir, fname), encoding="utf-8") as f:
                    outputs[int(m.group(1))] = f.read()
    test_cases = []
    for idx in sorted(inputs):
        if idx in outputs:
            test_cases.append({"input": inputs[idx], "output": outputs[idx]})
    return {"id": pid, "desc": desc, "test_cases": test_cases}

def ensure_problem_in_db(pid, is_visible=1):
    """Make sure a problem directory has a row in the problems table."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO problems (id, is_visible, created_at) VALUES (?, ?, ?)",
            (pid, is_visible, now),
        )
        conn.commit()

# ---------------------------------------------------------------------------
# Language detection & judge
# ---------------------------------------------------------------------------

def detect_lang(code, lang_hint=None):
    if lang_hint:
        lang_hint = lang_hint.lower()
        if lang_hint in ("py", "python", "python3"): return "py"
        if lang_hint in ("cpp", "c++", "cxx", "cc"): return "cpp"
        if lang_hint in ("java",): return "java"
        if lang_hint in ("js", "javascript", "node"): return "js"
    first = code.strip().split("\n")[0] if code.strip() else ""
    if first.startswith("#!/"):
        if "python" in first: return "py"
        elif "g++" in first or "c++" in first: return "cpp"
        elif "node" in first: return "js"
        elif "java" in first: return "java"
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
            if not passed: all_passed = False
        except subprocess.TimeoutExpired:
            passed = False; actual = "Time Limit Exceeded"; all_passed = False
        except Exception as e:
            passed = False; actual = "Judge Error: %s" % str(e)[:100]; all_passed = False
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
            import traceback; traceback.print_exc(); time.sleep(1)

# ---------------------------------------------------------------------------
# Contest helpers
# ---------------------------------------------------------------------------

def get_contest(contest_id):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT id, title, description, start_time, end_time, created_by, created_at "
            "FROM contests WHERE id = ?", (contest_id,)
        )
        row = cur.fetchone()
    if not row:
        return None
    cid, title, desc, st, et, cby, cat = row
    return {
        "id": cid, "title": title, "description": desc,
        "start_time": st, "end_time": et,
        "created_by": cby, "created_at": cat,
    }

def get_contest_problems(contest_id):
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT cp.problem_id, cp.display_order, p.is_visible "
            "FROM contest_problems cp "
            "JOIN problems p ON p.id = cp.problem_id "
            "WHERE cp.contest_id = ? ORDER BY cp.display_order ASC",
            (contest_id,),
        ).fetchall()
    result = []
    for pid, order, is_visible in rows:
        pp = _desc_path(pid)
        if pp:
            result.append({
                "id": pid, "title": _read_title(pp),
                "display_order": order, "is_visible": is_visible,
            })
    return result

def compute_acm_rank(contest_id):
    """Return sorted ACM rank list for a contest."""
    contest = get_contest(contest_id)
    if not contest:
        return []
    start = datetime.fromisoformat(contest["start_time"])
    end = datetime.fromisoformat(contest["end_time"])

    with sqlite3.connect(DB_PATH) as conn:
        submissions = conn.execute(
            "SELECT s.user_id, u.student_id, u.name, s.problem_id, "
            "       s.status, s.created_at "
            "FROM submissions s "
            "JOIN users u ON u.id = s.user_id "
            "WHERE s.contest_id = ? "
            "AND s.created_at >= ? AND s.created_at <= ? "
            "ORDER BY s.user_id, s.problem_id, s.created_at ASC",
            (contest_id, contest["start_time"], contest["end_time"]),
        ).fetchall()

    # Build per-user per-problem stats
    users = {}
    for uid, sid, name, pid, status, created_at in submissions:
        users.setdefault(uid, {"student_id": sid, "name": name, "problems": {}})
        pdata = users[uid]["problems"].setdefault(pid, {"attempts": 0, "ac_time": None})
        if status == "Accepted" and pdata["ac_time"] is None:
            pdata["ac_time"] = datetime.fromisoformat(created_at)
            # penalty for THIS problem = minutes from start + 20 * previous attempts
            minutes = max(0, (pdata["ac_time"] - start).total_seconds() // 60)
            pdata["penalty"] = int(minutes + pdata["attempts"] * 20)
        elif status != "Accepted":
            pdata["attempts"] += 1

    # Compute totals and build rank
    rank_list = []
    for uid, data in users.items():
        ac_count = 0
        total_penalty = 0
        prob_status = {}
        for pid, pdata in data["problems"].items():
            if pdata["ac_time"] is not None:
                ac_count += 1
                total_penalty += pdata["penalty"]
                prob_status[pid] = {"accepted": True, "attempts": pdata["attempts"],
                                    "penalty": pdata["penalty"]}
            else:
                prob_status[pid] = {"accepted": False, "attempts": pdata["attempts"]}
        rank_list.append({
            "student_id": data["student_id"],
            "name": data["name"],
            "ac_count": ac_count,
            "penalty": int(total_penalty),
            "problems": prob_status,
        })

    rank_list.sort(key=lambda x: (-x["ac_count"], x["penalty"], x["student_id"]))
    for i, r in enumerate(rank_list):
        r["rank"] = i + 1
    return rank_list

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
    return jsonify({"ok": True, "user": {"student_id": student_id, "name": name, "is_admin": bool(is_admin)}})

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
    problems = get_problems(visible_only=True)
    return render_template("index.html", problems=problems)

@app.route("/problem/<pid>")
@login_required
def problem_page(pid):
    problem = get_problem(pid)
    if not problem:
        return "题目不存在", 404
    problems = get_problems(visible_only=False)
    contest_id = request.args.get("contest", type=int)
    return render_template("problem.html", problem=problem, problems=problems, contest_id=contest_id)

@app.route("/rank")
@login_required
def rank_page():
    return render_template("rank.html")

@app.route("/admin")
@admin_required
def admin_page():
    problems = get_problems(visible_only=True)
    return render_template("admin.html", problems=problems)

@app.route("/contests")
@login_required
def contests_page():
    return render_template("contests.html")

@app.route("/contest/<int:contest_id>")
@login_required
def contest_detail_page(contest_id):
    contest = get_contest(contest_id)
    if not contest:
        return "比赛不存在", 404
    return render_template("contest_detail.html", contest=contest)

@app.route("/admin/contests")
@admin_required
def admin_contests_page():
    problems = get_problems(visible_only=False)
    return render_template("admin_contests.html", problems=problems)

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
    contest_id = data.get("contest_id")

    if not code.strip():
        return jsonify({"error": "Code is empty"}), 400
    if len(code) > 65536:
        return jsonify({"error": "Code too long (max 64KB)"}), 400

    # If contest_id provided, validate contest is active
    if contest_id is not None:
        contest = get_contest(contest_id)
        if not contest:
            return jsonify({"error": "比赛不存在"}), 400
        now = datetime.now()
        start = datetime.fromisoformat(contest["start_time"])
        end = datetime.fromisoformat(contest["end_time"])
        if now < start:
            return jsonify({"error": "比赛尚未开始"}), 400
        # If ended, still allow submission but don't count (store contest_id anyway)
        if now > end:
            pass  # allowed, just won't count in rank

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO submissions (user_id, user_name, problem_id, code, lang, status, contest_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'Pending', ?, ?)",
            (session["user_id"], session["student_id"], pid, code, lang, contest_id, now_str),
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
            SELECT u.student_id, u.name,
                COUNT(DISTINCT CASE WHEN s.status = 'Accepted' THEN s.problem_id END) AS ac_count,
                COUNT(s.id) AS total_sub
            FROM users u
            LEFT JOIN submissions s ON s.user_id = u.id
            GROUP BY u.id
            ORDER BY ac_count DESC, total_sub ASC, u.student_id ASC
        """).fetchall()
    rank = [{"rank": i+1, "student_id": r[0], "name": r[1],
             "ac_count": r[2], "total_sub": r[3]} for i, r in enumerate(rows)]
    return jsonify(rank)

# ---------------------------------------------------------------------------
# API: contests
# ---------------------------------------------------------------------------

@app.route("/api/contests")
def api_contests():
    now = datetime.now().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, title, description, start_time, end_time FROM contests ORDER BY start_time DESC"
        ).fetchall()
    result = []
    for cid, title, desc, st, et in rows:
        status = "ended"
        if now < st:
            status = "upcoming"
        elif now <= et:
            status = "running"
        result.append({
            "id": cid, "title": title, "description": desc,
            "start_time": st, "end_time": et, "status": status,
        })
    return jsonify(result)

@app.route("/api/contest/<int:contest_id>")
def api_contest(contest_id):
    contest = get_contest(contest_id)
    if not contest:
        return jsonify({"error": "Not found"}), 404
    problems = get_contest_problems(contest_id)
    now = datetime.now().isoformat()
    status = "ended"
    if now < contest["start_time"]:
        status = "upcoming"
    elif now <= contest["end_time"]:
        status = "running"
    return jsonify({**contest, "problems": problems, "status": status})

@app.route("/api/contest/<int:contest_id>/rank")
def api_contest_rank(contest_id):
    contest = get_contest(contest_id)
    if not contest:
        return jsonify({"error": "Not found"}), 404
    rank_list = compute_acm_rank(contest_id)
    problems = get_contest_problems(contest_id)
    return jsonify({"rank": rank_list, "problems": [p["id"] for p in problems]})

# ---------------------------------------------------------------------------
# API: admin - contests
# ---------------------------------------------------------------------------

@app.route("/api/admin/contests/create", methods=["POST"])
@admin_required
def api_admin_create_contest():
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    start_time = request.form.get("start_time", "").strip()
    end_time = request.form.get("end_time", "").strip()
    problem_ids = request.form.getlist("problem_ids[]")

    if not title or not start_time or not end_time:
        return jsonify({"error": "请填写标题和起止时间"}), 400

    # Parse problem IDs from form (allow both single and multi)
    all_pids = []
    for val in problem_ids:
        for pid in val.split(","):
            pid = pid.strip()
            if pid:
                all_pids.append(pid)

    # Handle temp problem uploads
    temp_problems = []
    upload_count = int(request.form.get("temp_count", "0"))
    for i in range(upload_count):
        desc_file = request.files.get(f"temp_desc_{i}")
        zip_file = request.files.get(f"temp_zip_{i}")
        if desc_file and desc_file.filename:
            temp_problems.append((desc_file, zip_file))

    with sqlite3.connect(DB_PATH) as conn:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            "INSERT INTO contests (title, description, start_time, end_time, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (title, description, start_time, end_time, session["user_id"], now),
        )
        cid = cur.lastrowid

        # Assign temp problem IDs
        next_temp_id = TEMP_PROBLEM_START
        if temp_problems:
            max_id = conn.execute("SELECT MAX(CAST(id AS INTEGER)) FROM problems WHERE CAST(id AS INTEGER) >= ?",
                                  (TEMP_PROBLEM_START,)).fetchone()[0]
            if max_id:
                next_temp_id = max(max_id + 1, TEMP_PROBLEM_START)

        temp_ids = []
        for desc_file, zip_file in temp_problems:
            pid = str(next_temp_id)
            next_temp_id += 1
            pdir = os.path.join(PROBLEMS_DIR, pid)
            os.makedirs(pdir, exist_ok=True)

            # Save description
            desc_content = desc_file.read().decode("utf-8")
            if desc_content.strip().startswith("#"):
                with open(os.path.join(pdir, "desc.md"), "w", encoding="utf-8") as f:
                    f.write(desc_content)
            else:
                with open(os.path.join(pdir, "desc.txt"), "w", encoding="utf-8") as f:
                    f.write(desc_content)

            # Save test data zip if provided
            if zip_file and zip_file.filename and zip_file.filename.endswith(".zip"):
                _extract_test_zip(zip_file, pdir)

            conn.execute(
                "INSERT OR IGNORE INTO problems (id, is_visible, created_at) VALUES (?, 0, ?)",
                (pid, now),
            )
            all_pids.append(pid)
            temp_ids.append(pid)

        # Insert contest_problems
        for order, pid in enumerate(all_pids):
            # Ensure problem exists in DB
            conn.execute(
                "INSERT OR IGNORE INTO problems (id, is_visible, created_at) VALUES (?, 1, ?)",
                (pid, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO contest_problems (contest_id, problem_id, display_order) VALUES (?, ?, ?)",
                (cid, pid, order),
            )
        conn.commit()

    return jsonify({"ok": True, "contest_id": cid, "temp_ids": temp_ids})

@app.route("/api/admin/contest/<int:contest_id>/publish", methods=["POST"])
@admin_required
def api_admin_publish_contest_problems(contest_id):
    """Make temp contest problems visible (is_visible=1)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE problems SET is_visible = 1 WHERE id IN ("
            "  SELECT problem_id FROM contest_problems WHERE contest_id = ?"
            ") AND CAST(id AS INTEGER) >= ?",
            (contest_id, TEMP_PROBLEM_START),
        )
        conn.commit()
    return jsonify({"ok": True})

def _extract_test_zip(zip_file, target_dir):
    """Extract input/output txt files from a zip file to target_dir."""
    import tempfile as tf
    tmp = tf.mkdtemp()
    try:
        zpath = os.path.join(tmp, "upload.zip")
        zip_file.save(zpath)
        extracted = set()
        with zipfile.ZipFile(zpath, "r") as zf:
            for info in zf.infolist():
                if info.is_dir(): continue
                name = os.path.basename(info.filename)
                if not re.match(r"(input|output)\d+\.txt$", name): continue
                zf.extract(info, tmp)
                extracted.add(name)
        for name in sorted(extracted):
            shutil.copy2(os.path.join(tmp, name), os.path.join(target_dir, name))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------------------
# API: admin - problem visibility (for publishing single problem)
# ---------------------------------------------------------------------------

@app.route("/api/admin/problem/<pid>/publish", methods=["POST"])
@admin_required
def api_admin_publish_problem(pid):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE problems SET is_visible = 1 WHERE id = ?", (pid,))
        conn.commit()
    return jsonify({"ok": True})

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
