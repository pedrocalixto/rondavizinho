"""Motor do vigia: padrões de suspeita sobre eventos de movimento do DVR.

Padrões implementados: permanência por runs de marcas, recorrência
por comparação de descrições, interação com portão/muro via LLM, resumo horário,
cooldowns por classe e HITL (botão de falso positivo). Rua notifica só padrão;
fundos só pessoa junto ao muro/permanência; tudo vai pro CSV.
"""
import time
from collections import deque

EMOJI = {"portao": "🚨", "muro": "🧱", "permanencia": "⏱️", "recorrencia": "🔁"}


def run_atual(ts_list, gap_max):
    """(inicio, duracao) da sequência contígua mais recente de marcas."""
    if not ts_list:
        return None, 0.0
    fim = inicio = ts_list[-1]
    for t in reversed(ts_list):
        if inicio - t > gap_max:
            break
        inicio = t
    return inicio, fim - inicio


def conta_recorrencia(agora, ts_matches, janela):
    """1 (a aparição atual) + matches dentro da janela."""
    return 1 + sum(1 for t in ts_matches if agora - t <= janela)


DIAS = {"seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4, "sab": 5, "dom": 6}


def dias_da_faixa(spec):
    """'seg-sex' ou 'sab,dom' → set de dias (segunda=0); intervalo pode dar a volta."""
    dias = set()
    for tok in spec.split(","):
        if "-" in tok:
            a, b = (DIAS[t] for t in tok.split("-"))
            dias.update(range(a, b + 1) if a <= b
                        else list(range(a, 7)) + list(range(b + 1)))
        else:
            dias.add(DIAS[tok])
    return dias


def agenda_ativa(faixas, agora_hm, dow=None):
    """faixas: [["22:00","06:30"], ["00:00","24:00","sab,dom"], ...]; o 3º item
    opcional restringe aos dias (do dia corrente); atravessa meia-noite quando
    ini > fim."""
    if dow is None:
        dow = time.localtime().tm_wday
    for faixa in faixas:
        ini, fim = faixa[0], faixa[1]
        if len(faixa) > 2 and dow not in dias_da_faixa(faixa[2]):
            continue
        if ini <= fim:
            if ini <= agora_hm < fim:
                return True
        elif agora_hm >= ini or agora_hm < fim:
            return True
    return False


def pes_em_frente(pes, limiar):
    """Gate geométrico: pés (y2 0-1000) da pessoa mais próxima abaixo do limiar
    do canal = em frente DESTA casa. None = não mediu (na dúvida, não bloqueia)."""
    if not pes:
        return None
    return max(pes) >= limiar


class Core:
    def __init__(self, cfg, estado, dvr, llm, tg, storage, diag, log):
        self.cfg, self.estado = cfg, estado
        self.dvr, self.llm, self.tg = dvr, llm, tg
        self.storage, self.diag = storage, diag
        self.log = log
        cal = cfg["calibracao"]
        self.PERM_GAP_MAX = cal["perm_gap_max"]
        self.PERM_DUR_MIN = cal["perm_dur_min"]
        self.REC_JANELA = cal["rec_janela"]
        self.REC_MIN = cal["rec_min"]
        self.RESUMO_INTERVALO = cal["resumo_intervalo"]
        self.COOLDOWN_VISAO = cal["cooldown_visao"]
        self.ALERTA_COOLDOWN = cal["alerta_cooldown"]
        from .config import canais_por_zona
        self.RUA, self.FUNDOS = canais_por_zona(cfg)
        self.nome_canal = {int(n) - 1: c["nome"] for n, c in cfg["canais"].items()}
        self.canal_cfg = {int(n) - 1: c for n, c in cfg["canais"].items()}
        self.atividade = {}
        self.descricoes = deque(maxlen=cal["desc_buffer"])
        self.perm_avisada, self.alerta_avisado, self.ultimo_visao = {}, {}, {}
        self.resumo = {"inicio": time.time(), "rua": 0, "fundos": 0, "alertas": 0}
        self._vig_cache = {"t": 0.0, "on": False}
        import threading
        self._hitl_lock = threading.Lock()

    def vigilia_ativa(self):
        if time.time() - self._vig_cache["t"] < 60:
            return self._vig_cache["on"]
        manual = self.estado.get("vigilia_manual", "auto")
        if manual in ("on", "off"):
            on = manual == "on"
        else:
            on = agenda_ativa(self.cfg["vigilia"]["agenda"], time.strftime("%H:%M"))
        self._vig_cache.update(t=time.time(), on=on)
        return on

    def _contexto(self, canal):
        return self.canal_cfg.get(canal, {}).get("contexto", "")

    def _so_pessoa(self, canal):
        return bool(self.canal_cfg.get(canal, {}).get("somente_pessoa"))

    def notifica(self, jpeg, desc, canal, tipo, classe, motivo):
        agora = time.time()
        if agora - self.alerta_avisado.get((classe, canal), 0) \
                < self.ALERTA_COOLDOWN[classe]:
            return
        limiar = self.canal_cfg.get(canal, {}).get("pes_y_min")
        if tipo == "pessoa" and limiar is not None:
            veredito = pes_em_frente(self.llm.pes(jpeg), int(limiar))
            if veredito is False:
                self.log(f"gate geométrico suprimiu alerta {classe} canal "
                         f"{canal + 1} (pessoa longe/no vizinho)")
                return
        self.alerta_avisado[(classe, canal)] = agora
        self.resumo["alertas"] += 1
        nome = self.nome_canal.get(canal, f"câmera {canal + 1}")
        hhmm = time.strftime("%H:%M")
        self.tg.foto(jpeg, f"{EMOJI[classe]} Vigília ({nome} {hhmm}): {motivo}."
                           + (f"\n{desc}" if desc else ""))
        self.log(f"ALERTA {classe} canal {canal + 1}: {motivo}")
        self._hitl(classe, canal, tipo, motivo)
        ficha = ""
        d = self.llm.forense(jpeg, self._contexto(canal))
        if d:
            from .llm import formata_ficha
            ficha = formata_ficha(d)
            if ficha:
                try:
                    self.tg.msg(f"🧾 Ficha ({nome} {hhmm}):\n{ficha}")
                except Exception as e:
                    self.log(f"envio da ficha falhou: {e}")
        self.storage.salva(jpeg, f"[{classe}] {motivo}\n{desc}"
                           + (f"\n\nFICHA:\n{ficha}" if ficha else ""),
                           f"canal{canal + 1}-{nome}_{tipo}")
        self.storage.evento_jsonl(canal=canal + 1, zona="rua" if canal in self.RUA
                                  else "fundos", tipo=tipo, classe=classe,
                                  descricao=desc, ficha=ficha)

    def _hitl(self, classe, canal, tipo, motivo):
        """Botão de 'falso positivo' que silencia a câmera; thread (não trava o loop)."""
        import threading
        if not self._hitl_lock.acquire(blocking=False):
            return
        cal = self.cfg["calibracao"]

        def watch():
            try:
                self.tg.msg("ℹ️ Situação suspeita — fico de olho e aviso se mudar.",
                            botoes=[("✅ Tudo certo, pode silenciar (30 min)",
                                     "hitl:nao")])
                r = self.tg.espera_callback("hitl:", cal["hitl_timeout"])
                if r == "nao":
                    self.alerta_avisado[(classe, canal)] = \
                        time.time() + cal["silencio_pos_nao"]
                    self.tg.msg("✅ Ok. Alertas dessa câmera silenciados por 30 min.")
            except Exception as e:
                self.log(f"hitl falhou: {e}")
            finally:
                self._hitl_lock.release()

        threading.Thread(target=watch, daemon=True).start()

    def trata_evento(self, code, canal, action):
        tipo = self.dvr.tipo_do_code(code)
        chave = (tipo, canal)
        agora = time.time()
        self.atividade.setdefault(chave, deque(maxlen=400)).append(agora)
        if action == "Start":
            vigilia = self.vigilia_ativa()
            if vigilia and (canal in self.RUA or canal in self.FUNDOS):
                self.resumo["rua" if canal in self.RUA else "fundos"] += 1
            quer_visao = vigilia and (
                canal in self.RUA or (canal in self.FUNDOS and tipo != "veiculo"))
            if quer_visao and agora - self.ultimo_visao.get(chave, 0) \
                    >= self.COOLDOWN_VISAO:
                self.ultimo_visao[chave] = agora
                self._analisa(code, canal, tipo, vigilia)
            else:
                self.storage.registra(code, canal)
        self._checa_permanencia(chave, canal, tipo)

    def _analisa(self, code, canal, tipo, vigilia):
        try:
            jpeg = self.dvr.frame(canal + 1)
        except Exception as e:
            self.storage.registra(code, canal, "erro", str(e))
            self.diag.falha("dvr_frame", str(e))
            return
        self.diag.ok("dvr_frame")
        d = self.llm.visao(jpeg, self._contexto(canal))
        if d is None:
            self.storage.registra(code, canal)
            if vigilia and canal in self.FUNDOS and tipo == "pessoa":
                self.notifica(jpeg, "", canal, tipo, "muro",
                              "pessoa nos fundos (sem análise de imagem)")
            return
        if self._so_pessoa(canal) and tipo != "pessoa":
            self.storage.registra(code, canal, "", d["descricao"])
            return
        flags = [k for k in ("pessoa_perto_muro", "interagindo_portao",
                             "parece_entregador") if d[k]]
        self.storage.registra(code, canal, "", d["descricao"]
                              + (f" [{'|'.join(flags)}]" if flags else ""))
        self.log(f"{tipo} canal {canal + 1}: vigilia={vigilia} flags={flags}")
        if not vigilia:
            return
        if d["interagindo_portao"]:
            self.notifica(jpeg, d["descricao"], canal, tipo, "portao",
                          f"{tipo} interagindo com o portão")
        elif canal in self.FUNDOS and d["pessoa_perto_muro"]:
            self.notifica(jpeg, d["descricao"], canal, tipo, "muro",
                          "pessoa junto ao muro")
        elif canal in self.RUA:
            agora = time.time()
            hist = [(t, x) for t, x in self.descricoes
                    if agora - t <= self.REC_JANELA]
            self.descricoes.append((agora, d["descricao"]))
            if hist:
                n = conta_recorrencia(agora, self.llm.compara(d["descricao"], hist),
                                      self.REC_JANELA)
                if n >= self.REC_MIN:
                    self.notifica(jpeg, d["descricao"], canal, tipo, "recorrencia",
                                  f"mesmo {tipo} — {n}ª aparição na última hora")

    def _checa_permanencia(self, chave, canal, tipo):
        if canal in self.FUNDOS and tipo == "veiculo":
            return
        if self._so_pessoa(canal) and tipo != "pessoa":
            return
        if not self.vigilia_ativa():
            return
        ini, dur = run_atual(list(self.atividade.get(chave, ())), self.PERM_GAP_MAX)
        if dur >= self.PERM_DUR_MIN and self.perm_avisada.get(chave) != ini:
            self.perm_avisada[chave] = ini
            try:
                jpeg = self.dvr.frame(canal + 1)
            except Exception as e:
                self.log(f"frame permanência falhou: {e}")
                return
            d = self.llm.visao(jpeg, self._contexto(canal))
            desc = d["descricao"] if d else ""
            if d and d["parece_entregador"]:
                desc += "\n📦 Parece entregador."
            self.notifica(jpeg, desc, canal, tipo, "permanencia",
                          f"{tipo} parado há {int(dur // 60)} min")

    def tick_resumo(self):
        agora = time.time()
        if agora - self.resumo["inicio"] < self.RESUMO_INTERVALO:
            return
        total = self.resumo["rua"] + self.resumo["fundos"]
        if self.vigilia_ativa() and total:
            fim = ("Nada suspeito." if self.resumo["alertas"] == 0
                   else f"{self.resumo['alertas']} alerta(s) enviados.")
            try:
                self.tg.msg(f"🕐 Vigília, última hora: {total} detecções — "
                            f"rua {self.resumo['rua']}, fundos "
                            f"{self.resumo['fundos']}. {fim}")
            except Exception as e:
                self.log(f"resumo falhou: {e}")
        self.resumo.update(inicio=agora, rua=0, fundos=0, alertas=0)
