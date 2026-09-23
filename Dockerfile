# Watson piloto — variante plow-hermes-agent para plow-agents (NÃO OpenClaw).
#
# O tag base-<sha> é imutável e nomeia um commit de plow-pbc/plow-hermes-agent.
# Digest pinado: mesmo padrão de life-assistant-hermes-agent. Ao subir a base,
# atualize tag + digest juntos e reconstrua.
FROM public.ecr.aws/e1h7x4a2/plow-cloud-agents:base-67021a7029e33e80bcb27899be6515a5a0e9b37b@sha256:0c3892e93c1a001c61fb7106396e0a4b7e0219008184fd90719caa84a3390ff0

# Identidade da variante: plow-init compõe SOUL.md = base + este arquivo a cada boot.
# Não copiar para /var/lib/hermes/SOUL.md — é sobrescrito.
COPY --chmod=0644 runtime/persona.md /opt/hermes/plow-seed/persona.md
COPY LICENSE /usr/share/doc/watson-triage/LICENSE

# Pacote Watson (MCP stdio + CLI) no venv da imagem.
COPY pyproject.toml /opt/watson-triage/pyproject.toml
COPY watson /opt/watson-triage/watson
RUN set -eu; \
    uv pip install --python /opt/hermes/.venv/bin/python --no-cache /opt/watson-triage; \
    /opt/hermes/.venv/bin/watson --help >/dev/null; \
    test -x /opt/hermes/.venv/bin/watson

# Injeta mcp_servers.watson no seed da imagem (homes novos herdam via cont-init 00).
# Homes já existentes recebem o mesmo bloco em image/cont-init.d/20-watson-mcp.
COPY --chmod=0644 runtime/mcp-watson.yaml /opt/watson-triage/mcp-watson.yaml
RUN set -eu; \
    /opt/hermes/.venv/bin/python - <<'PY'
from pathlib import Path
import yaml

overlay = yaml.safe_load(Path("/opt/watson-triage/mcp-watson.yaml").read_text())
watson = overlay["mcp_servers"]["watson"]
for path in (
    Path("/opt/hermes/plow-seed/config.yaml"),
    Path("/var/lib/hermes/config.yaml"),
):
    data = yaml.safe_load(path.read_text()) or {}
    servers = data.setdefault("mcp_servers", {})
    servers["watson"] = watson
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    print(f"seeded watson mcp into {path}")
PY

# GitHub CLI: watson_investigate uses `gh api`. Auth via GH_TOKEN (see compose).
# Official apt repo from cli.github.com.
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg; \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg; \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends gh; \
    rm -rf /var/lib/apt/lists/*; \
    gh --version; \
    git --version

# Codex CLI + Claude Code CLI (latest). Node/npm already on plow-hermes base.
# --allow-scripts required so Claude Code postinstall links the native binary.
RUN set -eu; \
    npm install -g --allow-scripts=@anthropic-ai/claude-code \
      @openai/codex@latest \
      @anthropic-ai/claude-code@latest; \
    codex --version; \
    claude --version

# Estado Watson sob o home Hermes (volume agent-home). Criado de novo no boot.
COPY --chmod=0755 image/cont-init.d/20-watson-mcp /etc/cont-init.d/20-watson-mcp
