#!/usr/bin/env python3
"""Мост Wired Code ↔ clangd. QML не умеет говорить с процессом через stdin/stdout, поэтому мост держит clangd
(LSP по stdio) и отвечает редактору по HTTP на 127.0.0.1. Порт и токен пишутся в файл: lsp.py <файл-порта>.
POST /complete {path, text, line, col} → {items: [{label, insert, kind, detail, doc, start}]}
Мост завершается вместе с редактором (родительским процессом)."""
import json, os, secrets, signal, subprocess, sys, threading, time
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


def main(portfile):
    cd, token = Clangd(), secrets.token_hex(16)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_POST(self):
            if self.headers.get("X-Token") != token: self.send_error(403); return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            try: res = {"items": cd.complete(body["path"], body["text"], int(body["line"]), int(body["col"]))}
            except Exception as e: res = {"items": [], "error": str(e)}
            b = json.dumps(res).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    os.makedirs(os.path.dirname(portfile), exist_ok=True)
    tmp = portfile + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f: f.write(f"{srv.server_address[1]} {token}\n")
    os.replace(tmp, portfile)
    parent = os.getppid()

    def watch():   # редактор закрылся — уходим и гасим clangd
        while os.getppid() == parent and cd.p.poll() is None: time.sleep(1)
        cd.p.kill(); os._exit(0)
    threading.Thread(target=watch, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit))   # kill → убрать файл порта в finally
    try: srv.serve_forever()
    finally:
        cd.p.kill()
        try: os.remove(portfile)
        except OSError: pass


def selftest():   # python3 lsp.py --test: clangd предлагает push_back для std::vector
    import tempfile
    cd = Clangd()
    d = tempfile.mkdtemp(); p = os.path.join(d, "t.cpp")
    text = "#include <vector>\nint main() {\n    std::vector<int> v;\n    v.pu\n}\n"
    items = cd.complete(p, text, 3, 8)
    cd.p.kill()
    labels = [i["label"] for i in items]
    assert any(l.startswith("push_back") for l in labels), labels[:10]
    print("LSP TEST OK", labels[:3])


if __name__ == "__main__":
    selftest() if sys.argv[1:] == ["--test"] else main(sys.argv[1])
