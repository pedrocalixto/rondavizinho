"""Acesso ao DVR Intelbras/Dahua-compat: eventos SMD + frames RTSP.

SOMENTE eventos de "Detecção inteligente de pessoas e veículos" (nome oficial
Intelbras; na API da plataforma o recurso chama-se SmartMotionDetect/"SMD" e os
eventos SmartMotionHuman/Vehicle). O fallback VideoMotion foi removido de
propósito: movimento bruto (chuva, sombra, farol) mandado ao LLM é pouquíssimo
efetivo e gera falso positivo estrutural. DVR sem o recurso não é suportado;
`suporta_smd()` sonda a capacidade real via API (a etiqueta da caixa mente: há
MHDX que anuncia o recurso sem expô-lo no CGI).
"""
import subprocess, time, urllib.error, urllib.parse, urllib.request

CODES = ["SmartMotionHuman", "SmartMotionVehicle"]


class Dvr:
    def __init__(self, cfg):
        d = cfg["dvr"]
        self.host, self.user, self.senha = d["host"], d["user"], d["senha"]
        self.porta_http, self.porta_rtsp = d["porta_http"], d["porta_rtsp"]
        self.jpeg_q = str(cfg["calibracao"]["jpeg_q"])
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, f"http://{self.host}:{self.porta_http}", self.user, self.senha)
        self.opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))

    def frame(self, canal_1based, timeout=40):
        """1 frame JPEG do main stream (HD). Levanta RuntimeError se falhar 2x."""
        url = (f"rtsp://{self.user}:{urllib.parse.quote(self.senha)}@{self.host}:"
               f"{self.porta_rtsp}/cam/realmonitor?channel={canal_1based}&subtype=0")
        for _ in range(2):
            r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                                "-rtsp_transport", "tcp", "-i", url,
                                "-frames:v", "1", "-q:v", self.jpeg_q,
                                "-f", "image2", "pipe:1"],
                               capture_output=True, timeout=timeout)
            if r.returncode == 0 and r.stdout:
                return r.stdout
            time.sleep(3)
        raise RuntimeError(f"ffmpeg não capturou frame do canal {canal_1based}")

    def suporta_smd(self):
        """Sonda a capacidade REAL de SMD via API (não confiar no menu/etiqueta).
        True/False; None se não deu para determinar (DVR fora, auth...)."""
        url = (f"http://{self.host}:{self.porta_http}/cgi-bin/configManager.cgi"
               f"?action=getConfig&name=SmartMotionDetect")
        try:
            corpo = self.opener.open(url, timeout=10).read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            return False if e.code in (400, 404) else None
        except Exception:
            return None
        return "SmartMotionDetect" in corpo and "Error" not in corpo.split("\n")[0]

    def escuta(self, on_evento, on_tick):
        """Long-poll do eventManager; chama on_evento(code, canal_0based, action)
        para cada evento e on_tick() a cada linha (heartbeat=10s garante tick)."""
        codes = "%2C".join(CODES)
        url = (f"http://{self.host}:{self.porta_http}/cgi-bin/eventManager.cgi"
               f"?action=attach&codes=%5B{codes}%5D&heartbeat=10")
        resp = self.opener.open(url, timeout=60)
        for raw in resp:
            on_tick()
            line = raw.decode(errors="replace").strip()
            if "Code=" not in line:
                continue
            kv = dict(p.split("=", 1) for p in line.split(";") if "=" in p)
            code, action = kv.get("Code", ""), kv.get("action", "")
            try:
                canal = int(kv.get("index", -1))
            except ValueError:
                continue
            if code not in CODES or action not in ("Start", "Stop") or canal < 0:
                continue
            on_evento(code, canal, action)

    def tipo_do_code(self, code):
        return "pessoa" if code == "SmartMotionHuman" else "veiculo"
