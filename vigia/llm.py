"""LLM de visão plugável: gemini | openai (compatível) | none.

Degradação limpa: qualquer falha/quota devolve None e o core segue sem
descrição/ficha (padrões de movimento continuam funcionando). Orçamento diário
protege o free tier; estourou ou 429 → modo degradado até virar o dia.
"""
import base64, json, time, urllib.error, urllib.request

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

PROMPT_VISAO = (
    "Imagem de câmera de segurança residencial. Contexto: casa com muro e portão; "
    "movimento na via pública em frente é NORMAL.\n"
    "Responda SOMENTE JSON:\n"
    '{"descricao": "<1-2 frases: pessoas, veículos, ações; inclua roupas/cores/modelo '
    "para reidentificação; para veículos diga explicitamente se está ESTACIONADO ou "
    'em movimento>",\n'
    ' "tipo_principal": "pessoa|veiculo|nada",\n'
    ' "pessoa_perto_muro": true|false, "interagindo_portao": true|false,\n'
    ' "parece_entregador": true|false}\n'
    "Regras: pessoa_perto_muro=true APENAS pessoa encostada/junto ao muro ou cerca da "
    "casa (passando na rua/calçada = false). interagindo_portao=true se mexendo no "
    "portão/fechadura, escalando ou olhando para dentro da propriedade. "
    "parece_entregador=true se uniforme, pacote/caixa ou moto/bicicleta de entrega.")

PROMPT_PES = (
    "Localize TODAS as pessoas na imagem. Responda SOMENTE JSON: "
    '{"pessoas": [{"bbox": [x1, y1, x2, y2]}]} — coordenadas normalizadas 0-1000, '
    "y2 na altura dos pés. Sem pessoas: lista vazia.")

PROMPT_FORENSE = (
    "Análise FORENSE de imagem de câmera de segurança residencial (houve alerta de "
    "suspeita). Descreva TODAS as pessoas e veículos visíveis. Responda SOMENTE JSON:\n"
    '{"pessoas": [{"estatura_aparente": "baixa|média|alta (ou estimativa em metros)",\n'
    '  "cabelo": "<cor e estilo>", "pele": "<tom de pele>", "roupas": "<peças e cores>"}],\n'
    ' "veiculos": [{"tipo": "carro|moto|caminhão|...", "cor": "...",\n'
    '  "modelo_aparente": "<marca/modelo se reconhecível, senão null>",\n'
    '  "placa_legivel": true|false, "placa": "<caracteres ou null>"}]}\n'
    "Regras: placa: transcreva SOMENTE se os caracteres estiverem realmente legíveis "
    "na imagem; na dúvida, placa_legivel=false e placa=null (NÃO invente). Se não "
    "houver pessoas ou veículos, use lista vazia.")


