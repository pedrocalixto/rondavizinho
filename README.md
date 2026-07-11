<p align="center">
  <img src="docs/img/banner.svg" alt="RondaVizinho — o vigia inteligente da sua rua" width="460">
</p>

Avisos inteligentes no seu Telegram, com foto e descrição, usando as câmeras
que você já tem. Ainda não conhece o projeto? Comece pela
[página de apresentação](https://rondavizinho.com.br/).

## O que você precisa

- **DVR ou câmera com "Detecção inteligente de pessoas e veículos"** — maioria
  dos Intelbras MHDX e outros compatíveis com o padrão Dahua. Não precisa
  adivinhar: o assistente de instalação testa o seu e avisa (alguns modelos
  anunciam o recurso e não entregam).
- **Um computador ligado na sua rede** — opções abaixo.
- **Telegram** no celular.
- **Uma IA que enxerga fotos** — qualquer IA multimodal compatível serve
  (inclusive uma local, se você for técnico). Recomendamos o plano **gratuito**
  do Google Gemini. Gere a chave em
  [aistudio.google.com/api-keys](https://aistudio.google.com/api-keys).
- **Internet.**

## Onde rodar

Qualquer opção abaixo serve, desde que fique ligado 24h.

| Dispositivo | Consumo |
|---|---|
| **PC/notebook** (Windows ou Linux) | — |
| **Raspberry Pi 4/5** (2 GB basta) | ~3–7 W |
| **Mini-PC / NUC** (Intel N100 e afins) | ~6–15 W |

## Como instalar

**Windows** — abra o PowerShell (menu Iniciar → digite "PowerShell") e cole:

```powershell
irm https://rondavizinho.com.br/get.ps1 | iex
```

**Linux (Debian/Ubuntu)** — no terminal:

```bash
curl -fsSL https://rondavizinho.com.br/get.sh | sudo bash
```

O instalador baixa tudo sozinho (código, Python, ffmpeg) e **abre o assistente
no navegador** (`http://localhost:8080`) — ele acha seu DVR, testa as câmeras
e te guia para criar o bot do Telegram e a chave da IA.

## Problemas comuns

- **"Não consigo abrir a página do assistente"** — abra no próprio computador
  (`http://localhost:8080`); pelo celular, confira se está no mesmo Wi-Fi e
  use o nome do computador no lugar de `localhost`.
- **"Meu DVR não tem detecção inteligente"** — sem esse recurso o
  RondaVizinho não recebe os eventos de pessoa/veículo. Verifique se há
  atualização de firmware; senão, o caminho é um DVR compatível (MHDX recente).
- **"O assistente não achou meu DVR"** — o DVR precisa estar no mesmo roteador;
  se souber o IP dele, digite direto.
- **"Parou de me avisar"** — mande `/status` ao bot; sem resposta, desligue e
  ligue o computador da tomada.
- **"Está avisando demais"** — `/vigiar off` quando estiver em casa, ou ajuste
  os horários no assistente.

## Painel e comandos

Histórico, fotos dos alertas e resumo da sua rua: o mesmo endereço do
assistente (`http://localhost:8080`) vira o painel quando a instalação termina.

- `/vigiar on` / `off` / `auto` — liga, desliga ou segue os horários configurados
- `/status` — como está tudo
- `/ajuda` — lista os comandos

## ⚠️ Aviso importante

Projeto **comunitário, sem fins lucrativos**, oferecido **as-is** (ver [LICENSE](LICENSE), Apache 2.0). Ele **ajuda** a monitorar
sua casa, mas **não substitui** monitoramento profissional nem garante detectar
ou impedir qualquer evento. Depende de serviços e equipamentos de terceiros
(DVR/câmera, IA) que podem falhar ou mudar sem aviso. **Use por sua conta e risco.**

---

Documentação técnica: [docs/TECNICO.md](docs/TECNICO.md)
