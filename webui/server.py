"""Painel web do RondaVizinho — histórico de detecções, fotos, gráfico e insights.

Stdlib puro no backend; Bootstrap 5 + Chart.js via CDN no frontend (zero build).
Lê o que o daemon já grava: deteccoes.csv (todas as detecções), eventos.jsonl
(alertas com classe/ficha) e arquivo/YYYY-MM/*.jpg+txt (fotos dos alertas).

Uso: python3 webui/server.py [--porta 8080] [--check]
Serve na LAN (http://localhost:8080). Sem autenticação — LAN da casa.
"""
import json, os, sys, time
from collections import Counter
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vigia import config as cfgmod
import setup as setupmod

ESTATICO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def serie_dias(contagem_por_dia):
    """dias ordenados, total/dia e aumento percentual ACUMULADO vs 1º dia.
    pct[i] = (cumulativo[i] - cumulativo[0]) / cumulativo[0] * 100"""
    dias = sorted(contagem_por_dia)
    tot = [contagem_por_dia[d] for d in dias]
    cum, acc = [], 0
    for n in tot:
        acc += n
        cum.append(acc)
    if not cum or cum[0] == 0:
        return dias, tot, [0.0] * len(cum)
    base = cum[0]
    pct = [round((c - base) / base * 100, 1) for c in cum]
    return dias, tot, pct


def monta_insights(por_dia, por_hora, por_canal, por_tipo, alertas, total):
    """Frases prontas p/ os cards — só o que é acionável pro morador."""
    ins = []
    if not total:
        return ["Ainda não há detecções registradas — o painel enche sozinho "
                "conforme o vigia trabalha."]
    if por_hora:
        h = por_hora.most_common(1)[0]
        ins.append(f"🕐 Horário de maior movimento: {h[0]:02d}h "
                   f"({h[1]} detecções). Bom horário para reforçar atenção.")
    if por_canal:
        c = por_canal.most_common(1)[0]
        ins.append(f"📷 Câmera mais ativa: {c[0]} "
                   f"({c[1]} detecções, {round(c[1] / total * 100)}% do total).")
    p, v = por_tipo.get("pessoa", 0), por_tipo.get("veiculo", 0)
    if p or v:
        ins.append(f"🚶 Pessoas × 🚗 veículos: {p} × {v}.")
    razao = round(alertas / total * 100, 1) if total else 0
    ins.append(f"🔎 De {total} detecções, {alertas} viraram alerta "
               f"({razao}%) — o resto foi filtrado como rotina.")
    if len(por_dia) >= 8:
        dias = sorted(por_dia)
        ult7 = sum(por_dia[d] for d in dias[-7:]) / 7
        ant = sum(por_dia[d] for d in dias[:-7]) / max(1, len(dias) - 7)
        if ant:
            var = round((ult7 - ant) / ant * 100)
            seta = "📈 subiu" if var > 10 else ("📉 caiu" if var < -10 else "➡️ estável")
            ins.append(f"{seta.split()[0]} Movimento na última semana {seta.split(' ', 1)[1]} "
                       f"{abs(var)}% vs período anterior." if abs(var) > 10 else
                       "➡️ Movimento estável na última semana.")
    return ins


def _self_check():
    dias, tot, pct = serie_dias({"2026-01-01": 10, "2026-01-02": 20, "2026-01-03": 30})
    assert dias == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert tot == [10, 20, 30] and pct == [0.0, 200.0, 500.0]
    _, _, pct0 = serie_dias({"d": 0})
    assert pct0 == [0.0]
    assert serie_dias({}) == ([], [], [])
    ins = monta_insights({}, Counter(), Counter(), Counter(), 0, 0)
    assert "Ainda não há" in ins[0]
    ins = monta_insights({"d": 4}, Counter({14: 3}), Counter({"frente": 4}),
                         Counter({"pessoa": 3, "veiculo": 1}), 1, 4)
    assert any("14h" in i for i in ins) and any("frente" in i for i in ins)
    print("self-check ok")


