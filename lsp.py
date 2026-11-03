#!/usr/bin/env python3
"""Мост автодополнения Wired Code. QML не умеет говорить с процессом через stdin/stdout, поэтому мост отвечает
редактору по HTTP на 127.0.0.1: C++ — через clangd (LSP по stdio, запускается при первом C++-файле),
Python — через библиотеку jedi (если установлена: python-jedi). Порт, токен и что доступно пишутся в файл:
lsp.py <файл-порта> → «порт токен cpp,python».
POST /<op> {lang, path, text, line, col, ...} (line и col с 0):
  complete  → {items: [{label, insert, kind, detail, doc, start}]}   kind — виды LSP (3 функция, 7 класс, 6 переменная…)
  signature → {label, params: [[начало, конец]…], active}            подсказка параметров функции
  hover     → {text}                                                 тип и описание под мышью
  definition→ {path, line, col}                                      line и col с 1
  rename {name} → {files: {путь: новый текст}}
  symbols   → {items: [{name, kind, line, depth}]}                   структура файла, line с 1
  highlight → {items: [[строка, столбец, длина]…]}                   где в файле это же имя (та же переменная)
  format {tab} → {text} или {error}                                  clang-format / black / ruff / autopep8
Мост завершается вместе с редактором (родительским процессом)."""
import json, os, secrets, shutil, signal, subprocess, sys, threading, time
from urllib.parse import unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STD = os.environ.get("WIRED_CODE_STD", "c++20")


class Clangd:
    def __init__(self):
        # --completion-parse=always: иначе сразу после открытия ответ пустой — заголовки ещё не разобраны
        self.p = subprocess.Popen(["clangd", "--header-insertion=never", "--background-index", "--log=error",
                                   "--completion-style=detailed", "--limit-results=80", "--completion-parse=always"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.lock, self.n, self.wait, self.docs = threading.Lock(), 0, {}, {}
        threading.Thread(target=self.reader, daemon=True).start()
        self.request("initialize", {"processId": os.getpid(), "rootUri": None, "capabilities": {
            "textDocument": {"completion": {"completionItem": {"snippetSupport": False, "documentationFormat": ["plaintext"]}},
                             "hover": {"contentFormat": ["plaintext"]},
                             "signatureHelp": {"signatureInformation": {"parameterInformation": {"labelOffsetSupport": True}}},
                             "documentSymbol": {"hierarchicalDocumentSymbolSupport": True}}},
            "initializationOptions": {"fallbackFlags": [f"-std={STD}", "-Wall", "-Wextra"]}})
        self.notify("initialized", {})

    def send(self, msg):
        b = json.dumps(msg).encode()
        with self.lock:
            self.p.stdin.write(b"Content-Length: %d\r\n\r\n" % len(b) + b); self.p.stdin.flush()

    def notify(self, method, params): self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method, params, timeout=15):
        with self.lock: self.n += 1; i = self.n
        ev = threading.Event(); self.wait[i] = [ev, None]
        self.send({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
        ev.wait(timeout)
        return self.wait.pop(i)[1]

    def reader(self):
        f = self.p.stdout
        while True:
            n = 0
            while True:
                line = f.readline()
                if not line: return
                if line in (b"\r\n", b"\n"): break
                if line.lower().startswith(b"content-length:"): n = int(line.split(b":")[1])
            msg = json.loads(f.read(n))
            if "id" in msg and msg["id"] in self.wait and "method" not in msg:
                self.wait[msg["id"]][1] = msg.get("result"); self.wait[msg["id"]][0].set()

    def sync(self, path, text):   # открыть файл или отправить новый текст целиком
        uri = "file://" + path
        if uri not in self.docs:
            self.docs[uri] = 1
            self.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "cpp", "version": 1, "text": text}})
        else:
            self.docs[uri] += 1
            self.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": self.docs[uri]}, "contentChanges": [{"text": text}]})
        return uri

    def at(self, method, path, text, line, col, **extra):   # запрос о месте в файле
        uri = self.sync(path, text)
        return self.request(method, {"textDocument": {"uri": uri}, "position": {"line": line, "character": col}, **extra})

    def complete(self, path, text, line, col):
        r = self.at("textDocument/completion", path, text, line, col)
        items = r.get("items", []) if isinstance(r, dict) else (r or [])
        out = []
        for it in sorted(items, key=lambda x: x.get("sortText", x["label"])):
            te = it.get("textEdit")
            ins = te["newText"] if te else it.get("insertText", it["label"].strip())
            doc = it.get("documentation")
            out.append({"label": it["label"].strip(), "insert": ins, "kind": it.get("kind", 0), "detail": it.get("detail", ""),
                        "doc": (doc.get("value", "") if isinstance(doc, dict) else doc or "")[:400],
                        "start": te["range"]["start"]["character"] if te else -1})
        return out

    def signature(self, path, text, line, col):
        r = self.at("textDocument/signatureHelp", path, text, line, col)
        if not r or not r.get("signatures"): return {}
        s = r["signatures"][r.get("activeSignature") or 0]
        params = [p["label"] if isinstance(p["label"], list) else span(s["label"], p["label"]) for p in s.get("parameters", [])]
        return {"label": s["label"], "params": params, "active": s.get("activeParameter", r.get("activeParameter")) or 0}

    def hover(self, path, text, line, col):
        r = self.at("textDocument/hover", path, text, line, col)
        c = (r or {}).get("contents", "")
        return {"text": (c.get("value", "") if isinstance(c, dict) else str(c)).strip()[:1500]}

    def definition(self, path, text, line, col):
        r = self.at("textDocument/definition", path, text, line, col)
        r = r[0] if isinstance(r, list) and r else r
        if not r: return {}
        uri, rng = r.get("targetUri", r.get("uri")), r.get("targetSelectionRange", r.get("range"))
        return {"path": unquote(uri[7:]), "line": rng["start"]["line"] + 1, "col": rng["start"]["character"] + 1}

    def highlight(self, path, text, line, col):
        r = self.at("textDocument/documentHighlight", path, text, line, col) or []
        return {"items": [[h["range"]["start"]["line"], h["range"]["start"]["character"],
                           h["range"]["end"]["character"] - h["range"]["start"]["character"]]
                          for h in r if h["range"]["start"]["line"] == h["range"]["end"]["line"]]}

    def rename(self, path, text, line, col, name):
        r = self.at("textDocument/rename", path, text, line, col, newName=name) or {}
        ch = dict(r.get("changes") or {})
        for d in r.get("documentChanges") or []: ch[d["textDocument"]["uri"]] = d["edits"]
        files = {}
        for uri, edits in ch.items():
            p = unquote(uri[7:])
            if p == path: src = text
            else:
                with open(p) as f: src = f.read()
            files[p] = apply_edits(src, edits)
        return {"files": files}

    def symbols(self, path, text):
        uri = self.sync(path, text)
        out = []
        def walk(items, depth):
            for s in items or []:
                rng = s.get("selectionRange") or s.get("location", {}).get("range") or s["range"]
                out.append({"name": s["name"], "kind": s["kind"], "line": rng["start"]["line"] + 1, "depth": depth})
                walk(s.get("children"), depth + 1)
        walk(self.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}}), 0)
        return {"items": out}


