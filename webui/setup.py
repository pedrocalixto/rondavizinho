"""Assistente de instalação (wizard F1) — roda no MESMO servidor do painel.

Máquina de estados retomável: o progresso (passo atual + rascunho da config)
vive em setup.json ao lado do config.json. O config.json REAL só é escrito no
passo final, após o teste ponta-a-ponta verde — um config parcial derrubaria o
daemon em loop, porque a validação na carga é dura de propósito.

Passos (cada um só avança verde):
  1 descoberta do DVR   2 credenciais   3 câmeras e zonas   4 eventos (sonda
  SMD real — sem fallback VideoMotion)   5 Telegram   6 IA de visão (pulável)
  7 semáforo final ponta-a-ponta
"""
import json, os, re, socket, threading, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

from vigia import config as cfgmod
from vigia.dvr import Dvr
from vigia.llm import Llm
from vigia.notify import Telegram

SETUP_PATH = os.path.join(os.path.dirname(cfgmod.CONFIG_PATH), "setup.json")
PORTAS_DVR = (37777, 554, 80)


def parse_kv(texto):
    """Linhas 'chave=valor' do CGI Dahua → dict (ignora linhas sem '=')."""
    out = {}
    for ln in texto.splitlines():
        k, sep, v = ln.partition("=")
        if sep:
            out[k.strip()] = v.strip()
    return out


def parse_titulos(kv):
    """table.ChannelTitle[N].Name=x → {"N+1": x} (config de canais é 1-based)."""
    out = {}
    for k, v in kv.items():
        m = re.match(r"table\.ChannelTitle\[(\d+)\]\.Name$", k)
        if m:
            out[str(int(m.group(1)) + 1)] = v
    return out


def ordena_candidatos(cand):
    """DVR confirmado primeiro; depois quem tem a porta proprietária 37777."""
    return sorted(cand, key=lambda c: (not c.get("confirmado"),
                                       37777 not in c.get("portas", [])))


