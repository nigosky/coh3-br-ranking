# CoH3 Brasil — Ranking Unificado BR

Projeto dedicado exclusivamente ao **Ranking Unificado Brasileiro** de **Company of Heroes 3 (Steam)**, separado do monitor de partidas (`nigosky/coh3-br-tracker`).

## O que este projeto faz
- Consulta diretamente a API oficial da Relic (`getPersonalStat`) para os **44 jogadores brasileiros monitorados / Veteranos (47 contas Steam)** nas 16 filas oficiais (`1v1`, `2v2`, `3v3`, `4v4` × 4 facções), usando filas com pelo menos **10 partidas** (`wins + losses >= 10`) no cálculo de ELO/Skill Points e somando **todo o histórico acumulado de partidas PvP desde o lançamento do CoH3** (inclusive filas `< 10` jogos ou não calibradas + contas mescladas) na coluna `JOGOS` / `TAXA`.
- Consolida todos os modos individuais em um único **Ranking Unificado (Skill Points)** exclusivamente com os Veteranos (`ranking_unificado_br.json` / `.csv`):
  - **ELO base por modo ($M_m$):** maior ELO entre as 4 facções (`USF`, `UKF`, `Wehrmacht`, `DAK`) com $N \ge 10$ jogos na fila.
  - **Ponderação sobre o excedente ($P_m$):** $P_m = 1000 + (M_m - 1000) \cdot w_m$, com pesos $w_{1\text{v}1} = 2{,}0$, $w_{2\text{v}2} = 1{,}5$, $w_{3\text{v}3} = 1{,}0$ e $w_{4\text{v}4} = 1{,}0$.
  - **Skill Points (Âncora + Bônus não-punitivo):** $\max_m(P_m) + 0{,}15 \sum_{j \neq \text{âncora}} \max(0, P_j - 1000)$.
- Renderiza tabelas em alta definição (`Pillow` + fontes `Roboto`/`DejaVu`/`Noto Sans CJK` + 257 bandeiras oficiais ISO PNG + avatares Steam + patente Veteranos), exibindo os Skill Points e o ELO de cada modo (`1v1`, `2v2`, `3v3`, `4v4`) com o modo âncora destacado em dourado e jogadores 100% inativos em cor de fundo própria abaixo dos ativos, **publicando a versão atualizada e apagando automaticamente a mensagem anterior** no Discord para manter sempre apenas a última atualização.
- Consome o repositório público **COH3 Stats Open Data** (`storage.coh3stats.com/matches/matches-{ts}.json`, mantendo **todo o histórico completo** de partidas no cache incremental `ranking_cache.db`) para gerar o **Ranking Unificado Top 32 de Times Brasileiros 2v2 Arranjados** (média ELO Eixo + Aliados com selo de lado, ambos os jogadores brasileiros ou Veteranos, fusão de contas secundárias e nomes padronizados pela base compartilhada, mínimo de **10 jogos no geral** para calibração de ELO, Veteranos em destaque dourado, **somente duplas ativas nos últimos 30 dias** — removendo qualquer dupla inativa >30 dias — e corte estrito no **Top 32**, sem exibir duplas após a 32ª colocação), substituindo automaticamente a mensagem anterior no mesmo canal.
- Publica automaticamente todos os dias às **`06:02 BRT`** (`09:02 UTC`, workflow `boletim_diario.yml`) o **Boletim Diário dos Veteranos** (apagando o boletim do dia anterior assim que o novo é postado) com o resumo completo do dia anterior (partidas, vitórias/derrotas, saldo de ELO, modos jogados, Prêmios do Dia) + o card especial do **Jogo do Dia** (priorizando vitórias em `1v1`, `2v2`, `3v3`/`4v4` em time fechado 100% Veteranos, duelos contra outro brasileiro ou **partidas que mexam com o Top 5 do Ranking Unificado dos Veteranos** — deixando `3v3`/`4v4` isolados por último — e aceitando derrota qualificada apenas se for disputada, longa `>= 30 min` contra **Top Global** Top 20 Relic com `ELO > 1800` ou derrota disputada `>= 20 min` que troque posição no Top 5, com links diretos de replay no `COH3 Stats` e `cohdb`).
- Executa os rankings automaticamente **2x por dia** (**`00:02 BRT`** com backup `00:37` e **`12:02 BRT`** com backup `12:37`) e o Boletim Diário às **`06:02 BRT`** (backups `06:37` e `08:17`) via GitHub Actions com **guarda idempotente + watchdog cruzado** (`schedule_guard.py`) que evita duplicidades e recupera automaticamente qualquer janela perdida por atraso de cron do GitHub, além de gate obrigatório de testes unitários e disparo manual (**Actions → Run workflow**).

## Configuração dos Secrets no GitHub
1. No canal do Discord onde deseja exibir o **Ranking Unificado BR**, crie um Webhook (**Editar Canal → Integrações → Webhooks → Novo Webhook → Copiar URL do Webhook**).
2. Abra a página de Secrets deste repositório:
   `https://github.com/nigosky/coh3-br-ranking/settings/secrets/actions/new`
3. Crie o Secret obrigatório:
   - **Name:** `DISCORD_RANKING_WEBHOOK_URL`
   - **Secret:** cole a URL do Webhook (`https://discord.com/api/webhooks/...`)
4. *(Opcional)* Para enviar o **Boletim Diário (06:00 BRT)** em um canal separado do canal de rankings fixos, crie também o Secret `DISCORD_DAILY_RECAP_WEBHOOK_URL` (se não configurado, ele publica automaticamente no mesmo canal de `DISCORD_RANKING_WEBHOOK_URL`).
5. Clique em **Add secret** e, se quiser publicar imediatamente, vá em **Actions → Run workflow**.

## Uso local (opcional)
```bash
# Executar suíte de testes automatizados (ranking, ELO, boletim diário, PNG e segurança)
python3 -m unittest discover -s tests -v

# Varredura + exportação JSON/CSV + prévia dos PNGs sem enviar ao Discord
python3 run_ranking.py --dry-run --preview-dir ./preview

# Prévia local do Boletim Diário + Jogo do Dia sem enviar ao Discord
python3 run_daily_recap.py --dry-run --preview-dir ./preview_daily

# Publicação real no Discord usando a variável de ambiente
DISCORD_RANKING_WEBHOOK_URL="https://discord.com/api/webhooks/..." python3 run_ranking.py --verify-discord --force
```
