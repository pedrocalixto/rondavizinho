"""Form interativo no terminal: dá CONTEXTO a cada câmera em linguagem leiga.

O contexto vira texto injetado nos prompts do LLM (o que é NORMAL nesta cena?)
e ajusta os knobs certos (somente_pessoa). Roda com:
    python -m vigia --configurar
Edita o config.json existente (só a parte de canais); o resto fica intacto.
"""
import json, os

from . import config as cfgmod

ZONAS = {"1": ("rua", "rua/calçada na frente da casa"),
         "2": ("fundos", "fundos, quintal ou lateral"),
         "3": ("ignorar", "ambiente interno ou câmera que não deve alertar")}


def _pergunta(texto, padrao=""):
    r = input(f"{texto}{f' [{padrao}]' if padrao else ''}: ").strip()
    return r or padrao


def _sim_nao(texto, padrao="n"):
    r = _pergunta(f"{texto} (s/n)", padrao).lower()
    return r.startswith("s")


def _configura_canal(n, canal):
    print(f"\n===== Câmera {n} =====")
    canal["nome"] = _pergunta("Dê um nome curto (ex.: frente, garagem, quintal)",
                              canal.get("nome", f"camera{n}"))
    print("O que essa câmera vê?")
    for k, (_, desc) in ZONAS.items():
        print(f"  {k}) {desc}")
    canal["zona"] = ZONAS.get(_pergunta("Escolha 1, 2 ou 3",
                                        "1"), ZONAS["1"])[0]
    if canal["zona"] == "ignorar":
        return canal

    print("\nAgora descreva a cena — isso ensina a inteligência a separar o "
          "normal do suspeito.")
    canal["contexto"] = _pergunta(
        "Descreva em 1-2 frases o que aparece NORMALMENTE nessa imagem\n"
        "(ex.: 'rua residencial tranquila, carros estacionados dos dois lados,\n"
        " ponto de ônibus à esquerda; vizinho tem oficina em frente')",
        canal.get("contexto", ""))

    extras = []
    if _sim_nao("É comércio ou local com circulação constante de pessoas"):
        canal["somente_pessoa"] = True
        extras.append("local com circulação constante — movimento de pessoas e "
                      "veículos passando é normal")
        print("  → ok: esse canal só vai alertar com PESSOA confirmada pela "
              "análise de imagem (menos avisos falsos).")
    else:
        canal["somente_pessoa"] = bool(canal.get("somente_pessoa", False))
    if _sim_nao("Costuma ter carros estacionados na frente (cenário normal)"):
        extras.append("carros estacionados em frente são normais")
    if _sim_nao("Há casa de vizinho ou área pública visível no mesmo quadro"):
        extras.append("há propriedade vizinha/área pública no quadro — só "
                      "considere suspeito quem interage com ESTA casa")
        print("  → dica: dá para calibrar a 'linha de proximidade' dessa câmera "
              "depois (pes_y_min no config) para ignorar quem está longe.")
    if extras:
        canal["contexto"] = (canal["contexto"] + ". " if canal["contexto"] else "") \
            + "; ".join(extras)
    print(f"  Contexto salvo: \"{canal['contexto']}\"")
    return canal


def main():
    path = cfgmod.CONFIG_PATH
    print(f"== RondaVizinho — configuração das câmeras ==\nConfig: {path}\n")
    try:
        with open(path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        print("Config ainda não existe — criando uma nova a partir do exemplo.")
        cfg = {"canais": {}}
    canais = cfg.setdefault("canais", {})

    if canais:
        nomes = ", ".join(f"{n} ({c.get('nome', '?')})"
                          for n, c in sorted(canais.items()))
        print(f"Câmeras já configuradas: {nomes}")
    while True:
        n = _pergunta("\nNúmero da câmera para configurar (ENTER para terminar)")
        if not n:
            break
        if not n.isdigit() or int(n) < 1:
            print("Digite o número do canal como aparece no DVR (1, 2, 3...).")
            continue
        canais[n] = _configura_canal(n, canais.get(n, {}))

    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    print(f"\n✅ Salvo em {path}. Reinicie o vigia para aplicar "
          "(ou aguarde: no serviço, restart automático).")
