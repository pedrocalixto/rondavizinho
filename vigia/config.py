"""Config do vigia — um único config.json que o assistente de instalação escreve.

Quem edita na mão é técnico; o leigo usa o assistente web. Validação dura na
carga: erro de config para o daemon COM mensagem clara (o diag repassa ao dono).
"""
import json, os

_PADRAO_DIR = (os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Vigia")
               if os.name == "nt" else "/var/lib/vigia")
CONFIG_PATH = os.environ.get("VIGIA_CONFIG", os.path.join(_PADRAO_DIR, "config.json"))
ESTADO_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "estado.json")

PADRAO = {
    "dvr": {"host": "", "user": "", "senha": "", "porta_http": 80, "porta_rtsp": 554},
    "canais": {},
    "telegram": {"token": "", "chat_id": ""},
    "llm": {"provider": "none",
            "api_key": "", "base_url": "", "modelo": "gemini-2.5-flash",
            "orcamento_dia": 500},
    "vigilia": {"agenda": [["22:00", "06:30"]]},
    "storage": {"dir": _PADRAO_DIR, "min_livre_pct": 10},
    "heartbeat": {"url": "", "casa_id": ""},
    "calibracao": {"perm_gap_max": 90, "perm_dur_min": 180, "rec_janela": 3600,
                   "rec_min": 3, "desc_buffer": 12, "resumo_intervalo": 3600,
                   "cooldown_visao": 120,
                   "alerta_cooldown": {"portao": 600, "muro": 600,
                                       "permanencia": 900, "recorrencia": 3600},
                   "silencio_pos_nao": 1800, "hitl_timeout": 180, "jpeg_q": 2},
}


def _mescla(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _mescla(base[k], v)
        else:
            base[k] = v
    return base


def carrega(path=None):
    path = path or CONFIG_PATH
    cfg = json.loads(json.dumps(PADRAO))
    with open(path) as f:
        _mescla(cfg, json.load(f))
    valida(cfg)
    return cfg


def valida(cfg):
    erros = []
    if not cfg["dvr"]["host"]:
        erros.append("dvr.host vazio")
    if not cfg["dvr"]["user"] or not cfg["dvr"]["senha"]:
        erros.append("credenciais do DVR vazias")
    if not cfg["telegram"]["token"] or not cfg["telegram"]["chat_id"]:
        erros.append("telegram.token/chat_id vazios")
    zonas_ok = {"rua", "fundos", "ignorar"}
    for n, c in cfg["canais"].items():
        if c.get("zona") not in zonas_ok:
            erros.append(f"canal {n}: zona inválida ({c.get('zona')})")
        pym = c.get("pes_y_min")
        if pym is not None and not (0 <= int(pym) <= 1000):
            erros.append(f"canal {n}: pes_y_min fora de 0-1000")
    for f in cfg["vigilia"]["agenda"]:
        if not (isinstance(f, list) and len(f) in (2, 3)):
            erros.append(f"agenda: faixa inválida {f}")
            continue
        if len(f) == 3:
            from .core import dias_da_faixa
            try:
                dias_da_faixa(f[2])
            except KeyError:
                erros.append(f"agenda: dias inválidos '{f[2]}' (seg|ter|qua|qui|sex|sab|dom)")
    if cfg["llm"]["provider"] not in ("none", "gemini", "openai"):
        erros.append(f"llm.provider inválido: {cfg['llm']['provider']}")
    if cfg["llm"]["provider"] != "none" and not cfg["llm"]["api_key"]:
        erros.append("llm.api_key vazio com provider ativo")
    if erros:
        raise ValueError("config inválida: " + "; ".join(erros))


def canais_por_zona(cfg):
    """Retorna (rua, fundos) como sets de índices 0-based (o DVR indexa de 0)."""
    rua, fundos = set(), set()
    for n, c in cfg["canais"].items():
        idx = int(n) - 1
        if c["zona"] == "rua":
            rua.add(idx)
        elif c["zona"] == "fundos":
            fundos.add(idx)
    return rua, fundos


def estado_carrega():
    try:
        with open(ESTADO_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def estado_grava(estado):
    os.makedirs(os.path.dirname(ESTADO_PATH), exist_ok=True)
    tmp = ESTADO_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(estado, f)
    os.replace(tmp, ESTADO_PATH)