def span(label, part):   # параметр, заданный строкой, → [начало, конец] в подписи
    i = label.find(part)
    return [i, i + len(part)] if i >= 0 else [0, 0]


def apply_edits(src, edits):   # правки LSP (строка/столбец) → новый текст; с конца, чтобы позиции не съезжали
    # shortcut: столбцы LSP в UTF-16, здесь — в символах Python; расходятся только на эмодзи и т. п. в той же строке
    starts = [0] + [i + 1 for i, c in enumerate(src) if c == "\n"]
    pos = lambda p: min(len(src), starts[min(p["line"], len(starts) - 1)] + p["character"])
    for e in sorted(edits, key=lambda e: (e["range"]["start"]["line"], e["range"]["start"]["character"]), reverse=True):
        src = src[:pos(e["range"]["start"])] + e["newText"] + src[pos(e["range"]["end"]):]
    return src


try: import jedi
except ImportError: jedi = None
# вид варианта jedi → вид LSP (как у clangd), чтобы редактор рисовал одинаково
JEDI_KIND = {"function": 3, "class": 7, "module": 9, "instance": 6, "statement": 6, "param": 6, "keyword": 14,
             "property": 10, "path": 17}


def py_complete(path, text, line, col):
    out = []
    for c in jedi.Script(code=text, path=path or None).complete(line + 1, col)[:80]:
        call = c.type in ("function", "class")
        label = c.name + ("()" if call else "")
        if call:   # есть ли параметры — от этого зависит, встанет ли курсор внутрь скобок
            try:
                sig = c.get_signatures()
                if sig and sig[0].params and not (c.type == "class" and [p.name for p in sig[0].params] == ["self"]):
                    label = c.name + "(" + ", ".join(p.name for p in sig[0].params) + ")"
            except Exception: pass
        try: doc = c.docstring(raw=True)[:400]
        except Exception: doc = ""
        out.append({"label": label, "insert": c.name, "kind": JEDI_KIND.get(c.type, 0), "detail": c.type if c.type != "statement" else "",
                    "doc": doc, "start": -1})
    return out