def subrede_local():
    """Base /24 da rede local ('192.168.1.'). Socket UDP não envia nada — só
    força o SO a escolher a interface de saída."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ip.rsplit(".", 1)[0] + "."


def porta_aberta(ip, porta, timeout=0.4):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex((ip, porta)) == 0
    finally:
        s.close()


def scan_subrede(base, portas=PORTAS_DVR):
    """Varre base+1..254; devolve [{ip, portas}] de quem tem porta de DVR."""
    def sonda(ip):
        abertas = [p for p in portas if porta_aberta(ip, p)]
        return {"ip": ip, "portas": abertas} if abertas else None
    with ThreadPoolExecutor(max_workers=64) as ex:
        hits = ex.map(sonda, (base + str(n) for n in range(1, 255)))
    return [h for h in hits if h]


def parece_dvr(ip, porta_http=80, timeout=4):
    """Confirma plataforma Dahua-compat via magicBox getDeviceType — 401 com
    Digest já identifica (não precisa de credencial para confirmar)."""
    url = f"http://{ip}:{porta_http}/cgi-bin/magicBox.cgi?action=getDeviceType"
    try:
        corpo = urllib.request.urlopen(url, timeout=timeout).read().decode(errors="replace")
        return bool(parse_kv(corpo).get("type"))
    except urllib.error.HTTPError as e:
        auth = (e.headers.get("WWW-Authenticate") or "").lower()
        return e.code == 401 and "digest" in auth
    except Exception:
        return False


def _opener_digest(host, porta, user, senha):
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, f"http://{host}:{porta}", user, senha)
    return urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))


def testa_credenciais(host, porta_http, user, senha):
    """→ (modelo, {canal_1based: nome}). RuntimeError com mensagem leiga."""
    base = f"http://{host}:{porta_http}/cgi-bin"
    op = _opener_digest(host, porta_http, user, senha)
    try:
        tipo = op.open(base + "/magicBox.cgi?action=getDeviceType",
                       timeout=8).read().decode(errors="replace")
        titulos = op.open(base + "/configManager.cgi?action=getConfig&name=ChannelTitle",
                          timeout=8).read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise RuntimeError("o DVR recusou usuário ou senha — confira e tente "
                               "de novo (é o mesmo login do aplicativo/monitor)") from None
        raise RuntimeError(f"o DVR respondeu com erro HTTP {e.code}") from None
    except Exception as e:
        raise RuntimeError(f"não consegui falar com o DVR em {host}: {e}") from None
    modelo = parse_kv(tipo).get("type", "?")
    canais = parse_titulos(parse_kv(titulos))
    if not canais:
        raise RuntimeError("conectei no DVR mas não encontrei câmeras "
                           "(ChannelTitle vazio) — firmware não suportado?")
    return modelo, canais


def tg(token, metodo, payload=None, timeout=15):
    """Chamada mínima à API do Telegram; erro HTTP vira RuntimeError leiga."""
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{metodo}",
        json.dumps(payload or {}).encode(), {"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 404):
            raise RuntimeError("o Telegram recusou este token — confira o texto "
                               "que o BotFather mandou (copie a linha inteira)") from None
        raise RuntimeError(f"Telegram respondeu HTTP {e.code}") from None


class Assistente:
    """Estado + ações do wizard. server.py despacha /api/setup/<acao> para
    o método _<get|post>_<acao>; cada ação devolve dict com "ok"."""

    ZONAS = ("rua", "fundos", "ignorar")

    def __init__(self, ao_concluir=None):
        self.ao_concluir = ao_concluir or (lambda: None)
        self.escuta = {"rodando": False, "evento": None, "erro": None}
        self.st = self._carrega()

    def _carrega(self):
        try:
            with open(SETUP_PATH) as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {"passo": 1, "draft": {}}

    def _grava(self):
        os.makedirs(os.path.dirname(SETUP_PATH), exist_ok=True)
        tmp = SETUP_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, SETUP_PATH)

    def _avanca(self, passo):
        self.st["passo"] = max(self.st.get("passo", 1), passo)
        self._grava()

    def _cfg_completa(self):
        cfg = json.loads(json.dumps(cfgmod.PADRAO))
        cfgmod._mescla(cfg, self.st["draft"])
        return cfg

    def _dvr(self):
        if not self.st["draft"].get("dvr", {}).get("host"):
            raise RuntimeError("DVR ainda não configurado — volte ao passo 2")
        return Dvr(self._cfg_completa())

    def trata(self, acao, metodo, corpo, q):
        """→ (status_http, dict). Erros esperados viram ok=False com msg leiga."""
        fn = getattr(self, f"_{metodo.lower()}_{acao}", None)
        if not fn:
            return 404, {"ok": False, "erro": f"ação desconhecida: {acao}"}
        try:
            return 200, fn(corpo or {}, q or {})
        except RuntimeError as e:
            return 200, {"ok": False, "erro": str(e)}
        except Exception as e:
            return 500, {"ok": False, "erro": f"{type(e).__name__}: {e}"}

    def _get_estado(self, corpo, q):
        d = self.st["draft"]
        return {"ok": True, "passo": self.st.get("passo", 1),
                "concluido": self.st.get("concluido", False),
                "dvr": {k: v for k, v in d.get("dvr", {}).items() if k != "senha"},
                "modelo": self.st.get("modelo", ""),
                "canais_detectados": self.st.get("canais_detectados", {}),
                "canais": d.get("canais", {}),
                "telegram_bot": self.st.get("telegram_bot", ""),
                "telegram_ok": bool(d.get("telegram", {}).get("chat_id")),
                "llm": d.get("llm", {}).get("provider", "")}

    def _post_scan(self, corpo, q):
        host = (corpo.get("host") or "").strip()
        if host:
            porta = int(corpo.get("porta_http") or 80)
            return {"ok": True, "candidatos": [
                {"ip": host, "portas": [], "confirmado": parece_dvr(host, porta)}]}
        cand = scan_subrede(subrede_local())
        for c in cand:
            c["confirmado"] = 80 in c["portas"] and parece_dvr(c["ip"])
        return {"ok": True, "candidatos": ordena_candidatos(cand)}

    def _post_credenciais(self, corpo, q):
        host = (corpo.get("host") or "").strip()
        user = (corpo.get("user") or "").strip()
        senha = corpo.get("senha") or ""
        if not (host and user and senha):
            raise RuntimeError("preencha endereço, usuário e senha do DVR")
        ph = int(corpo.get("porta_http") or 80)
        pr = int(corpo.get("porta_rtsp") or 554)
        modelo, canais = testa_credenciais(host, ph, user, senha)
        self.st["draft"]["dvr"] = {"host": host, "user": user, "senha": senha,
                                   "porta_http": ph, "porta_rtsp": pr}
        self.st["modelo"] = modelo
        self.st["canais_detectados"] = canais
        self._avanca(3)
        return {"ok": True, "modelo": modelo, "canais": canais}

    def thumb(self, canal):
        return self._dvr().frame(int(canal), timeout=25)

    def _post_canais(self, corpo, q):
        canais = corpo.get("canais") or {}
        if not canais:
            raise RuntimeError("classifique ao menos uma câmera")
        for n, c in canais.items():
            if c.get("zona") not in self.ZONAS:
                raise RuntimeError(f"câmera {n}: escolha rua, fundos ou ignorar")
            c["nome"] = (c.get("nome") or f"câmera {n}").strip()
            c["contexto"] = (c.get("contexto") or "").strip()
        if all(c["zona"] == "ignorar" for c in canais.values()):
            raise RuntimeError("todas as câmeras ficaram como 'ignorar' — "
                               "o vigia ficaria cego; marque ao menos uma")
        self.st["draft"]["canais"] = canais
        self._avanca(4)
        return {"ok": True}

    def _post_eventos(self, corpo, q):
        ok = self._dvr().suporta_smd()
        if ok is None:
            return {"ok": False, "erro": "não consegui consultar o DVR agora "
                                         "(fora do ar?) — tente de novo"}
        if not ok:
            return {"ok": False, "fatal": True, "erro":
                    "este DVR não expõe a Detecção inteligente de pessoas e "
                    "veículos — o vigia não funciona nele. Alguns modelos "
                    "anunciam o recurso no menu, mas o firmware não o expõe."}
        self._avanca(5)
        return {"ok": True}

    def _post_eventos_escuta(self, corpo, q):
        """Teste vivo opcional: 60s ouvindo eventos ('ande na frente da câmera')."""
        if self.escuta["rodando"]:
            return {"ok": True, "ja_rodando": True}
        self.escuta.update({"rodando": True, "evento": None, "erro": None})
        threading.Thread(target=self._escuta_thread, daemon=True).start()
        return {"ok": True}

    def _escuta_thread(self, dur=60):
        fim = time.time() + dur

        def tick():
            if time.time() > fim:
                raise TimeoutError

        def evento(code, canal, action):
            if action == "Start":
                self.escuta["evento"] = {"code": code, "canal": canal + 1}
                raise TimeoutError

        try:
            self._dvr().escuta(evento, tick)
        except TimeoutError:
            pass
        except Exception as e:
            self.escuta["erro"] = str(e)
        self.escuta["rodando"] = False

    def _get_eventos_status(self, corpo, q):
        return {"ok": True, **{k: self.escuta[k] for k in
                               ("rodando", "evento", "erro")}}

    def _post_telegram(self, corpo, q):
        token = (corpo.get("token") or "").strip()
        if not token:
            raise RuntimeError("cole o token que o BotFather te mandou")
        r = tg(token, "getMe")
        if not r.get("ok"):
            raise RuntimeError("o Telegram recusou este token — confira com o BotFather")
        self.st["draft"].setdefault("telegram", {})["token"] = token
        self.st["telegram_bot"] = r["result"]["username"]
        self._grava()
        return {"ok": True, "bot": r["result"]["username"]}

    def _get_telegram_chat(self, corpo, q):
        token = self.st["draft"].get("telegram", {}).get("token")
        if not token:
            raise RuntimeError("valide o token primeiro")
        r = tg(token, "getUpdates", {"timeout": 0})
        for u in reversed(r.get("result", [])):
            chat = (u.get("message") or {}).get("chat") or {}
            if chat.get("type") == "private":
                return {"ok": True, "chat_id": str(chat["id"]),
                        "nome": chat.get("first_name", "")}
        return {"ok": False, "erro": "ainda não vi sua mensagem — abra o chat "
                                     "do SEU bot no Telegram (não o do "
                                     "BotFather) e aperte INICIAR; qualquer "
                                     "mensagem serve"}

    def _post_telegram_confirma(self, corpo, q):
        chat_id = str(corpo.get("chat_id") or "").strip()
        if not chat_id:
            raise RuntimeError("chat_id vazio — mande /start para o bot antes")
        token = self.st["draft"]["telegram"]["token"]
        tg(token, "sendMessage", {"chat_id": chat_id, "text":
           "Olá! Sou o RondaVizinho, o vigia da sua casa. Quando a instalação terminar, "
           "os alertas chegam aqui. Comandos: /status /vigiar /ajuda"})
        self.st["draft"]["telegram"]["chat_id"] = chat_id
        self._avanca(6)
        return {"ok": True}

    def _post_llm(self, corpo, q):
        if corpo.get("pular"):
            self.st["draft"]["llm"] = {"provider": "none"}
            self._avanca(7)
            return {"ok": True, "pulado": True,
                    "aviso": "sem IA o vigia não descreve pessoas/veículos nem "
                             "monta a ficha — só avisa que houve movimento suspeito"}
        provider = corpo.get("provider") or "gemini"
        api_key = (corpo.get("api_key") or "").strip()
        base_url = (corpo.get("base_url") or "").strip()
        modelo = (corpo.get("modelo") or "gemini-2.5-flash").strip()
        if provider not in ("gemini", "openai"):
            raise RuntimeError(f"provedor inválido: {provider}")
        if not api_key:
            raise RuntimeError("cole a chave da IA (ou pule este passo)")
        if provider == "openai" and not base_url:
            raise RuntimeError("para provedor compatível com OpenAI, informe o "
                               "endereço (base_url) do serviço")
        llm_cfg = {"provider": provider, "api_key": api_key,
                   "base_url": base_url, "modelo": modelo}
        cfg = self._cfg_completa()
        cfg["llm"].update(llm_cfg)
        frame = self._frame_teste(cfg)
        visao = Llm(cfg, {}, log=lambda m: None).visao(frame)
        if visao is None:
            return {"ok": False, "erro": "a IA não respondeu — confira a chave "
                                         "(ou a cota do dia) e tente de novo"}
        self.st["draft"]["llm"] = llm_cfg
        self._avanca(7)
        return {"ok": True, "descricao": visao.get("descricao", "")}

    def _frame_teste(self, cfg):
        """Frame da 1ª câmera não-ignorada (ou canal 1) p/ testes."""
        canais = self.st["draft"].get("canais", {})
        canal = next((n for n, c in sorted(canais.items(), key=lambda kv: int(kv[0]))
                      if c.get("zona") != "ignorar"), "1")
        return Dvr(cfg).frame(int(canal), timeout=25)

    def _post_finalizar(self, corpo, q):
        self.st["draft"].setdefault("storage", {}).setdefault(
            "dir", os.path.dirname(cfgmod.CONFIG_PATH))
        cfg = self._cfg_completa()
        itens, frame = [], None

        def item(nome, fn):
            try:
                det = fn()
                itens.append({"nome": nome, "ok": True, "detalhe": det or ""})
                return True
            except Exception as e:
                itens.append({"nome": nome, "ok": False, "detalhe": str(e)})
                return False

        def pega_frame():
            nonlocal frame
            frame = self._frame_teste(cfg)
            return f"{len(frame) // 1024} kB"

        def sonda_smd():
            if Dvr(cfg).suporta_smd() is not True:
                raise RuntimeError("sonda SMD não confirmou")

        item("Câmera (frame de vídeo)", pega_frame)
        item("Detecção inteligente do DVR", sonda_smd)
        if cfg["llm"]["provider"] != "none":
            def testa_llm():
                v = Llm(cfg, {}, log=lambda m: None).visao(frame)
                if v is None:
                    raise RuntimeError("IA não respondeu")
                return v.get("descricao", "")[:120]
            item("IA de visão", testa_llm)
        def manda_foto():
            if frame is None:
                raise RuntimeError("sem frame da câmera")
            if not (cfg["telegram"]["token"] and cfg["telegram"]["chat_id"]):
                raise RuntimeError("Telegram não configurado — volte ao passo 5")
            erros = []
            t = Telegram(cfg, log=erros.append)
            t.foto(frame, "Teste da instalação — esta é a visão "
                          "da sua câmera.")
            if erros:
                raise RuntimeError("; ".join(str(e) for e in erros))
        item("Alerta no Telegram (foto)", manda_foto)
        def testa_storage():
            d = cfg["storage"]["dir"]
            try:
                os.makedirs(d, exist_ok=True)
                p = os.path.join(d, ".teste-setup")
                with open(p, "w") as f:
                    f.write("ok")
                os.remove(p)
            except PermissionError:
                raise RuntimeError(f"sem permissão para gravar em {d}") from None
            return d
        item("Armazenamento local", testa_storage)

        todos = all(i["ok"] for i in itens)
        if todos:
            cfgmod.valida(cfg)
            os.makedirs(os.path.dirname(cfgmod.CONFIG_PATH), exist_ok=True)
            tmp = cfgmod.CONFIG_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.st["draft"], f, ensure_ascii=False, indent=1)
            os.replace(tmp, cfgmod.CONFIG_PATH)
            self.st["concluido"] = True
            self._avanca(8)
            self.ao_concluir()
            try:
                Telegram(cfg, log=lambda m: None).msg(
                    "Instalação concluída! O vigia entra em serviço assim "
                    "que o programa for (re)iniciado. Comandos: /status "
                    "/vigiar /ajuda")
            except Exception:
                pass
        return {"ok": todos, "itens": itens,
                "config": cfgmod.CONFIG_PATH if todos else None}


def _self_check():
    import tempfile
    global SETUP_PATH
    assert parse_kv("a=1\nlixo\nb=c=d") == {"a": "1", "b": "c=d"}
    assert "type" not in parse_kv('<html><input type="text" name="user"></html>')
    assert parse_kv("type=MHDX 3104-C").get("type") == "MHDX 3104-C"
    kv = parse_kv("table.ChannelTitle[0].Name=Frente\n"
                  "table.ChannelTitle[1].Name=Fundos\n"
                  "table.ChannelTitle[0].Outro=x")
    assert parse_titulos(kv) == {"1": "Frente", "2": "Fundos"}
    cand = ordena_candidatos([{"ip": "a", "portas": [554], "confirmado": False},
                              {"ip": "b", "portas": [37777], "confirmado": False},
                              {"ip": "c", "portas": [80], "confirmado": True}])
    assert [c["ip"] for c in cand] == ["c", "b", "a"]
    assert subrede_local().count(".") == 3 and subrede_local().endswith(".")
    antigo = SETUP_PATH
    with tempfile.TemporaryDirectory() as td:
        SETUP_PATH = os.path.join(td, "setup.json")
        a = Assistente()
        assert a.st["passo"] == 1
        a.st["draft"]["dvr"] = {"host": "10.0.0.9", "user": "u", "senha": "s",
                                "porta_http": 80, "porta_rtsp": 554}
        a._avanca(3)
        b = Assistente()
        assert b.st["passo"] == 3 and b.st["draft"]["dvr"]["host"] == "10.0.0.9"
        st, r = b.trata("canais", "POST", {"canais": {"1": {"zona": "sei lá"}}}, {})
        assert st == 200 and not r["ok"] and "rua" in r["erro"]
        st, r = b.trata("canais", "POST",
                        {"canais": {"1": {"zona": "ignorar"}}}, {})
        assert not r["ok"] and "cego" in r["erro"]
        st, r = b.trata("canais", "POST",
                        {"canais": {"1": {"nome": "", "zona": "rua"}}}, {})
        assert r["ok"] and b.st["draft"]["canais"]["1"]["nome"] == "câmera 1"
        assert b.st["passo"] == 4
        st, r = b.trata("inexistente", "GET", None, {})
        assert st == 404
        cfg = b._cfg_completa()
        assert cfg["dvr"]["host"] == "10.0.0.9" and cfg["calibracao"]["jpeg_q"] == 2
        st, r = b.trata("estado", "GET", None, {})
        assert r["passo"] == 4 and "senha" not in r["dvr"]
    SETUP_PATH = antigo
    print("setup self-check ok")


if __name__ == "__main__":
    _self_check()
