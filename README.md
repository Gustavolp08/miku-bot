# Miku Bot

Prefixo: `m!`

## Comandos

- `m!help`
- `m!status`
- `m!util`
- `m!miku`
- `m!analise` + anexo de trace
- `m!deob` + anexo de trace

## Estrutura

O `bot.py` deve ficar no mesmo nível da pasta `source-bot`:

miku-bot/
├── bot.py
├── requirements.txt
├── .gitignore
└── source-bot/
    ├── deVirtualizer.py
    ├── vmAnalyzer.py
    └── utils.py

## Token

Não coloque o token no código nem no GitHub.

Defina a variável de ambiente:

DISCORD_TOKEN=SEU_TOKEN

No Wispbyte, coloque isso em Startup > Environment Variables.

## Wispbyte

Startup command:

python bot.py

A pasta `source-bot` precisa estar presente no servidor.
