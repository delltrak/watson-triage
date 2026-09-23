# E2E do Watson (iMessage real)

Testes de ponta a ponta que mandam mensagens **de verdade** do Messages do Mac do
dono para a linha do Watson e conferem o que o Watson respondeu, em PT e EN.

## Laboratório

`e2e/lab_setup.py` cria (se não existir) o repositório **privado**
`delltrak/watson-e2e-lab` a partir de `e2e/lab/` e monta um cenário por caso de
uso. É idempotente (`e2e/.lab-state.json`) e só escreve no repo do laboratório.

| Id | Cenário | Esperado |
|---|---|---|
| S01 | PR mergeado com `Refs #N` que entrega o que a issue pede (`app/customers.py` + testes) + veredito APROVAR | sugere fechar |
| S02 | PR com `Closes #N`, issue reaberta depois do merge | confira antes de fechar |
| S03 | PR em rascunho aberto | "já existe o PR", não duplica |
| S04 | issue guarda-chuva citada por PR multi-issue | sem frase de abertura |
| S05 | PR diz "sem encerrá-la" | nada |
| S06 | checklist com item desmarcado | confira antes de fechar |
| S07 | PR posterior multi-issue também cita a issue | confira antes de fechar |
| S08 | merge com CI vermelho | confira antes de fechar |
| S09 | issue fechada | nada |
| S10 | número de PR no lugar de issue | erro claro |
| S11 | bug real sem PR (`app/cart.py`) | investigação normal (futuro draft) |
| S12 | issue vaga | investigação normal |
| E01/E02 | issue em inglês (`app/coupons.py`) e S01 pedido em inglês | copy em inglês |
| R01/R02 | `#N` sem repo depois de investigar o lab | "Olhando …#N (último repositório que investigamos)" |

Casos de chat (C00 zera o estado do onboarding): primeira saudação com o checklist
completo, depois a saudação curta (PT/EN, texto exato), ajuda em linguagem
natural ("o que você faz?", "what can you do?"), `/help` seguindo o idioma
lembrado, tropa (ver, trocar, resetar), status sem tutorial, pergunta sobre
modelos.

Um PR de fixture precisa entregar de verdade o que a issue pede: o Watson
rebaixa "sugiro fechar" para "confira antes de fechar" quando a investigação vê
que falta algo, e isso é o comportamento certo.

## Rodar

```sh
python3 e2e/lab_setup.py            # uma vez (ou para completar cenários)
python3 e2e/run.py --offline        # só as decisões de PR ligado, sem iMessage
python3 e2e/run.py                  # tudo pelo iMessage real (~30-40 min)
python3 e2e/run.py --only C01,S01   # subconjunto
```

Precisa: macOS com o Messages logado na conta do dono (AppleScript/Automação),
`docker` com o container `watson-triage-agent-1`, `gh` logado. O resultado de
cada turno é lido do `state.db` do Hermes; comandos (`/help`) pelo log de envio.
Relatórios em `e2e/reports/`.
