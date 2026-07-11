"""Telegram: envio (foto/msg/pergunta com botões) e recepção (comandos + callbacks).

Recepção roda em thread própria com getUpdates long-poll — o MESMO poll atende
comandos (/vigiar /status /ajuda) e respostas de botão do HITL (callback_query).
"""
import json, queue, threading, time, urllib.error, urllib.request


def _abre(req, timeout):
    """urlopen que expõe o corpo do erro da API (ex.: 'chat not found')."""
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            corpo = e.read().decode()[:300]
        except Exception:
            corpo = ""
        raise RuntimeError(f"telegram HTTP {e.code}: {corpo}") from None


class Telegram:
    def __init__(self, cfg, log):
        t = cfg["telegram"]
        self.base = f"https://api.telegram.org/bot{t['token']}"
        self.chats = [str(c) for c in (t["chat_id"] if isinstance(t["chat_id"], list)
                                       else [t["chat_id"]])]
        self.log = log
        self.callbacks = queue.Queue()
        self.comandos = {}
        self._offset = 0

    def _post(self, metodo, payload, timeout=30):
        req = urllib.request.Request(f"{self.base}/{metodo}",
            json.dumps(payload).encode(), {"Content-Type": "application/json"})
        return json.loads(_abre(req, timeout).read())

    def msg(self, texto, botoes=None):
        """botoes: [("rótulo", "callback_data"), ...] — vira inline keyboard.
        Vai para todos os chats autorizados (qualquer um pode responder o HITL)."""
        for chat in self.chats:
            p = {"chat_id": chat, "text": texto}
            if botoes:
                p["reply_markup"] = {"inline_keyboard":
                    [[{"text": r, "callback_data": d}] for r, d in botoes]}
            try:
                self._post("sendMessage", p)
            except Exception as e:
                self.log(f"telegram: falha p/ chat {chat}: {e}")

    def foto(self, jpeg, caption):
        b = "b0und4ry" + str(int(time.time()))
        part = lambda k, v: (f'--{b}\r\nContent-Disposition: form-data; '
                             f'name="{k}"\r\n\r\n{v}\r\n').encode()
        for chat in self.chats:
            body = (part("chat_id", chat) + part("caption", caption[:1024])
                + (f'--{b}\r\nContent-Disposition: form-data; name="photo"; '
                   f'filename="vigia.jpg"\r\nContent-Type: image/jpeg\r\n\r\n').encode()
                + jpeg + f"\r\n--{b}--\r\n".encode())
            req = urllib.request.Request(f"{self.base}/sendPhoto", body,
                {"Content-Type": f"multipart/form-data; boundary={b}"})
            try:
                _abre(req, 60).read()
            except Exception as e:
                self.log(f"telegram: falha p/ chat {chat}: {e}")

    def registra_comando(self, nome, fn):
        self.comandos[nome] = fn

    def _trata_update(self, u):
        if "callback_query" in u:
            cq = u["callback_query"]
            self.callbacks.put(cq.get("data", ""))
            try:
                self._post("answerCallbackQuery", {"callback_query_id": cq["id"]})
            except Exception:
                pass
            return
        msg = u.get("message", {})
        if str(msg.get("chat", {}).get("id", "")) not in self.chats:
            return
        texto = (msg.get("text") or "").strip()
        if not texto.startswith("/"):
            return
        partes = texto.split()
        fn = self.comandos.get(partes[0].split("@")[0])
        if fn:
            try:
                fn(partes[1:])
            except Exception as e:
                self.log(f"comando {partes[0]} falhou: {e}")

    def loop_updates(self):
        """Thread: long-poll getUpdates. Nunca morre — loga e reconecta."""
        while True:
            try:
                r = self._post("getUpdates",
                    {"offset": self._offset + 1, "timeout": 50,
                     "allowed_updates": ["message", "callback_query"]},
                    timeout=60)
                for u in r.get("result", []):
                    self._offset = max(self._offset, u["update_id"])
                    self._trata_update(u)
            except Exception as e:
                self.log(f"getUpdates falhou ({e}); retry em 10s")
                time.sleep(10)

    def inicia(self):
        threading.Thread(target=self.loop_updates, daemon=True).start()

    def espera_callback(self, prefixo, segundos):
        """Aguarda um callback_data que comece com prefixo. None se timeout."""
        fim = time.time() + segundos
        while time.time() < fim:
            try:
                data = self.callbacks.get(timeout=min(5, max(0.1, fim - time.time())))
            except queue.Empty:
                continue
            if data.startswith(prefixo):
                return data[len(prefixo):]
        return None