def py_signature(path, text, line, col):
    sigs = jedi.Script(code=text, path=path or None).get_signatures(line + 1, col)
    if not sigs: return {}
    s, params, label = sigs[0], [], sigs[0].name + "("
    for i, p in enumerate(s.params):
        if i: label += ", "
        params.append([len(label), len(label) + len(p.to_string())]); label += p.to_string()
    return {"label": label + ")", "params": params, "active": s.index or 0}


def py_hover(path, text, line, col):
    names = jedi.Script(code=text, path=path or None).help(line + 1, col)
    if not names: return {"text": ""}
    n = names[0]
    doc = n.docstring() or ""
    return {"text": (doc if doc.startswith(n.name) else (n.description + "\n\n" + doc)).strip()[:1500]}


def py_definition(path, text, line, col):
    for d in jedi.Script(code=text, path=path or None).goto(line + 1, col, follow_imports=True):
        if d.module_path and d.line: return {"path": str(d.module_path), "line": d.line, "col": d.column + 1}
    return {}


def py_highlight(path, text, line, col):
    refs = jedi.Script(code=text, path=path or None).get_references(line + 1, col, scope="file")
    return {"items": [[r.line - 1, r.column, len(r.name)] for r in refs if r.line]}


def py_rename(path, text, line, col, name):
    ch = jedi.Script(code=text, path=path or None).rename(line + 1, col, new_name=name).get_changed_files()
    return {"files": {str(p): f.get_new_code() for p, f in ch.items()}}


def py_symbols(path, text):
    out = []
    for n in jedi.Script(code=text, path=path or None).get_names(all_scopes=True, definitions=True):
        if n.type not in ("function", "class"): continue
        depth, p = 0, n.parent()
        while p and p.type != "module": depth += 1; p = p.parent()
        out.append({"name": n.name, "kind": 5 if n.type == "class" else 6 if depth else 12, "line": n.line, "depth": depth})
    return {"items": out}


def find_up(d, names):   # .clang-format рядом с файлом или выше
    while d and d != "/":
        if any(os.path.exists(os.path.join(d, n)) for n in names): return True
        d = os.path.dirname(d)
    return False


def fmt(lang, path, text, tab):
    if lang == "cpp":
        if not shutil.which("clang-format"): return {"error": "нет clang-format: sudo pacman -S clang"}
        style = "file" if path and find_up(os.path.dirname(path), (".clang-format", "_clang-format")) else \
            "{BasedOnStyle: LLVM, IndentWidth: %d, ColumnLimit: 110, AllowShortFunctionsOnASingleLine: Empty}" % tab
        cmd = ["clang-format", "--assume-filename=" + (path or "main.cpp"), "--style=" + style]
    else:
        has = lambda m: subprocess.run([sys.executable, "-c", "import " + m], capture_output=True).returncode == 0
        cmd = (["black", "-q", "-"] if shutil.which("black") else [sys.executable, "-m", "black", "-q", "-"] if has("black")
               else ["ruff", "format", "-"] if shutil.which("ruff") else [sys.executable, "-m", "autopep8", "-"] if has("autopep8") else None)
        if not cmd: return {"error": "нет форматтера Python: sudo pacman -S python-black"}
    r = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=30, cwd=os.path.dirname(path) if path else None)
    if r.returncode: return {"error": (r.stderr.strip().splitlines() or ["ошибка форматирования"])[-1][:200]}
    return {"text": r.stdout}