class Llm:
    def __init__(self, cfg, estado, log, gravar=None):
        c = cfg["llm"]
        self.provider = c["provider"]
        self.key = c["api_key"]
        self.modelo = c["modelo"]
        self.url = c["base_url"] or GEMINI_URL if self.provider == "gemini" \
            else (c["base_url"].rstrip("/") + "/chat/completions" if c["base_url"] else "")
        self.orcamento = c["orcamento_dia"]
        self.estado = estado
        self.gravar = gravar or (lambda: None)
        self.log = log
        self.degradado_avisado = False

    def ativo(self):
        return self.provider != "none"

    def _gasta(self):
        """True se ainda há orçamento hoje; contabiliza a chamada."""
        hoje = time.strftime("%Y-%m-%d")
        if self.estado.get("llm_dia") != hoje:
            self.estado["llm_dia"], self.estado["llm_gasto"] = hoje, 0
            self.degradado_avisado = False
        if self.estado["llm_gasto"] >= self.orcamento:
            return False
        self.estado["llm_gasto"] += 1
        try:
            self.gravar()
        except Exception:
            pass
        return True

    def _chat(self, content, max_tokens):
        body = {"model": self.modelo, "max_tokens": max_tokens, "temperature": 0.1,
                "messages": [{"role": "user", "content": content}]}
        req = urllib.request.Request(self.url, json.dumps(body).encode(),
            {"Content-Type": "application/json", "Authorization": "Bearer " + self.key})
        txt = json.loads(urllib.request.urlopen(req, timeout=120).read())
        txt = txt["choices"][0]["message"]["content"]
        return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])

    def _visao_raw(self, jpeg, prompt, max_tokens=3000):
        if not self.ativo() or not self._gasta():
            return None
        try:
            return self._chat([
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url":
                 "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}}],
                max_tokens)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                self.estado["llm_gasto"] = self.orcamento
                self.log("LLM 429 (quota) — modo degradado até virar o dia")
            else:
                self.log(f"LLM HTTP {e.code}")
            return None
        except Exception as e:
            self.log(f"LLM falhou: {e}")
            return None

    @staticmethod
    def _com_contexto(prompt, contexto):
        if not contexto:
            return prompt
        return (f"Contexto deste local, fornecido pelo morador: {contexto}\n"
                "Use o contexto para distinguir o que é NORMAL do que é suspeito.\n\n"
                + prompt)

    def visao(self, jpeg, contexto=""):
        d = self._visao_raw(jpeg, self._com_contexto(PROMPT_VISAO, contexto))
        if d is None:
            return None
        return {"descricao": d.get("descricao", ""),
                "tipo_principal": d.get("tipo_principal", "nada"),
                **{k: bool(d.get(k)) for k in
                   ("pessoa_perto_muro", "interagindo_portao", "parece_entregador")}}

    def forense(self, jpeg, contexto=""):
        return self._visao_raw(jpeg, self._com_contexto(PROMPT_FORENSE, contexto))

    def pes(self, jpeg):
        """Gate geométrico: lista de y2 (pés, 0-1000) das pessoas, ou None se não
        mediu. O bbox é mais estável que a flag semântica, que flutua entre
        chamadas."""
        d = self._visao_raw(jpeg, PROMPT_PES, max_tokens=4000)
        if d is None:
            return None
        try:
            return [p["bbox"][3] for p in d.get("pessoas", [])]
        except (KeyError, TypeError, IndexError):
            return None

    def compara(self, nova_desc, historico):
        """historico: [(ts, desc)] → ts das que descrevem a MESMA pessoa/veículo."""
        if not self.ativo() or not historico or not self._gasta():
            return []
        linhas = "\n".join(f"{i}. {d}" for i, (_, d) in enumerate(historico))
        prompt = (f'Nova observação de câmera de segurança: "{nova_desc}"\n\n'
                  f"Observações anteriores (última hora):\n{linhas}\n\n"
                  "Quais das anteriores descrevem a MESMA pessoa ou o MESMO veículo da "
                  "nova (mesmas roupas/cores/físico, ou mesmo modelo/cor de veículo)? "
                  "IGNORE veículos ESTACIONADOS: se a nova ou a anterior descreve apenas "
                  "um veículo parado/estacionado sem pessoas, NÃO conte como match. "
                  'Na dúvida, NÃO inclua. Responda SOMENTE JSON: {"matches": [<números>]}')
        try:
            d = self._chat(prompt, 2000)
            return [historico[i][0] for i in d.get("matches", [])
                    if isinstance(i, int) and 0 <= i < len(historico)]
        except Exception as e:
            self.log(f"compara falhou: {e}")
            return []


def formata_ficha(d):
    if not d:
        return ""
    linhas = []
    for i, p in enumerate(d.get("pessoas", []), 1):
        linhas.append(f"Pessoa {i}: estatura {p.get('estatura_aparente') or '?'}, "
                      f"cabelo {p.get('cabelo') or '?'}, pele {p.get('pele') or '?'}, "
                      f"{p.get('roupas') or 'roupas não identificadas'}")
    for i, v in enumerate(d.get("veiculos", []), 1):
        nome = v.get("modelo_aparente") or v.get("tipo") or "veículo"
        placa = f"placa {v['placa']}" if v.get("placa_legivel") and v.get("placa") \
                else "placa não legível"
        linhas.append(f"Veículo {i}: {nome} {v.get('cor') or ''}".rstrip() + f", {placa}")
    return "\n".join(linhas)
