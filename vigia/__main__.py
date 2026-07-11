"""Ponto de entrada: python -m vigia [--check | --teste]

--check: valida as funções puras e a config (sem rede) — sai 0 se ok.
--teste: um ciclo real (frame → LLM → Telegram → storage) para o assistente
         de instalação e para diagnóstico manual.
sem flag: daemon (loop de eventos com reconexão infinita).
"""
import os, sys, time

from . import config as cfgmod
from .core import (Core, agenda_ativa, conta_recorrencia,
                   pes_em_frente, run_atual)
from .diag import Diag
from .dvr import Dvr
from .llm import Llm
from .notify import Telegram
from .storage import Storage


_LOG_PATH = os.path.join(os.path.dirname(cfgmod.ESTADO_PATH), "vigia.log")


def log(msg):
    linha = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    if sys.stdout:
        print(linha, flush=True)
    else:
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(linha + "\n")
        except OSError:
            pass


def _self_check():
    assert run_atual([], 90) == (None, 0.0)
    assert run_atual([100], 90) == (100, 0.0)
    assert run_atual([0, 60, 120, 180], 90) == (0, 180)
    assert run_atual([0, 60, 300, 350], 90) == (300, 50)
    assert conta_recorrencia(3600, [100, 200], 3600) == 3
    assert conta_recorrencia(7200, [100, 200], 3600) == 1
    assert agenda_ativa([["22:00", "06:30"]], "23:15")
    assert agenda_ativa([["22:00", "06:30"]], "03:00")
    assert not agenda_ativa([["22:00", "06:30"]], "12:00")
    assert agenda_ativa([["08:00", "18:00"]], "12:00")
    fx = [["19:00", "07:00", "seg-sex"], ["00:00", "24:00", "sab,dom"]]
    assert agenda_ativa(fx, "23:00", dow=4)
    assert agenda_ativa(fx, "03:00", dow=0)
    assert not agenda_ativa(fx, "12:00", dow=2)
    assert agenda_ativa(fx, "12:00", dow=5)
    assert agenda_ativa(fx, "23:59", dow=6)
    from .core import dias_da_faixa
    assert dias_da_faixa("sex-seg") == {4, 5, 6, 0}
    assert Telegram({"telegram": {"token": "x", "chat_id": "1"}}, log).chats == ["1"]
    assert Telegram({"telegram": {"token": "x", "chat_id": ["1", 2]}}, log).chats == ["1", "2"]
    assert pes_em_frente([303], 350) is False
    assert pes_em_frente([197], 150) is True
    assert pes_em_frente([100, 400], 350) is True
    assert pes_em_frente([], 350) is None and pes_em_frente(None, 350) is None
    pa = {}
    ini, dur = run_atual([0, 60, 120, 180], 90)
    assert dur >= 180 and pa.get(("pessoa", 1)) != ini
    pa[("pessoa", 1)] = ini
    ini2, _ = run_atual([0, 60, 120, 180, 240], 90)
    assert pa[("pessoa", 1)] == ini2
    print("self-check ok")


def monta(cfg):
    estado = cfgmod.estado_carrega()
    tg = Telegram(cfg, log)
    diag = Diag(cfg, tg, log)
    dvr = Dvr(cfg)
    llm = Llm(cfg, estado, log, gravar=lambda: cfgmod.estado_grava(estado))
    storage = Storage(cfg, log)
    core = Core(cfg, estado, dvr, llm, tg, storage, diag, log)
    return core, tg, diag, dvr, llm, storage, estado


def registra_comandos(core, tg, estado):
    def vigiar(args):
        modo = args[0].lower() if args else ""
        if modo in ("on", "off", "auto"):
            estado["vigilia_manual"] = modo
            cfgmod.estado_grava(estado)
            core._vig_cache["t"] = 0
            tg.msg(f"🛡️ Vigília: {modo} " +
                   ("(sigo a agenda)" if modo == "auto" else ""))
        else:
            tg.msg("Uso: /vigiar on | off | auto")

    def status(args):
        v = "ATIVA" if core.vigilia_ativa() else "inativa"
        modo = estado.get("vigilia_manual", "auto")
        ia = (f"IA: {estado.get('llm_gasto', 0)}/{core.llm.orcamento} análises hoje"
              if core.llm.ativo() else "IA: desativada")
        tg.msg(f"🛡️ Vigília {v} (modo {modo}). Detecções na última hora: "
               f"rua {core.resumo['rua']}, fundos {core.resumo['fundos']}, "
               f"alertas {core.resumo['alertas']}. {ia}.")

    def ajuda(args):
        tg.msg("Comandos:\n/vigiar on|off|auto — liga/desliga a vigilância\n"
               "/status — como estou agora\n/ajuda — esta mensagem")

    tg.registra_comando("/vigiar", vigiar)
    tg.registra_comando("/status", status)
    tg.registra_comando("/ajuda", ajuda)
    tg.registra_comando("/start", ajuda)


def main():
    if "--check" in sys.argv:
        _self_check()
        return
    if "--configurar" in sys.argv:
        from .setup_cli import main as configurar
        configurar()
        return
    cfg = cfgmod.carrega()
    core, tg, diag, dvr, llm, storage, estado = monta(cfg)

    if "--teste" in sys.argv:
        smd = dvr.suporta_smd()
        log("Detecção inteligente de pessoas e veículos no DVR: "
            f"{'SIM' if smd else 'NÃO' if smd is False else 'não determinado'}")
        if smd is False:
            log("AVISO: sem a detecção inteligente o vigia NÃO recebe eventos de "
                "pessoa/veículo — veja o requisito na documentação")
        canal = min(core.RUA | core.FUNDOS) if (core.RUA | core.FUNDOS) else 0
        jpeg = dvr.frame(canal + 1)
        log(f"frame ok ({len(jpeg)} bytes)")
        d = llm.visao(jpeg)
        log(f"llm: {d if d else 'desativado/degradado'}")
        caminho = storage.salva(jpeg, "teste de instalação", "teste")
        log(f"storage: {caminho}")
        tg.foto(jpeg, "🧪 Teste do vigia — se você recebeu esta foto, "
                      "a instalação está funcionando!")
        log("telegram ok")
        return

    tg.inicia()
    diag.inicia_heartbeat()
    registra_comandos(core, tg, estado)
    if dvr.suporta_smd() is False:
        log("DVR NÃO expõe SMD — o vigia não vai receber eventos!")
        try:
            tg.msg("⚠️ Seu gravador NÃO tem a \"Detecção inteligente de pessoas e "
                   "veículos\" disponível — sem ela eu não recebo os eventos e a "
                   "análise por IA fica comprometida. O vigia continua ligado, mas "
                   "provavelmente mudo. Procure esse recurso no menu/datasheet do "
                   "seu MHDX ou atualize o firmware. Detalhes na documentação.")
        except Exception:
            pass
    log("vigia iniciando — escutando eventos do DVR")
    while True:
        try:
            dvr.escuta(core.trata_evento, core.tick_resumo)
            diag.ok("dvr_attach")
        except Exception as e:
            diag.falha("dvr_attach", str(e))
            log(f"stream caiu ({e}); reconectando em 15s")
            time.sleep(15)


if __name__ == "__main__":
    main()
