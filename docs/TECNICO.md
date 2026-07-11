# Documentação técnica

## Arquitetura

Daemon Python **100% stdlib** (única dependência externa: binário `ffmpeg`).
Long-poll dos eventos de detecção do DVR (SMD `SmartMotionHuman/Vehicle` — SOMENTE SMD, ver requisito abaixo) via `eventManager.cgi` + frames 1080p do main stream RTSP.

```
vigia/
├── __main__.py   entrada: daemon | --check (asserts puros) | --teste (e2e real)
├── config.py     config.json único + estado.json (mutável pequeno)
├── core.py       motor de padrões: permanência (runs de marcas), recorrência
│                 (comparação de descrições), portão/muro (LLM), resumo horário,
│                 cooldowns por classe, HITL (botão de "falso positivo")
├── dvr.py        digest auth, escuta() com heartbeat-tick, frame() ffmpeg -q:v 2
├── llm.py        provider gemini|openai|none; orçamento diário; degradação em 429
├── notify.py     Telegram: sendPhoto multipart stdlib, inline keyboards,
│                 getUpdates long-poll (comandos + callbacks HITL)
├── storage.py    CSV + arquivo jpg/txt com rotação por espaço + eventos.jsonl
│                 (schema estável p/ correlação de vizinhança futura)
└── diag.py       falhas consecutivas → aviso self-service no Telegram; heartbeat
```

Decisões importantes:
- Só PESSOA confirmada pela visão vira alerta em canais `somente_pessoa` —
  veículo/movimento genérico não (carro estacionado em rua pública é benigno; as
  marcas de movimento do DVR não são por objeto, tráfego contaminaria a
  "persistência").
- Recorrência ignora veículos estacionados (prompt) pelo mesmo motivo.
- Sem LLM (provider none / quota): permanência, resumo e fotos continuam;
  recorrência e portão/muro desativam (dependem de visão).

## Rodando manualmente (sem o assistente)

```bash
sudo apt install ffmpeg
git clone <este repo> /opt/vigia && cd /opt/vigia
cp config.example.json /var/lib/vigia/config.json   # edite
python3 -m vigia --check     # valida funções puras
python3 -m vigia --teste     # frame → LLM → Telegram → storage
python3 -m vigia             # daemon
```

`VIGIA_CONFIG=/outro/caminho/config.json` muda o caminho da config.
Units systemd em `systemd/` (produção: `Restart=always`).

## Requisito: DVR/câmera com "Detecção inteligente de pessoas e veículos"

O único requisito de hardware é uma câmera ou DVR que exponha os eventos de
**"Detecção inteligente de pessoas e veículos"** pelo protocolo compatível
**Dahua** (`eventManager.cgi` com `SmartMotionHuman`/`SmartMotionVehicle`). Não
é amarrado à Intelbras — qualquer gravador Dahua-compat com o recurso serve;
a Intelbras (OEM Dahua) é só a marca mais comum no Brasil.

Nomenclatura: "Detecção inteligente de pessoas e veículos" é o termo comercial
(o que o usuário vê no datasheet/menu). "SMD" (Smart Motion Detection) é o nome
interno da plataforma Dahua — aparece só na API (`SmartMotionDetect`) e é como
o código se refere ao recurso.

O produto consome EXCLUSIVAMENTE esses eventos. Não há fallback para
`VideoMotion`: movimento bruto (chuva, sombra, farol) mandado ao LLM é
pouquíssimo efetivo e gera falso positivo estrutural.

**Modelos** (referência, não exaustivo): na Intelbras a linha MHDX anuncia o
recurso nas séries 1000, 3000 e 5000 — MAS a exposição via API depende da
GERAÇÃO do firmware: há aparelhos que anunciam o recurso no menu e não expõem
`SmartMotionDetect` no CGI. Outros DVRs Dahua-compat com o mesmo recurso também
funcionam. **A etiqueta da caixa não vale; a sonda vale.**

**Sonda automática** (`Dvr.suporta_smd()`): `GET configManager.cgi?action=
getConfig&name=SmartMotionDetect` — roda no startup do daemon, no `--teste` e no
assistente de instalação; sem o recurso o dono recebe aviso no Telegram de que
o vigia ficará surdo.

## LLM — qualquer IA multimodal

O produto não é amarrado a nenhum LLM específico: precisa apenas de um modelo
**multimodal** (que enxerga imagem) acessível por API. Três providers:

- **Gemini** (sugerido pela facilidade): endpoint OpenAI-compatible
  (`generativelanguage.googleapis.com/v1beta/openai/`), key do AI Studio,
  free tier ~1.5k req/dia. `orcamento_dia` corta antes do 429; 429 = degradado
  até virar o dia.
- **openai**: qualquer endpoint compatível (`base_url` + `api_key` + `modelo`) —
  LM Studio/Ollama/OpenRouter/etc.
- **none**: modo degradado permanente.

## Windows

`windows/install.ps1` (winget: Python + ffmpeg; Task Scheduler com restart
automático; config em `%ProgramData%\Vigia`). O código é portável: sem strftime
%F/%T, paths via os.path, threads (sem fork).

## Anti-falso-positivo por canal

- `canais.N.contexto` — texto livre injetado nos prompts ("carros estacionados
  são normais aqui"); gere com `python3 -m vigia --configurar` (form leigo)
- `canais.N.somente_pessoa` — só alerta com PESSOA confirmada pela visão
- `canais.N.pes_y_min` (0-1000) — gate geométrico: bbox dos pés da pessoa mais
  próxima abaixo do limiar = em frente DESTA casa; suprime pessoa na casa
  vizinha visível no mesmo quadro (a flag semântica flutua; o bbox não)

## Painel web (dashboard)

`webui/server.py` (stdlib) + `webui/static/index.html` (Bootstrap 5 + Chart.js
via CDN, zero build). Porta 8080; unit `systemd/vigia-web.service`; no Windows a
tarefa `VigiaWeb` é criada pelo install.ps1. Lê direto o storage do daemon:
- `GET /api/resumo` — série detecções/dia + aumento % acumulado (vs 1º dia) +
  alertas/dia + insights (hora de pico, câmera mais ativa, razão sinal/ruído,
  tendência 7 dias)
- `GET /api/fotos[?dia=]` — fotos dos alertas com o contexto do `.txt`
- `GET /foto?p=...` — serve o JPEG (com proteção a path traversal)
Sem autenticação: pensado para a LAN da casa. `--check` valida as funções puras.

## Contribuindo

- Núcleo permanece stdlib-only; dependências novas só em serviços isolados,
  com venv própria.
- `python3 -m vigia --check` precisa passar; adicione asserts para lógica nova.
