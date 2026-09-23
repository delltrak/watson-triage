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
| `runtime/mcp-watson.yaml` | Bloco `mcp_servers.watson` → `watson mcp` (stdio, só leitura), com `env` (`GH_TOKEN`, `PLOW_AGENT_TOKEN`, `HOME`) + skill `github` desligada no plow_chat |
| `image/watson-config-merge.py` | Merge idempotente do overlay acima no `config.yaml` (seed da imagem e boot) |
| `image/cont-init.d/20-watson-mcp` | Garante home Watson + merge do MCP em boots com volume existente |
| `image/hooks/watson-greet/` + `image/cont-init.d/25-watson-greeting-bypass` | Hook de gateway: greeting do dono → `speak_this` exato, sem LLM |

`watson_investigate` aceita **número ou URL** de issue (parser no núcleo +
ponte MCP). Ainda **não** expõe repair/draft-PR via MCP. A ponte continua só
leitura: `watson_status` e `watson_investigate` (ver [hermes-plow.md](hermes-plow.md)).

## Pré-requisitos

- Docker Compose **2.24+**
- CLI [plow-agents](https://github.com/plow-pbc/plow-agents) autenticada na conta Plow do João
- Linha iMessage Plow **já existente** (mesma do piloto atual)
- Checkout deste repositório na branch `piloto`

GitHub no container: a imagem instala o CLI `gh`. A autenticação entra pelo
arquivo local **`github-credentials`** (gitignored) com `GH_TOKEN=...`, carregado
pelo `compose.yml` (opcional até existir). Sem esse token, `watson_status` e
`watson_investigate` **dizem claramente** que o GitHub não está conectado e
pedem para o dono da linha conectar — sem tutorial de token/arquivo no chat e
sem fingir que está tudo ok. O passo a passo fica só nesta doc.

Codex local continua necessário para a inferência completa da investigação;
falta de Codex também aparece no status.

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


## Conectar o GitHub (uma vez, no Mac do piloto)

O login `gh` do macOS usa o keychain — montar `~/.config/gh` **não** leva o token
para o container. Caminho suportado:

1. Crie um token com leitura dos repositórios (GitHub → Settings → Developer
   settings → Personal access tokens), **ou** exporte o token já logado no host:
   `gh auth token` (não cole o valor em chats/logs).
2. No checkout `watson-triage`, copie o exemplo e preencha:

   ```sh
   cp github-credentials.example github-credentials
   # edite github-credentials: uma linha GH_TOKEN=...
   chmod 600 github-credentials
   ```

3. Recrie o agente para carregar o arquivo:

   ```sh
   docker compose up --build -d
   ```

Depois disso, `watson_status` deve reportar GitHub conectado e
`watson_investigate` pode consultar issues. Sem o arquivo, o status fica
honesto e pede para conectar.

## Como o status chega ao chat

- O Hermes sobe servidores MCP stdio com env **filtrado** (só `PATH`/`HOME` e
  afins). Por isso `runtime/mcp-watson.yaml` repassa `GH_TOKEN` e
  `PLOW_AGENT_TOKEN` via `env:` (`${VAR}` resolvido do env do gateway; o
  `config.yaml` guarda só o template). Sem isso o `watson_status` via MCP via o
  GitHub como desconectado mesmo com o token montado.
- Greeting puro do dono na DM Plow (texto só com `oi`, `olá`, `hey`, `bom dia`,
  …) é respondido pelo hook `watson-greet` com o `speak_this` exato do
  `watson_status`, **sem chamar o LLM**; o wake de restart do Plow fica em
  silêncio (`NO_REPLY`) do mesmo jeito. O turno vai para o transcript
  normalmente. Qualquer outra mensagem (pedido, pergunta de status, foto,
  resposta citando outra mensagem, grupo, não-dono) segue pelo LLM + tools.
- Investigação lê a **timeline da issue**: se um PR ligado já entregou a issue,
  a resposta começa com uma linha fixa (`speak_first`) sugerindo fechar, ou
  pedindo conferência quando há pendências (itens sem marcar, PR posterior que
  também cita a issue, CI falhando, revisão pedindo mudanças). Com PR aberto, diz
  que já existe e não abre outro. O Watson só **sugere**; nunca fecha issue.
- **Idioma:** o Watson responde no idioma do dono. Ordem: idioma explícito da
  chamada → último idioma detectado nas mensagens do dono (hook) → padrão da
  linha (`config.json` `"language": "en"` ou `WATSON_LANGUAGE=en` no
  `github-credentials`/env do container) → português.
- `/help` na Plow responde o help do Watson (`watson/chat_help.py`, hook
  `command:help`) em vez da lista de comandos do Hermes; `/help en` em inglês.
  Outros argumentos (`/help skills`) seguem para o help do Hermes.
- Codex/Claude: login via chat (`watson_connect_codex` / `watson_connect_claude`);
  credenciais ficam em `/var/lib/hermes/.codex` e `/var/lib/hermes/.claude*`.

Para conferir o status **do jeito que o chat vê**, nunca use `docker exec` como
root (HOME=/root, com `GH_TOKEN` herdado — mostra outro estado). Use o uid do
Hermes com o HOME do runtime:

```sh
docker exec -u 10000 -e HOME=/var/lib/hermes watson-triage-agent-1 \
  sh -c 'tr "\0" "\n" </proc/$(pgrep -o -f "[w]atson --home")/environ | cut -d= -f1'
docker exec -u 10000 -e HOME=/var/lib/hermes watson-triage-agent-1 codex login status
docker logs watson-triage-agent-1 2>&1 | grep -E "\[hooks\]|\[watson-greet\]"
```

## O que NÃO fazer

- Não usar `plow-openclaw-agent` nem qualquer stack OpenClaw neste piloto.
- Não fazer push para `main` a partir deste recorte.
- Não commitar `plow-credentials`, `github-credentials`, `.env` ou tokens.
- Não tratar Plow Latch como dependência obrigatória deste scaffolding.
- Não fazer merge de PRs pelo agente — correção = draft PR.

## Segredos e estado

| Item | Onde | Notas |
|---|---|---|
| Credencial da linha Plow | `plow-credentials` ou `PLOW_CREDENTIALS` | Escopo da linha; mint/rotate via plow-agents |
| Home Hermes + Watson | volume Docker `agent-home` → `/var/lib/hermes` | Estado em `/var/lib/hermes/watson` |
| GitHub (`GH_TOKEN`) | `./github-credentials` (gitignore) | Ver seção abaixo; nunca commitar |
| Codex / Claude Code | login via chat → `/var/lib/hermes/.codex`, `/var/lib/hermes/.claude*` | Codex é necessário para inferência da investigação |

## Referências

- [plow-agents](https://github.com/plow-pbc/plow-agents) — `deploy --local`, mint, lines
- [plow-hermes-agent](https://github.com/plow-pbc/plow-hermes-agent) — base Hermes / variantes
- [life-assistant-hermes-agent](https://github.com/plow-pbc/life-assistant-hermes-agent) — exemplo de variante
- [docs/hermes-plow.md](hermes-plow.md) — ponte MCP e estado do piloto
