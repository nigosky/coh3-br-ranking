# CoH3 Brasil — Ranking Unificado BR

Projeto dedicado exclusivamente ao **Ranking Unificado Brasileiro** de **Company of Heroes 3 (Steam)**, separado do monitor de partidas (`nigosky/coh3-br-tracker`).

## O que este projeto faz
- Consulta diretamente a API oficial da Relic (`getPersonalStat`) para os **Veteranos brasileiros monitorados** (sincronizados automaticamente de `players_roster.json`, **sem merge de contas Smurf**) nas 16 filas oficiais (`1v1`, `2v2`, `3v3`, `4v4` × 4 facções), usando filas com pelo menos **10 partidas** (`wins + losses >= 10`) no cálculo de ELO/Skill Points e somando **todo o histórico acumulado de partidas PvP da conta desde o lançamento do CoH3** (inclusive filas `< 10` jogos ou não calibradas) na coluna `JOGOS` / `TAXA`.
- Consolida todos os modos individuais em um único **Ranking Unificado (Skill Points)** exclusivamente com os Veteranos (`ranking_unificado_br.json` / `.csv`):
  - **ELO base por modo ($M_m$):** maior ELO entre as 4 facções (`USF`, `UKF`, `Wehrmacht`, `DAK`) com $N \ge 10$ jogos na fila.
  - **Ponderação sobre o excedente ($P_m$):** $P_m = 1000 + (M_{m,\text{eff}} - 1000) \cdot w_m$, com pesos $w_{1\text{v}1} = 2{,}0$, $w_{2\text{v}2} = 1{,}5$, $w_{3\text{v}3} = 1{,}0$ e $w_{4\text{v}4} = 1{,}0$.
  - **Skill Points (Âncora + Bônus não-punitivo):** $\max_m(P_m) + 0{,}15 \sum_{j \neq \text{âncora}} \max(0, P_j - 1000)$.
  - **Sistema Anti-Carry compartilhado (`anti_carry.py`):** aplicado tanto no **Ranking Unificado de Veteranos** quanto nos **Campeões BR por Battlegroup**. Mantém o `1v1` 100% intacto, protege carregadores reais e jogadores com paridade solo no `1v1`, e deflaciona ELO/WR inflados em `2v2/3v3/4v4` por parceiros ou contas Smurf muito mais fortes.
- Renderiza tabelas em alta definição (`Pillow` + fontes `Roboto`/`DejaVu`/`Noto Sans CJK` + 257 bandeiras oficiais ISO PNG + ícones oficiais dos 28 Battlegroups + avatares Steam + patente Veteranos), exibindo os Skill Points e o ELO de cada modo (`1v1`, `2v2`, `3v3`, `4v4`) com o modo âncora destacado em dourado, **contas Smurf contabilizadas separadamente em azul com aviso automático indicando de qual conta original cada Smurf pertence** e jogadores 100% inativos em vinho abaixo dos ativos, **publicando a versão atualizada e apagando automaticamente a mensagem anterior** no Discord para manter sempre apenas a última atualização.
- Consome o repositório público **COH3 Stats Open Data** (`storage.coh3stats.com/matches/matches-{ts}.json`, mantendo **todo o histórico completo** de partidas no cache incremental `ranking_cache.db`) complementado em **tempo real pela API oficial da Relic** (`getPersonalStat` + `getRecentMatchHistoryByProfileId`) para gerar o **Ranking Unificado Top 32 de Times Brasileiros 2v2 Arranjados** (média ELO Eixo + Aliados com selo de lado, mínimo de 10 jogos, somente duplas ativas nos últimos 30 dias e corte estrito no Top 32).
- Publica semanalmente todo **sábado às `12:05 BRT`** (`15:05 UTC`, workflow `campeoes_battlegroups.yml`) o painel **Campeões BR por Battlegroup** (28 Battlegroups Automatch 1v1 a 4v4 — zero custom/skirmish, sem viés artificial por tema, com ELO + Força dos Rivais, WR% + Forma `L30` com teto de 18 vitórias, eficiência de combate neutra, controle de mapa, vitórias `VS ELO+ >= 1350`, APM, defesa de cinturão semanal e **Sistema Anti-Carry compartilhado**).
- Publica automaticamente todos os dias às **`08:03 BRT`** (`11:03 UTC`, workflow `boletim_diario.yml`) o **Boletim Diário dos Veteranos** com o resumo completo do dia anterior + o card especial do **Jogo do Dia** (sincronizado com a classificação Anti-Carry do Top 5 do Ranking Unificado, com links diretos de replay no `COH3 Stats` e `cohdb`).
- Executa via GitHub Actions com **guarda idempotente + watchdogs cruzados** (`schedule_guard.py`) que evitam duplicidades e recuperam automaticamente qualquer janela perdida por atraso de cron do GitHub, além de gate obrigatório de testes unitários e disparo manual (**Actions → Run workflow**).

## Configuração dos Secrets no GitHub
1. No canal do Discord onde deseja exibir o **Ranking Unificado BR**, crie um Webhook (**Editar Canal → Integrações → Webhooks → Novo Webhook → Copiar URL do Webhook**).
2. Abra a página de Secrets deste repositório:
   `https://github.com/nigosky/coh3-br-ranking/settings/secrets/actions/new`
3. Crie o Secret obrigatório:
   - **Name:** `DISCORD_RANKING_WEBHOOK_URL`
   - **Secret:** cole a URL do Webhook (`https://discord.com/api/webhooks/...`)
4. *(Opcional)* Para enviar o **Boletim Diário (08:03 BRT)** ou os **Campeões BR por Battlegroup (Sábado 12:00 BRT)** em canais separados, crie também `DISCORD_DAILY_RECAP_WEBHOOK_URL` e/ou `DISCORD_BATTLEGROUPS_WEBHOOK_URL` (se não configurados, ambos fazem fallback seguro para `DISCORD_RANKING_WEBHOOK_URL`).
5. Clique em **Add secret** e, se quiser publicar imediatamente, vá em **Actions → Run workflow**.

## Uso local (opcional)
```bash
# Executar suíte de testes automatizados (ranking, anti-carry, ELO, battlegroups, boletim diário, PNG e segurança)
python3 -m unittest discover -s tests -v

# Varredura + exportação JSON/CSV + prévia dos PNGs sem enviar ao Discord
python3 run_ranking.py --dry-run --preview-dir ./preview

# Prévia local dos Campeões BR por Battlegroup sem enviar ao Discord
python3 run_battlegroups.py --dry-run --preview-dir ./preview_bg

# Prévia local do Boletim Diário + Jogo do Dia sem enviar ao Discord
python3 run_daily_recap.py --dry-run --preview-dir ./preview_daily

# Publicação real no Discord usando a variável de ambiente
DISCORD_RANKING_WEBHOOK_URL="https://discord.com/api/webhooks/..." python3 run_ranking.py --verify-discord --force
```
