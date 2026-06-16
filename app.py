import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime

from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
BASE = os.path.dirname(os.path.abspath(__file__))
PROBLEMS_DIR = os.path.join(BASE, "problems")
DB_PATH = os.path.join(BASE, "oj.db")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_name   TEXT    NOT NULL,
                problem_id  TEXT    NOT NULL,
                code        TEXT    NOT NULL,
                lang        TEXT    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'Pending',
                result_details TEXT,
                created_at  TEXT    NOT NULL
            )
        """)
        conn.commit()

# ---------------------------------------------------------------------------
# Problem helpers
# ---------------------------------------------------------------------------

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

PYTHON_CMD = "python"
JAVA_CMD = "java"
JAVAC_CMD = "javac"
GXX_CMD = "g++"
NODE_CMD = "node"
CPP_STD = "c++17"


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
    """
    Run code against test cases.
    Returns (verdict, list_of_result_dicts).
    """
    lang = detect_lang(code, lang_hint)
    tmpdir = tempfile.mkdtemp()
    results = []
    all_passed = True

    try:
        if lang == "py":
            src = os.path.join(tmpdir, "solution.py")
            with open(src, "w", encoding="utf-8") as f:
                f.write(code)
            run_cmd = [PYTHON_CMD, src]

        elif lang == "cpp":
            src = os.path.join(tmpdir, "solution.cpp")
            exe = os.path.join(tmpdir, "solution.exe")
            with open(src, "w", encoding="utf-8") as f:
                f.write(code)
            comp = subprocess.run(
                [GXX_CMD, src, "-o", exe, f"-std={CPP_STD}", "-O2", "-Wall"],
                capture_output=True, text=True, timeout=15,
            )
            if comp.returncode != 0:
                return "Compilation Error", [
                    {"input": "", "expected": "", "actual": comp.stderr.strip(), "passed": False}
                ]
            run_cmd = [exe]

        elif lang == "java":
            src = os.path.join(tmpdir, "Main.java")
            with open(src, "w", encoding="utf-8") as f:
                f.write(code)
            comp = subprocess.run(
                [JAVAC_CMD, src],
                capture_output=True, text=True, timeout=15,
            )
            if comp.returncode != 0:
                return "Compilation Error", [
                    {"input": "", "expected": "", "actual": comp.stderr.strip(), "passed": False}
                ]
            run_cmd = [JAVA_CMD, "-cp", tmpdir, "Main"]

        elif lang == "js":
            src = os.path.join(tmpdir, "solution.js")
            with open(src, "w", encoding="utf-8") as f:
                f.write(code)
            run_cmd = [NODE_CMD, src]

        else:
            return "Unsupported Language", [
                {"input": "", "expected": "", "actual": f"Unsupported language: {lang}", "passed": False}
            ]

        for idx, tc in enumerate(test_cases):
            inp = tc["input"]
            expected = tc["output"].strip()
            try:
                p = subprocess.run(
                    run_cmd,
                    input=inp,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=tmpdir,
                )
                actual = (p.stdout or "").strip()
                error = (p.stderr or "").strip()
                if p.returncode != 0:
                    passed = False
                    actual = f"Runtime Error: {error}" if error else "Runtime Error"
                else:
                    passed = actual == expected
                if not passed:
                    all_passed = False
            except subprocess.TimeoutExpired:
                passed = False
                actual = "Time Limit Exceeded"
            except Exception as e:
                passed = False
                actual = str(e)
                all_passed = False

            results.append({
                "idx": idx + 1,
                "input": inp.strip() if inp.strip() else "(empty)",
                "expected": expected,
                "actual": actual,
                "passed": passed,
            })

        verdict = "Accepted" if all_passed else "Wrong Answer"
        return verdict, results

    finally:
        def cleanup():
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        threading.Thread(target=cleanup, daemon=True).start()


# ---------------------------------------------------------------------------
# Background judge worker
# ---------------------------------------------------------------------------

def judge_worker():
    """Daemon thread: polls for Pending submissions, judges them, updates DB."""
    while True:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                cur = conn.execute(
                    "SELECT id, problem_id, code, lang FROM submissions WHERE status = 'Pending' ORDER BY id ASC LIMIT 1"
                )
                row = cur.fetchone()
                if row is None:
                    time.sleep(0.5)
                    continue

                sub_id, pid, code, lang = row

                # Mark Running
                conn.execute("UPDATE submissions SET status = 'Running' WHERE id = ?", (sub_id,))
                conn.commit()

            # Judge outside the DB connection block (may be slow)
            problem = get_problem(pid)
            if problem is None:
                verdict = "System Error"
                details = json.dumps([{"input":"", "expected":"", "actual":"Problem not found", "passed":False}], ensure_ascii=False)
            else:
                verdict, details_list = judge_code(code, problem["test_cases"], lang_hint=lang)
                details = json.dumps(details_list, ensure_ascii=False)

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
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    problems = get_problems()
    return render_template("index.html", problems=problems)


@app.route("/api/problem/<pid>")
def api_problem(pid):
    problem = get_problem(pid)
    if not problem:
        return jsonify({"error": "Problem not found"}), 404
    return jsonify({"id": problem["id"], "desc": problem["desc"]})


@app.route("/api/submit", methods=["POST"])
def api_submit():
    data = request.get_json()
    pid = data.get("pid", "")
    code = data.get("code", "")
    lang = data.get("lang", "")
    user_name = data.get("user_name", "Anonymous")

    if not code.strip():
        return jsonify({"error": "Code is empty"}), 400
    if len(code) > 65536:
        return jsonify({"error": "Code too long (max 64KB)"}), 400

    # Insert into DB — immediate return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO submissions (user_name, problem_id, code, lang, status, created_at) VALUES (?, ?, ?, ?, 'Pending', ?)",
            (user_name, pid, code, lang, now),
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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=judge_worker, daemon=True)
    t.start()
    from waitress import serve
    serve(app, host="0.0.0.0", port=5000)
