#!/usr/bin/env python3
"""Мост автодополнения Wired Code. QML не умеет говорить с процессом через stdin/stdout, поэтому мост отвечает
редактору по HTTP на 127.0.0.1: C++ — через clangd (LSP по stdio, запускается при первом C++-файле),
Python — через библиотеку jedi (если установлена: python-jedi). Порт, токен и что доступно пишутся в файл:
lsp.py <файл-порта> → «порт токен cpp,python».
POST /complete {lang, path, text, line, col} → {items: [{label, insert, kind, detail, doc, start}]}
kind — виды LSP (3 функция, 7 класс, 6 переменная…). Мост завершается вместе с редактором (родительским процессом)."""
import json, os, secrets, shutil, signal, subprocess, sys, threading, time
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
            "textDocument": {"completion": {"completionItem": {"snippetSupport": False, "documentationFormat": ["plaintext"]}}}},
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

    def complete(self, path, text, line, col):
        uri = self.sync(path, text)
        r = self.request("textDocument/completion", {"textDocument": {"uri": uri}, "position": {"line": line, "character": col}})
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
            args = (body["path"], body["text"], int(body["line"]), int(body["col"]))
            try:
                if body.get("lang") == "python": res = {"items": py_complete(*args) if jedi else []}
                else: res = {"items": clangd().complete(*args) if shutil.which("clangd") else []}
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
        cd.p.kill()
        assert any(l.startswith("push_back") for l in labels), labels[:10]
        print("C++ OK", labels[:2])
    else: print("C++: clangd не найден — пропуск")
    if jedi:
        p = os.path.join(d, "t.py")
        labels = [i["label"] for i in py_complete(p, "xs = [1, 2]\nxs.app\n", 1, 6)]
        assert "append(object)" in labels or any(l.startswith("append(") for l in labels), labels
        items = py_complete(p, "pri", 0, 3)
        assert items[0]["insert"] == "print" and items[0]["kind"] == 3, items[:2]
        print("Python OK", labels[:2], items[0]["label"])
    else: print("Python: jedi не установлен (sudo pacman -S python-jedi) — пропуск")
    print("LSP TEST OK")


if __name__ == "__main__":
    selftest() if sys.argv[1:] == ["--test"] else main(sys.argv[1])