class Dados:
    def __init__(self, cfg):
        self.dir = cfg["storage"]["dir"]
        self.csv = os.path.join(self.dir, "deteccoes.csv")
        self.jsonl = os.path.join(self.dir, "eventos.jsonl")
        self.arquivo = os.path.join(self.dir, "arquivo")
        self.nomes = {n: c.get("nome", f"câmera {n}")
                      for n, c in cfg["canais"].items()}

    def deteccoes(self):
        """Linhas do CSV como dicts (ts, code, canal, extra, descricao)."""
        try:
            with open(self.csv, encoding="utf-8", errors="replace") as f:
                linhas = f.read().splitlines()[1:]
        except FileNotFoundError:
            return []
        out = []
        for ln in linhas:
            p = ln.split(",", 4)
            if len(p) == 5:
                out.append({"ts": p[0], "code": p[1], "canal": p[2],
                            "extra": p[3], "descricao": p[4]})
        return out

    def alertas(self):
        try:
            with open(self.jsonl, encoding="utf-8") as f:
                return [json.loads(l) for l in f if l.strip()]
        except (FileNotFoundError, ValueError):
            return []

    def resumo(self):
        det = self.deteccoes()
        por_dia, por_hora, por_canal, por_tipo = Counter(), Counter(), Counter(), Counter()
        tipo_map = {"SmartMotionHuman": "pessoa", "SmartMotionVehicle": "veiculo",
                    "VideoMotion": "movimento"}
        for d in det:
            dia = d["ts"][:10]
            por_dia[dia] += 1
            try:
                por_hora[int(d["ts"][11:13])] += 1
            except ValueError:
                pass
            nome = self.nomes.get(str(int(d["canal"]) + 1), f"canal {d['canal']}") \
                if d["canal"].isdigit() else d["canal"]
            por_canal[nome] += 1
            por_tipo[tipo_map.get(d["code"], d["code"])] += 1
        alertas = self.alertas()
        dias, tot, pct = serie_dias(dict(por_dia))
        alertas_dia = Counter(a.get("ts", "")[:10] for a in alertas)
        return {"dias": dias, "total_dia": tot, "pct_acumulado": pct,
                "alertas_dia": [alertas_dia.get(d, 0) for d in dias],
                "total": len(det), "total_alertas": len(alertas),
                "por_tipo": dict(por_tipo),
                "por_canal": dict(por_canal.most_common(8)),
                "insights": monta_insights(dict(por_dia), por_hora, por_canal,
                                           por_tipo, len(alertas), len(det))}

    def fotos(self, dia=None, limite=24):
        """Fotos dos alertas (mais recentes primeiro) com o contexto do .txt."""
        out = []
        if not os.path.isdir(self.arquivo):
            return out
        for mes in sorted(os.listdir(self.arquivo), reverse=True):
            mdir = os.path.join(self.arquivo, mes)
            if not os.path.isdir(mdir):
                continue
            for jpg in sorted((f for f in os.listdir(mdir) if f.endswith(".jpg")),
                              reverse=True):
                data = f"{jpg[:4]}-{jpg[4:6]}-{jpg[6:8]}"
                if dia and data != dia:
                    continue
                txt, ctx = os.path.join(mdir, jpg[:-4] + ".txt"), ""
                try:
                    with open(txt, encoding="utf-8", errors="replace") as f:
                        ctx = f.read().strip()
                except OSError:
                    pass
                out.append({"url": f"/foto?p={mes}/{jpg}", "nome": jpg,
                            "data": data, "hora": f"{jpg[9:11]}:{jpg[11:13]}",
                            "contexto": ctx})
                if len(out) >= limite:
                    return out
        return out

    def caminho_foto(self, rel):
        """Resolve com proteção contra path traversal."""
        alvo = os.path.realpath(os.path.join(self.arquivo, rel))
        raiz = os.path.realpath(self.arquivo)
        return alvo if alvo.startswith(raiz + os.sep) and alvo.endswith(".jpg") \
            else None


def cria_handler(box, assistente):
    """box["dados"] = Dados(cfg) ou None (sem config → modo assistente)."""
    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=ESTATICO, **kw)

        def _json(self, obj, status=200):
            corpo = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def _jpeg(self, corpo, cache=None):
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(corpo)))
            if cache:
                self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(corpo)

        def do_GET(self):
            rota, _, query = self.path.partition("?")
            q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            dados = box["dados"]
            if rota == "/api/setup/thumb":
                try:
                    return self._jpeg(assistente.thumb(q.get("canal", "1")))
                except Exception as e:
                    return self._json({"ok": False, "erro": str(e)}, 500)
            if rota.startswith("/api/setup/"):
                status, obj = assistente.trata(rota[len("/api/setup/"):],
                                               "GET", None, q)
                return self._json(obj, status)
            if rota == "/" and dados is None:
                self.send_response(302)
                self.send_header("Location", "/setup.html")
                self.end_headers()
                return
            if dados is None and rota.startswith(("/api/", "/foto")):
                return self._json({"erro": "instalação não concluída",
                                   "setup": True}, 409)
            if rota == "/api/resumo":
                return self._json(dados.resumo())
            if rota == "/api/fotos":
                return self._json(dados.fotos(dia=q.get("dia") or None))
            if rota == "/foto":
                caminho = dados.caminho_foto(q.get("p", ""))
                if not caminho or not os.path.exists(caminho):
                    self.send_error(404)
                    return
                with open(caminho, "rb") as f:
                    return self._jpeg(f.read(), cache="max-age=86400")
            super().do_GET()

        def do_POST(self):
            rota = self.path.partition("?")[0]
            if not rota.startswith("/api/setup/"):
                self.send_error(404)
                return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                corpo = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                return self._json({"ok": False, "erro": "JSON inválido"}, 400)
            status, obj = assistente.trata(rota[len("/api/setup/"):],
                                           "POST", corpo, {})
            self._json(obj, status)

        def log_message(self, fmt, *args):
            pass

    return H


def main():
    if "--check" in sys.argv:
        _self_check()
        setupmod._self_check()
        return
    porta = int(sys.argv[sys.argv.index("--porta") + 1]) \
        if "--porta" in sys.argv else 8080
    box = {"dados": None}

    def carrega_dados():
        """True se há config válida; senão o servidor fica em modo assistente."""
        try:
            box["dados"] = Dados(cfgmod.carrega())
            return True
        except Exception as e:
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} sem config válida "
                  f"({e}) — servindo o assistente de instalação", flush=True)
            return False

    carrega_dados()
    assistente = setupmod.Assistente(ao_concluir=carrega_dados)
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} painel em http://0.0.0.0:{porta}",
          flush=True)
    ThreadingHTTPServer(("0.0.0.0", porta),
                        cria_handler(box, assistente)).serve_forever()


if __name__ == "__main__":
    main()
