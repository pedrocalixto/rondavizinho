"""Autodiagnóstico: falhas persistentes viram aviso ao dono NO TELEGRAM com
instrução de correção (suporte self-service). Heartbeat opt-in (só up/down)."""
import json, threading, time, urllib.request

INSTRUCOES = {
    "dvr_attach": ("📵 Não estou conseguindo falar com o DVR há vários minutos.\n"
                   "Verifique: o DVR está ligado? O cabo de rede está conectado?\n"
                   "Se reiniciou o roteador, aguarde 5 min que eu reconecto sozinho."),
    "dvr_frame": ("📷 O DVR responde mas não consigo puxar imagem das câmeras.\n"
                  "Verifique no monitor do DVR se as câmeras aparecem."),
    "telegram": None,
    "llm": ("🧠 A análise de imagem (IA) está falhando. Os avisos continuam saindo, "
            "mas sem descrição. Se persistir amanhã, verifique a chave da sua IA "
            "na página do assistente (http://localhost:8080)."),
}
LIMIAR = 5
REAVISO = 6 * 3600


class Diag:
    def __init__(self, cfg, tg, log):
        self.cfg, self.tg, self.log = cfg, tg, log
        self.falhas = {}
        self.avisado = {}

    def ok(self, chave):
        self.falhas[chave] = 0

    def falha(self, chave, detalhe=""):
        n = self.falhas.get(chave, 0) + 1
        self.falhas[chave] = n
        self.log(f"diag {chave}: falha #{n} {detalhe}")
        if n < LIMIAR or time.time() - self.avisado.get(chave, 0) < REAVISO:
            return
        texto = INSTRUCOES.get(chave)
        if texto:
            try:
                self.tg.msg(texto)
                self.avisado[chave] = time.time()
            except Exception:
                pass

    def inicia_heartbeat(self):
        h = self.cfg["heartbeat"]
        if not h["url"]:
            return

        def loop():
            while True:
                try:
                    body = json.dumps({"casa_id": h["casa_id"], "up": True,
                                       "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}).encode()
                    urllib.request.urlopen(urllib.request.Request(
                        h["url"], body, {"Content-Type": "application/json"}),
                        timeout=15).read()
                except Exception:
                    pass
                time.sleep(900)

        threading.Thread(target=loop, daemon=True).start()
