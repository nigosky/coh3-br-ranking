# CoH3 Brasil — Ranking Unificado BR

Projeto dedicado exclusivamente ao **Ranking Unificado Brasileiro** de **Company of Heroes 3 (Steam)**, separado do monitor de partidas (`nigosky/coh3-br-tracker`).

## O que este projeto faz
- Consulta diretamente a API oficial da Relic (`getPersonalStat`) para os **44 jogadores brasileiros monitorados / Veteranos (45 contas Steam)** nas 16 filas oficiais (`1v1`, `2v2`, `3v3`, `4v4` × 4 facções), considerando filas ativas e inativas com pelo menos **10 partidas** (`wins + losses >= 10`).
- Consolida todos os modos individuais em um único **Ranking Unificado (Skill Points)** exclusivamente com os Veteranos (`ranking_unificado_br.json` / `.csv`):
  - **ELO base por modo ($M_m$):** maior ELO entre as 4 facções (`USF`, `UKF`, `Wehrmacht`, `DAK`) com $N \ge 10$ jogos na fila.
  - **Ponderação sobre o excedente ($P_m$):** $P_m = 1000 + (M_m - 1000) \cdot w_m$, com pesos $w_{1\text{v}1} = 2{,}0$, $w_{2\text{v}2} = 1{,}5$, $w_{3\text{v}3} = 1{,}0$ e $w_{4\text{v}4} = 1{,}0$.
  - **Skill Points (Âncora + Bônus não-punitivo):** $\max_m(P_m) + 0{,}15 \sum_{j \neq \text{âncora}} \max(0, P_j - 1000)$.
- Renderiza tabelas em alta definição (`Pillow` + fontes `Roboto`/`DejaVu`/`Noto Sans CJK` + 257 bandeiras oficiais ISO PNG + avatares Steam + patente Veteranos), exibindo os Skill Points e o ELO de cada modo (`1v1`, `2v2`, `3v3`, `4v4`) com o modo âncora destacado em dourado e jogadores 100% inativos em cor de fundo própria abaixo dos ativos, mantendo **1 mensagem fixa atualizada in-place** no Discord.
- Consome o repositório público **COH3 Stats Open Data** (`storage.coh3stats.com/matches/matches-{ts}.json`, com cache incremental em `ranking_cache.db`) para gerar o **Ranking Unificado de Times Brasileiros 2v2 Arranjados** (média ELO Eixo + Aliados com selo de lado, ambos os jogadores brasileiros ou Veteranos, mínimo de 3 partidas juntos por lado, Veteranos em destaque dourado e exclusão de times inativos há mais de 30 dias), publicado em **uma segunda mensagem fixa** no mesmo canal.
- Executa automaticamente **2x por dia** (**`00:01 BRT`** e **`12:00 BRT`**) via GitHub Actions (com gate obrigatório de testes unitários antes da publicação), além de permitir disparo manual sob demanda (**Actions → Run workflow**).

## Configuração única do Secret no GitHub
1. No canal do Discord onde deseja exibir o **Ranking Unificado BR**, crie um Webhook (**Editar Canal → Integrações → Webhooks → Novo Webhook → Copiar URL do Webhook**).
2. Abra a página de Secrets deste repositório:
   `https://github.com/nigosky/coh3-br-ranking/settings/secrets/actions/new`
3. Crie o Secret:
   - **Name:** `DISCORD_RANKING_WEBHOOK_URL`
   - **Secret:** cole a URL do Webhook (`https://discord.com/api/webhooks/...`)
4. Clique em **Add secret** e, se quiser publicar imediatamente, vá em **Actions → CoH3 Brasil — Ranking Unificado BR → Run workflow**.

## Uso local (opcional)
```bash
# Executar suíte de testes automatizados (ranking, ELO, PNG e segurança)
python3 -m unittest discover -s tests -v

# Varredura + exportação JSON/CSV + prévia dos PNGs sem enviar ao Discord
python3 run_ranking.py --dry-run --preview-dir ./preview

# Publicação real no Discord usando a variável de ambiente
DISCORD_RANKING_WEBHOOK_URL="https://discord.com/api/webhooks/..." python3 run_ranking.py --verify-discord --force
```
