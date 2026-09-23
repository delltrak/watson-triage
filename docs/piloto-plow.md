# Piloto Watson + plow-agents (sem OpenClaw)

Este documento descreve o **primeiro recorte de infra** do piloto: imagem Docker
e `compose.yml` para subir o Watson como variante Hermes na stack oficial
**[plow-agents](https://github.com/plow-pbc/plow-agents)**.

**OpenClaw / `plow-openclaw-agent` não são usados.** O chat entra pela linha
iMessage Plow que o João já tem; reutilizamos a credencial dessa linha.

## O que este recorte entrega

| Arquivo | Papel |
|---|---|
| `Dockerfile` | `FROM` a imagem base plow-hermes-agent (pin `base-<sha>` + digest), instala o pacote `watson`, persona e MCP |
| `compose.yml` | Serviço `agent` no formato do `compose.example.yml` do plow-agents |
| `runtime/persona.md` | Persona PT-BR (sempre draft PR, nunca merge; sem falar de branch/PAT/Docker) |
| `runtime/mcp-watson.yaml` | Bloco `mcp_servers.watson` → `watson mcp` (stdio, só leitura) |
| `image/cont-init.d/20-watson-mcp` | Garante home Watson + merge do MCP em boots com volume existente |

`watson_investigate` aceita **número ou URL** de issue (parser no núcleo +
ponte MCP). Ainda **não** expõe repair/draft-PR via MCP. A ponte continua só
leitura: `watson_status` e `watson_investigate` (ver [hermes-plow.md](hermes-plow.md)).

## Pré-requisitos

- Docker Compose **2.24+**
- CLI [plow-agents](https://github.com/plow-pbc/plow-agents) autenticada na conta Plow do João
- Linha iMessage Plow **já existente** (mesma do piloto atual)
- Checkout deste repositório na branch `piloto`

Credenciais GitHub (`gh`) e Codex no *host* continuam necessárias para a
investigação completa funcionar dentro do container; neste recorte o importante
é a imagem **subir** e falar na linha Plow. Sem `gh`/Codex autenticados no
ambiente do agente, `watson_investigate` pode falhar — isso é gap conhecido do
piloto chat-first, não bloqueio deste scaffolding.

## Pin da imagem base

O `Dockerfile` fixa (imutável):

```text
public.ecr.aws/e1h7x4a2/plow-cloud-agents:base-67021a7029e33e80bcb27899be6515a5a0e9b37b@sha256:0c3892e93c1a001c61fb7106396e0a4b7e0219008184fd90719caa84a3390ff0
```

Mesmo padrão de
[life-assistant-hermes-agent](https://github.com/plow-pbc/life-assistant-hermes-agent).
Ao atualizar a base, mude **tag e digest** juntos.

## Subir localmente com a linha existente

No diretório do repositório (`watson-triage`, branch `piloto`):

1. Liste as linhas e anote o ID da linha iMessage que o João já usa:

   ```sh
   plow-agents lines
   ```

2. Faça o deploy local (mint da credencial + `docker compose up` com build):

   ```sh
   plow-agents deploy --local --line ln_XXXXXXXX
   ```

   Substitua `ln_XXXXXXXX` pelo ID real. O CLI escreve `./plow-credentials`
   (já no `.gitignore`) e sobe o serviço `agent`.

3. Acompanhe os logs:

   ```sh
   docker compose logs -f
   ```

4. Envie uma mensagem na conversa iMessage dessa linha. A persona Watson deve
   responder em português; ferramentas MCP ficam disponíveis para o gateway.

5. Para encerrar e limpar o volume do home (sessões/estado):

   ```sh
   docker compose down -v
   ```

### Credencial fora do checkout

Se a credencial da linha já existir em outro caminho (recomendado para não
perder no `down -v` + re-export):

```sh
export PLOW_CREDENTIALS="$HOME/.config/plow/watson-piloto-credentials"
plow-agents deploy --local --line ln_XXXXXXXX
```

O `compose.yml` lê `${PLOW_CREDENTIALS:-./plow-credentials}`.

### Só mint (sem subir)

```sh
plow-agents mint ln_XXXXXXXX --credential-file ./plow-credentials
docker compose up --build
```

## O que NÃO fazer

- Não usar `plow-openclaw-agent` nem qualquer stack OpenClaw neste piloto.
- Não fazer push para `main` a partir deste recorte.
- Não commitar `plow-credentials`, `.env` ou tokens.
- Não tratar Plow Latch como dependência obrigatória deste scaffolding.
- Não fazer merge de PRs pelo agente — correção = draft PR.

## Segredos e estado

| Item | Onde | Notas |
|---|---|---|
| Credencial da linha Plow | `plow-credentials` ou `PLOW_CREDENTIALS` | Escopo da linha; mint/rotate via plow-agents |
| Home Hermes + Watson | volume Docker `agent-home` → `/var/lib/hermes` | Estado em `/var/lib/hermes/watson` |
| GitHub / Codex | ambiente do container / volume | Fora do escopo deste recorte; necessário para investigar de verdade |

## Referências

- [plow-agents](https://github.com/plow-pbc/plow-agents) — `deploy --local`, mint, lines
- [plow-hermes-agent](https://github.com/plow-pbc/plow-hermes-agent) — base Hermes / variantes
- [life-assistant-hermes-agent](https://github.com/plow-pbc/life-assistant-hermes-agent) — exemplo de variante
- [docs/hermes-plow.md](hermes-plow.md) — ponte MCP e estado do piloto
