from flask import Flask, render_template, request, jsonify
import subprocess
import tempfile
import os
import re
import threading

app = Flask(__name__)
BASE = os.path.dirname(os.path.abspath(__file__))
PROBLEMS_DIR = os.path.join(BASE, "problems")


def get_problems():
    """Scan problems/ directory and return list of problem IDs."""
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
    """Get full problem info by ID."""
    desc_path = os.path.join(PROBLEMS_DIR, pid, "desc.txt")
    if not os.path.isfile(desc_path):
        return None
    with open(desc_path, encoding="utf-8") as f:
        desc = f.read()
    # Collect test cases
    inputs = {}
    outputs = {}
    for fname in os.listdir(os.path.join(PROBLEMS_DIR, pid)):
        m = re.match(r"input(\d+)\.txt", fname)
        if m:
            with open(os.path.join(PROBLEMS_DIR, pid, fname),
                      encoding="utf-8") as f:
                inputs[int(m.group(1))] = f.read()
        m = re.match(r"output(\d+)\.txt", fname)
        if m:
            with open(os.path.join(PROBLEMS_DIR, pid, fname),
                      encoding="utf-8") as f:
                outputs[int(m.group(1))] = f.read()
    test_cases = []
    for idx in sorted(inputs):
        if idx in outputs:
            test_cases.append({"input": inputs[idx], "output": outputs[idx]})
    return {"id": pid, "desc": desc, "test_cases": test_cases}


# Compilers / interpreters
PYTHON_CMD = "python"
JAVA_CMD = "java"
JAVAC_CMD = "javac"
GXX_CMD = "g++"
NODE_CMD = "node"
# C++ standard: default to c++17 (g++ 8.1), user can override via shebang
CPP_STD = "c++17"


def detect_lang(code, lang_hint=None):
    """Detect language from hint or shebang. Returns ('lang', {opts})."""
    if lang_hint:
        lang_hint = lang_hint.lower()
        if lang_hint in ("py", "python", "python3"):
            return "py", {}
        if lang_hint in ("cpp", "c++", "cxx", "cc"):
            return "cpp", {}
        if lang_hint in ("java",):
            return "java", {}
        if lang_hint in ("js", "javascript", "node"):
            return "js", {}

    # Fallback: detect from shebang
    first = code.strip().split("\n")[0] if code.strip() else ""
    if first.startswith("#!/"):
        if "python" in first:
            return "py", {}
        elif "g++" in first or "c++" in first:
            return "cpp", {}
        elif "node" in first:
            return "js", {}
        elif "java" in first:
            return "java", {}
    return "py", {}


LANG_NAMES = {
    "py": "Python",
    "cpp": "C++",
    "java": "Java",
    "js": "JavaScript",
}


def judge_code(code, test_cases, timeout=2, lang_hint=None):
    """
    Run code against test cases.
    Returns (verdict, results) where results is a list of
    (test_idx, passed, input_display, expected, actual, error_or_time).
    """
    lang, _ = detect_lang(code, lang_hint)
    tmpdir = tempfile.mkdtemp()
    results = []
    all_passed = True

    try:
        if lang == "py":
            src_path = os.path.join(tmpdir, "solution.py")
            with open(src_path, "w", encoding="utf-8") as f:
                f.write(code)
            run_cmd = [PYTHON_CMD, src_path]

        elif lang == "cpp":
            src_path = os.path.join(tmpdir, "solution.cpp")
            bin_path = os.path.join(tmpdir, "solution.exe")
            with open(src_path, "w", encoding="utf-8") as f:
                f.write(code)
            comp = subprocess.run(
                [GXX_CMD, src_path, "-o", bin_path, f"-std={CPP_STD}", "-O2", "-Wall"],
                capture_output=True, text=True, timeout=15
            )
            if comp.returncode != 0:
                return "Compilation Error", [{
                    "input": "",
                    "expected": "",
                    "actual": comp.stderr.strip(),
                    "passed": False
                }]
            run_cmd = [bin_path]

        elif lang == "java":
            # Java: class must be named "Main" (OJ convention)
            src_path = os.path.join(tmpdir, "Main.java")
            with open(src_path, "w", encoding="utf-8") as f:
                f.write(code)
            comp = subprocess.run(
                [JAVAC_CMD, src_path],
                capture_output=True, text=True, timeout=15
            )
            if comp.returncode != 0:
                return "Compilation Error", [{
                    "input": "",
                    "expected": "",
                    "actual": comp.stderr.strip(),
                    "passed": False
                }]
            run_cmd = [JAVA_CMD, "-cp", tmpdir, "Main"]

        elif lang == "js":
            src_path = os.path.join(tmpdir, "solution.js")
            with open(src_path, "w", encoding="utf-8") as f:
                f.write(code)
            run_cmd = [NODE_CMD, src_path]

        else:
            return "Unsupported Language", [{
                "input": "", "expected": "",
                "actual": f"Unsupported language: {lang}",
                "passed": False
            }]

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
                    passed = (actual == expected)
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
        # Clean up temp files
        def cleanup():
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        threading.Thread(target=cleanup, daemon=True).start()


@app.route("/")
def index():
    problems = get_problems()
    return render_template("index.html", problems=problems)


@app.route("/api/problem/<pid>")
def api_problem(pid):
    problem = get_problem(pid)
    if not problem:
        return jsonify({"error": "Problem not found"}), 404
    # Don't send test cases to client (only needed by server)
    return jsonify({
        "id": problem["id"],
        "desc": problem["desc"],
    })


@app.route("/api/submit", methods=["POST"])
def api_submit():
    data = request.get_json()
    pid = data.get("pid", "")
    code = data.get("code", "")
    problem = get_problem(pid)
    if not problem:
        return jsonify({"error": "Problem not found"}), 404
    if not code.strip():
        return jsonify({"error": "Code is empty"}), 400
    if len(code) > 65536:
        return jsonify({"error": "Code too long (max 64KB)"}), 400

    lang = data.get("lang", "")
    verdict, results = judge_code(code, problem["test_cases"], lang_hint=lang)

    return jsonify({
        "verdict": verdict,
        "results": results,
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