def main(portfile):
    token, cdl = secrets.token_hex(16), threading.Lock()
    cd = [None]

    def clangd():   # clangd тяжёлый — запускаем, только когда понадобился C++
        with cdl:
            if cd[0] is None or cd[0].p.poll() is not None: cd[0] = Clangd()
            return cd[0]

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_POST(self):
            if self.headers.get("X-Token") != token: self.send_error(403); return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            op, path, text = self.path.strip("/"), body["path"], body["text"]
            pos = (int(body.get("line", 0)), int(body.get("col", 0)))
            try:
                if op == "format": res = fmt(body.get("lang"), path, text, int(body.get("tab", 4)))
                elif body.get("lang") == "python":
                    if not jedi: res = {}
                    elif op == "complete": res = {"items": py_complete(path, text, *pos)}
                    elif op == "symbols": res = py_symbols(path, text)
                    elif op == "rename": res = py_rename(path, text, *pos, body["name"])
                    else: res = {"signature": py_signature, "hover": py_hover, "definition": py_definition, "highlight": py_highlight}[op](path, text, *pos)
                elif not shutil.which("clangd"): res = {}
                else:
                    c = clangd()
                    if op == "complete": res = {"items": c.complete(path, text, *pos)}
                    elif op == "symbols": res = c.symbols(path, text)
                    elif op == "rename": res = c.rename(path, text, *pos, body["name"])
                    else: res = {"signature": c.signature, "hover": c.hover, "definition": c.definition, "highlight": c.highlight}[op](path, text, *pos)
            except Exception as e: res = {"items": [], "error": str(e)}
            b = json.dumps(res).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    os.makedirs(os.path.dirname(portfile), exist_ok=True)
    tmp = portfile + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f: f.write(f"{srv.server_address[1]} {token} " +
                                                                                     ",".join(l for l, ok in (("cpp", shutil.which("clangd")), ("python", jedi)) if ok) + "\n")
    os.replace(tmp, portfile)
    parent = os.getppid()

    def stop():
        if cd[0]: cd[0].p.kill()

    def watch():   # редактор закрылся — уходим и гасим clangd
        while os.getppid() == parent: time.sleep(1)
        stop(); os._exit(0)
    threading.Thread(target=watch, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit))   # kill → убрать файл порта в finally
    try: srv.serve_forever()
    finally:
        stop()
        try: os.remove(portfile)
        except OSError: pass


def selftest():   # python3 lsp.py --test: clangd предлагает push_back, jedi — append и print
    import tempfile
    d = tempfile.mkdtemp()
    if shutil.which("clangd"):
        cd = Clangd()
        text = "#include <vector>\nint main() {\n    std::vector<int> v;\n    v.pu\n}\n"
        labels = [i["label"] for i in cd.complete(os.path.join(d, "t.cpp"), text, 3, 8)]
        assert any(l.startswith("push_back") for l in labels), labels[:10]
        p = os.path.join(d, "t.cpp")
        text = "#include <cmath>\nint add(int a, int b) { return a + b; }\nint main() {\n    int total = add(1, 2);\n    return total;\n}\n"
        sig = cd.signature(p, text, 3, 20)
        assert sig["label"].startswith("add(") and sig["active"] == 0, sig
        assert "int" in cd.hover(p, text, 3, 9)["text"]
        assert cd.definition(p, text, 3, 17)["line"] == 2
        assert cd.rename(p, text, 3, 9, "sum")["files"][p].count("sum") == 2
        assert [s["name"] for s in cd.symbols(p, text)["items"]] == ["add", "main"]
        assert cd.highlight(p, text, 3, 9)["items"] == [[3, 8, 5], [4, 11, 5]], cd.highlight(p, text, 3, 9)
        cd.p.kill()
        print("C++ OK", labels[:2], sig["label"])
    else: print("C++: clangd не найден — пропуск")
    if jedi:
        p = os.path.join(d, "t.py")
        labels = [i["label"] for i in py_complete(p, "xs = [1, 2]\nxs.app\n", 1, 6)]
        assert "append(object)" in labels or any(l.startswith("append(") for l in labels), labels
        items = py_complete(p, "pri", 0, 3)
        assert items[0]["insert"] == "print" and items[0]["kind"] == 3, items[:2]
        text = "def add(a, b):\n    return a + b\n\nclass P:\n    def go(self): pass\n\ntotal = add(1, 2)\n"
        sig = py_signature(p, text, 6, 12)
        assert sig["label"] == "add(a, b)" and sig["params"][0] == [4, 5], sig
        assert "add" in py_hover(p, text, 6, 9)["text"]
        assert py_definition(p, text, 6, 9)["line"] == 1
        assert py_rename(p, text, 6, 1, "s")["files"][p].count("s = add") == 1
        assert [(s["name"], s["depth"]) for s in py_symbols(p, text)["items"]] == [("add", 0), ("P", 0), ("go", 1)]
        assert py_highlight(p, text, 1, 11)["items"] == [[0, 8, 1], [1, 11, 1]], py_highlight(p, text, 1, 11)
        print("Python OK", labels[:2], items[0]["label"], sig["label"])
    else: print("Python: jedi не установлен (sudo pacman -S python-jedi) — пропуск")
    r = fmt("cpp", os.path.join(d, "f.cpp"), "int main(){return 0;}", 4)
    assert r.get("text") == "int main() {\n    return 0;\n}" or "clang-format" in r.get("error", ""), r
    print("format:", r.get("text", r.get("error")).strip())
    print("LSP TEST OK")


if __name__ == "__main__":
    selftest() if sys.argv[1:] == ["--test"] else main(sys.argv[1])
