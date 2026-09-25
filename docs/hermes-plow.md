# Integração Hermes / Plow

Estado: núcleo local e entrega Plow validados; integração Hermes opcional pendente.

No piloto, o login por iMessage e a criação da linha já foram concluídos.
A API teve uma indisponibilidade HTTP 503 e se recuperou. Texto e áudio neutros
foram recebidos no iMessage. O envio do M4A exige `audio/mp4`: o tipo inferido
`audio/mp4a-latm` foi recusado pela API. A versão atual já corrige essa identificação.

Em 13/09, Samuel Odio confirmou que Hermes não é obrigatório para a competição.
Uma estrutura própria pode participar com registro e verificação; a variante
Hermes continua uma opção para distribuição pela infraestrutura Plow.
Fonte: [resposta da organização](https://discord.com/channels/1519035948191449268/1544106357865586718/threads/1548329792032612473).

## Componentes consultados em 15/09/2026

| Projeto | Revisão consultada | Papel |
|---|---|---|
| `plow-pbc/plow-agents` | `8ce907e220ab67018d6857e8054a41eed4ecd279` | Login, linhas e credenciais do agente |
| `plow-pbc/plow-hermes-agent` | `b78250ee114c8cd00c6dd2c4e84e0842c4e1fa14` | Base Hermes, inicialização e gateway |
| `plow-pbc/hermes-plugin-plow` | `8e055e059ce774b455869d915525e63933db18fe` | Transporte de texto e anexos, incluindo voz |

O `plow-agents` administra a implantação; o cérebro do Watson é o pacote deste
repositório. A variante deve herdar a imagem Hermes do Plow e adicionar a persona
e a integração, preservando inicialização, identidade e contratos da base.

## Ponte já implementada

`watson --home /caminho/estado mcp` atende MCP por stdio e oferece:

- `watson_status`: histórico local.
- `watson_investigate(number)`: investigação no único repositório configurado.

Não há ferramenta MCP para enviar mensagens, alterar destinatários, escrever
comentários, criar branches ou fazer merge. Argumentos extras são recusados.

Exemplo de configuração para uma instalação Hermes **local**, com os caminhos
substituídos pelos caminhos absolutos da instalação do usuário:

```yaml
mcp_servers:
  watson:
    command: /caminho/watson/.venv/bin/watson
    args: ["--home", "/caminho/estado", "mcp"]
    timeout: 900
    connect_timeout: 30
```

O processo da ponte deve encontrar `gh` autenticado e uma credencial Plow para
inferência -- `PLOW_API_BASE` e `HERMES_CUSTOM_PLOW_API_KEY` no ambiente, ou
`plow_credential_file` no config, que é o que `PlowInference.from_config` lê.
Não há mais `codex login`: o Codex deixou de ser o backend de inferência. Esse
exemplo não foi carregado em um gateway Hermes real. Não copiar o ambiente
pessoal inteiro ou credenciais de escrita para dentro de uma imagem pública.

## Estado do piloto

O Watson usa a inferência do Plow e a API Plow diretamente. A ponte Hermes é opcional e ainda não foi validada em um gateway real.

- Login Plow, linha e entrega ao proprietário: validados.
- Texto e vídeo reproduzível no iMessage: validados.
- Áudio Sol: geração validada com o endpoint Read Aloud usado pelo Deca.
- Mensagem de voz nativa: bloqueada até o Plow expor a operação do provedor; [issue #199](https://github.com/plow-pbc/hermes-plugin-plow/issues/199).
- Memória, retomada após resposta e pedido privado de acesso: validados no laboratório fictício.
- Registro de uso: adaptador local para o cliente oficial do Agent Index, sem incluir histórico pessoal.

Uma instalação Hermes precisa configurar a ponte e a inferência separadamente. Ferramentas independentes de shell/plugins no Hermes não são protegidas pelas restrições do adaptador Watson. Não copiar credenciais pessoais para imagens públicas.

## Fontes

- [Base e contrato para variantes](https://github.com/plow-pbc/plow-hermes-agent)
- [Administração de agentes e login](https://github.com/plow-pbc/plow-agents)
- [Plugin Plow e mídia](https://github.com/plow-pbc/hermes-plugin-plow)
- [Configuração MCP no Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp/)
