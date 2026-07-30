# O relógio dos top blitz

Dashboard estático que mostra padrões de horário e aberturas mais jogadas
dos top 50 jogadores de blitz do Chess.com, sempre em Horário de Brasília
(UTC-3, fixo). Os dados são buscados **server-side** (sem problema de CORS)
por uma GitHub Action agendada, salvos em `docs/data.json`, e a página em
`docs/index.html` só lê esse arquivo (mesma origem, sempre funciona).

**Recursos:**
- Todos os horários já convertidos para o horário de Brasília
- Filtra só partidas blitz **3+0** (configurável em `TIME_CONTROL_FILTER`)
- Rotina do jogador-alvo (`TARGET_USERNAME`, hoje configurado para `LPSupi`)
  e um **ranking de sobreposição**: para cada jogador, calcula a % das
  partidas dele que caem em algum horário (dia+hora) em que o jogador-alvo
  já jogou pelo menos uma vez no período — quanto maior, mais esse jogador
  costuma estar ativo nos mesmos horários
- Clique num jogador na tabela para ver: as **melhores janelas para
  encontrá-lo** (dia + hora com maior % histórico de partidas dele) e as
  **últimas partidas** dele
- **Forma recente** (últimas 5 partidas) de cada jogador, em bolinhas
  verde/dourado/vermelho (vitória/empate/derrota) — passe o mouse ou clique
  no jogador pra ver adversário, data e horário de cada uma

## Estrutura

```
scripts/fetch_data.py         # busca os dados na API do Chess.com e gera docs/data.json
.github/workflows/update-data.yml   # roda o script todo dia às 06:00 UTC (e sob demanda)
docs/index.html                # página estática (o que vira o GitHub Pages)
docs/data.json                 # dados gerados (placeholder vazio incluso)
requirements.txt
```

## Configurar

1. Crie um repositório novo no GitHub e suba estes arquivos (mantendo a
   estrutura de pastas).
2. Edite `scripts/fetch_data.py` e troque o e-mail de contato em `HEADERS`
   pelo seu (a API do Chess.com pede um User-Agent identificável).
3. Ajuste `PLAYER_COUNT` (até 50) e `MONTHS_BACK` no topo do script, se quiser.
4. Ative o GitHub Pages: **Settings → Pages → Deploy from a branch → branch
   `main`, pasta `/docs`**.
5. Rode a Action manualmente uma vez para gerar os primeiros dados: aba
   **Actions → Atualizar dados de blitz → Run workflow**.
6. Depois disso ela roda sozinha todo dia. A página sempre mostra o
   `data.json` mais recente commitado no repo.

## Rodar localmente (opcional)

```bash
pip install -r requirements.txt
python scripts/fetch_data.py
```

Isso gera/atualiza `docs/data.json` na hora, sem precisar esperar a Action.

## Por que isso resolve o problema de CORS

CORS é uma regra do **navegador**, não do servidor da API. O Chess.com não
envia o cabeçalho `Access-Control-Allow-Origin`, então qualquer fetch feito
*a partir do navegador* para `api.chess.com` é bloqueado ou intermitente —
não importa onde a página esteja hospedada. Rodando a busca dentro da
GitHub Action (um ambiente servidor, sem navegador), essa regra simplesmente
não se aplica, e o resultado é salvo como um arquivo estático que a página
lê normalmente.
